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
from .storage.state import State
from .threads.client import ThreadsClient
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
def cmd_post(config: Config, args: argparse.Namespace) -> int:
    history = History(config.history_path)
    state = State(config.state_path)
    state.operation_start_date()  # 初回に運用開始日を記録

    pipeline = Pipeline(config, history=history, state=state)

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
                draft.segments, link_attachment=draft.link_attachment
            )
            published = results[0] if results else None
            if published is None:
                raise PostRejectedError("スレッドを1本も投稿できませんでした")
        else:
            published = client.post_text(
                draft.text, link_attachment=draft.link_attachment
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
        "selftest": cmd_selftest,
        "token": cmd_token,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
