"""公式APIのキーワード検索。

## なぜこれが要るのか

スクレイプで取れるパーマリンクの短縮ID（DTVoI4xlSTZ）は、
API が `reply_to_id` に要求する数値ID（17908396266285102）とは
**別のID空間**で、変換できない（自分の投稿40件で検証済み）。

公式検索は `id` を返すので、**これ経由で見つけた投稿にだけ返信できる。**

## 取れないもの

いいね数・返信数は返らない。返るのは
`id / text / media_type / permalink / timestamp / username /
has_replies / is_quote_post / is_reply` だけ。

「伸びている投稿」を選ぶには反応数が要るので、
スクレイプした反応数と permalink で突き合わせる（engage/candidates.py）。

## 権限

`threads_keyword_search` が必要。未付与だと HTTP 500 が返る。
承認前は自分の投稿しか検索できない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..errors import AuthError, TransientError
from ..http import HttpClient

logger = logging.getLogger(__name__)

FIELDS = "id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply"


@dataclass
class SearchHit:
    """検索で見つかった他人の投稿。"""

    post_id: str          # reply_to_id に使える数値ID
    text: str
    username: str
    permalink: str
    timestamp: str = ""
    has_replies: bool = False
    is_reply: bool = False
    is_quote_post: bool = False

    @property
    def shortcode(self) -> str:
        """permalink 末尾の短縮ID。スクレイプ結果との突き合わせに使う。"""
        return self.permalink.rstrip("/").rsplit("/", 1)[-1] if self.permalink else ""


class ThreadsSearch:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.credentials = config.credentials
        th = config.threads
        self.api_base = th["api_base"]
        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            max_retries=th.get("max_retries", 3),
        )

    @property
    def _token(self) -> str:
        return self.credentials.threads_access_token

    def search(
        self,
        keyword: str,
        *,
        search_type: str = "TOP",
        limit: int = 25,
        media_type: str | None = None,
    ) -> list[SearchHit]:
        """公開投稿をキーワードで検索する。

        取得できなければ空を返す。1つのキーワードで落ちても
        呼び出し側は次へ進めるようにする。
        """
        params: dict[str, Any] = {
            "q": keyword,
            "search_type": search_type,
            "fields": FIELDS,
            "limit": min(limit, 100),
            "access_token": self._token,
        }
        if media_type:
            params["media_type"] = media_type

        try:
            response = self.http.get(f"{self.api_base}/keyword_search", params=params)
        except (TransientError, AuthError) as exc:
            logger.warning("検索できませんでした（%s）: %s", keyword, exc)
            return []

        if response.status_code >= 500:
            # 権限が無いと 500 が返る。何が足りないかを明示する。
            logger.error(
                "検索が HTTP %d。threads_keyword_search が未付与の可能性があります"
                "（Metaアプリに権限を追加し、トークンを取り直してください）",
                response.status_code,
            )
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.warning("検索の応答を解釈できません（%s）", keyword)
            return []

        if "error" in payload:
            logger.warning("検索エラー（%s）: %s", keyword,
                           str(payload["error"].get("message"))[:200])
            return []

        hits = []
        for d in payload.get("data", []):
            if not d.get("id"):
                continue
            hits.append(SearchHit(
                post_id=str(d["id"]),
                text=d.get("text") or "",
                username=d.get("username") or "",
                permalink=d.get("permalink") or "",
                timestamp=d.get("timestamp") or "",
                has_replies=bool(d.get("has_replies")),
                is_reply=bool(d.get("is_reply")),
                is_quote_post=bool(d.get("is_quote_post")),
            ))
        logger.info("検索「%s」: %d件", keyword, len(hits))
        return hits

    def available(self) -> bool:
        """検索が使える状態か。権限の有無を確かめるのに使う。"""
        try:
            response = self.http.get(
                f"{self.api_base}/keyword_search",
                params={"q": "test", "fields": "id", "limit": 1,
                        "access_token": self._token},
            )
        except (TransientError, AuthError):
            return False
        return response.status_code < 400
