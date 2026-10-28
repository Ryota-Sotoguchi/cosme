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

from src.content.books import is_book  # noqa: E402
from src.content.voices import extract_voices, vocabulary_for  # noqa: E402

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
  // レビュー本文は dl/dd や review 系のクラスに入る。
  // 構造が変わっても拾えるよう、複数の当たり方を試す。
  //
  // **候補を試したところで打ち切らない。**
  // もとは out.length >= 5 で break していたため、最初に当たった
  // セレクタだけを見て終わっていた。実測で1商品あたり4〜6件しか
  // 集まらず、「2件以上が同じことを言っている」の判定に届かない
  // 商品が半分あった。全部の当たり方を試して、本文で重複を除く。
  const seen = new Set();
  const sels = [
    '[class*="review-body"]', '[class*="reviewBody"]', '[class*="ReviewBody"]',
    '[class*="comment"]', 'dd.comment',
    '[class*="review"] p', '[data-testid*="review"]',
  ];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(n => {
      const t = (n.innerText || '').trim();
      if (t.length >= 10 && t.length <= 600) seen.add(t);
    });
  }
  return Array.from(seen);
}
"""

# レビューは下までスクロールしないと読み込まれないことがある。
# 開いた直後に読むと、画面に入っている数件しか取れない。
SCROLL_JS = """
async () => {
  const step = () => new Promise(r => setTimeout(r, 400));
  for (let i = 0; i < 8; i++) {
    window.scrollBy(0, window.innerHeight);
    await step();
  }
  window.scrollTo(0, 0);
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
                # 遅延読み込みぶんを出してから読む。
                # 失敗しても、画面に出ているぶんだけで続行する。
                try:
                    page.evaluate(SCROLL_JS)
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass
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



def _candidate_targets(limit: int) -> list[tuple[str, str, str]]:
    """これから投稿されそうな候補を、スコアの高い順に返す。

    本番の選び方（collect_candidates → filter_items → score_items）をそのまま使う。
    順番が本番と揃っていないと、集めた声が使われないまま終わる。
    """
    from src.config import load_config
    from src.content.facts import category_label
    from src.rakuten.client import RakutenClient
    from src.selector.filters import filter_items
    from src.selector.scoring import score_items

    config = load_config()
    try:
        pool = RakutenClient(config).collect_candidates(
            config.genres,
            min_price=config.selection.get("min_price"),
            max_price=config.selection.get("max_price"),
        )
    except Exception as exc:  # noqa: BLE001 — 候補が取れなくても履歴ぶんは集める
        print(f"候補を取得できませんでした: {exc}", file=sys.stderr)
        return []

    kept = filter_items(pool, config.exclusion, config.selection)
    scored = score_items(kept, config.raw.get("scoring", {}), config.selection)
    out: list[tuple[str, str, str]] = []
    for entry in scored[:limit]:
        item = entry.item
        if item.item_code and item.item_url:
            out.append((item.item_code, item.item_url, category_label(item)))
    print(f"候補から {len(out)}件", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-code", action="append", default=None,
                        help="この商品だけ（動作確認用）")
    parser.add_argument("--limit", type=int, default=10, help="1回に見る商品数")
    parser.add_argument("--wait-ms", type=int, default=6000)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--interval", type=float, default=3.0,
                        help="商品間の間隔（秒）。まとめて叩かない")
    parser.add_argument("--candidates", type=int, default=0,
                        help="これから投稿されそうな候補からも集める件数")
    args = parser.parse_args()

    # 対象は2系統ある。
    #
    # 1. **投稿済みの商品**（従来）。履歴にある itemCode と item_url を使う
    # 2. **これから投稿されそうな候補**（--candidates、2026-10-28 に追加）
    #
    # 1だけだと、声は «もう一度同じ商品を投稿するとき» にしか使えない。
    # 同じ商品は30日クールダウンに入るので、実際にはほとんど使われていなかった
    # （投稿45件に対して声があるのは15件）。
    # これから出る候補を先に集めておけば、**初めて出す商品にも声が付く**。
    #
    # 候補を取るだけではクールダウンに入らない（履歴に書かないので）。
    #
    # レビューURLの数値IDは商品ページからしか分からないので、
    # itemCode と item_url を組で持つ。
    targets: list[tuple[str, str, str]] = []

    if args.candidates > 0:
        targets.extend(_candidate_targets(args.candidates))

    history = ROOT / "data" / "history.jsonl"
    if history.exists():
        seen: set[str] = {code for code, _, _ in targets}
        for line in history.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            code, url = record.get("item_code"), record.get("item_url")
            # **カテゴリーごとに語彙を分けて数える**（2026-10-28）。
            # それまでは本を丸ごと除いていた。化粧品の語で本のレビューを数えると
            # 「軽くて読みやすい」が「軽いつけ心地」になるため。
            # voices.VOCABULARIES に本と道具の語彙を足したので、除かずに数える。
            if code and url and code not in seen:
                seen.add(code)
                targets.append((code, url, str(record.get("category") or "")))

    if args.item_code:
        wanted = set(args.item_code)
        targets = [t for t in targets if t[0] in wanted]
    else:
        # 候補は先頭にあるので、そちらを優先して残す
        targets = targets[:args.limit] if args.candidates else targets[-args.limit:]

    if not targets:
        print("対象の商品がありません", file=sys.stderr)
        return 1

    store = load_existing()
    ok = 0
    for i, (code, item_url, category) in enumerate(targets):
        if i:
            time.sleep(args.interval)
        bodies, error = fetch_reviews(item_url, wait_ms=args.wait_ms,
                                      timeout_ms=args.timeout_ms)
        if error:
            print(f"NG  {code}: {error}", file=sys.stderr)
            continue
        summary = extract_voices(code, bodies, vocabulary_for(category))
        if not summary.counts:
            print(f"--  {code}: 使用感を拾えず（レビュー{len(bodies)}件）", file=sys.stderr)
            continue
        ok += 1
        # **本文は保存しない。** 数えた結果だけ。
        store[code] = {
            "updated_at": datetime.now(JST).strftime("%Y-%m-%d"),
            "reviews_seen": summary.total_reviews,
            "voices": [w for w, _ in summary.counts],
            # 何件のレビューがそう書いていたか。
            # 「〜って人が多かった」と書けるかどうかの根拠になる。
            # 2件で「多い」とは言えないので、投稿側で言い方を変える。
            "counts": {w: n for w, n in summary.counts},
        }
        print(f"OK  {code}: {summary.phrase(3)}", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"書き出し: {OUT}  ({ok}/{len(targets)}商品)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
