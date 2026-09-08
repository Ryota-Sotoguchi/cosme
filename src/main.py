"""エントリポイント。

使い方:
    python -m src.main post --slot noon              # 投稿（DRY_RUN 環境変数に従う）
    python -m src.main post --slot morning --dry-run # 強制 DRY_RUN
    python -m src.main post --slot night --live      # 強制本番投稿
    python -m src.main check                         # 接続確認（楽天/Threads）
    python -m src.main preview --slot late           # 生成だけして本文を表示
    python -m src.main token --exchange <短命トークン> # 長期トークンへ交換
    python -m src.main token --refresh               # 長期トークンを更新
    python -m src.main schedule                      # 次回自動実行時刻を表示

終了コード:
    0 = 成功 / スキップ（運用上の正常）
    1 = 設定・認証の問題（人手が必要）
    2 = 一時障害（次回の定期実行で自然に回復しうる）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .compliance.checker import ComplianceChecker
from .config import Config, load_config
from .content.builder import ContentBuilder
from .errors import (
    AuthError,
    ComplianceSkip,
    ConfigError,
    MissingSecretError,
    NoDataError,
    PostRejectedError,
    TransientError,
)
from .logging_setup import setup_logging
from .pipeline import Pipeline
from .rakuten.client import RakutenClient
from .storage.history import History, PostRecord
from .content.voices import load_voices
from .storage.revenue import Revenue, RevenueDay
from .storage.state import State
from .threads.client import ThreadsClient
from .threads.insights import ThreadsInsights
from .threads.replies import ReplyResponder
from .threads.token import ThreadsTokenManager

logger = logging.getLogger("cosme")

JST = timezone(timedelta(hours=9))

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_TRANSIENT = 2


# ======================================================================
def _record(
    history: History,
    *,
    slot: str,
    status: str,
    draft=None,
    published=None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """履歴へ1行追記する。Secret は保存しない。"""
    now = datetime.now(JST).isoformat(timespec="seconds")
    item = draft.primary_item if draft and draft.items else None

    record = PostRecord(
        posted_at=now,
        slot=slot,
        post_type=draft.post_type if draft else "",
        template_id=draft.template_id if draft else "",
        status=status,
        text=draft.text if draft else "",
        has_affiliate_link=bool(draft and draft.has_affiliate_link),
        thread_post_id=published.post_id if published else None,
        permalink=published.permalink if published else None,
        item_code=item.item_code if item else None,
        item_name=item.item_name if item else None,
        item_price=item.item_price if item else None,
        category=(item.raw or {}).get("_genre_label") if item else None,
        review_average=item.review_average if item else None,
        review_count=item.review_count if item else None,
        affiliate_rate=item.affiliate_rate if item else None,
        postage_free=item.is_postage_free if item else None,
        shop_code=item.shop_code if item else None,
        brand_key=item.brand_key if item else None,
        genre_id=item.genre_id if item else None,
        affiliate_url_hash=item.affiliate_url_hash if item else None,
        item_url_hash=item.item_url_hash if item else None,
        item_url=item.item_url if item else None,
        error_type=error_type,
        error_message=(error_message or "")[:500] or None,
        extra={
            "template_parts": draft.part_ids if draft else {},
            "item_codes": [i.item_code for i in draft.items] if draft else [],
            # 手動実行か定期実行か。ランプアップの枠計算で使う。
            "trigger": os.environ.get("GITHUB_EVENT_NAME", "manual"),
            # 連投した本数。**自己リプライを反応数から差し引くのに要る。**
            # Threads の insights.replies は自分の連投も1件として数えるので、
            # これが無いと「返信が付いた投稿」を取り違える（実際に取り違えて
            # config の枠配分を組み替えていた）。
            "segments": len(draft.segments) if draft else 1,
            # リンクをどこに置いたか。A/B の識別子。
            "link_position": (draft.link_position if draft else ""),
        },
    )
    history.append(record)


def _print_draft(draft, header: str = "生成された投稿") -> None:
    line = "─" * 58
    print(f"\n{line}\n{header}  [template={draft.template_id} / type={draft.post_type}]\n{line}")
    print(draft.text)
    print(f"{line}\n{len(draft.text)} 文字 / アフィリエイトリンク: "
          f"{'あり' if draft.has_affiliate_link else 'なし'}")
    if draft.items:
        print("参照した商品データ:")
        for item in draft.items:
            print(
                f"  - {item.item_code} | {item.item_price:,}円 | "
                f"レビュー {item.review_count}件 平均 {item.review_average} | "
                f"送料無料={item.is_postage_free} | 料率={item.affiliate_rate}"
            )
    print(line + "\n")


# ======================================================================
def _schedule_drift_minutes(config: Config, slot_name: str, now: datetime) -> float:
    """設定された投稿時刻から、実際の実行が何分ずれているか。

    GitHub Actions の定期実行は混雑時に大きく遅れる。実測（2026-08-20〜09-03）
    では72件すべてが設定時刻とずれており、中央値43分・最大11時間半だった。
    8/27以降は4〜11時間の遅延が続き、朝の投稿が12:28に、夜の投稿が翌06:30に
    出ていた。そのとき表示回数1の投稿が3本出ている。

    前後どちらのずれも同じ大きさとして返す（0〜720分）。
    """
    try:
        slot = config.slot(slot_name)
    except Exception:
        return 0.0
    hour, minute = (int(x) for x in slot.time_jst.split(":"))
    target = now.astimezone(JST).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    drift = abs((now.astimezone(JST) - target).total_seconds()) / 60
    # 日付をまたいだ場合は近いほうを採る（22:30枠が翌00:30に走る等）
    return min(drift, 1440 - drift)


def cmd_post(config: Config, args: argparse.Namespace) -> int:
    history = History(config.history_path)
    state = State(config.state_path)
    state.operation_start_date()  # 初回に運用開始日を記録

    pipeline = Pipeline(config, history=history, state=state)

    # 定期実行が大きく遅れていたら、その枠は捨てる。
    #
    # 時間帯を設計していても、実行が数時間ずれると設計が成立しない。
    # 深夜3時に「朝の支度」の話が出るくらいなら、出さないほうがいい。
    # 手動実行（workflow_dispatch / ローカル）には掛けない。
    limit = float(config.schedule_settings.get("max_drift_minutes", 0) or 0)
    is_scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    if limit > 0 and is_scheduled:
        drift = _schedule_drift_minutes(config, args.slot, datetime.now(JST))
        if drift > limit:
            logger.warning(
                "定期実行が %.0f 分ずれています（上限 %.0f 分）。この枠は見送ります。",
                drift, limit,
            )
            _record(history, slot=args.slot, status="skipped",
                    error_type="ScheduleDrift",
                    error_message=f"設定時刻から{drift:.0f}分ずれているため見送りました")
            state.save()
            return EXIT_OK
        logger.info("定期実行のずれ %.0f 分（上限 %.0f 分）", drift, limit)

    # 1日の総投稿数の上限。ここを超えると Meta にブロックされうる。
    if pipeline.daily_cap_reached():
        _record(history, slot=args.slot, status="skipped",
                error_type="DailyCapReached",
                error_message="本日の投稿数が上限に達したためスキップしました")
        state.save()
        return EXIT_OK

    try:
        result = pipeline.run(args.slot)
    except ComplianceSkip as exc:
        logger.warning("投稿をスキップしました: %s", exc)
        _record(history, slot=args.slot, status="skipped",
                error_type="ComplianceSkip", error_message=str(exc))
        state.save()
        return EXIT_OK  # 運用上は正常。次回の定期実行は止めない。
    except NoDataError as exc:
        logger.warning("候補商品が見つかりませんでした: %s", exc)
        _record(history, slot=args.slot, status="skipped",
                error_type="NoDataError", error_message=str(exc))
        state.save()
        return EXIT_OK
    except MissingSecretError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except AuthError as exc:
        logger.error("認証エラー: %s", exc)
        return EXIT_CONFIG
    except TransientError as exc:
        logger.error("一時障害のため今回はスキップします: %s", exc)
        state.save()
        return EXIT_TRANSIENT

    draft = result.draft
    _print_draft(draft, "投稿予定の本文")
    logger.info(
        "生成完了: 試行 %d 回 / 候補スキップ %d 件", result.attempts, result.skipped_items
    )

    # --- DRY_RUN はここまで（Threads への POST だけ行わない） ---
    if config.dry_run:
        logger.info("DRY_RUN のため Threads への投稿は行いません")
        _record(history, slot=args.slot, status="dry_run", draft=draft)
        pipeline.builder.commit(draft)
        state.save()
        return EXIT_OK

    # --- 本番投稿 ---
    client = ThreadsClient(config)
    token_manager = ThreadsTokenManager(config, state)
    token_manager.warn_if_expiring()

    try:
        if len(draft.segments) > 1:
            results = client.post_thread(
                draft.segments,
                link_attachment=draft.link_attachment,
                topic_tag=draft.topic_tag,
            )
            published = results[0] if results else None
            if published is None:
                raise PostRejectedError("スレッドを1本も投稿できませんでした")
        else:
            published = client.post_text(
                draft.text,
                link_attachment=draft.link_attachment,
                topic_tag=draft.topic_tag,
            )
    except MissingSecretError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except AuthError as exc:
        logger.error("Threads 認証エラー: %s", exc)
        _record(history, slot=args.slot, status="failed", draft=draft,
                error_type="AuthError", error_message=str(exc))
        state.save()
        return EXIT_CONFIG
    except PostRejectedError as exc:
        logger.error("Threads が投稿を拒否しました: %s", exc)
        _record(history, slot=args.slot, status="failed", draft=draft,
                error_type="PostRejectedError", error_message=str(exc))
        state.save()
        return EXIT_OK  # 本文の問題。次回の定期実行は止めない。
    except TransientError as exc:
        logger.error("一時障害で投稿できませんでした: %s", exc)
        _record(history, slot=args.slot, status="failed", draft=draft,
                error_type="TransientError", error_message=str(exc))
        state.save()
        return EXIT_TRANSIENT

    _record(history, slot=args.slot, status="success", draft=draft, published=published)
    pipeline.builder.commit(draft)
    state.save()

    print(f"\n✅ 投稿成功  post_id={published.post_id}")
    if published.permalink:
        print(f"   {published.permalink}")
    print(f"   実在検証: {'OK' if published.verified else '未確認'}\n")
    return EXIT_OK


# ======================================================================
def cmd_preview(config: Config, args: argparse.Namespace) -> int:
    """生成だけして表示する（Threads へは一切アクセスしない）。"""
    history = History(config.history_path)
    state = State(config.state_path)
    pipeline = Pipeline(config, history=history, state=state)

    # 1日の総投稿数の上限。ここを超えると Meta にブロックされうる。
    if pipeline.daily_cap_reached():
        _record(history, slot=args.slot, status="skipped",
                error_type="DailyCapReached",
                error_message="本日の投稿数が上限に達したためスキップしました")
        state.save()
        return EXIT_OK

    try:
        result = pipeline.run(args.slot)
    except (ComplianceSkip, NoDataError) as exc:
        logger.warning("生成できませんでした: %s", exc)
        return EXIT_OK
    except MissingSecretError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    _print_draft(result.draft, "プレビュー（保存も投稿もしません）")
    return EXIT_OK


# ======================================================================
def cmd_check(config: Config, args: argparse.Namespace) -> int:
    """楽天API と Threads API への接続を確認する。"""
    status: dict[str, str] = {}
    exit_code = EXIT_OK

    print("\n=== 接続確認 ===\n")

    # --- 楽天 ---
    try:
        config.credentials.require_rakuten()
        client = RakutenClient(config)
        genre = config.genres[0]
        items = client.search(
            genre_id=genre["id"],
            min_price=config.selection["min_price"],
            max_price=config.selection["max_price"],
            hits=3,
        )
        if not items:
            status["楽天API"] = "接続OK / 該当商品なし"
        else:
            sample = items[0]
            status["楽天API"] = f"OK ({len(items)}件取得)"
            print(f"楽天API サンプル:")
            print(f"  itemCode      : {sample.item_code}")
            print(f"  itemName      : {sample.display_name(40)}")
            print(f"  itemPrice     : {sample.item_price:,}円")
            print(f"  reviewCount   : {sample.review_count}")
            print(f"  reviewAverage : {sample.review_average}")
            print(f"  postageFlag   : {sample.postage_flag} (送料無料={sample.is_postage_free})")
            print(f"  affiliateRate : {sample.affiliate_rate}")
            print(f"  affiliateUrl  : {(sample.affiliate_url or '(なし)')[:110]}")
            print(f"  URL長         : {len(sample.affiliate_url or '')} 文字")
            status["affiliate URL"] = (
                "OK" if sample.affiliate_url and "rakuten" in sample.affiliate_url else "未取得"
            )
    except MissingSecretError as exc:
        status["楽天API"] = f"未設定 ({exc})"
        exit_code = EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - 確認コマンドなので全部拾って表示する
        status["楽天API"] = f"NG: {type(exc).__name__}: {exc}"
        exit_code = EXIT_CONFIG

    # --- Threads ---
    print()
    try:
        config.credentials.require_threads()
        threads = ThreadsClient(config)
        profile = threads.get_profile()
        status["Threads API"] = f"OK (@{profile.get('username')})"
        print("Threads プロフィール:")
        print(f"  id       : {profile.get('id')}")
        print(f"  username : {profile.get('username')}")
        print(f"  name     : {profile.get('name')}")
        bio = profile.get("threads_biography")
        print(f"  bio      : {bio if bio else '(未設定)'}")

        state = State(config.state_path)
        remaining = ThreadsTokenManager(config, state).warn_if_expiring()
        status["トークン残日数"] = f"{remaining}日" if remaining is not None else "未記録"
    except MissingSecretError as exc:
        status["Threads API"] = f"未設定 ({exc})"
        exit_code = EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        status["Threads API"] = f"NG: {type(exc).__name__}: {exc}"
        exit_code = EXIT_CONFIG

    print("\n--- まとめ ---")
    for key, value in status.items():
        print(f"  {key:16s}: {value}")
    print(f"\nDRY_RUN = {config.dry_run}\n")
    return exit_code


# ======================================================================
def cmd_token(config: Config, args: argparse.Namespace) -> int:
    state = State(config.state_path)
    manager = ThreadsTokenManager(config, state)

    try:
        if args.exchange:
            info = manager.exchange_for_long_lived(args.exchange)
        elif args.refresh:
            config.credentials.require_threads()
            info = manager.refresh(config.credentials.threads_access_token or "")
        else:
            manager.warn_if_expiring()
            state.save()
            return EXIT_OK
    except (MissingSecretError, AuthError) as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except TransientError as exc:
        logger.error("一時障害: %s", exc)
        return EXIT_TRANSIENT

    state.save()

    if args.store_secret:
        manager.store_to_github_secret(info.access_token)
        # 標準出力にトークンを出さない
        print(f"\nトークンを取得し、GitHub Secret への書き戻しを試行しました（有効期間 約{info.days}日）\n")
    else:
        # ローカル確認用。GitHub Actions のログには出さないこと。
        print("\n--- 新しいアクセストークン（この値を GitHub Secrets へ登録してください）---")
        print(info.access_token)
        print(f"--- 有効期間: 約{info.days}日 ---\n")
    return EXIT_OK


def cmd_insights(config: Config, args: argparse.Namespace) -> int:
    """公開済み投稿の成績を取得して履歴へ書き戻す。

    収益化の判断材料。どのテンプレート・時間帯・カテゴリーが
    見られているかが分からないと、改善が勘になる。
    """
    history = History(config.history_path)
    state = State(config.state_path)
    insights = ThreadsInsights(config)

    account = insights.for_account()
    if account:
        print("\n=== アカウント全体 ===")
        for key, value in account.items():
            print(f"  {key:16s} {value:,}")

    targets = [r for r in history.successful() if r.thread_post_id]
    own = state.get("threads_username") or ""
    updated = 0
    backfilled = 0
    for record in targets:
        result = insights.for_post(record.thread_post_id)
        if result is None:
            continue

        # 自分の連投は insights.replies に混ざっている。差し引ける形にする。
        #
        # 新しい投稿は履歴に連投本数が入っているので API を叩かない。
        # 入っていない古い投稿だけ、返信の投稿者を照合して埋める
        # （返信が0件の投稿は照合するまでもないので飛ばす）。
        segments = record.extra.get("segments")
        if isinstance(segments, int) and segments > 0:
            result.replies_self = segments - 1
        elif record.insights.get("replies_self") is not None:
            result.replies_self = int(record.insights["replies_self"])
        elif (result.replies or 0) == 0:
            result.replies_self = 0
        elif own:
            humans = insights.human_reply_count(record.thread_post_id, own)
            if humans is not None:
                result.replies_self = max(0, (result.replies or 0) - humans)
                backfilled += 1

        if history.update_insights(record.thread_post_id, result.as_dict()):
            updated += 1

    print(f"\n成績を更新: {updated}/{len(targets)} 件")
    if backfilled:
        print(f"  自己リプライを照合して補正: {backfilled} 件")

    rows = [
        r for r in history.successful() if r.insights.get("views") is not None
    ]
    if rows:
        print("\n=== 投稿別（表示回数の多い順）===")
        rows.sort(key=lambda r: r.insights.get("views") or 0, reverse=True)
        # 「反応」は人の反応だけを数える。自分の連投は除く。
        print(f"  {'表示':>7} {'人反応':>6}  {'種別':<12} {'テンプレ':<12} {'リンク':<5} 冒頭")
        for r in rows[:20]:
            head = (r.text or "").replace("\n", " ")[:26]
            print(
                f"  {r.views:>7,} {r.human_engagements:>6}  "
                f"{r.post_type:<12} {r.template_id:<12} "
                f"{'あり' if r.has_affiliate_link else 'なし':<5} {head}"
            )

        # 種別ごとの平均表示回数。次に何を増やすかの判断材料。
        buckets: dict[str, list[int]] = {}
        for r in rows:
            buckets.setdefault(r.post_type, []).append(r.insights.get("views") or 0)
        print("\n=== 種別ごとの平均表示回数 ===")
        for kind, values in sorted(
            buckets.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])
        ):
            print(f"  {kind:<12} 平均 {sum(values)//len(values):>6,}  ({len(values)}件)")
    print()
    state.save()
    return EXIT_OK


# ======================================================================
def cmd_revenue(config: Config, args: argparse.Namespace) -> int:
    """楽天アフィリエイトの日次実績を取り込む。

    楽天にはレポートの公開APIが無いので、ここは手作業の入口。

        python -m src.main revenue --csv ~/Downloads/report.csv
        python -m src.main revenue --date 2026-09-03 --clicks 5 --orders 1 --reward 48
        python -m src.main revenue              # いま入っている分を表示
    """
    revenue = Revenue(config.revenue_path)

    if args.csv:
        path = Path(args.csv)
        if not path.is_file():
            logger.error("CSVが見つかりません: %s", path)
            return EXIT_CONFIG
        entries = Revenue.parse_csv(path)
        if not entries:
            logger.error(
                "CSVから実績を読めませんでした。日付の列が必要です。"
                " 手入力なら --date/--clicks/--orders/--reward を使ってください。"
            )
            return EXIT_CONFIG
        count = revenue.put_many(entries)
        print(f"\n{count}日分を取り込みました → {config.revenue_path}")
    elif args.date:
        entry = RevenueDay(
            date=args.date,
            clicks=args.clicks,
            orders=args.orders,
            reward=args.reward,
            period_days=max(1, args.days),
        )
        revenue.put(entry)
        span = "" if entry.is_daily else f"（{entry.period_days}日間の合計）"
        print(f"\n{entry.date} を記録しました{span} → {config.revenue_path}")

    rows = revenue.load()
    if not rows:
        print("\nまだ実績が入っていません。")
        print("  楽天アフィリエイトの管理画面 → レポート → CSVをダウンロードして")
        print("  python -m src.main revenue --csv <ファイル> で取り込んでください。\n")
        return EXIT_OK

    print(f"\n=== 日次実績（{len(rows)}日分）===")
    print(f"  {'日付':<12}{'クリック':>8}{'成果':>6}{'報酬':>8}")
    for key in sorted(rows)[-14:]:
        r = rows[key]
        print(f"  {r.date:<12}{r.clicks:>8,}{r.orders:>6,}{r.reward:>7,}円")
    total = revenue.totals()
    print(f"  {'合計':<12}{total.clicks:>8,}{total.orders:>6,}{total.reward:>7,}円")
    if total.clicks:
        print(f"\n  EPC {total.reward / total.clicks:>6.1f}円/クリック", end="")
        print(f"   CVR {total.orders / total.clicks * 100:>5.1f}%")
    print()
    return EXIT_OK


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    """収益から見た成績表。

    「いいねが多い投稿」ではなく「収益を生む投稿」を見るための表。
    表示と反応は Threads から、クリックと報酬は楽天のレポートから来る。

    クリックは日次でしか取れないので、**リンク投稿がその日1本だけ**の日
    しか投稿に割り当てない。2本以上あった日は帰属不能として除く。
    """
    if getattr(args, "out", None):
        # 週次の記録として残す。差分で「何が変わったか」を追えるようにする。
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = _report_body(config)
        body = buffer.getvalue()
        print(body)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# {datetime.now(JST).strftime('%Y-%m-%d')} 週次点検\n\n"
            "`python -m src.main report` の出力。\n"
            "**点検** の欄が空でなければ、そこだけ見て判断する。\n\n```\n"
        )
        out.write_text(header + body.strip() + "\n```\n", encoding="utf-8")
        print(f"書き出し: {out}")
        return code
    return _report_body(config)


def _report_body(config: Config) -> int:
    history = History(config.history_path)
    revenue = Revenue(config.revenue_path)
    posts = [r for r in history.successful() if r.insights.get("views") is not None]
    if not posts:
        print("\n成績のある投稿がありません。先に insights を実行してください。\n")
        return EXIT_OK

    # --- 全体 ---
    views = sum(r.views for r in posts)
    human = sum(r.human_engagements for r in posts)
    self_replies = sum(r.self_replies for r in posts)
    link_posts = [r for r in posts if r.has_affiliate_link]
    link_views = sum(r.views for r in link_posts)
    total = revenue.totals()

    print(f"\n=== 全体（{len(posts)}投稿）===")
    print(f"  総表示           {views:>10,}")
    print(f"  人からの反応      {human:>10,}   （自分の連投 {self_replies:,} 件は除外）")
    print(f"  リンク投稿の表示  {link_views:>10,}   （{len(link_posts)}投稿）")
    if total.clicks or total.reward:
        print(f"  クリック         {total.clicks:>10,}")
        print(f"  成果報酬         {total.reward:>9,}円")
        if link_views:
            print(f"  CTR              {total.clicks / link_views * 100:>10.3f}%  （リンク投稿の表示比）")
            print(f"  EPM              {total.reward / views * 1000:>10.2f}円  （総表示1,000あたり）")
        if total.clicks:
            print(f"  EPC              {total.reward / total.clicks:>10.1f}円  （1クリックあたり）")
    else:
        print("  クリック・報酬は未取り込み（revenue コマンドで入れてください）")

    # --- 投稿単位に割り当てられる日だけ抜く ---
    #
    # 期間合計（管理画面の「30日間」）は日別が分からないので使わない。
    daily_revenue = revenue.daily()
    per_day: dict[str, list] = {}
    for record in link_posts:
        dt = record.posted_datetime
        if dt is None:
            continue
        per_day.setdefault(dt.astimezone(JST).date().isoformat(), []).append(record)

    attributable = []
    for day, records in sorted(per_day.items()):
        if len(records) != 1:
            continue
        entry = daily_revenue.get(day)
        if entry is None:
            continue
        attributable.append((records[0], entry))

    if attributable:
        print(f"\n=== リンク投稿の実績（帰属できた {len(attributable)}日分）===")
        print(f"  {'日付':<12}{'表示':>7}{'クリック':>8}{'CTR':>8}{'報酬':>7}  形式 / テンプレ")
        for record, entry in attributable:
            ctr = entry.clicks / record.views * 100 if record.views else 0.0
            position = record.extra.get("link_position") or "-"
            print(
                f"  {entry.date:<12}{record.views:>7,}{entry.clicks:>8,}{ctr:>7.2f}%"
                f"{entry.reward:>6,}円  {position} / {record.template_id}"
            )

        # --- A/B: リンクの置き場所ごと ---
        groups: dict[str, list] = {}
        for record, entry in attributable:
            groups.setdefault(record.extra.get("link_position") or "-", []).append((record, entry))
        if len(groups) > 1:
            print("\n=== A/B: リンクの置き場所 ===")
            print(f"  {'形式':<10}{'投稿':>5}{'表示':>8}{'クリック':>8}{'CTR':>8}{'報酬':>8}")
            for name, rows in sorted(groups.items()):
                v = sum(r.views for r, _ in rows)
                c = sum(e.clicks for _, e in rows)
                w = sum(e.reward for _, e in rows)
                ctr = c / v * 100 if v else 0.0
                print(f"  {name:<10}{len(rows):>5}{v:>8,}{c:>8,}{ctr:>7.2f}%{w:>7,}円")
    else:
        print("\n  リンク投稿に日次実績を割り当てられていません。")
        print("  （リンク投稿が1日1本の日 かつ その日の実績が入っている日だけ集計します）")
        if revenue.load() and not daily_revenue:
            print("  いま入っているのは期間合計だけです。日別はCSVからしか取れません。")

    # --- 型ごと ---
    by_type: dict[str, list] = {}
    for record in posts:
        by_type.setdefault(record.post_type, []).append(record)
    print("\n=== 型ごと（人の反応で見る）===")
    print(f"  {'型':<14}{'件数':>5}{'表示中央':>9}{'人の反応':>9}{'反応率':>8}")
    for kind, records in sorted(by_type.items(), key=lambda kv: -_median([r.views for r in kv[1]])):
        v = [r.views for r in records]
        reactions = sum(r.human_engagements for r in records)
        rate = reactions / sum(v) * 100 if sum(v) else 0.0
        print(f"  {kind:<14}{len(records):>5}{_median(v):>9,.0f}{reactions:>9}{rate:>7.2f}%")

    # --- 実際に投稿された時刻ごと ---
    by_hour: dict[int, list] = {}
    for record in posts:
        dt = record.posted_datetime
        if dt is None:
            continue
        by_hour.setdefault(dt.astimezone(JST).hour, []).append(record)
    print("\n=== 実投稿時刻ごと（設定時刻ではなく、実際に出た時刻）===")
    print(f"  {'時':>4}{'件数':>5}{'表示中央':>9}")
    for hour in sorted(by_hour):
        v = [r.views for r in by_hour[hour]]
        print(f"  {hour:>3}時{len(v):>5}{_median(v):>9,.0f}")

    # --- 点検 ---
    #
    # 数字を並べるだけだと、毎週見ても何をすればいいか分からない。
    # 判断が要るところだけを名指しする。
    flags = _review_flags(config, posts, link_posts, attributable, revenue)
    print("\n=== 点検 ===")
    if flags:
        for line in flags:
            print(f"  ▲ {line}")
    else:
        print("  気になるところなし")
    print()
    return EXIT_OK


def _review_flags(config, posts, link_posts, attributable, revenue) -> list[str]:
    """毎週見るべき異常だけを拾う。

    「分析 → 改善」を回すには、数字より先に「どこを触るか」が要る。
    ここが空なら、その週は config を触らなくていい。
    """
    flags: list[str] = []
    total = revenue.totals()

    # --- 収益の入口 ---
    if not revenue.load():
        flags.append(
            "楽天の実績が未取り込み。CTR も EPC も出せない"
            "（python -m src.main revenue --csv <レポート>）"
        )
    elif not revenue.daily():
        flags.append(
            "実績が期間合計だけ。日別が無いと投稿ごとのCTRが出せず、"
            "リンク位置のA/Bを判定できない（管理画面のレポートからCSVを落とす）"
        )
    elif link_posts and total.clicks == 0:
        flags.append(
            f"リンク投稿 {len(link_posts)}本でクリック0。"
            "リンクの置き場所（[experiment] link_position）を見直す"
        )

    # --- A/B の判定material ---
    positions = {}
    for record, entry in attributable:
        positions.setdefault(record.extra.get("link_position") or "-", []).append(entry)
    if len(positions) > 1:
        thin = [name for name, rows in positions.items() if len(rows) < 5]
        if thin:
            flags.append(f"A/B の標本が薄い（{', '.join(thin)} が5本未満）。まだ判定しない")

    # --- 指標の汚染 ---
    unknown = [r for r in posts if not r.extra.get("segments") and r.insights.get("replies")]
    if unknown:
        flags.append(
            f"連投本数が記録されていない投稿が {len(unknown)}件ある。"
            "自己リプライを差し引けないので反応率が過大に出る"
        )

    # --- 実投稿時刻 ---
    #
    # ずれ自体は避けられない（GitHub Actions の混雑）。言い回しは
    # 実際の時刻から選ぶようにしたので、多少のずれは害にならない。
    # 見るのは「捨てられる水準まで遅れていないか」だけ。
    limit = float(config.schedule_settings.get("max_drift_minutes", 0) or 0)
    if limit > 0:
        dropped = sum(
            1 for r in posts[-30:]
            if r.posted_datetime
            and _schedule_drift_minutes(config, r.slot, r.posted_datetime) > limit
        )
        if dropped:
            flags.append(
                f"直近30本のうち {dropped}本が {limit:.0f}分以上ずれている。"
                "この水準の遅延は投稿ごと捨てられる"
            )

    # --- 標本が薄いまま config を触らないための歯止め ---
    by_type: dict[str, int] = {}
    for record in posts:
        by_type[record.post_type] = by_type.get(record.post_type, 0) + 1
    thin_types = [k for k, n in by_type.items() if n < 5]
    if thin_types:
        flags.append(
            f"標本が5本未満の型がある（{', '.join(sorted(thin_types))}）。"
            "この型の成績で枠を組み替えない"
        )

    # --- 使った人の感想の在庫 ---
    voices = load_voices(config.data_dir / "voices.json")
    posted_codes = {r.item_code for r in posts if r.item_code}
    if posted_codes:
        covered = len(posted_codes & set(voices))
        ratio = covered / len(posted_codes)
        if ratio < 0.5:
            flags.append(
                f"使った人の感想がある商品は {covered}/{len(posted_codes)}件だけ。"
                "投稿の主役にする材料が足りていない"
            )
    return flags


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def cmd_replies(config: Config, args: argparse.Namespace) -> int:
    """自分の投稿に付いたコメントに返信する。

    会話が続くと投稿自体のリーチも伸びる。露出を増やす手段のうち
    規約上いちばん安全なもの（他人の投稿への自動返信とは別物）。
    既定は下書き表示のみ。--live のときだけ実際に返信する。
    """
    state = State(config.state_path)
    history = History(config.history_path)
    responder = ReplyResponder(config, state)

    # 人の返信が付いている投稿を履歴から拾う。
    #
    # 直近15投稿だけを見ていたときは、8/20・8/26・9/1 に付いた返信を
    # 取りこぼしていた（1日10本だと15投稿は1日半ぶんしかない）。
    # 成績は毎日 insights で取っているので、そこから絞れば
    # API を余分に叩かずに古い投稿まで届く。
    candidates = [
        r.thread_post_id
        for r in history.successful()
        if r.thread_post_id and r.human_replies > 0
    ]
    # 成績がまだ入っていない直近の投稿も見る（返信は投稿直後に付く）
    try:
        for post_id in responder.recent_post_ids():
            if post_id not in candidates:
                candidates.append(post_id)
    except (AuthError, PostRejectedError, TransientError) as exc:
        logger.warning("直近の投稿一覧を取得できませんでした: %s", exc)

    try:
        planned = responder.run(dry_run=not args.live, post_ids=candidates)
    except MissingSecretError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except (AuthError, PostRejectedError) as exc:
        logger.error("返信の取得に失敗しました: %s", exc)
        return EXIT_CONFIG
    except TransientError as exc:
        logger.error("一時障害: %s", exc)
        return EXIT_TRANSIENT

    if not planned:
        print("\n未返信のコメントはありません\n")
        state.save()
        return EXIT_OK

    # 投稿IDからパーマリンクを引けるようにしておく。
    # 定型文の返信は会話として不自然なので、返すのは人。
    # そのとき「どの投稿か」を探す手間が最大の障害になる。
    permalinks = {
        r.thread_post_id: r.permalink
        for r in history.successful()
        if r.thread_post_id and r.permalink
    }

    print(f"\n=== 未返信のコメント {len(planned)}件 ===")
    for entry in planned:
        mark = "✅ 返信済み" if entry["posted"] else "（下書き）"
        print(f"\n  @{entry['username']}: {entry['comment']}")
        print(f"    → {entry['reply']}  {mark}")
        link = permalinks.get(entry["post_id"])
        if link:
            print(f"    {link}")
    if not args.live:
        print("\n  下書きは定型文です。会話として不自然なので、"
              "上のリンクを開いて手で返すことを勧めます。")
        print("  そのまま投稿する場合は --live を付けてください")
    print()
    state.save()
    return EXIT_OK


def cmd_hours(config: Config, args: argparse.Namespace) -> int:
    """自分の投稿の成績を、判断に使える形で出す。

    見たいのは「次に何を増やすか」なので、時間帯だけでなく
    投稿タイプ・テンプレート・タグの有無でも割る。
    """
    from collections import defaultdict

    history = History(config.history_path)
    rows = [
        r for r in history.successful()
        if r.insights.get("views") is not None and r.posted_datetime
    ]
    if not rows:
        print("\nまだ成績データがありません。insights を実行してください\n")
        return EXIT_OK

    def reactions(record) -> int:
        i = record.insights
        return sum((i.get(k) or 0) for k in ("likes", "replies", "reposts", "shares"))

    def report(title: str, buckets: dict[str, list]) -> None:
        print(f"\n=== {title} ===")
        print(f"  {'':<14}{'件数':>4}{'平均表示':>10}{'反応':>6}{'反応率':>8}")
        print("  " + "\u2500" * 44)
        ranked = sorted(
            buckets.items(),
            key=lambda kv: -sum(r.insights["views"] or 0 for r in kv[1]) / len(kv[1]),
        )
        for name, items in ranked:
            views = [r.insights["views"] or 0 for r in items]
            total_reactions = sum(reactions(r) for r in items)
            total_views = sum(views) or 1
            print(
                f"  {name:<14}{len(items):>4}{sum(views)//len(views):>10,}"
                f"{total_reactions:>6}{total_reactions / total_views:>8.2%}"
            )

    by_type: dict[str, list] = defaultdict(list)
    by_template: dict[str, list] = defaultdict(list)
    by_hour: dict[str, list] = defaultdict(list)
    by_tag: dict[str, list] = defaultdict(list)
    for r in rows:
        by_type[r.post_type or "?"].append(r)
        by_template[r.template_id or "?"].append(r)
        by_hour[f"{r.posted_datetime.astimezone(JST).hour}時"].append(r)
        by_tag["リンクあり" if r.has_affiliate_link else "リンクなし"].append(r)

    print(f"\n分析対象: {len(rows)}件")
    report("投稿タイプ別", by_type)
    report("テンプレート別", by_template)
    report("リンクの有無", by_tag)
    report("時間帯별".replace("별", "別"), by_hour)

    best = max(
        by_type.items(),
        key=lambda kv: sum(r.insights["views"] or 0 for r in kv[1]) / len(kv[1]),
    )
    print(f"\n  いちばん見られている型: {best[0]}")

    if len(rows) < 30:
        print(f"\n  ※ まだ{len(rows)}件です。型ごとに10件は欲しいので、"
              "1〜2週間ためてから判断してください")
    print()
    return EXIT_OK


def _report_autoreply_readiness(config: Config) -> None:
    """自動返信の前提を表示する。**問題があっても点検は失敗させない。**

    autoreply はローカル実行専用の付加機能で、GitHub Actions では動かない。
    CI で claude CLI が無いのは正常なので、ここを致命扱いにすると
    doctor が毎日赤くなって本当の異常が埋もれる。
    """
    from .engage.llm import build_client

    client = build_client(config)
    if client.available:
        print(f"  claude CLI         : ✅ {client.resolved_binary()}")
    else:
        print("  claude CLI         : － 未インストール（autoreply のみ使用）")

    if config.browser_profile_dir.is_dir():
        print("  Threads ログイン   : ✅ プロファイルあり")
    else:
        print("  Threads ログイン   : － 未設定（autoreply --login）")


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    """運用が壊れていないかを点検する。

    定期実行は失敗しても静かに次へ進む設計なので、放っておくと
    「何日も投稿できていない」ことに気づけない。
    問題があれば非ゼロで終了し、GitHub Actions の失敗通知に載せる。
    """
    history = History(config.history_path)
    state = State(config.state_path)
    problems: list[str] = []
    warnings: list[str] = []

    print("\n=== 運用点検 ===\n")

    # --- APIが使えるか（アプリごとブロックされることがある）---
    try:
        profile = ThreadsClient(config).get_profile()
        print(f"  Threads API        : ✅ @{profile.get('username')}")
    except AuthError as exc:
        detail = str(exc)
        if "API access blocked" in detail:
            problems.append(
                "Threads API がブロックされています。"
                "Meta アプリダッシュボードの通知を確認してください "
                "（アプリ単位の制限で、トークンもアカウントも有効なまま起きる）"
            )
        else:
            problems.append(f"Threads API の認証エラー: {detail[:120]}")
        print("  Threads API        : ❌ 使用不可")
    except (MissingSecretError, TransientError) as exc:
        warnings.append(f"Threads API を確認できませんでした: {str(exc)[:100]}")
        print("  Threads API        : ⚠️ 確認不可")

    # --- 自動返信の前提（ローカル実行専用。無くても運用は回る）---
    _report_autoreply_readiness(config)

    # --- 本日の投稿数（出しすぎるとブロックされる）---
    posted_today = len(history.posts_today())
    cap = int(config.ramp_up.get("max_posts_per_day", 7))
    print(f"  本日の投稿数       : {posted_today}/{cap}")
    if posted_today > cap:
        problems.append(f"本日の投稿数が上限を超えています（{posted_today}/{cap}）")

    # --- トークン期限 ---
    remaining = state.token_days_remaining()
    if remaining is None:
        warnings.append("Threads トークンの期限が記録されていません")
    elif remaining <= 0:
        problems.append("Threads トークンが失効しています。再認可が必要です")
    elif remaining <= int(config.threads.get("token_warn_days", 14)):
        problems.append(f"Threads トークンの残りが {remaining} 日です。更新を確認してください")
    print(f"  トークン残日数     : {remaining if remaining is not None else '不明'}")

    # --- 直近の投稿状況 ---
    recent = history.since(3, only_success=False)
    succeeded = [r for r in recent if r.status == "success"]
    failed = [r for r in recent if r.status == "failed"]
    print(f"  直近3日の投稿      : 成功 {len(succeeded)} / 失敗 {len(failed)}")

    if recent and not succeeded:
        problems.append("直近3日で成功した投稿がありません。投稿が止まっています")
    if len(failed) >= 3:
        problems.append(f"直近3日で {len(failed)} 件失敗しています")

    # --- アフィリエイト投稿が出ているか（収益の前提） ---
    link_posts = [r for r in history.since(7) if r.has_affiliate_link]
    print(f"  直近7日のリンク投稿: {len(link_posts)} 件")
    if not link_posts:
        warnings.append(
            "直近7日にリンク投稿がありません。収益は発生しません"
            "（ランプアップ中か、商品候補が枯れている可能性）"
        )

    # --- 成績が取れているか ---
    with_insights = [r for r in history.successful() if r.insights.get("views") is not None]
    print(f"  成績記録済み       : {len(with_insights)} 件")
    if history.successful() and not with_insights:
        warnings.append("投稿の成績が1件も記録されていません。改善の判断材料がありません")

    print()
    for item in problems:
        print(f"  ❌ {item}")
    for item in warnings:
        print(f"  ⚠️  {item}")
    if not problems and not warnings:
        print("  ✅ 問題なし")
    print()

    return EXIT_CONFIG if problems else EXIT_OK


# ======================================================================
def cmd_schedule(config: Config, args: argparse.Namespace) -> int:
    """設定されている投稿スケジュールと、次回実行時刻を表示する。"""
    now = datetime.now(JST)
    print(f"\n現在時刻 (JST): {now:%Y-%m-%d %H:%M}\n")
    print(f"{'スロット':<10} {'JST':<8} {'UTC cron':<16} {'種別':<12} {'リンク':<6} 次回実行")
    print("─" * 78)

    upcoming: list[tuple[datetime, str]] = []
    for slot in config.schedule:
        hour, minute = (int(x) for x in slot.time_jst.split(":"))
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        upcoming.append((nxt, slot.slot))
        print(
            f"{slot.slot:<10} {slot.time_jst:<8} {slot.cron_utc:<16} "
            f"{slot.post_type:<12} {'あり' if slot.allow_affiliate else 'なし':<6} "
            f"{nxt:%Y-%m-%d %H:%M} JST"
        )

    upcoming.sort()
    print(f"\n次回自動実行: {upcoming[0][0]:%Y-%m-%d %H:%M} JST （{upcoming[0][1]} スロット）")
    print(f"1日の投稿本数: {len(config.schedule)} 件\n")

    state = State(config.state_path)
    history = History(config.history_path)
    if config.state_path.is_file():
        print(f"運用開始からの日数: {state.days_since_start()} 日目")
    print(f"記録済み投稿数: {len(history)} 件（成功 {len(history.successful())} 件）")
    print(f"本日のリンク投稿: {history.affiliate_posts_today()} 件\n")
    return EXIT_OK


# ======================================================================

def cmd_engage(config: Config, args: argparse.Namespace) -> int:
    """他人の投稿への返信を支援する。**このコマンドは投稿しない。**

    スクレイプで取れる短縮ID（DTVoI4xlSTZ）は、API が reply_to_id に
    要求する数値ID（17908396266285102）とは別のID空間なので、
    そもそも API から自動で返信できない。パーマリンクを開いて人が返す。

    ここでやるのは、返信文の検査と、返信した相手の記録だけ。
    """
    from .engage.review import EngagementLog, review

    log = EngagementLog(config.engagements_path)

    if args.check:
        result = review(args.check)
        print(f"{'✅' if result.ok else '❌'} {result.summary()}")
        print(f"   {len(args.check)}字")
        return 0 if result.ok else 1

    if args.mark:
        username, shortcode = args.mark
        log.append(username.lstrip("@"), shortcode, args.text or "")
        print(f"記録しました: @{username} / {shortcode}")
        return 0

    if args.reply_to:
        # 公式検索で引いた投稿にだけ返信できる。
        # スクレイプの短縮IDは別のID空間なので reply_to_id に使えない。
        from .engage.responder import Responder

        username, post_id = args.reply_to
        if not args.text:
            print("--text で返信文を渡してください")
            return 1
        responder = Responder(config, log)
        result = responder.reply(
            username=username.lstrip("@"), post_id=post_id,
            shortcode=args.shortcode or "", text=args.text,
            dry_run=not args.live,
        )
        if not result.ok:
            print(f"❌ {result.reason}")
            return 1
        if not args.live:
            print(f"✅ 検査通過。投稿するには --live を付けてください")
            print(f"   @{username} ← {args.text}")
        else:
            print(f"✅ 返信しました: {result.reply_id}")
        return 0

    if args.permissions:
        from .engage.permissions import PermissionProbe

        print("トークンで実際に何ができるかを確かめます。")
        print("（Threads には権限の内省エンドポイントが無いので、叩いて判断します）\n")
        missing = []
        for check in PermissionProbe(config).run():
            mark = "✅" if check.ok else "❌"
            print(f"{mark} {check.name:16} {check.scope}")
            print(f"     {check.detail}")
            print(f"     用途: {check.needed_for}")
            if not check.ok:
                missing.append(check.scope)
        if missing:
            print("\n入っていない権限:", ", ".join(sorted(set(missing))))
            if "threads_keyword_search" in missing:
                print()
                print("キーワード検索が通らないときに見るところ（上から順に）:")
                print("  1. Threadsテスターになっているか")
                print("     アプリロールでの招待だけでは足りない。")
                print("     Threads側（threads.net の設定 → ウェブサイトの許可）で")
                print("     招待を**承諾**する必要がある。")
                print("  2. トークンにスコープが入っているか")
                print("     権限を足しても既存トークンには入らない。")
                print("     Graph API Explorer でスコープを選んで発行し直す。")
                print("  3. 高度なアクセス（App Review）を取っているか")
                print("     **標準アクセスでは自分の投稿しか検索できない。**")
                print("     他人の投稿を探すには App Review が要る。")
            return 1
        print("\nすべて通りました。")
        return 0

    if args.search:
        from .threads.search import ThreadsSearch

        search = ThreadsSearch(config)
        hits = search.search(args.search, limit=args.limit)
        if not hits:
            print("見つかりませんでした。")
            print("threads_keyword_search の権限が要ります（未付与だと HTTP 500）。")
            return 1
        for h in hits:
            print(f"  {h.post_id}  @{h.username}")
            print(f"    {h.permalink}")
            print(f"    {h.text[:80]}")
        return 0

    limit = int(config.engagement.get("max_per_day", 3))
    today = log.replied_today()
    recent = log.recent_usernames(days=int(config.engagement.get("same_account_cooldown_days", 7)))

    print(f"本日の返信 {today}/{limit}件")
    if today >= limit:
        print("上限に達しています。今日はここまでにしてください。")
    print(f"最近返信した相手 {len(recent)}人（この人たちには返さない）")
    if recent:
        print("  " + " ".join(f"@{u}" for u in sorted(recent)))

    if args.history:
        print()
        for e in log.all()[-20:]:
            print(f"  {e.replied_at[:16]}  @{e.username:20} {e.text[:40]}")
        return 0

    latest = _latest_candidate_file()
    print()
    if latest is None:
        print("候補ファイルがありません。まず集めてください:")
        print("  .venv/bin/python scripts/collect_candidates.py")
        return 1
    print(f"候補ファイル: {latest}")
    print("返信案は /reply で作ります（投稿内容を読んでから書くため）。")
    return 0


def _latest_candidate_file() -> Path | None:
    directory = Path("research/replies")
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.md"), reverse=True)
    return files[0] if files else None


def cmd_autoreply(config: Config, args: argparse.Namespace) -> int:
    """ブラウザ操作で伸びている投稿へ自動返信する。**ローカル実行専用。**

    投稿するには鍵が2つ要る: config.toml の enabled = true と --live。
    どちらか片方だけでは投稿しない。

    ブラウザと LLM は重いので、**必要になった時点で import する**
    （selftest がこの経路に触れないようにするため）。
    """
    if args.llm_check:
        return _autoreply_llm_check(config)

    if args.history:
        return _autoreply_history(config)

    if args.login:
        return _autoreply_login(config)

    if args.selfcheck:
        return _autoreply_selfcheck(config, headed=args.headed)

    if args.explain:
        return _autoreply_explain(config, args.explain)

    return _autoreply_run(config, args)


def _autoreply_run(config: Config, args: argparse.Namespace) -> int:
    """収集から投稿まで回す。既定は DRY_RUN。"""
    from .engage.browser.session import ThreadsSession
    from .engage.llm import build_client
    from .engage.review import EngagementLog
    from .engage.runner import EngageRunner
    from .engage.store import EngageStore

    # **--live を明示したときだけ投稿する。**
    #
    # config.dry_run に任せない。あれは DRY_RUN 環境変数を見ており、
    # post.yml が定期実行で DRY_RUN=false を設定している。そのままだと
    # 環境変数が残っている端末で --live 無しに本番投稿してしまい、
    # 「鍵が2つ要る」という取り決めが崩れる。
    dry_run = not args.live
    llm = build_client(config)
    if not llm.available and not args.collect_only:
        print("claude CLI が見つかりません。--llm-check で確認してください。")
        return EXIT_CONFIG

    def make_session():
        session = ThreadsSession(config, headless=not args.headed).start()
        try:
            session.require_login()
        except AuthError:
            session.close()
            raise
        return session

    state = State(config.state_path)
    try:
        with EngageStore(config.engage_db_path) as store:
            runner = EngageRunner(
                config,
                store=store,
                engagement_log=EngagementLog(config.engagements_path),
                llm=llm,
                state=state,
                session_factory=make_session,
            )
            report = runner.run(
                dry_run=dry_run,
                limit=args.limit,
                sources=args.source,
                collect_only=args.collect_only,
            )
            _print_report(report, dry_run=dry_run, collect_only=args.collect_only)
            # **DRY_RUN でも保存する。**
            #
            # ここに入るのはローテーションのカーソルだけで、返信の実績では
            # ない（実績は threads_replies と engagements.jsonl で、そちらは
            # DRY_RUN では書かない）。保存しないと巡回するアカウントも
            # 返信の型も毎回同じものに固定され、仕掛けがまるごと効かなくなる。
            state.save()
    except ImportError:
        print(_PLAYWRIGHT_HINT)
        return EXIT_CONFIG
    except AuthError as exc:
        print(f"❌ {exc}")
        return EXIT_CONFIG
    except TransientError as exc:
        print(f"⚠️  一時的な障害です: {exc}")
        return EXIT_TRANSIENT

    return EXIT_OK


def _print_report(report, *, dry_run: bool, collect_only: bool = False) -> None:
    mode = "DRY_RUN（投稿しません）" if dry_run else "本番"
    print(f"\n=== 自動返信 / {mode} ===\n")

    if report.stopped:
        print(f"実行しませんでした: {report.stopped}")
        return

    if report.budget:
        print(f"  残枠      : {report.budget}")
    if report.paused:
        print(f"  ⏸  {report.paused}")
        print("     （収集と観測は続けています。次回の伸び率の計算に使います）")
    print(f"  収集      : {report.collected} 件")
    print(f"  未処理    : {report.after_seen} 件")
    print(f"  順位付け  : {report.ranked} 件")
    print(f"  検討      : {report.considered} 件")
    print(f"  LLM 呼出  : {report.llm_calls} 回")

    if report.ranked_candidates and not report.posted:
        print("\n  順位付けを通った候補:")
        for i, (candidate, score) in enumerate(report.ranked_candidates[:10], 1):
            age = f"{candidate.age_hours:.0f}時間前" if candidate.age_hours is not None else "時期不明"
            print(f"\n  {i}. @{candidate.username}  buzz {score.total:.2f}")
            print(f"     {candidate.permalink}")
            print(f"     ♥{candidate.likes} 💬{candidate.replies} / {age}"
                  f" / {'美容' if candidate.is_beauty else 'その他'}")
            print(f"     {score.explain()}")
            print(f"     {candidate.text[:70]}")

    for plan, result in report.posted:
        candidate = plan.candidate
        head = "投稿しました" if not dry_run else "投稿する予定"
        mark = "✅" if (dry_run or result.confirmed) else "⚠️ "
        print(f"\n{mark} {head}: @{candidate.username}")
        print(f"     {candidate.permalink}")
        print(f"     元投稿 : {candidate.text[:60]}")
        print(f"     buzz   : {plan.buzz.explain()}")
        print(f"     対象   : {plan.target_verdict.summary()}")
        print(f"     型     : {plan.draft.shape}（{plan.draft.attempts}回目）")
        print(f"     返信   : {plan.draft.text}")
        print(f"     審査   : {plan.reply_verdict.summary()}")
        if plan.draft.rejected:
            print("     没案   :")
            for line in plan.draft.rejected:
                print(f"       - {line[:110]}")
        if not dry_run and not result.confirmed:
            print("     ⚠️  着弾を確認できませんでした（二重投稿を避けるため再送しません）")

    # ルールで落ちたものは数だけ。理由が似るので並べても読みにくい。
    rule_skips = report.skips("rule")
    if rule_skips:
        from collections import Counter
        counts = Counter(r.split("（")[0] for _, r in rule_skips)
        summary = " / ".join(f"{reason} {n}" for reason, n in counts.most_common())
        print(f"\n  ルールで見送り {len(rule_skips)} 件: {summary}")

    # **LLM と再確認で落ちたものは1件ずつ出す。** ここが知りたいところ。
    for stage, label in (("judge", "判定で見送り"), ("verify", "投稿直前に中止")):
        entries = report.skips(stage)
        if entries:
            print(f"\n  {label} {len(entries)} 件:")
            for shortcode, reason in entries:
                print(f"    - {shortcode}: {reason[:110]}")

    if report.errors:
        print("\n  ⚠️  問題:")
        for error in report.errors:
            print(f"    - {error[:160]}")

    if report.posted:
        return
    if collect_only:
        print("\n  ここまでが収集と順位付けです"
              "（--collect-only なので LLM 判定と返信生成は行っていません）。")
    elif not report.ranked_candidates:
        print("\n  順位付けを通る候補がありませんでした。")
    else:
        print("\n  候補はありましたが、返信までは至りませんでした。")


def _autoreply_explain(config: Config, shortcode: str) -> int:
    """1件について、何が起きたかを表示する。"""
    from .engage.store import EngageStore

    with EngageStore(config.engage_db_path) as store:
        seen = store.previous_sighting(shortcode)
        if seen is None:
            print(f"記録にありません: {shortcode}")
            return EXIT_OK

        print(f"\n=== {shortcode} ===\n")
        print(f"  投稿者     : @{seen.username}")
        print(f"  収集元     : {seen.source or '-'}")
        print(f"  初めて見た : {seen.first_seen_at}")
        print(f"  最後に見た : {seen.last_seen_at}")
        print(f"  反応       : ♥{seen.likes} 💬{seen.replies}")
        print(f"  経過時間   : {seen.age_hours}")
        print(f"  buzz       : {seen.buzz_score}")
        print(f"  判断       : {seen.decision}")

        for row in store.recent_replies(limit=200):
            if row.shortcode != shortcode:
                continue
            print(f"\n  返信       : {row.reply_text}")
            print(f"  型         : {row.reply_shape}（{row.generation_attempts}回目）")
            print(f"  対象の点   : {row.target_score}")
            print(f"  審査の点   : {row.reply_score}")
            print(f"  着弾確認   : {'✅' if row.confirmed else '⚠️ 未確認'}")
            if row.outcome_likes is not None:
                print(f"  成果       : ♥{row.outcome_likes} 💬{row.outcome_replies}")
            break
    return EXIT_OK


def _autoreply_login(config: Config) -> int:
    """ブラウザを開いて、人が手でログインする。

    **認証情報はコードで扱わない。** 2段階認証もキャプチャも人がやる。
    """
    from .engage.browser.session import ThreadsSession

    print(f"\nプロファイル: {config.browser_profile_dir}")
    print("ここに Threads のセッションが入ります。**絶対にコミットしないこと。**\n")

    try:
        with ThreadsSession(config, headless=False) as session:
            if session.login_interactively():
                print("\n✅ ログインしました。次回からはこのプロファイルを使います。")
            else:
                print("\n❌ 時間内にログインを確認できませんでした。")
                return EXIT_CONFIG
    except ImportError:
        print(_PLAYWRIGHT_HINT)
        return EXIT_CONFIG

    # 無視されていることをその場で見せる。data/ は公開リポジトリに載る。
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-v", str(config.browser_profile_dir)],
        capture_output=True, text=True, check=False)
    if result.returncode == 0:
        print(f"\ngitignore 確認: {result.stdout.strip()}")
    else:
        print("\n⚠️  プロファイルが gitignore されていません。"
              " .gitignore に .playwright/ を足してください。")
        return EXIT_CONFIG
    return EXIT_OK


def _autoreply_selfcheck(config: Config, *, headed: bool = False) -> int:
    """セレクタが今日も効くかを確かめる。

    Threads は継続的にデプロイするので、セレクタは数週間で壊れる前提。
    投稿する前に確かめるためのカナリア。

    **場所ごとに分けて探す。** 返信欄はダイアログを開くまで存在しないので、
    タイムラインで探して «無い» と言っても誤警報にしかならない。
    毎回赤くなる点検は、いずれ誰も見なくなる。

    ダイアログは開くが、**文字は打たないし投稿もしない。**
    """
    from .engage.browser import actions, selectors
    from .engage.browser.session import ThreadsSession

    print("\n=== セレクタ点検 ===\n")
    unmeasured = selectors.unmeasured()
    if unmeasured:
        print(f"未実測が {len(unmeasured)} 件: {', '.join(unmeasured)}")
        print("（いずれも «無いのが正常» な要素。必須ではない）\n")

    report: dict[str, bool] = {}
    try:
        with ThreadsSession(config, headless=not headed) as session:
            page = session.require_login()

            print("[1] タイムライン")
            report |= actions.selector_report(page, where=selectors.WHERE_FEED)

            link = actions.resolve(page, "post_permalink", required=False)
            href = link.get_attribute("href") if link is not None else None
            if not href:
                print("  ⚠️  投稿が1件も見つからず、返信欄まで確認できませんでした")
            else:
                url = f"https://www.threads.com{href.split('?')[0]}"
                print(f"\n[2] 投稿詳細 → 返信欄（{url}）")
                page = session.goto(url)
                if actions.open_reply_composer(page):
                    report |= actions.selector_report(page, where=selectors.WHERE_MODAL)
                    actions.close_dialog(page)
                    print("  （開いて確認しただけ。何も投稿していません）")
                else:
                    print("  ❌ 返信ボタンを押せませんでした")
                    report |= {k: False for k in selectors.keys_where(selectors.WHERE_MODAL)}
    except ImportError:
        print(_PLAYWRIGHT_HINT)
        return EXIT_CONFIG
    except AuthError as exc:
        print(f"❌ {exc}")
        return EXIT_CONFIG

    print()
    missing_required = []
    for key, ok in report.items():
        spec = selectors.get(key)
        mark = "✅" if ok else ("❌" if spec.required else "⚠️ ")
        suffix = "  [必須]" if spec.required else ""
        print(f"  {mark} {key}{suffix}")
        if not ok and spec.required:
            missing_required.append(key)

    print()
    if missing_required:
        print(f"❌ 必須セレクタが引けません: {missing_required}")
        print("   Threads の DOM が変わった可能性があります。")
        print("   python3 scripts/probe_threads_dom.py で実測し直してください。")
        return EXIT_CONFIG
    print("✅ 必須セレクタはすべて引けました")
    return EXIT_OK


_PLAYWRIGHT_HINT = """
playwright が入っていません。autoreply はブラウザを使います。

    pip install -r requirements-browser.txt
    python3 -m playwright install chromium
    sudo python3 -m playwright install-deps chromium

投稿パイプラインはブラウザを必要としないので、
requirements.txt には入れていません。
"""


def _autoreply_history(config: Config) -> int:
    """これまでの自動返信を表示する。"""
    from .engage.store import EngageStore

    with EngageStore(config.engage_db_path) as store:
        rows = store.recent_replies(limit=30)
        print(f"\n=== 自動返信の履歴（{config.engage_db_path}）===\n")
        if not rows:
            print("  まだありません。")
            return EXIT_OK
        for row in rows:
            mark = "✅" if row.confirmed else "⚠️ "
            outcome = ""
            if row.outcome_likes is not None:
                outcome = f"  → ♥{row.outcome_likes} 💬{row.outcome_replies}"
            print(f"  {mark} {row.replied_at[:16]}  @{row.username:18} "
                  f"[{row.reply_shape or '-'}] {row.reply_text[:40]}{outcome}")
        print(f"\n  合計 {len(rows)} 件（⚠️ は着弾を確認できなかったもの）")
    return EXIT_OK


def _autoreply_llm_check(config: Config) -> int:
    """claude CLI の疎通を確かめる。"""
    from .engage.llm import build_client

    client = build_client(config)
    print(f"binary : {client.binary}")
    print(f"model  : {client.model}")

    if not client.available:
        print("状態   : ❌ 見つかりません\n")
        try:
            client.ask("ping")
        except LlmUnavailableError as exc:
            print(exc)
        return EXIT_CONFIG

    print(f"解決先 : {client.resolved_binary()}")
    print("状態   : ✅ 見つかりました\n")
    print("疎通を確かめています…")
    try:
        response = client.ask(
            'JSON だけを返してください。前置きも説明もコードフェンスも付けないこと。'
            ' スキーマ: {"ok": true, "lang": "<この指示が書かれている言語>"}'
        )
    except TransientError as exc:
        print(f"❌ 応答がありません: {exc}")
        return EXIT_TRANSIENT

    print(f"応答     : {response.text[:200]}")
    print(f"JSON     : {response.data}")
    print(f"所要時間 : {response.duration_ms / 1000:.1f}秒")
    if not response.data:
        print("\n⚠️  JSON を取り出せませんでした。モデルか設定を見直してください。")
        return EXIT_TRANSIENT
    print("\n✅ LLM の疎通を確認しました")
    return EXIT_OK


def cmd_selftest(config: Config, args: argparse.Namespace) -> int:
    """認証情報なしで、生成〜コンプライアンスまでの経路を検証する。

    Secret がまだ無い段階でもパイプラインの健全性を確認できるようにするためのコマンド。
    """
    from .rakuten.models import RakutenItem

    state = State(config.state_path)
    builder = ContentBuilder(state, max_length=int(config.threads["max_text_length"]))
    checker = ComplianceChecker(config.compliance, config.dedup)

    sample = RakutenItem.from_api(
        {
            "itemCode": "selftest:0001",
            "itemName": "テスト用クレンジングジェル 200g",
            "itemPrice": 1980,
            "itemUrl": "https://item.rakuten.co.jp/selftest/0001/",
            "affiliateUrl": "https://hb.afl.rakuten.co.jp/hgc/selftest/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fselftest%2F0001%2F",
            "reviewCount": 1284,
            "reviewAverage": 4.42,
            "postageFlag": 0,
            "availability": 1,
            "affiliateRate": 3.0,
            "pointRate": 2.0,
            "mediumImageUrls": ["https://thumbnail.image.rakuten.co.jp/test.jpg?_ex=128x128"],
            "shopCode": "selftest",
            "shopName": "セルフテストショップ",
            "genreId": "216131",
        }
    )
    object.__setattr__(sample, "raw", {**sample.raw, "_genre_label": "スキンケア"})

    failures = 0
    print("\n=== セルフテスト（認証情報なしで実行可能）===\n")

    for post_type, items in (
        ("product", [sample]),
        ("no_link", []),
    ):
        for _ in range(3):
            draft = builder.build(post_type, items)
            result = checker.check(draft)
            mark = "OK  " if result.passed else "NG  "
            if not result.passed:
                failures += 1
            print(f"{mark} {post_type:<9} template={draft.template_id:<12} "
                  f"{len(draft.text):>3}文字  {result.summary()}")
            builder.commit(draft)

    print()
    draft = builder.build("product", [sample])
    _print_draft(draft, "サンプル出力")

    if failures:
        print(f"❌ {failures} 件が不合格でした\n")
        return EXIT_CONFIG
    print("✅ セルフテスト成功\n")
    return EXIT_OK


# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cosme", description="楽天アフィリエイト × Threads 自動投稿")
    parser.add_argument("--config", type=str, default=None, help="config.toml のパス")
    parser.add_argument("--data-dir", type=str, default=None, help="data ディレクトリのパス")

    sub = parser.add_subparsers(dest="command", required=True)

    p_post = sub.add_parser("post", help="投稿を生成して Threads へ投稿する")
    target = p_post.add_mutually_exclusive_group(required=True)
    target.add_argument("--slot", help="スロット名 (morning/noon/evening/night/late)")
    target.add_argument(
        "--cron",
        help="UTC cron 式からスロットを解決する（GitHub Actions の github.event.schedule 用）",
    )
    mode = p_post.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="強制的に DRY_RUN")
    mode.add_argument("--live", action="store_true", help="強制的に本番投稿")

    p_preview = sub.add_parser("preview", help="生成だけして表示する")
    p_preview_target = p_preview.add_mutually_exclusive_group(required=True)
    p_preview_target.add_argument("--slot")
    p_preview_target.add_argument("--cron")

    sub.add_parser("check", help="楽天/Threads への接続確認")
    sub.add_parser("schedule", help="スケジュールと次回実行時刻を表示")
    sub.add_parser("insights", help="投稿の成績を取得して履歴へ記録する")
    sub.add_parser("doctor", help="運用が壊れていないか点検する")
    sub.add_parser("hours", help="時間帯ごとの成績を見る")
    p_report = sub.add_parser("report", help="収益から見た成績表（CTR/EPC/EPM）")
    p_report.add_argument("--out", help="結果を書き出すファイル（週次の記録用）")

    p_revenue = sub.add_parser("revenue", help="楽天アフィリエイトの日次実績を取り込む")
    p_revenue.add_argument("--csv", help="楽天のレポートCSV")
    p_revenue.add_argument("--date", help="日付 YYYY-MM-DD（手入力するとき）")
    p_revenue.add_argument("--clicks", type=int, default=0)
    p_revenue.add_argument("--orders", type=int, default=0)
    p_revenue.add_argument("--reward", type=int, default=0, help="成果報酬（円）")
    p_revenue.add_argument(
        "--days", type=int, default=1,
        help="--date で終わる期間の日数。管理画面の「30日間」を入れるとき用。"
             "1より大きいと期間合計として扱い、投稿単位の集計には使わない",
    )

    p_replies = sub.add_parser("replies", help="自分の投稿へのコメントに返信する")
    p_replies.add_argument("--live", action="store_true", help="実際に返信する（既定は下書き表示のみ）")
    p_engage = sub.add_parser(
        "engage", help="他人の投稿への返信を支援する（投稿はしない）")
    p_engage.add_argument("--check", metavar="TEXT",
                          help="返信文を検査する（URL・NG表現・テンプレ度）")
    p_engage.add_argument("--mark", nargs=2, metavar=("USERNAME", "SHORTCODE"),
                          help="返信したことを記録する")
    p_engage.add_argument("--text", default="", help="--mark に添える返信本文")
    p_engage.add_argument("--history", action="store_true",
                          help="これまでの返信を表示する")
    p_engage.add_argument("--permissions", action="store_true",
                          help="トークンに入っている権限を実際に叩いて確かめる")
    p_engage.add_argument("--search", metavar="KEYWORD",
                          help="公式APIで公開投稿を検索する（post_id が取れる）")
    p_engage.add_argument("--limit", type=int, default=10,
                          help="--search で取る件数")
    p_engage.add_argument("--reply-to", nargs=2, metavar=("USERNAME", "POST_ID"),
                          help="返信する。--text と併せて使う")
    p_engage.add_argument("--shortcode", default="",
                          help="--reply-to に添える短縮ID（重複防止の記録用）")
    p_engage.add_argument("--live", action="store_true",
                          help="実際に投稿する（既定は検査だけ）")

    p_auto = sub.add_parser(
        "autoreply",
        help="ブラウザ操作で伸びている投稿へ自動返信する（ローカル実行専用）")
    p_auto.add_argument("--login", action="store_true",
                        help="ブラウザを開いてログインする（初回だけ。手で認証する）")
    p_auto.add_argument("--selfcheck", action="store_true",
                        help="セレクタが今日も効くか確かめる")
    p_auto.add_argument("--llm-check", action="store_true",
                        help="claude CLI の疎通を確かめる")
    p_auto.add_argument("--collect-only", action="store_true",
                        help="収集と順位付けだけ（LLM を呼ばない）")
    p_auto.add_argument("--history", action="store_true",
                        help="これまでの自動返信を表示する")
    p_auto.add_argument("--explain", metavar="SHORTCODE",
                        help="1件の点数内訳と判断理由を表示する")
    p_auto.add_argument("--source", action="append", default=None,
                        metavar="NAME", help="収集元を絞る（複数指定可）")
    p_auto.add_argument("--limit", type=int, default=None,
                        help="この実行で出す返信の上限")
    p_auto.add_argument("--headed", action="store_true",
                        help="ブラウザを表示する（動きを目で見る）")
    p_auto.add_argument("--live", action="store_true",
                        help="実際に返信する（既定は DRY_RUN）")

    sub.add_parser("selftest", help="認証情報なしで生成〜検証の経路をテスト")

    p_token = sub.add_parser("token", help="Threads アクセストークンの管理")
    p_token.add_argument("--exchange", metavar="SHORT_LIVED_TOKEN", help="短命トークンを長期トークンへ交換")
    p_token.add_argument("--refresh", action="store_true", help="長期トークンを更新")
    p_token.add_argument("--store-secret", action="store_true", help="結果を GitHub Secret へ書き戻す")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()

    dry_run: bool | None = None
    if getattr(args, "dry_run", False):
        dry_run = True
    elif getattr(args, "live", False):
        dry_run = False

    try:
        from pathlib import Path

        config = load_config(
            config_path=Path(args.config) if args.config else None,
            data_dir=Path(args.data_dir) if args.data_dir else None,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        logger.error("設定エラー: %s", exc)
        return EXIT_CONFIG

    # --cron 指定なら config.toml からスロットを解決する
    if getattr(args, "cron", None):
        try:
            args.slot = config.slot_for_cron(args.cron).slot
        except ConfigError as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG
        logger.info("cron '%s' -> slot '%s'", args.cron, args.slot)

    handlers = {
        "post": cmd_post,
        "preview": cmd_preview,
        "check": cmd_check,
        "schedule": cmd_schedule,
        "insights": cmd_insights,
        "doctor": cmd_doctor,
        "hours": cmd_hours,
        "report": cmd_report,
        "revenue": cmd_revenue,
        "replies": cmd_replies,
        "engage": cmd_engage,
        "autoreply": cmd_autoreply,
        "selftest": cmd_selftest,
        "token": cmd_token,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
