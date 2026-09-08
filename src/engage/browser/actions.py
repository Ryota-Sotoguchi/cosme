"""ページ操作。**playwright を import しない**（`page` を受け取るだけ）。

## セレクタの解決

`selectors.py` の候補を順に試し、当たったものを使う。全滅したら:

    required=True   SelectorMissError で止める。**推測で続行しない**
    required=False  None を返して続行（いいね数が読めないだけなら止めない）

## 静かな失敗のほうが怖い

「見つからない」のは気づける。怖いのは位置だけ変わって別の数字を読み続ける
こと。順位付けだけが静かに劣化し、エラーは出ない。
`looks_like_silent_drift()` がそれを検知する。
"""

from __future__ import annotations

import logging
from typing import Any

from ...errors import SelectorMissError
from . import selectors

logger = logging.getLogger(__name__)


def resolve(page: Any, key: str, *, required: bool | None = None) -> Any | None:
    """1要素を引く。見つからなければ None（必須なら例外）。"""
    spec = selectors.get(key)
    must = spec.required if required is None else required
    tried: list[str] = []

    for css in spec.candidates():
        tried.append(css)
        try:
            found = page.query_selector(css)
        except Exception as exc:  # noqa: BLE001 — 壊れたセレクタで止めない
            logger.debug("セレクタが使えません（%s / %s）: %s", key, css, exc)
            continue
        if found is not None:
            return found

    if spec.role is not None:
        role, name = spec.role
        tried.append(f'role={role} name="{name}"')
        try:
            locator = page.get_by_role(role, name=name, exact=spec.exact)
            if locator.count() > 0:
                return locator.first
        except Exception as exc:  # noqa: BLE001
            logger.debug("role で引けません（%s）: %s", key, exc)

    if must:
        raise SelectorMissError(key, tried)
    return None


def resolve_all(page: Any, key: str) -> list[Any]:
    """同じセレクタに当たる要素を全部引く。"""
    spec = selectors.get(key)
    for css in spec.candidates():
        try:
            found = page.query_selector_all(css)
        except Exception:  # noqa: BLE001
            continue
        if found:
            return list(found)
    return []


def exists(page: Any, key: str) -> bool:
    """その要素があるか。**必須でも例外にしない。**

    「削除された投稿のマーカーがあるか」のような、無いのが正常な判定に使う。
    """
    return resolve(page, key, required=False) is not None


def selector_report(page: Any, *, where: str | None = None) -> dict[str, bool]:
    """キーが今日も引けるか。`autoreply --selfcheck` が使う。

    `where` を指定すると、その場所で探すべきキーだけ見る。返信欄は
    ダイアログを開くまで存在しないので、タイムラインで探しても意味がない。
    """
    keys = selectors.keys_where(where) if where else selectors.all_keys()
    report: dict[str, bool] = {}
    for key in keys:
        try:
            report[key] = resolve(page, key, required=False) is not None
        except Exception:  # noqa: BLE001
            report[key] = False
    return report


def open_reply_composer(page: Any) -> bool:
    """返信ダイアログを開く。**何も打たないし、投稿もしない。**

    点検で「返信欄が今日も出るか」を確かめるために使う。
    """
    button = resolve(page, "reply_button", required=False)
    if button is None:
        return False
    button.click()
    page.wait_for_timeout(4000)
    return True


def close_dialog(page: Any) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)


def looks_like_silent_drift(candidates: list[Any], *, threshold: float = 0.8) -> bool:
    """反応数がまるごと読めなくなっていないか。

    Threads がいいね数の位置を変えると、セレクタは «当たる» まま
    別の要素を掴み、0 を読み続ける。エラーは出ない。順位付けだけが
    静かに壊れるので、**割合で気づく**。
    """
    if len(candidates) < 5:
        return False  # 母数が少ないと、たまたま全部0のこともある
    zeros = sum(1 for c in candidates if not (c.likes or 0) and not (c.replies or 0))
    return zeros / len(candidates) > threshold


def wait_for_posts(page: Any, *, timeout_ms: int = 30000) -> bool:
    """投稿が現れるまで待つ。

    固定時間の待ちにすると収集数が実行ごとに大きくぶれる
    （実測で 12件 と 4件 になった）。Threads は描画が遅いので、
    «何秒か待つ» ではなく «出るまで待つ» にする。
    """
    spec = selectors.get("post_permalink")
    for css in spec.candidates():
        try:
            page.wait_for_selector(css, timeout=timeout_ms, state="attached")
        except Exception as exc:  # noqa: BLE001 — 次の候補を試す
            logger.debug("投稿を待てませんでした（%s）: %s", css, exc)
            continue
        return True
    logger.warning("投稿が1件も描画されませんでした")
    return False


def scroll(page: Any, times: int, *, pixels: int = 3000, pause_ms: int = 1500) -> None:
    """スクロールして追加読み込みを促す。

    件数が増えなくなったら早めに切り上げる。無限スクロールの尻を
    追い続けても、返信したい «伸び始め» の投稿は上にある。
    """
    previous = 0
    for _ in range(max(times, 0)):
        page.mouse.wheel(0, pixels)
        page.wait_for_timeout(pause_ms)
        try:
            current = len(page.query_selector_all(selectors.get("post_permalink").css))
        except Exception:  # noqa: BLE001
            continue
        if current == previous:
            break
        previous = current


def type_like_a_person(element: Any, text: str, *, delay_ms: int) -> None:
    """1文字ずつ打つ。

    **fill() は使わない。** 返信欄は contenteditable で、fill() だと値が
    反映されないことがある。加えて、一瞬で全文が入るのは機械の signal。
    """
    element.click()
    element.type(text, delay=delay_ms)
