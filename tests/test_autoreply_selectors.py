"""セレクタレジストリの規律を機械で守らせる。

Threads の DOM は数週間で変わる。人間の注意力ではなくテストで縛る。
"""

from __future__ import annotations

import re

import pytest

from src.engage.browser import selectors
from src.errors import SelectorMissError


def test_registry_is_not_empty():
    assert len(selectors.all_keys()) >= 15


@pytest.mark.parametrize("key", selectors.all_keys())
def test_every_selector_explains_itself(key):
    """**note が空のエントリは許さない。**

    なぜその形にしたかが残っていないと、壊れたときに直せない。
    """
    assert selectors.get(key).note.strip()


@pytest.mark.parametrize("key", selectors.all_keys())
def test_no_selector_depends_on_an_obfuscated_class(key):
    """`.x1i10hfl` のような難読化 class に依存しない。

    Meta のビルドごとに変わるので、依存した瞬間に寿命が数週間になる。
    """
    spec = selectors.get(key)
    for css in spec.candidates():
        assert not selectors.OBFUSCATED_CLASS.search(css), f"{key}: {css}"


@pytest.mark.parametrize("key", selectors.all_keys())
def test_every_selector_has_a_way_to_be_found(key):
    """css か role のどちらかは要る。"""
    spec = selectors.get(key)
    assert spec.candidates() or spec.role


def test_unknown_key_raises_with_the_known_ones_listed():
    with pytest.raises(SelectorMissError) as excinfo:
        selectors.get("does_not_exist")
    assert "does_not_exist" in str(excinfo.value)


def test_the_keys_needed_to_reply_are_marked_required():
    """引けなければ止めるべきもの。ここを間違うと誤爆する。"""
    required = set(selectors.required_keys())
    assert {"reply_composer", "reply_submit", "reply_button"} <= required


def test_reading_metrics_is_not_required():
    """いいね数が読めないのは順位が落ちるだけ。返信そのものは止めない。"""
    required = set(selectors.required_keys())
    assert "metric_repost" not in required
    assert "metric_share" not in required


def test_unmeasured_selectors_say_so_in_their_note():
    """**推測のまま本番で使わない。**

    未実測のキーは selfcheck が警告する。note にもそう書いておく。
    """
    for key in selectors.unmeasured():
        assert "要実測" in selectors.get(key).note, f"{key} の note に «要実測» が無い"


def test_measured_selectors_record_when_they_were_checked():
    """実測は日付とセットで残す。1年前の «実測済» は当てにならない。"""
    for key in selectors.all_keys():
        spec = selectors.get(key)
        if spec.measured:
            assert re.search(r"20\d\d-\d\d-\d\d", spec.note), f"{key} の note に実測日が無い"


def test_the_reply_icon_prefers_the_logged_in_wording():
    """**返信アイコンの文言はログイン状態で変わる。**

        ログアウト時  svg|コメントする（ログイン時は0個）
        ログイン時    svg|返信      （ログアウト時は0個）

    自動返信は必ずログインして動くので「返信」が第一候補。
    ログアウトで測り直して書き戻すと、本番で 0個 になる。
    """
    for key in ("reply_button", "metric_reply"):
        spec = selectors.get(key)
        assert spec.css == 'svg[aria-label="返信"]', key
        assert 'svg[aria-label="コメントする"]' in spec.fallbacks, key


def test_the_reply_composer_is_scoped_to_the_dialog():
    """**限定しないと自分の新規投稿を書いてしまう。**

    投稿詳細ページには、返信欄を開く前から新規投稿用の contenteditable が
    1個ある（2026-09-04 実測）。素の div[contenteditable] を掴むと、
    相手への返信ではなく自分の投稿として書き込むことになる。
    """
    spec = selectors.get("reply_composer")
    assert 'div[role="dialog"]' in spec.css
    for css in spec.candidates():
        assert css != 'div[contenteditable="true"]', "限定されていない候補がある"


def test_the_submit_button_requires_an_exact_name():
    """部分一致だと「投稿オプション」も拾う（2026-09-04 実測で13個）。"""
    spec = selectors.get("reply_submit")
    assert spec.role == ("button", "投稿")
    assert spec.exact is True


def test_the_post_container_is_not_article():
    """2026-09-04 実測: Threads は article を使っていない（0個）。"""
    assert selectors.get("post_container").css == 'div[data-pressable-container="true"]'


def test_the_home_icon_is_not_used_as_a_login_marker():
    """ログアウト状態でも出るので、これでログイン判定すると誤検知する。"""
    spec = selectors.get("logged_in_marker")
    for css in spec.candidates():
        assert "ホーム" not in css


def test_the_post_permalink_selector_matches_the_proven_one():
    """scripts/collect_candidates.py が実データで動作確認済みの形。

    ここを変えるときは、また実測してからにする。
    """
    assert selectors.get("post_permalink").css == 'a[href*="/post/"]'
    assert selectors.get("post_permalink").measured is True


def test_candidates_puts_the_primary_selector_first():
    spec = selectors.get("reply_composer")
    assert spec.candidates()[0] == spec.css
