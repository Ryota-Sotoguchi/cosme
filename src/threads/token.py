"""Threads アクセストークンの管理。

公式仕様:
  * 短命トークン(1時間) → 長期トークン(60日)
      GET https://graph.threads.net/access_token
          ?grant_type=th_exchange_token&client_secret=...&access_token=...
  * 長期トークンの更新（発行から24時間以上経過、かつ未失効であること）
      GET https://graph.threads.net/refresh_access_token
          ?grant_type=th_refresh_token&access_token=...
  * 60日間更新されなかったトークンは失効し、再認可が必要になる

突然の運用停止を避けるため、
  - 週次で自動更新する（.github/workflows/token-refresh.yml）
  - 期限を state.json に記録し、残日数が少なくなったら警告する
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..errors import AuthError, MissingSecretError
from ..http import HttpClient
from ..storage.state import State

logger = logging.getLogger(__name__)


@dataclass
class TokenInfo:
    access_token: str
    expires_in: int  # 秒

    @property
    def expires_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.expires_in)

    @property
    def days(self) -> int:
        return self.expires_in // 86400


class ThreadsTokenManager:
    def __init__(self, config: Config, state: State, http: HttpClient | None = None) -> None:
        self.config = config
        self.state = state
        th = config.threads
        self.oauth_base: str = th.get("oauth_base", "https://graph.threads.net").rstrip("/")
        self.warn_days: int = int(th.get("token_warn_days", 14))
        self.http = http or HttpClient(
            connect_timeout=th.get("connect_timeout", 10.0),
            read_timeout=th.get("read_timeout", 30.0),
            max_retries=th.get("max_retries", 3),
            default_headers={"Accept": "application/json"},
        )

    # ------------------------------------------------------------------
    def exchange_for_long_lived(self, short_lived_token: str) -> TokenInfo:
        """短命トークンを60日トークンへ交換する。"""
        app_secret = self.config.credentials.threads_app_secret
        if not app_secret:
            raise MissingSecretError(["THREADS_APP_SECRET"])

        response = self.http.get(
            f"{self.oauth_base}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_lived_token,
            },
        )
        payload = response.json()
        if "error" in payload:
            raise AuthError(f"長期トークンへの交換に失敗: {payload['error']}")

        token = payload.get("access_token")
        if not token:
            raise AuthError(f"access_token が返りませんでした: {payload}")

        info = TokenInfo(access_token=str(token), expires_in=int(payload.get("expires_in", 0)))
        self.state.record_token_refresh(info.expires_in)
        logger.info("長期トークンを取得しました（有効期間 約%d日）", info.days)
        return info

    def refresh(self, long_lived_token: str) -> TokenInfo:
        """長期トークンを更新する（発行から24時間以上経過している必要がある）。"""
        response = self.http.get(
            f"{self.oauth_base}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": long_lived_token},
        )
        payload = response.json()
        if "error" in payload:
            raise AuthError(f"トークン更新に失敗: {payload['error']}")

        token = payload.get("access_token")
        if not token:
            raise AuthError(f"access_token が返りませんでした: {payload}")

        info = TokenInfo(access_token=str(token), expires_in=int(payload.get("expires_in", 0)))
        self.state.record_token_refresh(info.expires_in)
        logger.info("トークンを更新しました（有効期間 約%d日）", info.days)
        return info

    # ------------------------------------------------------------------
    def warn_if_expiring(self) -> int | None:
        """期限が近ければ警告を出す。残日数を返す（不明なら None）。"""
        remaining = self.state.token_days_remaining()
        if remaining is None:
            logger.info("トークンの期限情報がまだ記録されていません")
            return None
        if remaining <= 0:
            logger.error("Threads アクセストークンは失効しています。再認可が必要です。")
        elif remaining <= self.warn_days:
            logger.warning(
                "Threads アクセストークンの残り日数が %d 日です。更新してください。", remaining
            )
        else:
            logger.info("Threads アクセストークン残り %d 日", remaining)
        return remaining

    # ------------------------------------------------------------------
    def store_to_github_secret(self, token: str, secret_name: str = "THREADS_ACCESS_TOKEN") -> bool:
        """更新後のトークンを GitHub Secrets へ書き戻す。

        GH_PAT が無い環境では警告のみで False を返す（運用は止めない）。
        """
        pat = self.config.credentials.github_pat or os.environ.get("GITHUB_TOKEN")
        repository = os.environ.get("GITHUB_REPOSITORY")

        if not pat or not repository:
            logger.warning(
                "GH_PAT / GITHUB_REPOSITORY が無いため、Secret への書き戻しをスキップしました。"
                " 新しいトークンは手動で登録してください。"
            )
            return False

        gh = shutil.which("gh")
        if not gh:
            logger.warning("gh CLI が見つからないため Secret を更新できませんでした")
            return False

        try:
            subprocess.run(
                [gh, "secret", "set", secret_name, "--repo", repository, "--body", token],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "GH_TOKEN": pat},
                timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            logger.error("Secret の更新に失敗しました: %s", stderr[:300])
            return False

        logger.info("GitHub Secret '%s' を更新しました", secret_name)
        return True
