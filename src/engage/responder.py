"""他人の投稿への返信を実際に投稿する。

## 投稿の仕組み

自分の投稿へのコメント返信（`src/threads/replies.py`）とまったく同じ。
`reply_to_id` にコンテナを作って publish するだけで、
相手が自分かどうかで API の使い方は変わらない。

**ただし reply_to_id には数値ID（17908396266285102）が要る。**
スクレイプで取れる短縮ID（DTVoI4xlSTZ）は別のID空間なので使えない。
公式のキーワード検索で引いた投稿にだけ返信できる。

## 出す前に必ず止めるもの

返信は他人の投稿に残る。自分の投稿と違って後から直せない。

  * URL … 楽天アフィリエイト規約でコメント欄へのリンクは禁止
  * 効能の断定 … 薬機法。リンクの有無に関係なく効く
  * 自分への誘導・定型句だけの返信
  * 1日の上限、同じ相手への連投、同じ投稿への二度目
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..config import Config
from ..errors import AuthError, PostRejectedError, TransientError
from ..http import HttpClient
from .review import EngagementLog, review

logger = logging.getLogger(__name__)


@dataclass
class PostResult:
    ok: bool
    reason: str = ""
    reply_id: str = ""


class Responder:
    def __init__(self, config: Config, log: EngagementLog,
                 http: HttpClient | None = None) -> None:
        self.config = config
        self.log = log
        self.credentials = config.credentials
        th = config.threads
        self.api_base = th["api_base"]
        self.publish_wait = int(th.get("publish_wait_seconds", 30))
        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            max_retries=th.get("max_retries", 3),
        )
        eng = config.engagement
        self.max_per_day = int(eng.get("max_per_day", 3))
        self.cooldown_days = int(eng.get("same_account_cooldown_days", 7))

    # ------------------------------------------------------------------
    def check(self, *, username: str, post_id: str, shortcode: str,
              text: str) -> PostResult:
        """投稿してよいかを全部確かめる。**投稿はしない。**"""
        if not post_id:
            return PostResult(False, "post_id がありません（公式検索で引いた投稿だけ返信できます）")

        result = review(text)
        if not result.ok:
            return PostResult(False, result.summary())

        if self.log.replied_today() >= self.max_per_day:
            return PostResult(
                False,
                f"本日の上限に達しています（{self.max_per_day}件）")

        if shortcode and shortcode in self.log.replied_shortcodes():
            return PostResult(False, "この投稿にはすでに返信しています")

        if username in self.log.recent_usernames(days=self.cooldown_days):
            return PostResult(
                False,
                f"@{username} には直近{self.cooldown_days}日で返信済みです")

        return PostResult(True)

    # ------------------------------------------------------------------
    def reply(self, *, username: str, post_id: str, shortcode: str,
              text: str, dry_run: bool = True) -> PostResult:
        """検査を通してから返信する。"""
        verdict = self.check(username=username, post_id=post_id,
                             shortcode=shortcode, text=text)
        if not verdict.ok:
            logger.info("返信しません（@%s）: %s", username, verdict.reason)
            return verdict

        if dry_run:
            logger.info("DRY_RUN: @%s に返信する予定 / %s", username, text)
            return PostResult(True, "dry_run")

        user_id = self.credentials.threads_user_id or "me"
        try:
            response = self.http.post(
                f"{self.api_base}/{user_id}/threads",
                data={"media_type": "TEXT", "text": text,
                      "reply_to_id": post_id, "access_token":
                          self.credentials.threads_access_token},
            )
            payload = response.json()
            if "error" in payload:
                raise PostRejectedError(str(payload["error"].get("message"))[:200])
            creation_id = payload["id"]

            if self.publish_wait > 0:
                logger.info("返信コンテナの処理待ち %d 秒", self.publish_wait)
                time.sleep(self.publish_wait)

            response = self.http.post(
                f"{self.api_base}/{user_id}/threads_publish",
                data={"creation_id": creation_id,
                      "access_token": self.credentials.threads_access_token},
            )
            payload = response.json()
            if "error" in payload:
                raise PostRejectedError(str(payload["error"].get("message"))[:200])
            reply_id = str(payload["id"])
        except (TransientError, AuthError, PostRejectedError) as exc:
            logger.warning("返信できませんでした（@%s）: %s", username, exc)
            return PostResult(False, str(exc)[:200])

        # 記録は投稿できたときだけ。失敗を残すと上限を無駄に食う。
        self.log.append(username, shortcode or post_id, text)
        logger.info("返信しました（@%s）: %s", username, reply_id)
        return PostResult(True, reply_id=reply_id)
