"""投稿ごとの成績を取得して履歴に紐づける。

収益化のためには「どの投稿が見られ、どの投稿が反応されたか」が要る。
これが無いと、テンプレート・時間帯・カテゴリーのどれを直せばいいか
判断できない（勘で config をいじることになる）。

Threads の insights は公式APIで取得できる。追加コストは無い。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..errors import AuthError, PostRejectedError, TransientError
from ..http import HttpClient

logger = logging.getLogger(__name__)

# 投稿単位で取れる指標
POST_METRICS = "views,likes,replies,reposts,quotes,shares"
# アカウント単位で取れる指標
ACCOUNT_METRICS = "views,likes,replies,followers_count"

# 日の境目は JST で決める（アカウントの読者は日本時間で動いている）
_JST = timezone(timedelta(hours=9))


@dataclass
class PostInsight:
    post_id: str
    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    shares: int | None = None
    quotes: int | None = None
    # 自分の連投による返信数。API は返さないので別途埋める。
    replies_self: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        values: dict[str, int | None] = {
            "views": self.views,
            "likes": self.likes,
            "replies": self.replies,
            "reposts": self.reposts,
            "shares": self.shares,
            "quotes": self.quotes,
        }
        if self.replies_self is not None:
            values["replies_self"] = self.replies_self
        return values

    @property
    def engagements(self) -> int:
        return sum(v or 0 for v in (self.likes, self.replies, self.reposts, self.shares))

    @property
    def human_replies(self) -> int:
        """人からの返信数。自分の連投を引いた数。

        replies_self が分からない場合は 0 を仮定する（過大評価しない側には
        倒せないが、少なくとも「連投した本数」が分かる投稿では正しくなる）。
        """
        return max(0, (self.replies or 0) - (self.replies_self or 0))


@dataclass(frozen=True)
class AccountInsight:
    """アカウント全体の指標と、**それがどの日の数字なのか**。

    日付を一緒に返すのは、窓付き（前日1日ぶん）と窓なし（当日の途中まで）で
    指す日が違うため。`bool()` は指標が取れたかどうか。
    """

    metrics: dict[str, int]
    day: str
    complete: bool

    def __bool__(self) -> bool:
        return bool(self.metrics)


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
            quotes=values.get("quotes"),
        )

    def human_reply_count(self, post_id: str, own_username: str) -> int | None:
        """その投稿に付いた「人からの」トップレベル返信の数。

        連投した本数が履歴に無い古い投稿のために用意している。
        新しい投稿は extra["segments"] から引き算できるので、ここは呼ばない
        （投稿1件につきAPI1回なので、毎回全件に投げるとレート上限に近づく）。

        取れなければ None。運用は止めない。
        """
        try:
            response = self.http.get(
                f"{self.api_base}/{post_id}/replies",
                params={"fields": "id,username", "access_token": self._token},
            )
            payload = response.json()
        except (TransientError, AuthError, PostRejectedError) as exc:
            logger.warning("投稿 %s の返信を取得できませんでした: %s", post_id, exc)
            return None

        if "error" in payload:
            logger.info(
                "投稿 %s の返信はまだ取得できません: %s",
                post_id,
                str(payload["error"].get("message"))[:120],
            )
            return None

        return sum(
            1
            for entry in payload.get("data", [])
            if str(entry.get("username") or "") != own_username
        )

    def for_account(self, *, when: datetime | None = None) -> "AccountInsight":
        """アカウント全体の指標。フォロワー数の推移を見るのに使う。

        **窓を明示して «前日1日ぶん» を取る（2026-10-13）。**
        since / until を渡さないと API は直近の短い窓を日ごとの桶で返し、
        `_values()` が最後の桶（= 測った時点までの当日ぶん）を拾う。
        insights の発火は混雑で何時間もずれるので、何時までの値かが日によって違い、
        **日ごとの比較ができない数字になっていた**（9/28 が 5294、翌日が 67 など）。
        `followers_count` は total_value なので、この問題とは無関係に正しい。

        窓付きの取得が失敗したら、窓なしでもう一度試す（従来の値）。
        **フォロワー数を取り逃がさないことを優先する。**

        返すのは `AccountInsight`。**どの日の数字なのかを一緒に返す**ので、
        呼び出し側が «前日ぶんを今日の行に書く» 取り違えをしない。
        窓なしで取れたときは `complete=False` で、その日の途中までの値になる。
        """
        now = (when or datetime.now(_JST)).astimezone(_JST)
        windowed = self._account_call(self._previous_day_window(now))
        if windowed:
            yesterday = now.date() - timedelta(days=1)
            return AccountInsight(windowed, day=yesterday.isoformat(), complete=True)

        logger.info("窓付きのアカウント指標が取れないので、窓なしで取り直します"
                    "（その日の途中までの値になります）")
        return AccountInsight(self._account_call(None),
                              day=now.date().isoformat(), complete=False)

    @staticmethod
    def _previous_day_window(when: datetime | None = None) -> dict[str, int]:
        """前日の 00:00〜24:00（JST）を unix 秒で返す。"""
        now = (when or datetime.now(_JST)).astimezone(_JST)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        return {"since": int(start.timestamp()), "until": int(end.timestamp())}

    def _account_call(self, window: dict[str, int] | None) -> dict[str, int]:
        user_id = self.credentials.threads_user_id or "me"
        params: dict[str, Any] = {"metric": ACCOUNT_METRICS, "access_token": self._token}
        if window:
            params.update(window)
        try:
            response = self.http.get(
                f"{self.api_base}/{user_id}/threads_insights", params=params
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
