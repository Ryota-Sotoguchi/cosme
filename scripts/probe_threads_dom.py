#!/usr/bin/env python3
"""Threads の DOM を実測する。

## 何のためか

`src/engage/browser/selectors.py` を**推測ではなく実測で**書くための道具。
Threads の class は難読化されていて予告なく変わるので、ページを開いて
「実際にどんな構造・role・aria-label があるか」を数えて出す。

## 何を取るか

  * ページ全体の HTML スナップショット
  * a[href*="/post/"] の祖先を遡った構造ダンプ
      tagName / role / aria-label / data-* の属性名 / 子 svg の aria-label
      **class は意図的に記録しない。** 依存してはいけないものを出力に混ぜない
  * 全 [role] の値と出現数
  * 全 aria-label の値と出現数   ← いちばん重要。「返信」「いいね」がここに出る
  * 全 time[datetime]
  * レジストリの各キーが何ノードに当たったか（1が良い。0 と 50 は駄目）

## 出力先

research/dom/（**.gitignore 済み**）。第三者の投稿本文と、ログイン中の
自分のユーザー名が入るので、公開リポジトリに残さない。

## 使い方

    python3 -m src.main autoreply --login       # 先にログインしておく
    python3 scripts/probe_threads_dom.py --headed
    python3 scripts/probe_threads_dom.py --url https://www.threads.com/@someone
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.engage.browser import selectors  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "research" / "dom"

# 投稿カードの構造を遡って記録する。
# **class は取らない。** 難読化されていて、依存すると数週間で壊れる。
STRUCTURE_JS = """
() => {
  const attrsOf = (el) => {
    const out = {};
    for (const a of el.attributes || []) {
      if (a.name === 'class' || a.name === 'style') continue;
      if (a.name === 'href' || a.name.startsWith('data-') ||
          a.name.startsWith('aria-') || a.name === 'role' || a.name === 'datetime') {
        out[a.name] = (a.value || '').slice(0, 120);
      }
    }
    return out;
  };

  const links = Array.from(document.querySelectorAll('a[href*="/post/"]'));
  const seen = new Set();
  const posts = [];
  for (const a of links.slice(0, 6)) {
    const href = a.getAttribute('href');
    if (!href || seen.has(href)) continue;
    seen.add(href);

    const chain = [];
    let node = a;
    for (let i = 0; i < 12 && node; i++) {
      chain.push({
        depth: i,
        tag: node.tagName,
        attrs: attrsOf(node),
        svgLabels: Array.from(node.querySelectorAll(':scope > svg[aria-label]'))
                        .map(s => s.getAttribute('aria-label')),
        textHead: (node.innerText || '').slice(0, 160),
      });
      node = node.parentElement;
    }
    posts.push({ href, chain });
  }

  const labels = Array.from(document.querySelectorAll('[aria-label]'))
    .map(el => `${el.tagName}|${el.getAttribute('aria-label')}`);
  const roles = Array.from(document.querySelectorAll('[role]'))
    .map(el => el.getAttribute('role'));
  const times = Array.from(document.querySelectorAll('time[datetime]'))
    .map(el => ({ datetime: el.getAttribute('datetime'), text: el.innerText }));
  const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'))
    .map(el => ({ tag: el.tagName, attrs: attrsOf(el) }));

  return { posts, labels, roles, times, editables,
           title: document.title, url: location.href };
}
"""


def count_matches(page, selector) -> dict[str, int]:
    """レジストリの候補セレクタごとに、何ノードに当たったかを数える。"""
    counts: dict[str, int] = {}
    for css in selector.candidates():
        try:
            counts[css] = len(page.query_selector_all(css))
        except Exception as exc:  # noqa: BLE001 — 壊れたセレクタでも止めない
            counts[css] = -1
            print(f"    ! {css}: {type(exc).__name__}", file=sys.stderr)
    if selector.role:
        role, name = selector.role
        try:
            counts[f'role={role} name="{name}" exact={selector.exact}'] = (
                page.get_by_role(role, name=name, exact=selector.exact).count())
        except Exception as exc:  # noqa: BLE001
            counts[f'role={role} name="{name}" exact={selector.exact}'] = -1
            print(f"    ! role={role}: {type(exc).__name__}", file=sys.stderr)
    return counts


def probe(url: str, *, profile_dir: Path, headless: bool,
          wait_ms: int, timeout_ms: int, scrolls: int) -> dict:
    """1ページ分を実測する。失敗しても例外にせず、記録に残す。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 1600},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(wait_ms)
            for _ in range(scrolls):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1500)

            data = page.evaluate(STRUCTURE_JS)
            data["selector_matches"] = {
                key: count_matches(page, selectors.get(key))
                for key in selectors.all_keys()
            }
            data["html_length"] = len(page.content())
            return data
        finally:
            ctx.close()


def render(results: dict[str, dict], today: str) -> str:
    lines = [
        f"# {today} Threads DOM 実測",
        "",
        "`src/engage/browser/selectors.py` を書くための材料。",
        "**このファイルは gitignore 済み**（第三者の投稿本文とログイン中の",
        "ユーザー名が入るため）。",
        "",
        "セレクタの当たり数は **1 が良い**。0 は引けていない、",
        "50 は広すぎて別物を掴んでいる。",
        "",
    ]

    for url, data in results.items():
        lines += [f"## {url}", ""]
        if "error" in data:
            lines += [f"取得できませんでした: `{data['error']}`", ""]
            continue

        lines += [
            f"- 実効URL: {data.get('url')}",
            f"- title: {data.get('title')}",
            f"- HTML長: {data.get('html_length'):,}",
            "",
            "### セレクタの当たり数",
            "",
            "| キー | 実測済 | 必須 | セレクタ | 当たり |",
            "|---|---|---|---|---|",
        ]
        for key, counts in data.get("selector_matches", {}).items():
            spec = selectors.get(key)
            for css, n in counts.items():
                mark = "✅" if n == 1 else ("⚠️" if n > 1 else "❌")
                lines.append(
                    f"| {key} | {'✅' if spec.measured else '—'} | "
                    f"{'✅' if spec.required else '—'} | `{css}` | {mark} {n} |")
        lines.append("")

        labels = Counter(data.get("labels", []))
        lines += ["### aria-label（多い順・上位40）", "",
                  "**ここに「返信」「いいね」の実際の文言が出る。**", "", "```"]
        lines += [f"{n:4d}  {label}" for label, n in labels.most_common(40)]
        lines += ["```", ""]

        roles = Counter(data.get("roles", []))
        lines += ["### role", "", "```"]
        lines += [f"{n:4d}  {role}" for role, n in roles.most_common(30)]
        lines += ["```", ""]

        if data.get("editables"):
            lines += ["### contenteditable（返信の入力欄）", "", "```",
                      json.dumps(data["editables"], ensure_ascii=False, indent=2),
                      "```", ""]

        if data.get("times"):
            lines += ["### time[datetime]（先頭5件）", "", "```"]
            lines += [f"{t['datetime']}  {t['text']}" for t in data["times"][:5]]
            lines += ["```", ""]

        lines += ["### 投稿カードの構造（先頭2件）", "",
                  "class は意図的に記録していない。依存してはいけないため。", ""]
        for post in data.get("posts", [])[:2]:
            lines += [f"#### {post['href']}", "", "```json",
                      json.dumps(post["chain"], ensure_ascii=False, indent=2)[:6000],
                      "```", ""]

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", default=None,
                        help="調べるURL（複数指定可）")
    parser.add_argument("--headed", action="store_true",
                        help="ブラウザを表示する")
    parser.add_argument("--wait-ms", type=int, default=7000)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--scrolls", type=int, default=2)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    config = load_config()
    profile_dir = config.browser_profile_dir
    if not profile_dir.is_dir():
        # 止めない。ログアウト状態でも投稿カードの構造・反応数の aria-label・
        # ログイン壁は測れる。返信ボタンや入力欄はログイン後に測り直す。
        print("⚠️  ログイン済みプロファイルがありません。"
              "ログアウト状態で測れるぶんだけ調べます。", file=sys.stderr)
        print("    返信まわりを測るには先に:"
              " python3 -m src.main autoreply --login", file=sys.stderr)

    urls = args.url or ["https://www.threads.com/"]

    results: dict[str, dict] = {}
    for url in urls:
        print(f"調査中: {url}", file=sys.stderr)
        try:
            results[url] = probe(
                url, profile_dir=profile_dir, headless=not args.headed,
                wait_ms=args.wait_ms, timeout_ms=args.timeout_ms, scrolls=args.scrolls)
        except Exception as exc:  # noqa: BLE001 — 1つ落ちても次のURLへ
            results[url] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"  NG: {exc}", file=sys.stderr)
        else:
            print("  OK", file=sys.stderr)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out = args.out or (OUT_DIR / f"{today}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(results, today), encoding="utf-8")

    print(f"\n書き出し: {out}", file=sys.stderr)
    print("この結果を読んで selectors.py を手で直し、measured=True にしてください。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
