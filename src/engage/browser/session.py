"""ブラウザのセッション。**playwright を import する唯一のモジュール。**

## 永続コンテキストを使う

毎回ログインし直すと、その頻度自体が異常な signal になる。
プロファイルをディスクに残して、普通の人がブラウザを開き直すのと
同じ状態にする。

`launch_persistent_context()` は **BrowserContext を返す**。
`scripts/collect_candidates.py` の `launch()` + `new_context()` とは形が違うので、
あちらをコピーしないこと。

## プロファイルは絶対にコミットしない

中に Threads のセッション Cookie が入る。public リポジトリに載った時点で
アカウント乗っ取りと同義。`.gitignore` に `.playwright/` を入れてある。

## 認証情報をコードで扱わない

ログインは人が手でやる（`autoreply --login`）。2FA もキャプチャも人がやる。
パスワードをこのリポジトリのどこにも置かない。
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from ...config import Config
from ...errors import AuthError
from ...http import RateLimiter
from . import selectors

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

THREADS_HOME = "https://www.threads.com/"
THREADS_LOGIN = "https://www.threads.com/login"


class ThreadsSession:
    """ログイン状態を保ったブラウザ。"""

    def __init__(self, config: Config, *, headless: bool | None = None) -> None:
        self.config = config
        opts = config.autoreply_section("browser")
        self.profile_dir: Path = config.browser_profile_dir
        self.headless = opts.get("headless", True) if headless is None else headless
        self.nav_timeout_ms = int(opts.get("nav_timeout_ms", 60000))
        self.type_delay_range = (
            int(opts.get("type_delay_ms_min", 40)),
            int(opts.get("type_delay_ms_max", 110)),
        )
        auto = config.autoreply
        self.dwell_range = (
            int(auto.get("dwell_ms_min", 1500)),
            int(auto.get("dwell_ms_max", 5000)),
        )
        # ページ遷移の最短間隔。人が読む速さを下回らないようにする。
        interval = float(auto.get("min_interval_seconds", 8.0))
        self.rate_limiter = RateLimiter(interval) if interval > 0 else None
        self._pw = None
        self._ctx: Any = None

    # ------------------------------------------------------------------
    def __enter__(self) -> ThreadsSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> ThreadsSession:
        # ここでだけ playwright に触る。未インストールでも
        # src.engage.* の import は通る。
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 1600},
        )
        self._ctx.set_default_timeout(self.nav_timeout_ms)
        return self

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None
        if self._pw is not None:
            try:
                self._pw.stop()
            finally:
                self._pw = None

    # ------------------------------------------------------------------
    @property
    def page(self) -> Any:
        if self._ctx is None:
            raise RuntimeError("start() を呼んでいません")
        return self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def goto(self, url: str, *, dwell: bool = True) -> Any:
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        if dwell:
            self.dwell(page)
        return page

    def dwell(self, page: Any) -> None:
        """読んでいる時間のふり。一定間隔で動くと機械に見える。"""
        page.wait_for_timeout(random.randint(*self.dwell_range))

    def type_delay(self) -> int:
        """1文字ごとの待ち。貼り付けは機械に見える。"""
        return random.randint(*self.type_delay_range)

    # ------------------------------------------------------------------
    def is_logged_in(self, page: Any | None = None) -> bool:
        """ログインしているか。

        ログイン画面が出ていない **かつ** ログイン済みでしか出ない要素がある、
        の両方で見る。片方だけだと、読み込み途中を誤判定する。
        """
        page = page or self.page
        wall = selectors.get("login_wall")
        for css in wall.candidates():
            if page.query_selector(css) is not None:
                return False

        marker = selectors.get("logged_in_marker")
        return any(page.query_selector(css) is not None for css in marker.candidates())

    def require_login(self) -> Any:
        """ログイン済みのページを返す。未ログインなら止める。"""
        page = self.goto(THREADS_HOME)
        if not self.is_logged_in(page):
            raise AuthError(
                "Threads にログインしていません。次を実行してください:\n"
                "    python3 -m src.main autoreply --login"
            )
        return page

    # ------------------------------------------------------------------
    def login_interactively(self, *, wait_seconds: int = 300) -> bool:
        """人が手でログインするのを待つ。**認証情報はコードで扱わない。**"""
        page = self.page
        page.goto(THREADS_LOGIN, wait_until="domcontentloaded",
                  timeout=self.nav_timeout_ms)

        print("\nブラウザで Threads にログインしてください（2段階認証も含めて手で）。")
        print(f"最長 {wait_seconds} 秒待ちます…\n")

        waited = 0
        while waited < wait_seconds:
            page.wait_for_timeout(3000)
            waited += 3
            try:
                if self.is_logged_in(page):
                    logger.info("ログインを確認しました")
                    return True
            except Exception:  # noqa: BLE001 — 遷移中は読めないことがある
                continue
        return False
