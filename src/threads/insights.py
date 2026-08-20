"""投稿ごとの成績を取得して履歴に紐づける。

収益化のためには「どの投稿が見られ、どの投稿が反応されたか」が要る。
これが無いと、テンプレート・時間帯・カテゴリーのどれを直せばいいか
判断できない（勘で config をいじることになる）。

Threads の insights は公式APIで取得できる。追加コストは無い。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..errors import AuthError, PostRejectedError, TransientError
from ..http import HttpClient

logger = logging.getLogger(__name__)

# 投稿単位で取れる指標
POST_METRICS = "views,likes,replies,reposts,quotes,shares"
# アカウント単位で取れる指標
ACCOUNT_METRICS = "views,likes,replies,followers_count"


@dataclass
class PostInsight:
    post_id: str
    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    shares: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "views": self.views,
            "likes": self.likes,
            "replies": self.replies,
            "reposts": self.reposts,
            "shares": self.shares,
        }

    @property
    def engagements(self) -> int:
        return sum(v or 0 for v in (self.likes, self.replies, self.reposts, self.shares))


class ThreadsInsights:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.credentials = config.credentials
        th = config.threads
        self.api_base: str = th["api_base"].rstrip("/")
        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            max_retries=th.get("max_retries", 3),
            default_headers={"Accept": "application/json"},
        )

    @property
    def _token(self) -> str:
        self.credentials.require_threads()
        return self.credentials.threads_access_token or ""

    @staticmethod
    def _values(payload: dict[str, Any]) -> dict[str, int]:
        """insights のレスポンスを {指標名: 値} に均す。

        指標によって total_value と values のどちらで返るかが違う。
        """
        out: dict[str, int] = {}
        for metric in payload.get("data", []):
            name = metric.get("name")
            if not name:
                continue
            if "total_value" in metric:
                out[name] = int(metric["total_value"].get("value", 0))
            else:
                values = metric.get("values") or []
                if values:
                    out[name] = int(values[-1].get("value", 0))
        return out

    def for_post(self, post_id: str) -> PostInsight | None:
        """1投稿の成績。取れなければ None（運用は止めない）。"""
        try:
            response = self.http.get(
                f"{self.api_base}/{post_id}/insights",
                params={"metric": POST_METRICS, "access_token": self._token},
            )
            payload = response.json()
        except (TransientError, AuthError, PostRejectedError) as exc:
            logger.warning("投稿 %s の成績を取得できませんでした: %s", post_id, exc)
            return None

        if "error" in payload:
            # 投稿直後は集計前で取れないことがある。異常ではない。
            logger.info(
                "投稿 %s の成績はまだ取得できません: %s",
                post_id,
                str(payload["error"].get("message"))[:120],
            )
            return None

        values = self._values(payload)
        return PostInsight(
            post_id=post_id,
            views=values.get("views"),
            likes=values.get("likes"),
            replies=values.get("replies"),
            reposts=values.get("reposts"),
            shares=values.get("shares"),
        )

    def for_account(self) -> dict[str, int]:
        """アカウント全体の指標。フォロワー数の推移を見るのに使う。"""
        user_id = self.credentials.threads_user_id or "me"
        try:
            response = self.http.get(
                f"{self.api_base}/{user_id}/threads_insights",
                params={"metric": ACCOUNT_METRICS, "access_token": self._token},
            )
            payload = response.json()
        except (TransientError, AuthError, PostRejectedError) as exc:
            logger.warning("アカウント指標を取得できませんでした: %s", exc)
            return {}

        if "error" in payload:
            logger.warning(
                "アカウント指標を取得できませんでした: %s",
                str(payload["error"].get("message"))[:120],
            )
            return {}
        return self._values(payload)
