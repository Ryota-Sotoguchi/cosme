"""共通HTTPクライアント。

timeout / リトライ / 指数バックオフ + ジッター / ステータス分類 を一箇所に集約する。
無限リトライは行わない（max_retries で必ず打ち切る）。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Mapping

import requests

from .errors import AuthError, RateLimitError, TransientError

logger = logging.getLogger(__name__)

# リトライして意味のあるステータス
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    """呼び出し間隔の下限を保証する。楽天APIの「1秒1リクエスト」対策。"""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_call = time.monotonic()


class HttpClient:
    """リトライ付きの薄いラッパー。

    レスポンスは requests.Response をそのまま返す。JSON解釈は呼び出し側の責務。
    """

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        max_retries: int = 4,
        backoff_base: float = 1.5,
        backoff_max: float = 30.0,
        rate_limiter: RateLimiter | None = None,
        session: requests.Session | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout = (connect_timeout, read_timeout)
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.rate_limiter = rate_limiter
        self.session = session or requests.Session()
        if default_headers:
            self.session.headers.update(default_headers)

    # ------------------------------------------------------------------
    def _sleep_for_attempt(self, attempt: int, retry_after: float | None) -> None:
        """指数バックオフ + ジッター。Retry-After が来ていればそれを尊重する。"""
        if retry_after is not None:
            delay = min(retry_after, self.backoff_max)
        else:
            delay = min(self.backoff_base ** (attempt + 1), self.backoff_max)
        delay += random.uniform(0, delay * 0.25)  # thundering herd 回避
        logger.info("リトライ待機 %.1f 秒 (attempt=%d)", delay, attempt + 1)
        time.sleep(delay)

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_retry: bool = True,
    ) -> requests.Response:
        """HTTPリクエストを送る。

        Raises:
            AuthError: 401 / 403（リトライしない）
            RateLimitError: リトライを使い切った 429
            TransientError: リトライを使い切った 5xx / 接続エラー
        """
        attempts = self.max_retries + 1 if allow_retry else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if self.rate_limiter:
                self.rate_limiter.wait()

            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=dict(headers) if headers else None,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = TransientError(f"接続失敗: {type(exc).__name__}: {exc}")
                logger.warning("接続失敗 (%d/%d): %s", attempt + 1, attempts, exc)
                if attempt + 1 < attempts:
                    self._sleep_for_attempt(attempt, None)
                    continue
                raise last_error from exc
            except requests.RequestException as exc:  # 想定外だがリトライ対象にはしない
                raise TransientError(f"リクエスト失敗: {exc}") from exc

            status = response.status_code

            if status in (401, 403):
                # 403 は楽天APIでは Origin 未設定でも起きるので本文をログに残す
                raise AuthError(
                    f"認証/認可エラー {status}: {url} body={response.text[:400]}"
                )

            if status in _RETRYABLE_STATUS:
                retry_after = self._parse_retry_after(response)
                message = f"HTTP {status}: {url} body={response.text[:300]}"
                last_error = (
                    RateLimitError(message, retry_after)
                    if status == 429
                    else TransientError(message)
                )
                logger.warning("再試行対象の応答 (%d/%d): %s", attempt + 1, attempts, message)
                if attempt + 1 < attempts:
                    self._sleep_for_attempt(attempt, retry_after)
                    continue
                raise last_error

            return response

        # ループを抜けるのは attempts を使い切ったときだけ
        raise last_error or TransientError(f"リクエストに失敗しました: {url}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
