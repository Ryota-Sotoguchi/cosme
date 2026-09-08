"""ページから投稿を取り出す。**純粋なパース。playwright を import しない。**

## 反応数は innerText から読む

はじめは aria-label（「いいね 1,234件」のような）から件数を読む設計にしていた。
位置に頼らないぶん頑丈になるはずだった。

**2026-09-04 の実測でその前提が間違っていると分かった。** Threads の
aria-label に件数は入っていない。入っているのは操作名だけ:

    「いいね！」 / コメントする / 再投稿 / シェアする

件数はアイコンの隣のテキストにあるので、投稿カードの innerText を
後ろから読む（`parse_search_block`）。実データで検証済み。

## そのぶん静かに壊れうる

「末尾に並ぶ数字を後ろから拾う」ので、Threads が並び順を変えると
エラーを出さずに違う数字を読む。`actions.looks_like_silent_drift()` が
「候補のほぼ全件で反応数が0」を検知して気づく役目を持つ。

## 投稿時刻だけは属性から読める

`time[datetime]` に ISO8601 が入っている（実測確認済み）。表示は
「18時間」だが、属性のほうが正確なのでそちらを使う。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..candidates import Candidate, parse_search_block

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 投稿カードを取り出す。パーマリンクを持つ a を起点に祖先を遡る。
#
# 2026-09-04 実測: article は 0個で、投稿カードは
# div[data-pressable-container="true"]。祖先を遡る手が正しい。
EXTRACT_JS = """
(maxPosts) => {
  const links = Array.from(document.querySelectorAll('a[href*="/post/"]'));
  const seen = new Set();
  const out = [];

  for (const a of links) {
    const href = a.getAttribute('href');
    if (!href || seen.has(href)) continue;
    if (!/^\\/@[^/]+\\/post\\/[^/]+$/.test(href.split('?')[0])) continue;
    seen.add(href);

    // 投稿カードまで遡る。data-pressable-container が付いた要素で止める。
    let node = a;
    for (let i = 0; i < 10 && node.parentElement; i++) {
      node = node.parentElement;
      if (node.dataset && node.dataset.pressableContainer === 'true') break;
    }

    const timeEl = node.querySelector('time[datetime]');

    out.push({
      href,
      text: (node.innerText || '').slice(0, 1500),
      datetime: timeEl ? timeEl.getAttribute('datetime') : null,
      hasMedia: !!node.querySelector('img[alt]:not([alt=""]), video'),
    });
    if (out.length >= maxPosts) break;
  }
  return out;
}
"""


def age_hours_from_datetime(value: str | None, *, now: datetime | None = None) -> float | None:
    """time[datetime] から経過時間を出す。

    表示テキスト（「3時間」）より機械可読で、表示形式の変更に影響されない。
    """
    if not value:
        return None
    try:
        posted = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=JST)
    now = now or datetime.now(JST)
    return max((now - posted).total_seconds() / 3600, 0.0)


def parse_row(row: dict[str, Any], *, source: str = "",
              now: datetime | None = None) -> Candidate | None:
    """取り出した1件を Candidate にする。"""
    now = now or datetime.now(JST)
    href = (row.get("href") or "").split("?")[0]

    candidate = parse_search_block(href, row.get("text", ""), now=now)
    if candidate is None:
        return None

    candidate.source = source
    candidate.href = href
    candidate.collected_at = now.isoformat(timespec="seconds")
    candidate.has_media = bool(row.get("hasMedia"))

    precise_age = age_hours_from_datetime(row.get("datetime"), now=now)
    if precise_age is not None:
        candidate.age_hours = precise_age

    return candidate


def parse_rows(rows: list[dict[str, Any]], *, source: str = "",
               now: datetime | None = None) -> list[Candidate]:
    """取り出した一覧を Candidate の一覧にする。

    1件が壊れていても他は返す。**推測で埋めない。**
    """
    out: list[Candidate] = []
    for row in rows or []:
        try:
            candidate = parse_row(row, source=source, now=now)
        except Exception as exc:  # noqa: BLE001 — 1件で全部を落とさない
            logger.debug("投稿を解釈できませんでした: %s", exc)
            continue
        if candidate is not None:
            out.append(candidate)
    return out
