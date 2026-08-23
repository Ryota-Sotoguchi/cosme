#!/usr/bin/env python3
"""参考アカウントの投稿を毎日ためる。

## なぜスクリプトなのか

Threads の公開プロフィールは JavaScript で描画される。
curl では外枠しか取れないので、ブラウザで描画してから本文を読む。

    $ curl https://www.threads.com/@xxx | grep 投稿本文   # 0件

Threads API には他人の投稿を読む口が無いので、公開ページを見るしかない。

## なぜ分析までやらないのか

型の抽出には「これはうちの制約で使えるか」という判断が要る。
それは機械にやらせず、`/research` で人（と Claude）がやる。

このスクリプトの仕事は**取りこぼさないこと**だけ。
毎日ためておけば、何日か空けてから読んでも分析できる。

## 失敗しても止めない

1アカウント取れなくても、他は取る。全部落ちても異常終了しない。
取れなかったことは記録に残す。**憶測で埋めない。**
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_MD = ROOT / "research" / "accounts.md"
OUT_DIR = ROOT / "research" / "log" / "raw"

# accounts.md の表から URL を拾う
URL_PATTERN = re.compile(r"https://www\.threads\.com/@[A-Za-z0-9_.]+")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# プロフィール欄やナビの行。投稿本文ではないので落とす。
CHROME_LINES = {
    "スレッド", "返信", "メディア", "再投稿", "フォローする", "メンション",
    "もっと見る", "翻訳を見る", "Thread", "Replies", "Media", "Reposts",
}

# ログイン壁より後ろは投稿ではない。
#
# ログアウト状態だと数件でこの壁に当たる。GitHub Actions の
# データセンターIPだと4〜6件、手元の回線だと十数件まで見られる。
#
# 毎日走らせる前提なので、前日からの新着が拾えれば足りる。
# ログインして回避することはしない（アカウントを機械操作に使わない）。
LOGIN_WALL = re.compile(
    r"(投稿をもっと見るにはログイン|Threadsにログインするかサインアップ"
    r"|Log in to see more|Instagramでログイン)"
)


def read_accounts() -> list[str]:
    if not ACCOUNTS_MD.exists():
        return []
    seen: list[str] = []
    for url in URL_PATTERN.findall(ACCOUNTS_MD.read_text(encoding="utf-8")):
        if url not in seen:
            seen.append(url)
    return seen


def clean(text: str) -> tuple[list[str], bool]:
    """描画結果から、投稿本文らしい行だけ残す。

    余計なものを落としすぎるより、多めに残して人が読むほうがいい。
    判断は分析側でやる。

    (本文の行, ログイン壁に当たったか) を返す。
    """
    lines: list[str] = []
    walled = False
    for raw in text.split("\n"):
        line = raw.strip()
        if LOGIN_WALL.search(line):
            walled = True
            break  # ここから先は投稿ではない
        if not line or line in CHROME_LINES:
            continue
        # いいね数などの数字だけの行
        if re.fullmatch(r"[\d,.万kK]+", line):
            continue
        lines.append(line)
    return lines, walled


def fetch(url: str, *, wait_ms: int, timeout_ms: int) -> tuple[list[str], bool, str | None]:
    """1アカウント分を取る。(本文の行, ログイン壁, エラー) を返す。"""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(locale="ja-JP", user_agent=USER_AGENT)
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(wait_ms)
                body = page.inner_text("body")
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — 何が起きても次のアカウントへ進む
        return [], False, f"{type(exc).__name__}: {exc}"

    lines, walled = clean(body)
    if len(lines) < 5:
        return lines, walled, "描画されたが本文がほとんど無い"
    return lines, walled, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-ms", type=int, default=7000,
                        help="描画を待つ時間")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--out", type=Path, default=None,
                        help="出力先。既定は research/log/raw/YYYY-MM-DD.md")
    args = parser.parse_args()

    accounts = read_accounts()
    if not accounts:
        print("accounts.md からURLを読めませんでした", file=sys.stderr)
        return 1

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out = args.out or (OUT_DIR / f"{today}.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    parts = [
        f"# {today} 収集",
        "",
        "参考アカウントの公開ページを機械的にためたもの。**分析はしていない。**",
        "型の抽出は `/research` でやる。",
        "",
        "ログアウトで見られる範囲までしか取れない。直近の数件が拾えれば、",
        "毎日走らせている前提では足りる。",
        "",
    ]
    ok = 0
    for url in accounts:
        name = url.rsplit("/", 1)[-1]
        lines, walled, error = fetch(
            url, wait_ms=args.wait_ms, timeout_ms=args.timeout_ms
        )
        parts.append(f"## {name}")
        parts.append("")
        if error:
            parts.append(f"取得できず: {error}")
            print(f"NG  {name}: {error}", file=sys.stderr)
        else:
            ok += 1
            note = "（ログイン壁まで）" if walled else ""
            print(f"OK  {name}: {len(lines)}行{note}", file=sys.stderr)
            if walled:
                parts.append("※ ログイン壁の手前まで。これより古い投稿は取れていない。")
                parts.append("")
        if lines:
            parts.append("```")
            parts.extend(lines[:200])
            parts.append("```")
        parts.append("")

    parts.append(f"取得できたアカウント: {ok}/{len(accounts)}")
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"書き出し: {out}  ({ok}/{len(accounts)})", file=sys.stderr)

    # 全滅しても異常終了しない。記録は残っているので、翌日また試せばいい。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
