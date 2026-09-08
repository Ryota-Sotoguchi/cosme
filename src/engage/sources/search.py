"""検索から集める。

## なぜ要るのか

ホームタイムラインの中身は**フォローしている相手で決まる**。
2026-09-07 の実測では、流れてきた479件のうち32%が「Threads伸ばす」
「経由ありがとうございます」系で、美容の話は27%だった。
そこに返信して届くのは、同じく伸ばそうとしている人たちで、
**楽天でコスメを買う人ではない。**

検索はフォローに依存しない。「プチプラコスメ」で探せば、
その話をしている人のところへ直接行ける。目的（コスメの露出）に対して
いちばん素直な収集元。

## 1回に何語も引かない

検索ページも、プロフィールと同じく開きすぎれば絞られる対象。
毎回1〜2語だけ引いて、語をずらしながら回る。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from ...config import Config
from ...errors import ThrottledError
from ..browser import actions
from ..browser.extract import EXTRACT_JS, parse_rows
from ..candidates import Candidate

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.threads.com/search?q={}&serp_type=default"

# 設定に何も書かれていなければこれを使う。
# scripts/collect_candidates.py が実データで使っていた語を土台にしている。
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "プチプラコスメ", "スキンケア", "デパコス", "韓国コスメ",
    "新作コスメ", "購入品", "ヘアケア", "垢抜け",
)


class SearchSource:
    name = "search"

    def __init__(self, config: Config, state: Any | None = None) -> None:
        self.config = config
        self.state = state
        sources = config.autoreply_section("sources")
        self.keywords = list(sources.get("search_keywords") or DEFAULT_KEYWORDS)
        self.per_run = max(int(sources.get("search_per_run", 2)), 1)
        self.scrolls = int(sources.get("search_scrolls", 2))

    def _todays_keywords(self) -> list[str]:
        """今回引く語。毎回ずらして全部を薄く回る。"""
        if not self.keywords or self.per_run >= len(self.keywords):
            return list(self.keywords)
        start = 0
        if self.state is not None:
            start = int(self.state.get("autoreply_keyword_cursor", 0) or 0)
            self.state.set("autoreply_keyword_cursor",
                           (start + self.per_run) % len(self.keywords))
        doubled = list(self.keywords) * 2
        return doubled[start % len(self.keywords):][:self.per_run]

    def collect(self, session, *, limit: int = 20) -> list[Candidate]:
        if not self.keywords:
            logger.info("検索語が設定されていません（[autoreply.sources] search_keywords）")
            return []

        words = self._todays_keywords()
        per_word = max(limit // len(words), 1)

        found: list[Candidate] = []
        for word in words:
            if len(found) >= limit:
                break
            try:
                page = session.goto(SEARCH_URL.format(quote(word)))
                actions.wait_for_posts(page)
                actions.scroll(page, self.scrolls)
                rows = page.evaluate(EXTRACT_JS, per_word)
            except Exception as exc:  # noqa: BLE001 — 1語落ちても次へ
                logger.warning("検索できません（%s）: %s", word, exc)
                continue

            if actions.exists(page, "error_state"):
                raise ThrottledError(
                    f"Threads が検索の表示を拒否しました（{word}）。開きすぎです。")

            got = parse_rows(rows, source=self.name)
            for c in got:
                c.keyword = word
            logger.info("「%s」から %d件", word, len(got))
            found += got[:per_word]

        return found[:limit]
