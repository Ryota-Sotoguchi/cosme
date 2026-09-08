"""指定した人気アカウントのプロフィールから集める。

## 誰を見るかは人が選ぶ

`config.toml` の `[[autoreply.targets]]` に書く。既定は空。

**`research/accounts.md` を流用しないこと。** あちらは「文体の参考先」で、
体験談・効能断定・美容医療で伸ばしているアカウントが入っている。
そこに返信すると `SENSITIVE_WORDS` の領域に踏み込む。

## 新しく伸び始めたものを狙う

プロフィールは新しい順に並ぶので、先頭から数件だけ見る。
古い投稿まで遡っても、そこに返信して読まれることはない。
"""

from __future__ import annotations

import logging

from typing import Any

from ...config import Config
from ...errors import ThrottledError
from ..browser import actions
from ..browser.extract import EXTRACT_JS, parse_rows
from ..candidates import Candidate

logger = logging.getLogger(__name__)


class AccountsSource:
    name = "accounts"

    def __init__(self, config: Config, state: Any | None = None) -> None:
        self.config = config
        self.state = state
        self.targets = config.autoreply_targets
        sources = config.autoreply_section("sources")
        self.scrolls = int(sources.get("account_scrolls", 2))
        # **1回に全員を回らない。**
        # 2026-09-05 に13件を連続で開いたら、プロフィールだけ
        # 「エラーが発生しました」を返すようになった（ホームTLは正常）。
        # Threads は短時間に多くのプロフィールを開く動きを見ている。
        self.per_run = int(sources.get("accounts_per_run", 3))

    def _todays_targets(self) -> list:
        """今回巡回する相手。毎回ずらして、全員を薄く回る。"""
        if not self.targets or self.per_run >= len(self.targets):
            return list(self.targets)
        start = 0
        if self.state is not None:
            start = int(self.state.get("autoreply_target_cursor", 0) or 0)
            self.state.set("autoreply_target_cursor",
                           (start + self.per_run) % len(self.targets))
        doubled = list(self.targets) * 2
        return doubled[start % len(self.targets):][:self.per_run]

    def collect(self, session, *, limit: int = 5) -> list[Candidate]:
        if not self.targets:
            logger.info(
                "巡回するアカウントが設定されていません"
                "（config.toml の [[autoreply.targets]]）")
            return []

        # limit は**この収集元が返す総数**。1人あたりの数ではない。
        # 1人あたりに掛けると、アカウントを増やすほど上限を超えていく。
        per_account = max(limit // len(self.targets), 1)

        found: list[Candidate] = []
        for target in self._todays_targets():
            if len(found) >= limit:
                break
            url = f"https://www.threads.com/@{target.username}"
            try:
                page = session.goto(url)
                actions.wait_for_posts(page)
                actions.scroll(page, self.scrolls)
                rows = page.evaluate(EXTRACT_JS, per_account)
            except Exception as exc:  # noqa: BLE001 — 1人分が落ちても次へ
                logger.warning("プロフィールを開けません（@%s）: %s", target.username, exc)
                continue

            if actions.exists(page, "error_state"):
                # **0件なのではなく、止められている。**
                # 握りつぶすと次の定期実行がそのまま突っ込む。上まで上げる。
                raise ThrottledError(
                    f"Threads がプロフィールの表示を拒否しました（@{target.username}）。"
                    " 短時間に開きすぎです。")

            got = parse_rows(rows, source=self.name)
            # 別のアカウントの投稿（リポスト等）が混ざることがある。
            # **指定した本人の投稿だけを残す。**
            own = [c for c in got if c.username.lower() == target.username.lower()]
            logger.info("@%s から %d件（うち本人 %d件）",
                        target.username, len(got), len(own))
            found += own[:per_account]

        return found[:limit]
