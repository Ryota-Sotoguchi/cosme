"""トークンにどの権限が入っているかを、実際に叩いて確かめる。

## なぜ必要か

Threads のトークンには内省用のエンドポイントが無い。
`debug_token` は Facebook のトークン用で、Threads のトークンは解釈できない。

そのうえ **アプリに権限を足しても、既存のトークンには入らない。**
ダッシュボードで発行したトークンは発行時点の権限を焼き込んでいて、
`refresh_access_token` はスコープを引き継ぐだけで追加しない。

そのため「ダッシュボードでは有効なのに動かない」が起きる。
各エンドポイントを1回ずつ叩いて、どれが通るかで判断する。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..errors import AuthError, TransientError
from ..http import HttpClient


@dataclass
class PermissionCheck:
    name: str
    scope: str
    ok: bool
    detail: str = ""
    needed_for: str = ""


class PermissionProbe:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        th = config.threads
        self.api_base = th["api_base"]
        self.token = config.credentials.threads_access_token
        self.user_id = config.credentials.threads_user_id or "me"
        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            # 診断なので粘らない。落ちること自体が答え。
            max_retries=1,
        )

    def _probe(self, path: str, params: dict) -> tuple[bool, str]:
        try:
            response = self.http.get(f"{self.api_base}/{path}",
                                     params={**params, "access_token": self.token})
        except (TransientError, AuthError) as exc:
            return False, str(exc)[:120]

        if response.status_code >= 500:
            # 権限が無いエンドポイントは本文の無い 500 を返す
            return False, f"HTTP {response.status_code}（権限が入っていない可能性）"
        try:
            payload = response.json()
        except ValueError:
            return False, f"HTTP {response.status_code} 応答を解釈できません"
        if "error" in payload:
            return False, str(payload["error"].get("message"))[:120]
        return True, "OK"

    def run(self) -> list[PermissionCheck]:
        checks = []

        ok, detail = self._probe("me", {"fields": "id,username"})
        checks.append(PermissionCheck(
            "プロフィール取得", "threads_basic", ok, detail,
            "すべての土台"))

        ok, detail = self._probe(f"{self.user_id}/threads",
                                 {"fields": "id", "limit": 1})
        checks.append(PermissionCheck(
            "自分の投稿一覧", "threads_basic", ok, detail,
            "投稿・重複防止"))

        ok, detail = self._probe(f"{self.user_id}/threads_insights",
                                 {"metric": "views"})
        checks.append(PermissionCheck(
            "インサイト", "threads_manage_insights", ok, detail,
            "insights コマンド"))

        ok, detail = self._probe("keyword_search",
                                 {"q": "test", "fields": "id", "limit": 1})
        checks.append(PermissionCheck(
            "キーワード検索", "threads_keyword_search", ok, detail,
            "他人の投稿を見つけて返信する"))

        return checks
