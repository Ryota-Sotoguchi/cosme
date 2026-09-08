"""返信を実際に投稿する。

## 流れ

    パーマリンクを開く → **その場で再確認** → 返信ボタン → 入力 →
    見直し → 送信 → 着弾確認

再確認と入力の間にページ遷移を挟まない。挟むとそこが誤爆の窓になる。

## クリックしただけで成功にしない

送信ボタンを押しても、Threads 側で弾かれることはある（レート制限・
スパム判定・ネットワーク）。**返信がページに現れたことを見て**初めて
成功とする。

## 失敗してもリトライしない

「送信されたか分からない」状態でもう一度送るのが、二重投稿の作り方その
ものになる。確認できなければ confirmed=0 で記録して、そこで終える。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..errors import SelectorMissError
from .browser import actions
from .verify import PrePostVerifier, normalize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecuteResult:
    ok: bool
    reason: str = ""
    our_reply_url: str = ""
    confirmed: bool = False
    """返信がページに現れたことを確認できたか。"""


class BrowserReplyExecutor:
    def __init__(self, config: Config, verifier: PrePostVerifier | None = None) -> None:
        self.config = config
        self.verifier = verifier or PrePostVerifier(config)

    # ------------------------------------------------------------------
    def post(
        self,
        session: Any,
        candidate: Any,
        text: str,
        *,
        dry_run: bool = True,
        own_username: str = "",
    ) -> ExecuteResult:
        """返信する。DRY_RUN なら再確認まではやって、送信だけしない。"""
        page = session.goto(candidate.permalink)

        # **開いた直後にその場で確かめる。** ここから送信までページを移らない。
        verdict = self.verifier.verify(page, candidate, own_username=own_username)
        if not verdict.ok:
            logger.info("返信を中止します（@%s）: %s", candidate.username, verdict.reason)
            return ExecuteResult(False, verdict.reason)

        if dry_run:
            logger.info("DRY_RUN: @%s に返信する予定 / %s", candidate.username, text)
            return ExecuteResult(True, "dry_run")

        try:
            self._open_composer(session, page)
            self._write(session, page, text)
        except SelectorMissError as exc:
            return ExecuteResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            return ExecuteResult(False, f"入力できませんでした: {type(exc).__name__}: {exc}")

        # 送信の直前にもう一度、入力欄の中身が意図した文か見る。
        typed = self._read_composer(page)
        if typed and normalize(typed) != normalize(text):
            return ExecuteResult(
                False, f"入力欄の中身が違います（{typed[:40]}…）")

        try:
            self._submit(page)
        except Exception as exc:  # noqa: BLE001
            return ExecuteResult(False, f"送信できませんでした: {type(exc).__name__}: {exc}")

        confirmed, url = self._confirm(session, page, text)
        if not confirmed:
            # **リトライしない。** 送れたかどうか分からない状態で
            # もう一度送るのが二重投稿の作り方そのもの。
            logger.warning("返信の着弾を確認できませんでした（@%s）", candidate.username)
            return ExecuteResult(True, "着弾を確認できませんでした", confirmed=False)

        logger.info("返信しました（@%s）", candidate.username)
        return ExecuteResult(True, our_reply_url=url, confirmed=True)

    # ------------------------------------------------------------------
    def _open_composer(self, session: Any, page: Any) -> None:
        button = actions.resolve(page, "reply_button", required=True)
        button.click()
        session.dwell(page)

    def _write(self, session: Any, page: Any, text: str) -> None:
        composer = actions.resolve(page, "reply_composer", required=True)
        # fill() は使わない。contenteditable では反映されないことがあり、
        # 一瞬で全文が入るのは機械の signal でもある。
        actions.type_like_a_person(composer, text, delay_ms=session.type_delay())
        session.dwell(page)

    def _read_composer(self, page: Any) -> str:
        composer = actions.resolve(page, "reply_composer", required=False)
        if composer is None:
            return ""
        try:
            return (composer.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _submit(self, page: Any) -> None:
        actions.resolve(page, "reply_submit", required=True).click()

    def _confirm(self, session: Any, page: Any, text: str,
                 *, timeout_s: float = 60.0) -> tuple[bool, str]:
        """返信がページに現れたか見る。**クリックしただけで成功にしない。**

        **現れるまで待つ。** 固定待ちだと、実際には投稿できているのに
        «確認できなかった» になる（2026-09-07 に発生。返信は実在したのに
        confirmed=0 で記録された）。着弾を取り違えると、成果を測る先
        （our_reply_url）も残らない。
        """
        needle = normalize(text)[:30]
        if not needle:
            return False, ""

        session.dwell(page)
        deadline = time.monotonic() + timeout_s
        reloaded = False
        while time.monotonic() < deadline:
            try:
                body = normalize(page.inner_text("body"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("ページを読めません: %s", exc)
                body = ""

            if needle in body:
                return True, self._find_our_reply_url(page, text)

            if not reloaded:
                # 送信直後はコンポーザが閉じただけで、まだ描画されていないことがある。
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("再読み込みできません: %s", exc)
                reloaded = True
            page.wait_for_timeout(5000)

        logger.warning("返信の着弾を %.0f 秒待ちましたが確認できませんでした", timeout_s)
        return False, ""

    def _find_our_reply_url(self, page: Any, text: str) -> str:
        """自分の返信の permalink を拾う。成果の測定であとで開く先。

        パーマリンクの a 自体はたいてい時刻表示を包んでいるだけなので、
        **リンクのテキストと本文を突き合わせても当たらない。**
        リンクを含む投稿カードまで遡って、そこに本文があるかで見る。

        取れなくても失敗にしない。成果ループが1件諦めるだけ。
        なお、この経路はセレクタの実測が済むまで空を返しうる。
        """
        needle = normalize(text)[:20]
        if not needle:
            return ""
        try:
            links = actions.resolve_all(page, "post_permalink")
        except Exception:  # noqa: BLE001
            return ""

        for link in links:
            try:
                href = (link.get_attribute("href") or "").split("?")[0]
                if not href:
                    continue
                # リンクを含む投稿カードの中に、いま書いた本文があるか。
                card = link.evaluate_handle(
                    "el => el.closest('article') || el.parentElement")
                if card is None:
                    continue
                body = normalize(card.as_element().inner_text() or "")
                if needle in body:
                    return f"https://www.threads.com{href}"
            except Exception:  # noqa: BLE001 — 1つ当たらなくても次を見る
                continue
        return ""
