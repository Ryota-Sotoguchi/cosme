#!/usr/bin/env python3
"""過去のコスメ・美容の投稿を削除する（2026-09-14 の発信ジャンル切り替えに伴う片付け）。

## 3段階。いきなり消さない

1. **候補取得** … `data/history.jsonl`（自動投稿の記録）を読む。
   `--profile` を付けると、ログイン済みブラウザで自分のプロフィールも読む（手で投稿した分も拾う）
2. **Dry Run（既定）** … 件数と一覧を表示し、`data/engage/cleanup/candidates-YYYYMMDD.json` に保存する。
   **何も消さない**
3. **実削除** … 環境変数 `DELETE_OLD_THREADS_POSTS=true` **と** `--execute` の両方があるときだけ。
   Dry Run で保存した候補ファイルを読み、**そこに載っている投稿だけ**を消す（見たものだけを消す）

## 分類

    A  確実にコスメ   楽天の商品データから作った投稿（#PR・リンク・商品の型・商品名のつぶやき）
    B  コスメの語あり 本文にコスメ・美容の語があり、転職・仕事の語が無い
    C  あいまい       どちらでもない。**削除しない**（--include-ambiguous を明示したときだけ対象）

切り替え後（`--before` 以降）の投稿は候補にしない。投稿日時が分からない投稿も消さない。

## 実削除の歯止め

- 1回あたり既定 20件（`--limit`、上限 50）。削除の間は 30〜90秒あける
- Threads に絞られたら（「エラーが発生しました」）即停止。セレクタが引けなくても即停止
- 削除後にページを開き直して、消えたことを確かめてから記録する。確かめられなければ停止
- 消した投稿は `data/engage/cleanup/deleted.jsonl` に記録し、再実行では飛ばす
- 削除の確認ダイアログのセレクタが未実測のあいだは、1回に1件しか消さない
- 自動返信（cron）と同じロックを取る。同じブラウザプロファイルを同時に開かない

## 自動運用には組み込まない

手で実行する。GitHub Actions でも cron でも回さない（tests/test_cleanup_old_posts.py が見張る）。

## 使い方

    python3 scripts/cleanup_old_posts.py                 # Dry Run（履歴だけ。ブラウザ不要）
    python3 scripts/cleanup_old_posts.py --profile       # Dry Run（プロフィールも読む）
    python3 scripts/cleanup_old_posts.py --show-ambiguous

    # 一覧を確認してから
    DELETE_OLD_THREADS_POSTS=true python3 scripts/cleanup_old_posts.py --execute --limit 1
    DELETE_OLD_THREADS_POSTS=true python3 scripts/cleanup_old_posts.py --execute
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JST = timezone(timedelta(hours=9))

ENV_FLAG = "DELETE_OLD_THREADS_POSTS"
HISTORY_PATH = ROOT / "data" / "history.jsonl"
# data/engage/ は gitignore 済み。自分の投稿の本文が入るが、公開リポジトリには載らない。
OUT_DIR = ROOT / "data" / "engage" / "cleanup"
DELETED_LOG = "deleted.jsonl"
# 自動返信の cron（crontab）が使っているロック。同じブラウザプロファイルを同時に開かない。
LOCK_PATH = Path("/tmp/cosme-autoreply.lock")

# 発信ジャンルを切り替えた投稿が本番に出始めた時刻。これ以降の投稿は候補にしない。
GENRE_SWITCHED_AT = datetime(2026, 9, 15, 9, 45, tzinfo=JST)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
INTERVAL_SECONDS = (30, 90)

# 楽天の商品を載せる型（review_heavy は廃止済みだが過去の記録に残っている）
PRODUCT_TYPES = frozenset({
    "product", "price_band", "postage_free", "comparison", "longform", "review_heavy",
})
# 広告の目印。履歴の本文ではアフィリエイトURLが [affiliate-url] に伏せてある
AD_MARKERS = ("#PR", "【PR】", "[affiliate-url]", "hb.afl.rakuten.co.jp", "a.r10.to")

# コスメ・美容の語。**ここに当たっても、転職・仕事の語が同時にあれば消さない**（C に回す）
COSME_TERMS = (
    "コスメ", "美容", "スキンケア", "化粧", "メイク", "ヘアケア", "ボディケア", "ヘアオイル",
    "シャンプー", "トリートメント", "コンディショナー", "日焼け止め", "リップ", "クレンジング",
    "洗顔", "美容液", "乳液", "化粧水", "保湿", "ファンデ", "アイシャドウ", "マスカラ", "チーク",
    "アイライナー", "ネイル", "ハンドクリーム", "ボディクリーム", "ボディソープ", "入浴剤",
    "毛穴", "美白", "敏感肌", "乾燥肌", "肌", "ドライヤー", "プチプラ", "デパコス", "詰め替え",
    "ドラッグストア", "ドラスト", "楽天", "送料", "香水", "パック",
    "ポーチ", "パケ", "色もの", "新色", "限定色", "髪", "パフ", "ブラシ", "クリーム", "ボディ",
    "ヘア", "つめかえ", "無添加", "オーガニック", "日焼け", "UV", "まつげ", "ケア",
)
# 転職ジャンルの語。これがある投稿は自動では消さない
CAREER_TERMS = (
    "転職", "年収", "面接", "職務経歴書", "履歴書", "退職", "上司", "残業", "給料", "求人",
    "キャリア", "内定", "会社員", "有給", "出社", "評価面談", "昇給", "ボーナス", "職場",
)

_SHORTCODE = re.compile(r"/post/([A-Za-z0-9_-]+)")


# ======================================================================
# 候補取得（ブラウザ不要の部分）
# ======================================================================
@dataclass
class Post:
    shortcode: str
    url: str
    posted_at: str  # ISO8601。分からなければ空
    text: str
    post_type: str = ""
    has_affiliate_link: bool = False
    # 直近の商品名から作ったつぶやき（brand_murmurs）。ブランド名が入っている
    from_product_name: bool = False
    source: str = "history"
    # 連投の2本目以降（自分への返信）なら、1本目の短縮IDと本文。判定は1本目に合わせる
    thread_of: str = ""
    root_text: str = ""


def shortcode_of(url_or_href: str | None) -> str | None:
    m = _SHORTCODE.search(url_or_href or "")
    return m.group(1) if m else None


def load_history_posts(path: Path) -> list[Post]:
    """投稿に成功した記録だけを読む。パーマリンクが無い記録は消しようがないので飛ばす。"""
    posts: list[Post] = []
    if not path.exists():
        return posts
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "success":
            continue
        code = shortcode_of(row.get("permalink"))
        if not code:
            continue
        posts.append(Post(
            shortcode=code,
            url=row["permalink"],
            posted_at=row.get("posted_at") or "",
            text=row.get("text") or "",
            post_type=row.get("post_type") or "",
            has_affiliate_link=bool(row.get("has_affiliate_link")),
            from_product_name="casual_brand" in ((row.get("extra") or {}).get("template_parts") or {}),
            source="history",
        ))
    return posts


def username_from(posts: Iterable[Post]) -> str | None:
    """履歴のパーマリンクから自分のユーザー名を出す（いちばん多いもの）。"""
    names = Counter(
        m.group(1) for p in posts if (m := re.search(r"/@([^/]+)/post/", p.url))
    )
    return names.most_common(1)[0][0] if names else None


def profile_posts(rows: list[dict[str, Any]], username: str) -> list[Post]:
    """プロフィールから取り出した行を Post にする。**本人の投稿だけ**残す。"""
    from src.engage.browser.extract import parse_rows

    posts: list[Post] = []
    by_href = {(r.get("href") or "").split("?")[0]: r for r in rows}
    for candidate in parse_rows(rows, source="profile"):
        if candidate.username.lower() != username.lower():
            continue  # リポストなど、他人の投稿
        raw = by_href.get(candidate.href, {})
        posted_at = ""
        if raw.get("datetime"):
            try:
                posted_at = datetime.fromisoformat(
                    raw["datetime"].replace("Z", "+00:00")).astimezone(JST).isoformat()
            except ValueError:
                posted_at = ""
        posts.append(Post(
            shortcode=candidate.shortcode,
            url=f"https://www.threads.com/@{candidate.username}/post/{candidate.shortcode}",
            posted_at=posted_at,
            text=candidate.text,
            source="profile",
        ))
    return posts


# 連投の2本目以降を、1本目と突き合わせるときの時間の幅
THREAD_WINDOW = timedelta(minutes=60)


def _body_lines(text: str) -> list[str]:
    """本文の行。「2 / 2」のような通し番号と短すぎる行は突き合わせに使わない。"""
    return [
        line.strip() for line in text.split("\n")
        if len(line.strip()) >= 8 and not re.fullmatch(r"\d+\s*/\s*\d+", line.strip())
    ]


def find_thread_root(post: Post, history: list[Post]) -> Post | None:
    """プロフィールにだけある投稿が、履歴のどの連投の続きか。

    履歴は連投を1件（全文）で記録していて、2本目以降の短縮IDを持たない。
    本文の行が全文に含まれ、投稿時刻が近いものを1本目とみなす。
    見出し（「選ぶとき、だいたいここ見てる👀」）は多くの投稿で同じなので、
    **全部の行が含まれること**と時刻の近さの両方で見る。
    """
    lines = _body_lines(post.text)
    posted = _parse_time(post.posted_at)
    if not lines or posted is None:
        return None
    matches = []
    for root in history:
        root_time = _parse_time(root.posted_at)
        if root_time is None or abs(root_time - posted) > THREAD_WINDOW:
            continue
        if all(line in root.text for line in lines):
            matches.append((abs(root_time - posted), root))
    return min(matches, key=lambda m: m[0])[1] if matches else None


def merge_posts(history: list[Post], profile: list[Post]) -> list[Post]:
    """短縮IDで突き合わせる。型とリンクの情報は履歴を正とする。

    プロフィールにだけある投稿は、連投の続きなら1本目の情報を引き継ぐ
    （連投はまとめて消すか、まとめて残す）。どれでもなければ手で投稿したもの。
    """
    merged: dict[str, Post] = {p.shortcode: p for p in history}
    for post in profile:
        known = merged.get(post.shortcode)
        if known is not None:
            known.source = "both"
            if not known.posted_at:
                known.posted_at = post.posted_at
            continue
        root = find_thread_root(post, history)
        if root is not None:
            post.thread_of = root.shortcode
            post.root_text = root.text
            post.post_type = root.post_type
            post.has_affiliate_link = root.has_affiliate_link
            post.from_product_name = root.from_product_name
        merged[post.shortcode] = post
    return sorted(merged.values(), key=lambda p: p.posted_at or "")


def classify(post: Post) -> tuple[str, str]:
    """A / B / C と、その理由。連投の続きは1本目と同じ判定にする。"""
    if post.thread_of:
        root = Post(shortcode=post.thread_of, url="", posted_at=post.posted_at,
                    text=post.root_text, post_type=post.post_type,
                    has_affiliate_link=post.has_affiliate_link,
                    from_product_name=post.from_product_name)
        cls, reason = classify(root)
        return cls, f"連投の続き（1本目 {post.thread_of}: {reason}）"
    if post.post_type in PRODUCT_TYPES:
        return "A", f"商品投稿の型（{post.post_type}）"
    if post.has_affiliate_link:
        return "A", "アフィリエイトリンクあり"
    if post.from_product_name:
        return "A", "商品名から作ったつぶやき"
    markers = [m for m in AD_MARKERS if m in post.text]
    if markers:
        return "A", f"広告の目印（{'/'.join(markers)}）"

    career = [w for w in CAREER_TERMS if w in post.text]
    cosme = [w for w in COSME_TERMS if w in post.text]
    if career:
        return "C", f"転職・仕事の語あり（{'/'.join(career[:3])}）"
    if cosme:
        return "B", f"コスメの語（{'/'.join(cosme[:3])}）"
    return "C", "ジャンルを判定できない"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def build_candidates(posts: list[Post], *, before: datetime,
                     include_ambiguous: bool = False,
                     now: datetime | None = None) -> dict[str, Any]:
    """候補ファイルの中身を作る。**targets に載ったものだけが実削除の対象になる。**"""
    now = now or datetime.now(JST)
    targets: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for post in posts:
        posted = _parse_time(post.posted_at)
        if posted is not None and posted >= before:
            counts["after_switch"] += 1
            continue
        cls, reason = classify(post)
        if posted is None:
            # いつの投稿か分からないものは、切り替え後かもしれないので消さない
            cls, reason = "C", f"投稿日時が不明（{reason}）"
        counts[cls] += 1
        entry = {
            "shortcode": post.shortcode,
            "url": post.url,
            "posted_at": post.posted_at,
            "class": cls,
            "reason": reason,
            "excerpt": " ".join(post.text.split())[:40],
            "source": post.source,
            "thread_of": post.thread_of,
        }
        if cls in ("A", "B") or (cls == "C" and include_ambiguous):
            targets.append(entry)
        else:
            ambiguous.append(entry)

    return {
        "created_at": now.isoformat(timespec="seconds"),
        "before": before.isoformat(),
        "include_ambiguous": include_ambiguous,
        "counts": {k: counts.get(k, 0) for k in ("A", "B", "C", "after_switch")},
        "targets": targets,
        "ambiguous": ambiguous,
    }


def save_candidates(data: dict[str, Any], out_dir: Path, *, today: datetime | None = None) -> Path:
    today = today or datetime.now(JST)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"candidates-{today:%Y%m%d}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def latest_candidates(out_dir: Path) -> Path | None:
    files = sorted(out_dir.glob("candidates-*.json"))
    return files[-1] if files else None


def load_deleted(out_dir: Path) -> set[str]:
    path = out_dir / DELETED_LOG
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("shortcode"):
            done.add(row["shortcode"])
    return done


def append_deleted(out_dir: Path, entry: dict[str, Any], outcome: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "shortcode": entry["shortcode"],
        "url": entry["url"],
        "class": entry.get("class"),
        "outcome": outcome,
        "deleted_at": datetime.now(JST).isoformat(timespec="seconds"),
    }
    with (out_dir / DELETED_LOG).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ======================================================================
# 実削除の可否（ブラウザ不要の部分）
# ======================================================================
DELETE_SELECTORS = ("post_more_button", "delete_menu_item", "delete_confirm_button")


def execution_refusal(*, execute: bool, env: dict[str, str],
                      measured: dict[str, bool]) -> str | None:
    """実削除してはいけない理由。None なら実行してよい。"""
    if not execute:
        return "Dry Run です（--execute が無い）"
    if env.get(ENV_FLAG, "").strip().lower() != "true":
        return f"環境変数 {ENV_FLAG}=true が無いので実削除しません"
    for key in ("post_more_button", "delete_menu_item"):
        if not measured.get(key):
            return f"セレクタ {key} が未実測です。scripts/probe_threads_dom.py で実測してから"
    return None


def plan_execution(candidates: dict[str, Any], *, deleted: set[str], limit: int,
                   confirm_measured: bool) -> list[dict[str, Any]]:
    """今回消すもの。候補ファイルの targets だけから、消していないものを上限まで。"""
    limit = max(0, min(limit, MAX_LIMIT))
    if not confirm_measured:
        # 確認ダイアログを実測するまでは、1件ずつ確かめながら進める
        limit = min(limit, 1)
    pending = [e for e in candidates.get("targets", []) if e["shortcode"] not in deleted]
    if not candidates.get("include_ambiguous"):
        pending = [e for e in pending if e.get("class") in ("A", "B")]
    return pending[:limit]


# ======================================================================
# 表示
# ======================================================================
def print_report(data: dict[str, Any], path: Path | None, *, show_ambiguous: bool) -> None:
    counts = data["counts"]
    print("\n=== 過去投稿の削除候補（Dry Run・何も消していません） ===\n")
    print(f"  切り替え時刻（これより前だけ対象）: {data['before']}")
    print(f"  A 確実にコスメ   : {counts['A']}件")
    print(f"  B コスメの語あり : {counts['B']}件")
    print(f"  C あいまい       : {counts['C']}件  ← 削除しない"
          + ("（--include-ambiguous のため対象に含めた）" if data["include_ambiguous"] else ""))
    print(f"  切り替え後の投稿 : {counts['after_switch']}件  ← 対象外")
    print(f"\n  削除対象: {len(data['targets'])}件\n")
    for e in data["targets"]:
        print(f"  [{e['class']}] {e['posted_at'][:16]}  {e['excerpt']}")
        print(f"       {e['url']}  （{e['reason']}）")
    if show_ambiguous and data["ambiguous"]:
        print("\n  --- C あいまい（削除しない） ---")
        for e in data["ambiguous"]:
            print(f"  [C] {e['posted_at'][:16]}  {e['excerpt']}  （{e['reason']}）")
    if path:
        print(f"\n候補ファイル: {path}")
        print(f"実削除するには: {ENV_FLAG}=true python3 scripts/cleanup_old_posts.py --execute --limit 1")


# ======================================================================
# ブラウザ操作（page を受け取るだけ。playwright は session.py が持つ）
# ======================================================================
PROFILE_URL = "https://www.threads.com/@{username}"

# 対象の投稿カード（パーマリンクを持つ a から data-pressable-container まで遡る）の中で、
# セレクタに当たる要素を返す。ページ内の別の投稿（返信欄など）を押さないため、カードに限定する。
CARD_ELEMENT_JS = """
([code, css]) => {
  const a = document.querySelector('a[href*="/post/' + code + '"]');
  if (!a) return null;
  let node = a;
  for (let i = 0; i < 12 && node.parentElement; i++) {
    node = node.parentElement;
    if (node.dataset && node.dataset.pressableContainer === 'true') break;
  }
  return node.querySelector(css);
}
"""


def _post_is_present(page: Any, shortcode: str) -> bool:
    return page.query_selector(f'a[href*="/post/{shortcode}"]') is not None


def _throttled(page: Any) -> bool:
    from src.engage.browser import actions

    return actions.exists(page, "error_state")


def collect_profile_rows(session: Any, username: str, *, scrolls: int) -> list[dict[str, Any]]:
    """自分のプロフィールを下までスクロールしながら、見えた投稿を貯める。"""
    from src.engage.browser import actions
    from src.engage.browser.extract import EXTRACT_JS
    from src.errors import ThrottledError

    page = session.goto(PROFILE_URL.format(username=username))
    actions.wait_for_posts(page)
    if _throttled(page):
        raise ThrottledError("Threads がプロフィールの表示を拒否しました。時間をおいてください。")

    rows: dict[str, dict[str, Any]] = {}
    still = 0
    for _ in range(max(scrolls, 1)):
        for row in page.evaluate(EXTRACT_JS, 1000) or []:
            rows.setdefault((row.get("href") or "").split("?")[0], row)
        before = len(rows)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(2000)
        for row in page.evaluate(EXTRACT_JS, 1000) or []:
            rows.setdefault((row.get("href") or "").split("?")[0], row)
        still = still + 1 if len(rows) == before else 0
        if still >= 3:
            break
    return list(rows.values())


def delete_one(session: Any, entry: dict[str, Any]) -> str:
    """1件消す。戻り値は deleted / already_gone。確かめられなければ例外で止める。"""
    from src.engage.browser import selectors
    from src.errors import SelectorMissError, ThrottledError

    code = entry["shortcode"]
    page = session.goto(entry["url"])
    page.wait_for_timeout(2000)
    if _throttled(page):
        raise ThrottledError("Threads が投稿ページの表示を拒否しました。")
    if not session.is_logged_in(page):
        raise RuntimeError("ログインが切れています。python3 -m src.main autoreply --login")
    if not _post_is_present(page, code):
        return "already_gone"

    # 1. 対象の投稿カードの「その他」
    more = selectors.get("post_more_button")
    handle = None
    for css in more.candidates():
        handle = page.evaluate_handle(CARD_ELEMENT_JS, [code, css]).as_element()
        if handle is not None:
            break
    if handle is None:
        raise SelectorMissError("post_more_button", list(more.candidates()))
    handle.click()
    page.wait_for_timeout(1500)

    # 2. メニューの「削除」
    menu_item = _resolve_in_page(page, "delete_menu_item")
    menu_item.click()
    page.wait_for_timeout(1500)

    # 3. 確認ダイアログの「削除」
    confirm = _resolve_in_page(page, "delete_confirm_button")
    confirm.click()
    page.wait_for_timeout(4000)

    # 4. 開き直して、消えたことを確かめる
    page = session.goto(entry["url"])
    page.wait_for_timeout(2000)
    if _throttled(page):
        raise ThrottledError("削除後の確認で Threads に拒否されました。削除できたかは手で確認してください。")
    if _post_is_present(page, code):
        raise RuntimeError(f"削除を確認できません（{entry['url']}）。ここで止めます。")
    return "deleted"


def _resolve_in_page(page: Any, key: str) -> Any:
    """css → role の順に引く。**見つからなければ推測で続けず止める。**"""
    from src.engage.browser import selectors
    from src.errors import SelectorMissError

    spec = selectors.get(key)
    tried: list[str] = []
    for css in spec.candidates():
        tried.append(css)
        try:
            found = page.query_selector_all(css)
        except Exception:  # noqa: BLE001
            continue
        visible = [f for f in found if f.is_visible()]
        if len(visible) == 1:
            return visible[0]
        if len(visible) > 1:
            raise SelectorMissError(key, [f"{css}（{len(visible)}個に当たった。1個に絞れない）"])
    if spec.role is not None:
        role, name = spec.role
        tried.append(f'role={role} name="{name}"')
        locator = page.get_by_role(role, name=name, exact=spec.exact)
        if locator.count() == 1:
            return locator.first
        if locator.count() > 1:
            raise SelectorMissError(key, [f"{tried[-1]}（{locator.count()}個に当たった）"])
    raise SelectorMissError(key, tried)


# ======================================================================
def _acquire_lock() -> Any:
    """自動返信（cron）と同時に動かない。取れなければ None。"""
    handle = LOCK_PATH.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="過去のコスメ・美容の投稿を削除する（既定は Dry Run）")
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--before", default=GENRE_SWITCHED_AT.isoformat(),
                        help="この時刻より前の投稿だけを候補にする（ISO8601）")
    parser.add_argument("--profile", action="store_true",
                        help="ログイン済みブラウザで自分のプロフィールも読む（手動投稿も拾う）")
    parser.add_argument("--username", help="自分のユーザー名（既定: 履歴のパーマリンクから）")
    parser.add_argument("--scrolls", type=int, default=120)
    parser.add_argument("--include-ambiguous", action="store_true",
                        help="C（あいまい）も削除対象に含める。既定では含めない")
    parser.add_argument("--show-ambiguous", action="store_true")
    parser.add_argument("--execute", action="store_true",
                        help=f"実削除する。{ENV_FLAG}=true も要る")
    parser.add_argument("--candidates", type=Path, help="実削除に使う候補ファイル（既定: 最新）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    if args.execute:
        return _execute(args)
    return _dry_run(args)


def _dry_run(args: argparse.Namespace) -> int:
    before = _parse_time(args.before)
    if before is None:
        print(f"--before を読めません: {args.before}")
        return 2

    history = load_history_posts(args.history)
    posts = history
    if args.profile:
        username = args.username or username_from(history)
        if not username:
            print("ユーザー名が分かりません。--username で指定してください")
            return 2
        lock = _acquire_lock()
        if lock is None:
            print("自動返信が動いています。終わってからもう一度実行してください")
            return 1
        try:
            from src.config import load_config
            from src.engage.browser.session import ThreadsSession

            with ThreadsSession(load_config(), headless=not args.headed) as session:
                session.require_login()
                rows = collect_profile_rows(session, username, scrolls=args.scrolls)
        finally:
            lock.close()
        found = profile_posts(rows, username)
        print(f"プロフィールから {len(found)}件（@{username}）")
        posts = merge_posts(history, found)

    data = build_candidates(posts, before=before, include_ambiguous=args.include_ambiguous)
    path = save_candidates(data, args.out_dir)
    print_report(data, path, show_ambiguous=args.show_ambiguous)
    return 0


def _execute(args: argparse.Namespace) -> int:
    from src.config import load_config
    from src.engage.browser import selectors

    # .env の DELETE_OLD_THREADS_POSTS も読む（既存の環境変数は上書きしない）
    config = load_config()
    measured = {key: selectors.get(key).measured for key in DELETE_SELECTORS}
    refusal = execution_refusal(execute=True, env=dict(os.environ), measured=measured)
    if refusal:
        print(f"実削除しません: {refusal}")
        return 1

    path = args.candidates or latest_candidates(args.out_dir)
    if path is None or not path.exists():
        print("候補ファイルがありません。先に Dry Run を実行して一覧を確認してください")
        return 1
    candidates = json.loads(path.read_text(encoding="utf-8"))
    if args.include_ambiguous != bool(candidates.get("include_ambiguous")):
        print("--include-ambiguous が Dry Run のときと食い違っています。Dry Run からやり直してください")
        return 1

    plan = plan_execution(candidates, deleted=load_deleted(args.out_dir), limit=args.limit,
                          confirm_measured=measured["delete_confirm_button"])
    if not plan:
        print("消す投稿は残っていません")
        return 0
    if not measured["delete_confirm_button"]:
        print("確認ダイアログのセレクタが未実測なので、今回は1件だけ消します")

    lock = _acquire_lock()
    if lock is None:
        print("自動返信が動いています。終わってからもう一度実行してください")
        return 1

    from src.engage.browser.session import ThreadsSession
    from src.errors import SelectorMissError, ThrottledError

    print(f"\n候補ファイル: {path}\n今回消す: {len(plan)}件\n")
    done = 0
    try:
        with ThreadsSession(config, headless=not args.headed) as session:
            session.require_login()
            for index, entry in enumerate(plan):
                if index:
                    wait = random.randint(*INTERVAL_SECONDS)
                    print(f"  … {wait}秒あけます")
                    time.sleep(wait)
                outcome = delete_one(session, entry)
                append_deleted(args.out_dir, entry, outcome)
                done += 1
                label = "削除しました" if outcome == "deleted" else "すでにありません"
                print(f"  [{entry['class']}] {label}: {entry['excerpt']}  {entry['url']}")
    except ThrottledError as exc:
        print(f"\n⛔ 絞られています。ここで止めます: {exc}")
        return 1
    except SelectorMissError as exc:
        print(f"\n⛔ セレクタが引けません。ここで止めます: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"\n⛔ {exc}")
        return 1
    finally:
        lock.close()
        print(f"\n今回の処理: {done}件（記録: {args.out_dir / DELETED_LOG}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
