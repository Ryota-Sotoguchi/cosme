#!/usr/bin/env python3
"""商品のレビューから、使用感だけを集める。

## なぜ要るのか

投稿に「実際に使った人がどう感じたか」が一言も無かった。
「レビューが多いです」では何も伝わらない。

## なぜスクレイピングなのか

楽天のレビューAPI（IchibaItem/Review）は廃止されている。実測で確認済み。

    {"error_description":"Operation IchibaItem/Review doesn't exist"}

商品検索APIが返すのは件数と平均だけで、本文は含まれない。

## 何を保存するか

**レビュー本文は保存しない。**

保存するのは「使用感の語が何件のレビューに出たか」という数だけ。
本文は書いた人の著作物なので転載できないが、
語の出現回数は事実なので問題にならない。

効能に触れているレビューは、集計から丸ごと除く（voices.py）。
薬機法で、体験談を効能効果の証明に使うことはできないため。

## 負荷をかけない

1商品ずつ、間隔を空けて取る。まとめて叩かない。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.content.voices import extract_voices  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "voices.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# レビュー本文だけを取り出す。ページの他の文言を拾わないよう、
# レビュー1件ぶんの塊を選ぶ。
EXTRACT_JS = """
() => {
  const out = [];
  // レビュー本文は dl/dd や review 系のクラスに入る。
  // 構造が変わっても拾えるよう、複数の当たり方を試す。
  // 実測で review-body が取れた。構造が変わっても拾えるよう候補を並べる。
  const sels = [
    '[class*="review-body"]', '[class*="reviewBody"]', '[class*="ReviewBody"]',
    '[class*="comment"]', 'dd.comment',
  ];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(n => {
      const t = (n.innerText || '').trim();
      if (t.length >= 10 && t.length <= 600) out.push(t);
    });
    if (out.length >= 5) break;
  }
  return out;
}
"""


# レビューページのURLは **数値ID** を使う。
#
#   商品ページ  https://item.rakuten.co.jp/tsurunishi/90xb079s1wb7s/
#   レビュー    https://review.rakuten.co.jp/item/1/358413_10000288/1.1/
#                                              ↑ショップの数値ID ↑商品の数値ID
#
# APIが返す itemCode（"tsurunishi:10000288"）のショップ側は文字列なので、
# そこから直接は作れない。商品ページを開いてリンクを拾う。
REVIEW_LINK_JS = """
() => {
  const a = Array.from(document.querySelectorAll('a[href*="review.rakuten.co.jp/item/"]'))
    .map(x => x.href)
    .find(h => /\\/item\\/1\\/\\d+_\\d+\\//.test(h));
  return a || null;
}
"""


def resolve_review_url(page, item_url: str, *, wait_ms: int, timeout_ms: int) -> str | None:
    """商品ページを開いて、レビューページのURLを拾う。"""
    page.goto(item_url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(wait_ms)
    return page.evaluate(REVIEW_LINK_JS)


def fetch_reviews(item_url: str, *, wait_ms: int, timeout_ms: int) -> tuple[list[str], str | None]:
    """商品ページ経由でレビュー本文を取る。

    商品ページ → レビューページ、と2回開く。
    レビューURLの数値IDは商品ページからしか分からないため。
    """
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(locale="ja-JP", user_agent=USER_AGENT)
                page = ctx.new_page()
                url = resolve_review_url(page, item_url, wait_ms=wait_ms,
                                         timeout_ms=timeout_ms)
                if not url:
                    return [], "レビューページへのリンクが見つかりません"
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(wait_ms)
                bodies = page.evaluate(EXTRACT_JS)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — 1件落ちても次の商品へ
        return [], f"{type(exc).__name__}: {exc}"
    return list(bodies), None


def load_existing() -> dict:
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-code", action="append", default=None,
                        help="この商品だけ（動作確認用）")
    parser.add_argument("--limit", type=int, default=10, help="1回に見る商品数")
    parser.add_argument("--wait-ms", type=int, default=6000)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--interval", type=float, default=3.0,
                        help="商品間の間隔（秒）。まとめて叩かない")
    args = parser.parse_args()

    # 投稿済みの商品を対象にする。新しく候補を取ると
    # その商品が30日クールダウンに入ってしまう。
    #
    # レビューURLの数値IDは商品ページからしか分からないので、
    # itemCode と item_url を組で持つ。
    targets: list[tuple[str, str]] = []
    history = ROOT / "data" / "history.jsonl"
    if history.exists():
        seen: set[str] = set()
        for line in history.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            code, url = record.get("item_code"), record.get("item_url")
            if code and url and code not in seen:
                seen.add(code)
                targets.append((code, url))

    if args.item_code:
        wanted = set(args.item_code)
        targets = [t for t in targets if t[0] in wanted]
    else:
        targets = targets[-args.limit:]

    if not targets:
        print("対象の商品がありません", file=sys.stderr)
        return 1

    store = load_existing()
    ok = 0
    for i, (code, item_url) in enumerate(targets):
        if i:
            time.sleep(args.interval)
        bodies, error = fetch_reviews(item_url, wait_ms=args.wait_ms,
                                      timeout_ms=args.timeout_ms)
        if error:
            print(f"NG  {code}: {error}", file=sys.stderr)
            continue
        summary = extract_voices(code, bodies)
        if not summary.counts:
            print(f"--  {code}: 使用感を拾えず（レビュー{len(bodies)}件）", file=sys.stderr)
            continue
        ok += 1
        # **本文は保存しない。** 数えた結果だけ。
        store[code] = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d"),
            "reviews_seen": summary.total_reviews,
            "voices": [w for w, _ in summary.counts],
        }
        print(f"OK  {code}: {summary.phrase(3)}", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"書き出し: {OUT}  ({ok}/{len(targets)}商品)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
