"""ログ設定。

GitHub Actions のログで読めることを優先したシンプルな構成。
秘密情報がログに出ないよう、既知のトークン値をマスクするフィルタを入れている。
"""

from __future__ import annotations

import logging
import os
import sys

# マスク対象にする環境変数名
_SECRET_ENV_NAMES = (
    "RAKUTEN_APPLICATION_ID",
    "RAKUTEN_ACCESS_KEY",
    "THREADS_ACCESS_TOKEN",
    "THREADS_APP_SECRET",
    "GH_PAT",
)


class SecretMaskingFilter(logging.Filter):
    """ログメッセージに含まれる秘密情報を伏せ字にする。"""

    def __init__(self) -> None:
        super().__init__()
        self._secrets = [
            v for name in _SECRET_ENV_NAMES if (v := os.environ.get(name)) and len(v) >= 8
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            masked = message
            for secret in self._secrets:
                masked = masked.replace(secret, "***REDACTED***")
            if masked != message:
                record.msg = masked
                record.args = ()
        return True


def setup_logging(level: str | None = None) -> logging.Logger:
    """ルートロガーを初期化して返す。二重初期化しても安全。"""
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(resolved)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)

    mask = SecretMaskingFilter()
    for handler in root.handlers:
        handler.addFilter(mask)

    return root
