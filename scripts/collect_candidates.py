#!/usr/bin/env python3
"""返信する価値のある他人の投稿を集める。

## なぜスクレイピングなのか

公式APIのキーワード検索は **いいね数・返信数を返さない**
（id / text / permalink / timestamp / username / has_replies のみ）。
「伸びている投稿」を判定できないので、検索ページを描画して読むしかない。

参考アカウント収集（scripts/collect_threads.py）と同じ作りにしてある。
失敗しても止めない。取れなかったことは記録に残す。**憶測で埋めない。**

## 返信そのものはここでやらない

集めるだけ。返信文は `/reply` で人が確認しながら作る。

スクレイプで取れる短縮ID（DTVoI4xlSTZ）は、API が reply_to_id に要求する
数値ID（17908396266285102）とは**別のID空間**なので、そもそも
API から自動で返信できない。パーマリンクを開いて人が返す。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engage.candidates import Candidate, parse_search_block, rank_candidates  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "research" / "replies"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 探すキーワード。美容を主にしつつ、少しだけ外も見る。
BEAUTY_KEYWORDS: tuple[str, ...] = (
    "コスメ", "スキンケア", "メイク", "プチプラコスメ", "デパコス",
    "韓国コスメ", "新作コスメ", "購入品", "垢抜け", "ヘアケア",
)
OTHER_KEYWORDS: tuple[str, ...] = ("自分磨き", "買い物", "ファッション")

# 投稿要素を取り出す。パーマリンクを持つ a を起点に、投稿全体の親までさかのぼる。
EXTRACT_JS = """
() => {
  const links = Array.from(document.querySelectorAll('a[href*="/post/"]'));
  const seen = new Set();
  const out = [];
  for (const a of links) {
    const href = a.getAttribute('href');
    if (!href || seen.has(href)) continue;
    seen.add(href);
    let node = a;
    for (let i = 0; i < 8 && node.parentElement; i++) node = node.parentElement;
    out.push({ href, text: (node.innerText || '').slice(0, 1200) });
  }
  return out;
}
"""


def collect(keyword: str, *, wait_ms: int, timeout_ms: int,
            scrolls: int) -> tuple[list[Candidate], str | None]:
    """1キーワード分を集める。(候補, エラー) を返す。"""
    from playwright.sync_api import sync_playwright

    url = f"https://www.threads.com/search?q={quote(keyword)}&serp_type=default"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(locale="ja-JP", user_agent=USER_AGENT)
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(wait_ms)
                # ログアウトだと数件で壁に当たる。少しだけスクロールして稼ぐ。
                for _ in range(scrolls):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(2000)
                rows = page.evaluate(EXTRACT_JS)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — 1つ落ちても次のキーワードへ進む
        return [], f"{type(exc).__name__}: {exc}"

    now = datetime.now(JST)
    found = []
    for row in rows:
        c = parse_search_block(row.get("href", ""), row.get("text", ""),
                               keyword=keyword, now=now)
        if c is not None:
            found.append(c)
    return found, None


def render(picked: list[Candidate], errors: dict[str, str], today: str) -> str:
    lines = [
        f"# {today} 返信候補",
        "",
        "検索ページから機械的に集めたもの。**返信文はまだ作っていない。**",
        "`/reply` で内容を読んでから作る。",
        "",
        "投稿するときはパーマリンクを開いて自分で返す。",
        "スクレイプした短縮IDは API の reply_to_id には使えない（ID空間が別）。",
        "",
    ]
    if errors:
        lines += ["## 取得できなかったキーワード", ""]
        lines += [f"- {k}: {v}" for k, v in errors.items()]
        lines.append("")

    lines += [f"## 候補 {len(picked)}件", ""]
    for i, c in enumerate(picked, 1):
        age = f"{c.age_hours:.0f}時間前" if c.age_hours is not None else "時期不明"
        kind = "美容" if c.is_beauty else "その他"
        lines += [
            f"### {i}. @{c.username}　[{kind}]",
            "",
            f"- {c.permalink}",
            f"- いいね {c.likes:,} / 返信 {c.replies:,} / {age} / 検索語「{c.keyword}」",
            f"- 評価 {c.scores}",
            "",
            "```",
            c.text[:400],
            "```",
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", action="append", default=None,
                        help="このキーワードだけ探す（動作確認用）")
    parser.add_argument("--limit", type=int, default=12, help="出す候補の数")
    parser.add_argument("--beauty-ratio", type=float, default=0.8)
    parser.add_argument("--wait-ms", type=int, default=7000)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--scrolls", type=int, default=2)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    keywords = args.keyword or list(BEAUTY_KEYWORDS + OTHER_KEYWORDS)

    all_found: list[Candidate] = []
    errors: dict[str, str] = {}
    for kw in keywords:
        found, error = collect(kw, wait_ms=args.wait_ms,
                               timeout_ms=args.timeout_ms, scrolls=args.scrolls)
        if error:
            errors[kw] = error
            print(f"NG  {kw}: {error}", file=sys.stderr)
        else:
            print(f"OK  {kw}: {len(found)}件", file=sys.stderr)
        all_found += found

    picked = rank_candidates(all_found, limit=args.limit,
                             beauty_ratio=args.beauty_ratio)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out = args.out or (OUT_DIR / f"{today}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(picked, errors, today), encoding="utf-8")

    beauty = sum(1 for c in picked if c.is_beauty)
    print(f"書き出し: {out}", file=sys.stderr)
    print(f"  取得 {len(all_found)}件 → 候補 {len(picked)}件"
          f"（美容 {beauty} / その他 {len(picked) - beauty}）", file=sys.stderr)
    # 全滅しても異常終了しない。翌日また試せばいい。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
