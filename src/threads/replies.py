"""自分の投稿に付いたコメントへの返信。

会話が続くと投稿自体のリーチも伸びるので、露出を増やす手段として
いちばん安全なもの。**他人の投稿に自動で返信するのとは別物**で、
自分の投稿へのコメントに返すのは通常の利用の範囲。

それでも「繰り返しコメント」に見えないよう、以下を守る:
  * 同じコメントに二度返さない（返信済みを state で管理）
  * 1回の実行で返す数に上限を置く
  * 定型文でも複数用意して回す
  * 宣伝・スパムらしいコメントには返さない
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..errors import AuthError, PostRejectedError, TransientError
from ..http import HttpClient
from ..storage.state import State

logger = logging.getLogger(__name__)

# 返信しないコメント。宣伝・勧誘・URL付きなど。
_SKIP_PATTERNS = (
    re.compile(r"https?://"),
    re.compile(r"(DM|ディーエム)[くだ下]さい|副業|稼[げぐ]|投資|儲か|お金|LINE|ライン@"),
    re.compile(r"(相互|フォロバ|フォロー[しお])"),
)

# 質問っぽいコメントへの返し
_ANSWER_REPLIES = (
    "ありがとうございます！わたしも迷ってるところです🥺",
    "気になりますよね〜！わたしもまだ決めきれてないです💭",
    "コメントうれしいです🤍 参考になったらよかったです！",
)
# それ以外への返し
_THANKS_REPLIES = (
    "ありがとうございます🤍",
    "うれしいです、ありがとうございます☺️",
    "コメントありがとうございます〜！",
    "わ、ありがとうございます😌",
)


@dataclass
class Reply:
    reply_id: str
    text: str
    username: str | None

    @property
    def is_question(self) -> bool:
        return "？" in self.text or "?" in self.text

    @property
    def should_skip(self) -> bool:
        if not self.text.strip():
            return True
        return any(p.search(self.text) for p in _SKIP_PATTERNS)


class ReplyResponder:
    def __init__(self, config: Config, state: State, http: HttpClient | None = None) -> None:
        self.config = config
        self.state = state
        self.credentials = config.credentials
        th = config.threads
        self.api_base: str = th["api_base"].rstrip("/")
        self.max_per_run: int = int(th.get("max_replies_per_run", 5))
        # 通常投稿と同じく、コンテナ作成から公開まで待つ。
        # 待たずに公開すると "The requested resource does not exist" になる。
        self.publish_wait: int = int(th.get("publish_wait_seconds", 30))
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

    def _own_username(self) -> str:
        """自分のユーザー名。自分の返信に返さないための判定に使う。

        投稿IDとユーザーIDを比べる実装にしていたら、スレッドの2本目
        （自分の返信）をコメントとみなして自分に返信しかけた。
        """
        cached = self.state.get("threads_username")
        if cached:
            return str(cached)
        response = self.http.get(
            f"{self.api_base}/me",
            params={"fields": "username", "access_token": self._token},
        )
        payload = response.json()
        username = str(payload.get("username") or "")
        if username:
            self.state.set("threads_username", username)
        return username

    # ------------------------------------------------------------------
    def _answered(self) -> set[str]:
        return set(self.state.get("answered_replies", []))

    def _remember(self, reply_id: str, keep: int = 500) -> None:
        answered = list(self.state.get("answered_replies", []))
        answered.append(reply_id)
        self.state.set("answered_replies", answered[-keep:])

    # ------------------------------------------------------------------
    def recent_post_ids(self, limit: int = 15) -> list[str]:
        user_id = self.credentials.threads_user_id or "me"
        response = self.http.get(
            f"{self.api_base}/{user_id}/threads",
            params={"fields": "id", "limit": limit, "access_token": self._token},
        )
        payload = response.json()
        if "error" in payload:
            raise PostRejectedError(str(payload["error"].get("message"))[:200])
        return [d["id"] for d in payload.get("data", []) if d.get("id")]

    def replies_on(self, post_id: str) -> list[Reply]:
        response = self.http.get(
            f"{self.api_base}/{post_id}/replies",
            params={"fields": "id,text,username", "access_token": self._token},
        )
        payload = response.json()
        if "error" in payload:
            logger.info("投稿 %s の返信を取得できません: %s", post_id, payload["error"].get("message"))
            return []
        own = self._own_username()
        out = []
        for d in payload.get("data", []):
            # 自分の返信には返さない。スレッド投稿の2本目以降も
            # ここに出てくるので、必ず除外する。
            if own and str(d.get("username") or "") == own:
                continue
            out.append(Reply(reply_id=str(d["id"]), text=d.get("text") or "",
                             username=d.get("username")))
        return out

    def _reply_text(self, reply: Reply, cursor: int) -> str:
        pool = _ANSWER_REPLIES if reply.is_question else _THANKS_REPLIES
        return pool[cursor % len(pool)]

    def post_reply(self, reply_to_id: str, text: str) -> str | None:
        user_id = self.credentials.threads_user_id or "me"
        try:
            response = self.http.post(
                f"{self.api_base}/{user_id}/threads",
                data={"media_type": "TEXT", "text": text,
                      "reply_to_id": reply_to_id, "access_token": self._token},
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
                data={"creation_id": creation_id, "access_token": self._token},
            )
            payload = response.json()
            if "error" in payload:
                raise PostRejectedError(str(payload["error"].get("message"))[:200])
            return str(payload["id"])
        except (TransientError, AuthError, PostRejectedError) as exc:
            logger.warning("返信できませんでした (%s): %s", reply_to_id, exc)
            return None

    # ------------------------------------------------------------------
    def run(self, *, dry_run: bool = True) -> list[dict[str, Any]]:
        """未返信のコメントに返す。dry_run のときは投稿しない。"""
        answered = self._answered()
        planned: list[dict[str, Any]] = []
        cursor = len(answered)

        for post_id in self.recent_post_ids():
            if len(planned) >= self.max_per_run:
                break
            for reply in self.replies_on(post_id):
                if len(planned) >= self.max_per_run:
                    break
                if reply.reply_id in answered:
                    continue
                if reply.should_skip:
                    logger.info("返信をスキップ (@%s): 宣伝・空文のため", reply.username)
                    self._remember(reply.reply_id)   # 二度と見ない
                    continue

                text = self._reply_text(reply, cursor)
                cursor += 1
                entry = {
                    "post_id": post_id,
                    "reply_id": reply.reply_id,
                    "username": reply.username,
                    "comment": reply.text[:60],
                    "reply": text,
                    "posted": False,
                }
                if not dry_run:
                    result = self.post_reply(reply.reply_id, text)
                    entry["posted"] = result is not None
                    if result:
                        self._remember(reply.reply_id)
                planned.append(entry)

        return planned
