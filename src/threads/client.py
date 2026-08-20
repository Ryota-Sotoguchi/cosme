"""Threads 投稿クライアント。

公式仕様（graph.threads.net v1.0）:
  1. POST /{threads-user-id}/threads         … メディアコンテナ作成 (media_type=TEXT)
  2. （平均30秒待つことが推奨されている）
  3. POST /{threads-user-id}/threads_publish … 公開
  4. GET  /{post-id}?fields=id,permalink,...  … 実在検証

投稿上限は 250投稿 / 24時間 / プロフィール。
本文は 500文字まで。

「投稿できた」の判断は publish のレスポンスだけに頼らず、
必ず 4 の GET で投稿IDを引き直して確認する。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..errors import AuthError, PostRejectedError, TransientError
from ..http import HttpClient

logger = logging.getLogger(__name__)


@dataclass
class PublishedPost:
    post_id: str
    permalink: str | None
    timestamp: str | None
    text: str | None
    verified: bool

    def __str__(self) -> str:
        return f"post_id={self.post_id} permalink={self.permalink} verified={self.verified}"


class ThreadsClient:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.credentials = config.credentials
        th = config.threads

        self.api_base: str = th["api_base"].rstrip("/")
        self.publish_wait: int = int(th.get("publish_wait_seconds", 30))
        self.max_text_length: int = int(th.get("max_text_length", 500))

        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            max_retries=th.get("max_retries", 3),
            default_headers={"Accept": "application/json"},
        )
        # 数値IDでなければ無視して /me から引き直す。
        # ユーザー名を設定してしまう間違いが起きやすく、その場合 Graph API は
        # 「Object with ID '<username>' does not exist」という分かりにくい
        # エラーを返すため、ここで弾いておく。
        configured = (self.credentials.threads_user_id or "").strip()
        if configured and not configured.isdigit():
            logger.warning(
                "THREADS_USER_ID が数値ではないため無視します（ユーザー名ではなく"
                "数値IDを指定してください）。/me から取得し直します。"
            )
            configured = ""
        self._user_id: str | None = configured or None

    # ------------------------------------------------------------------
    @property
    def _token(self) -> str:
        self.credentials.require_threads()
        return self.credentials.threads_access_token or ""

    def _raise_for_payload(self, payload: dict[str, Any], context: str) -> None:
        """Graph API はHTTP 200でも error を返すことがあるので本文を検査する。"""
        error = payload.get("error")
        if not error:
            return
        message = error.get("message", "")
        code = error.get("code")
        detail = f"{context}: code={code} message={message}"
        if code in (190, 102, 10, 200):  # トークン失効・権限不足
            raise AuthError(detail)
        raise PostRejectedError(detail)

    # ------------------------------------------------------------------
    def get_user_id(self) -> str:
        """Threads ユーザーIDを取得する。環境変数にあればそれを使う。"""
        if self._user_id:
            return self._user_id

        response = self.http.get(
            f"{self.api_base}/me",
            params={"fields": "id,username", "access_token": self._token},
        )
        payload = response.json()
        self._raise_for_payload(payload, "/me の取得に失敗")

        user_id = payload.get("id")
        if not user_id:
            raise AuthError(f"/me からユーザーIDを取得できませんでした: {payload}")

        self._user_id = str(user_id)
        logger.info("Threads ユーザー: id=%s username=%s", self._user_id, payload.get("username"))
        return self._user_id

    def get_profile(self) -> dict[str, Any]:
        response = self.http.get(
            f"{self.api_base}/me",
            params={
                "fields": "id,username,name,threads_profile_picture_url,threads_biography",
                "access_token": self._token,
            },
        )
        payload = response.json()
        self._raise_for_payload(payload, "プロフィール取得に失敗")
        return payload

    # ------------------------------------------------------------------
    def create_container(
        self,
        text: str,
        *,
        link_attachment: str | None = None,
        reply_to_id: str | None = None,
        topic_tag: str | None = None,
    ) -> str:
        """テキスト投稿のメディアコンテナを作成し creation_id を返す。

        link_attachment を使うと、URLがリンクカードとして添付され
        **本文の500文字を消費しない**。楽天のアフィリエイトURLは
        280文字近くあるので、本文に直接書くと内容がほとんど入らない。

        reply_to_id を渡すと、その投稿への返信になる。
        自分の投稿に返信を重ねるとスレッドになる。
        """
        if not text.strip():
            raise PostRejectedError("本文が空です")
        if len(text) > self.max_text_length:
            raise PostRejectedError(
                f"本文が長すぎます: {len(text)} > {self.max_text_length}"
            )

        user_id = self.get_user_id()
        data: dict[str, Any] = {
            "media_type": "TEXT",
            "text": text,
            "access_token": self._token,
        }
        if link_attachment:
            data["link_attachment"] = link_attachment
        if reply_to_id:
            data["reply_to_id"] = reply_to_id
        if topic_tag:
            # トピックタグはそのトピックの一覧に載るので、
            # フォロワーがいなくても見つけてもらえる導線になる。
            data["topic_tag"] = topic_tag

        response = self.http.post(f"{self.api_base}/{user_id}/threads", data=data)
        payload = response.json()
        self._raise_for_payload(payload, "コンテナ作成に失敗")

        creation_id = payload.get("id")
        if not creation_id:
            raise PostRejectedError(f"creation_id が返りませんでした: {payload}")

        logger.info(
            "コンテナ作成: creation_id=%s (本文 %d文字%s%s)",
            creation_id,
            len(text),
            ", リンク添付あり" if link_attachment else "",
            f", {reply_to_id} への返信" if reply_to_id else "",
        )
        return str(creation_id)

    def publish_container(self, creation_id: str) -> str:
        """コンテナを公開し、投稿IDを返す。"""
        user_id = self.get_user_id()
        response = self.http.post(
            f"{self.api_base}/{user_id}/threads_publish",
            data={"creation_id": creation_id, "access_token": self._token},
        )
        payload = response.json()
        self._raise_for_payload(payload, "公開に失敗")

        post_id = payload.get("id")
        if not post_id:
            raise PostRejectedError(f"投稿IDが返りませんでした: {payload}")

        logger.info("公開: post_id=%s", post_id)
        return str(post_id)

    def fetch_post(self, post_id: str) -> dict[str, Any]:
        """投稿を取得して実在を確認する。"""
        response = self.http.get(
            f"{self.api_base}/{post_id}",
            params={
                "fields": "id,permalink,text,timestamp,media_type",
                "access_token": self._token,
            },
        )
        payload = response.json()
        self._raise_for_payload(payload, "投稿の取得に失敗")
        return payload

    # ------------------------------------------------------------------
    def post_text(
        self,
        text: str,
        *,
        wait_seconds: int | None = None,
        link_attachment: str | None = None,
        reply_to_id: str | None = None,
        topic_tag: str | None = None,
    ) -> PublishedPost:
        """テキスト投稿の全工程を実行する。

        成功レスポンスだけを根拠にせず、最後に GET で実在検証する。
        """
        creation_id = self.create_container(
            text,
            link_attachment=link_attachment,
            reply_to_id=reply_to_id,
            topic_tag=topic_tag,
        )

        wait = self.publish_wait if wait_seconds is None else wait_seconds
        if wait > 0:
            logger.info("コンテナ処理待ち %d 秒", wait)
            time.sleep(wait)

        post_id = self.publish_container(creation_id)

        # 検証。ここで失敗しても投稿自体は成功している可能性があるので、
        # 例外にせず verified=False で返して呼び出し側に判断させる。
        try:
            detail = self.fetch_post(post_id)
            published = PublishedPost(
                post_id=post_id,
                permalink=detail.get("permalink"),
                timestamp=detail.get("timestamp"),
                text=detail.get("text"),
                verified=str(detail.get("id")) == post_id,
            )
        except (TransientError, PostRejectedError) as exc:
            logger.warning("投稿の実在検証に失敗しました (投稿自体は成功の可能性): %s", exc)
            published = PublishedPost(
                post_id=post_id, permalink=None, timestamp=None, text=None, verified=False
            )

        logger.info("投稿完了: %s", published)
        return published

    def post_thread(
        self,
        segments: list[str],
        *,
        link_attachment: str | None = None,
        wait_seconds: int | None = None,
        topic_tag: str | None = None,
    ) -> list[PublishedPost]:
        """複数の投稿をつなげてスレッドにする。

        1本目がタイムラインに出る投稿で、2本目以降は返信として連なる。
        リンクは最後の投稿にだけ添付する（先頭に広告リンクを置かない構成）。

        途中で失敗した場合、それまでに公開した分は残る。すでに投稿された
        ものを取り消すと履歴と実態がずれるので、消さずに呼び出し側へ返す。
        """
        if not segments:
            raise PostRejectedError("投稿する本文がありません")

        published: list[PublishedPost] = []
        parent_id: str | None = None

        for index, segment in enumerate(segments):
            is_last = index == len(segments) - 1
            try:
                result = self.post_text(
                    segment,
                    wait_seconds=wait_seconds,
                    link_attachment=link_attachment if is_last else None,
                    reply_to_id=parent_id,
                    # タグは1本目だけ。返信に付けても一覧には出ない。
                    topic_tag=topic_tag if index == 0 else None,
                )
            except (PostRejectedError, TransientError) as exc:
                if not published:
                    raise
                logger.error(
                    "スレッド %d/%d 本目の投稿に失敗しました。ここまでは公開済みです: %s",
                    index + 1,
                    len(segments),
                    exc,
                )
                break
            published.append(result)
            parent_id = result.post_id

        logger.info("スレッド投稿完了: %d/%d 本", len(published), len(segments))
        return published

    # ------------------------------------------------------------------
    def recent_post_count(self, *, limit: int = 50) -> int:
        """直近の投稿数を数える。レート上限(250/24h)の目安確認用。"""
        user_id = self.get_user_id()
        response = self.http.get(
            f"{self.api_base}/{user_id}/threads",
            params={"fields": "id,timestamp", "limit": limit, "access_token": self._token},
        )
        payload = response.json()
        self._raise_for_payload(payload, "投稿一覧の取得に失敗")
        return len(payload.get("data", []))
