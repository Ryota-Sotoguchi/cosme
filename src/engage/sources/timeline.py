"""ホームタイムラインから集める。"""

from __future__ import annotations

import logging

from ...config import Config
from ...errors import ThrottledError
from ..browser import actions
from ..browser.extract import EXTRACT_JS, parse_rows
from ..candidates import Candidate

logger = logging.getLogger(__name__)

HOME_URL = "https://www.threads.com/"


class TimelineSource:
    name = "timeline"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.scrolls = int(
            config.autoreply_section("sources").get("timeline_scrolls", 6))

    def collect(self, session, *, limit: int = 40) -> list[Candidate]:
        page = session.goto(HOME_URL)
        actions.wait_for_posts(page)
        actions.scroll(page, self.scrolls)
        if actions.exists(page, "error_state"):
            # ホームTLまで拒否されたら、プロフィールより深刻。
            raise ThrottledError("Threads がタイムラインの表示を拒否しました。")

        rows = page.evaluate(EXTRACT_JS, limit)
        found = parse_rows(rows, source=self.name)

        if actions.looks_like_silent_drift(found):
            # セレクタは当たっているのに数字が全部0。位置が変わった疑い。
            # 順位付けだけが静かに壊れるので、ここで気づく。
            logger.warning(
                "タイムラインの反応数がほぼ全件0です（%d件）。"
                " DOM が変わった可能性があります。probe_threads_dom.py で実測してください。",
                len(found))
        return found
