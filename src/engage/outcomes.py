"""出した返信に、どれだけ反応が付いたかを測る。

## なぜ要るのか

返信の «成果» を書き戻す口（`store.update_outcome`）は前からあったのに、
**呼んでいる場所がどこにも無かった**（2026-09-24 に判明）。そのため

  * 反応の良かった返信を手本に回す仕組み（`writer._examples` →
    `store.best_replies`）が、いつまでも空のまま回っていた
  * どんな返信が効いたのかを、人も後から見られなかった

投稿側は `insights` が毎日成績を取り込んでいる。返信側にも同じ口を用意する。

## どう測るか

**相手の投稿を開いて、自分の返信の行を探す。** 自分の返信の permalink
（`our_reply_url`）は着弾確認が取れたときしか残らないので、そこに依存しない。

見つけたら、そのときのいいね数・返信数を書き戻し、ついでに permalink も埋める。
見つからなければ**何も書かない**（0件と記録すると「反応が無かった」と嘘になる）。

## 負荷をかけない

1件ずつ開く。開く間隔は `ThreadsSession` の rate limiter に任せる。
1回の実行で見るのは既定10件まで。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .browser import actions
from .browser.extract import EXTRACT_JS, parse_rows
from .verify import normalize

logger = logging.getLogger(__name__)

# 自分の返信を本文で照合するときの長さ。全文一致にしないのは、
# Threads が長い返信を「もっと見る」で省略するため。
MATCH_HEAD = 16
# 相手の投稿ページで、返信が描かれるまで下へ送る回数
SCROLLS = 3


@dataclass(frozen=True)
class Outcome:
    reply_id: int
    username: str
    likes: int
    replies: int
    url: str = ""


def _match(candidates: list[Any], *, own_username: str, reply_text: str) -> Any | None:
    """自分が書いた返信の行を選ぶ。

    **名前と本文の両方で照合する。** 名前だけだと連投の2本目を拾い、
    本文だけだと同じことを書いた別人を拾う。
    """
    head = normalize(reply_text)[:MATCH_HEAD]
    if not head:
        return None
    for candidate in candidates:
        if own_username and candidate.username.lower() != own_username.lower():
            continue
        if head in normalize(candidate.text):
            return candidate
    return None


def measure_one(session: Any, row: Any, *, own_username: str) -> Outcome | None:
    """返信1件ぶんの反応を読む。見つからなければ None。"""
    page = session.goto(row.permalink)
    actions.wait_for_posts(page)
    page.wait_for_timeout(1500)

    for attempt in range(SCROLLS + 1):
        found = _match(
            parse_rows(page.evaluate(EXTRACT_JS, 600) or [], source="own_reply"),
            own_username=own_username,
            reply_text=row.reply_text,
        )
        if found is not None:
            return Outcome(
                reply_id=row.id,
                username=row.username,
                likes=found.likes or 0,
                replies=found.replies or 0,
                url=found.permalink or "",
            )
        if attempt < SCROLLS:
            # 返信は投稿の下にある。画面に入るまで送る。
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1500)

    logger.info("自分の返信を見つけられませんでした（@%s / %s）", row.username, row.shortcode)
    return None


def measure(session: Any, store: Any, *, own_username: str, limit: int = 10,
            older_than_hours: int = 20) -> list[Outcome]:
    """まだ測っていない返信を、古いものから順に見る。

    **見つからなかったものは記録しない。** 次の実行でもう一度見る
    （相手が投稿ごと消していれば、いつまでも見つからないだけで害はない）。
    """
    pending = store.pending_outcomes(older_than_hours=older_than_hours)[:limit]
    if not pending:
        return []

    done: list[Outcome] = []
    for row in pending:
        try:
            outcome = measure_one(session, row, own_username=own_username)
        except Exception as exc:  # noqa: BLE001 — 1件で全部を落とさない
            logger.warning("成果を読めませんでした（@%s）: %s", row.username, exc)
            continue
        if outcome is None:
            continue
        store.update_outcome(outcome.reply_id, likes=outcome.likes, replies=outcome.replies)
        if outcome.url and not row.our_reply_url:
            store.set_reply_url(outcome.reply_id, outcome.url)
        done.append(outcome)
    return done
