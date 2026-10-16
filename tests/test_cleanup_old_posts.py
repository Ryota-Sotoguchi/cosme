"""過去投稿の削除（scripts/cleanup_old_posts.py）。**ブラウザは起動しない。**

見張ること:

1. 分類 … 商品由来は A、コスメの語は B、転職の語や判定できないものは C（削除しない）
2. 切り替え後の投稿・日時不明の投稿は消さない
3. 既定は Dry Run。実削除は DELETE_OLD_THREADS_POSTS=true と --execute の両方が要る
4. 消すのは候補ファイルに載ったものだけ。消したものは飛ばす。上限を守る
5. 自動運用（ワークフロー）に組み込まれていない
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SWITCH = datetime.fromisoformat("2026-09-15T12:00:00+09:00")


@pytest.fixture(scope="module")
def cleanup():
    spec = importlib.util.spec_from_file_location("cleanup_old_posts",
                                                  ROOT / "scripts" / "cleanup_old_posts.py")
    module = importlib.util.module_from_spec(spec)
    # dataclass が自分のモジュールを sys.modules から引くので、先に登録する
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _post(cleanup, text, *, code="Abc123", posted_at="2026-09-01T10:00:00+09:00", **kwargs):
    return cleanup.Post(shortcode=code, url=f"https://www.threads.com/@me/post/{code}",
                        posted_at=posted_at, text=text, **kwargs)


# ======================================================================
# 1. 分類
# ======================================================================
def test_product_posts_are_certainly_cosme(cleanup):
    assert cleanup.classify(_post(cleanup, "なんでも", post_type="product"))[0] == "A"
    assert cleanup.classify(_post(cleanup, "なんでも", post_type="longform"))[0] == "A"
    assert cleanup.classify(_post(cleanup, "なんでも", has_affiliate_link=True))[0] == "A"
    assert cleanup.classify(_post(cleanup, "資生堂、つめかえ用あるんだ", from_product_name=True))[0] == "A"
    assert cleanup.classify(_post(cleanup, "これ\n[affiliate-url]\n\n#PR"))[0] == "A"


def test_cosme_words_make_it_a_cosme_post(cleanup):
    cls, reason = cleanup.classify(_post(cleanup, "ポーチの中、いくつくらい入ってる？"))
    assert cls == "B" and "ポーチ" in reason


def test_career_words_are_never_deleted_automatically(cleanup):
    """コスメの語が混ざっていても、転職・仕事の語があれば C（削除しない）。"""
    post = _post(cleanup, "面接の前に、髪だけは整えていく")
    assert cleanup.classify(post)[0] == "C"


def test_posts_that_cannot_be_judged_are_ambiguous(cleanup):
    assert cleanup.classify(_post(cleanup, "眠い"))[0] == "C"
    assert cleanup.classify(_post(cleanup, "カートに入れたまま一晩おくと、半分くらい消えます。"))[0] == "C"


def test_the_new_genre_pools_are_never_classified_as_cosme(cleanup):
    """いまの投稿プールを誤って消さないこと（切り替え後の投稿は時刻でも外すが、二重に守る）。"""
    from src.content import parts as P

    for pool in (P.CASUAL_MURMURS, P.QUESTION_POSTS, P.NO_LINK_TOPICS, P.HOWTO_POSTS,
                 P.THREAD_TOPICS, P.SITE_LINK_POSTS):
        for part in pool:
            cls, reason = cleanup.classify(_post(cleanup, part.text))
            assert cls == "C", f"{part.id} が {cls} に分類された（{reason}）\n  {part.text}"


# ======================================================================
# 2. 候補ファイル
# ======================================================================
def test_ambiguous_posts_are_not_targets_by_default(cleanup):
    posts = [_post(cleanup, "ポーチの中", code="b1"), _post(cleanup, "眠い", code="c1"),
             _post(cleanup, "なんでも", code="a1", post_type="product")]
    data = cleanup.build_candidates(posts, before=SWITCH)
    assert {e["shortcode"] for e in data["targets"]} == {"a1", "b1"}
    assert [e["shortcode"] for e in data["ambiguous"]] == ["c1"]
    assert data["counts"] == {"A": 1, "B": 1, "C": 1, "after_switch": 0}


def test_ambiguous_posts_are_targets_only_when_asked(cleanup):
    posts = [_post(cleanup, "眠い", code="c1")]
    data = cleanup.build_candidates(posts, before=SWITCH, include_ambiguous=True)
    assert [e["shortcode"] for e in data["targets"]] == ["c1"]


def test_posts_after_the_switch_are_never_candidates(cleanup):
    posts = [_post(cleanup, "ポーチの中", code="new", posted_at="2026-09-15T12:30:00+09:00"),
             _post(cleanup, "なんでも", code="new2", post_type="product",
                   posted_at="2026-09-16T08:00:00+09:00")]
    data = cleanup.build_candidates(posts, before=SWITCH, include_ambiguous=True)
    assert data["targets"] == [] and data["ambiguous"] == []
    assert data["counts"]["after_switch"] == 2


def test_posts_without_a_date_are_not_deleted(cleanup):
    """いつの投稿か分からないものは、切り替え後かもしれない。"""
    data = cleanup.build_candidates([_post(cleanup, "ポーチの中", posted_at="")], before=SWITCH)
    assert data["targets"] == []
    assert data["ambiguous"][0]["class"] == "C"


def test_history_reads_only_published_posts_with_a_permalink(cleanup, tmp_path):
    rows = [
        {"status": "success", "permalink": "https://www.threads.com/@me/post/OK1",
         "posted_at": "2026-09-01T10:00:00+09:00", "text": "ポーチ", "post_type": "casual",
         "has_affiliate_link": False, "extra": {"template_parts": {"casual_brand": "brand"}}},
        {"status": "failed", "permalink": None, "text": "x"},
        {"status": "skipped", "permalink": None, "text": "x"},
        {"status": "success", "permalink": None, "text": "x"},
    ]
    path = tmp_path / "history.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    posts = cleanup.load_history_posts(path)
    assert [p.shortcode for p in posts] == ["OK1"]
    assert posts[0].from_product_name is True
    assert cleanup.username_from(posts) == "me"


def test_profile_rows_keep_only_our_own_posts(cleanup):
    rows = [
        {"href": "/@me/post/P1", "text": "me\n2026/09/01\nポーチの中\n3\n1",
         "datetime": "2026-09-01T01:00:00.000Z"},
        {"href": "/@someone/post/P2", "text": "someone\n2026/09/01\nリポスト\n3",
         "datetime": "2026-09-01T01:00:00.000Z"},
    ]
    posts = cleanup.profile_posts(rows, "me")
    assert [p.shortcode for p in posts] == ["P1"]
    assert posts[0].posted_at.startswith("2026-09-01T10:00")
    merged = cleanup.merge_posts([_post(cleanup, "ポーチの中", code="P1", post_type="casual")], posts)
    assert len(merged) == 1 and merged[0].source == "both"


def test_thread_continuations_follow_their_first_post(cleanup):
    """連投の2本目以降は履歴に短縮IDが無い。1本目と同じ判定にして、まとめて消すか残す。"""
    root = _post(cleanup, "迷ってるので並べてみた🥺\n\n選ぶとき、だいたいここ見てる👀\n・容量あたりの単価\n・続けられる価格か",
                 code="ROOT", posted_at="2026-09-01T10:05:00+09:00", post_type="product")
    other = _post(cleanup, "選ぶとき、だいたいここ見てる👀\n・発送までの日数",
                  code="OTHER", posted_at="2026-09-03T10:05:00+09:00", post_type="howto")
    rows = [{"href": "/@me/post/SEG", "text": "me\n2026/09/01\n2 / 2\n選ぶとき、だいたいここ見てる👀\n・容量あたりの単価\n・続けられる価格か\n1",
             "datetime": "2026-09-01T01:03:00.000Z"}]
    merged = cleanup.merge_posts([root, other], cleanup.profile_posts(rows, "me"))
    segment = next(p for p in merged if p.shortcode == "SEG")
    assert segment.thread_of == "ROOT"
    cls, reason = cleanup.classify(segment)
    assert cls == "A" and "ROOT" in reason


def test_a_shared_heading_alone_does_not_make_a_thread(cleanup):
    """同じ見出しの別の投稿や、時刻の離れた投稿を1本目と取り違えないこと。"""
    root = _post(cleanup, "選ぶとき、だいたいここ見てる👀\n・容量あたりの単価", code="ROOT",
                 posted_at="2026-09-01T10:05:00+09:00", post_type="product")
    far = cleanup.Post(shortcode="FAR", url="", posted_at="2026-09-02T10:05:00+09:00",
                       text="選ぶとき、だいたいここ見てる👀\n・容量あたりの単価")
    near_but_different = cleanup.Post(shortcode="DIFF", url="", posted_at="2026-09-01T10:04:00+09:00",
                                      text="選ぶとき、だいたいここ見てる👀\n・ぜんぜん別の観点です")
    assert cleanup.find_thread_root(far, [root]) is None
    assert cleanup.find_thread_root(near_but_different, [root]) is None


def test_candidate_files_round_trip_and_the_latest_is_used(cleanup, tmp_path):
    data = cleanup.build_candidates([_post(cleanup, "ポーチ")], before=SWITCH)
    old = cleanup.save_candidates(data, tmp_path, today=datetime(2026, 9, 14))
    new = cleanup.save_candidates(data, tmp_path, today=datetime(2026, 9, 15))
    assert cleanup.latest_candidates(tmp_path) == new != old
    assert json.loads(new.read_text(encoding="utf-8"))["targets"][0]["shortcode"] == "Abc123"


# ======================================================================
# 3. 実削除の鍵
# ======================================================================
MEASURED = {"post_more_button": True, "delete_menu_item": True, "delete_confirm_button": False}


def test_without_execute_nothing_is_deleted(cleanup):
    env = {"DELETE_OLD_THREADS_POSTS": "true"}
    assert cleanup.execution_refusal(execute=False, env=env, measured=MEASURED)


@pytest.mark.parametrize("value", [None, "", "false", "False", "1", "yes"])
def test_without_the_env_flag_nothing_is_deleted(cleanup, value):
    env = {} if value is None else {"DELETE_OLD_THREADS_POSTS": value}
    assert cleanup.execution_refusal(execute=True, env=env, measured=MEASURED)


def test_both_keys_allow_execution(cleanup):
    env = {"DELETE_OLD_THREADS_POSTS": "true"}
    assert cleanup.execution_refusal(execute=True, env=env, measured=MEASURED) is None


def test_unmeasured_menu_selectors_refuse_execution(cleanup):
    env = {"DELETE_OLD_THREADS_POSTS": "true"}
    for key in ("post_more_button", "delete_menu_item"):
        measured = {**MEASURED, key: False}
        assert key in cleanup.execution_refusal(execute=True, env=env, measured=measured)


def test_the_delete_flag_is_off_by_default():
    """.env.example などに ON で置かれていないこと。"""
    for path in ROOT.glob(".env*"):
        if path.name == ".env":
            continue  # 本人のローカル設定。gitignore 済み
        text = path.read_text(encoding="utf-8")
        assert "DELETE_OLD_THREADS_POSTS=true" not in text, path


def test_the_selectors_used_for_deleting_exist(cleanup):
    from src.engage.browser import selectors

    for key in cleanup.DELETE_SELECTORS:
        assert selectors.get(key).where == selectors.WHERE_OWN_POST
    # 点検（--selfcheck）では探さない
    assert not set(cleanup.DELETE_SELECTORS) & set(selectors.keys_where(selectors.WHERE_FEED))
    assert not set(cleanup.DELETE_SELECTORS) & set(selectors.keys_where(selectors.WHERE_MODAL))


# ======================================================================
# 4. 何を消すか
# ======================================================================
def _candidates(cleanup, n=5, *, include_ambiguous=False):
    posts = [_post(cleanup, "ポーチの中", code=f"b{i}") for i in range(n)]
    posts.append(_post(cleanup, "眠い", code="c0"))
    return cleanup.build_candidates(posts, before=SWITCH, include_ambiguous=include_ambiguous)


def test_only_targets_in_the_file_are_planned(cleanup):
    data = _candidates(cleanup)
    plan = cleanup.plan_execution(data, deleted=set(), limit=20, confirm_measured=True)
    assert [e["shortcode"] for e in plan] == [f"b{i}" for i in range(5)]


def test_already_deleted_posts_are_skipped(cleanup, tmp_path):
    data = _candidates(cleanup)
    cleanup.append_deleted(tmp_path, data["targets"][0], "deleted")
    cleanup.append_deleted(tmp_path, data["targets"][1], "already_gone")
    deleted = cleanup.load_deleted(tmp_path)
    plan = cleanup.plan_execution(data, deleted=deleted, limit=20, confirm_measured=True)
    assert [e["shortcode"] for e in plan] == ["b2", "b3", "b4"]


def test_the_limit_is_capped(cleanup):
    data = _candidates(cleanup, n=80)
    assert len(cleanup.plan_execution(data, deleted=set(), limit=3, confirm_measured=True)) == 3
    assert len(cleanup.plan_execution(data, deleted=set(), limit=999,
                                      confirm_measured=True)) == cleanup.MAX_LIMIT


def test_one_at_a_time_until_the_confirm_dialog_is_measured(cleanup):
    data = _candidates(cleanup)
    assert len(cleanup.plan_execution(data, deleted=set(), limit=20, confirm_measured=False)) == 1


def test_ambiguous_entries_are_dropped_unless_the_file_allowed_them(cleanup):
    """ファイルを手で書き換えて C を targets に足しても、ファイルの設定が許していなければ消さない。"""
    data = _candidates(cleanup, n=0)
    data["targets"] = data["ambiguous"]
    assert cleanup.plan_execution(data, deleted=set(), limit=20, confirm_measured=True) == []


def test_the_dry_run_writes_a_file_and_never_opens_a_browser(cleanup, tmp_path, capsys):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({
        "status": "success", "permalink": "https://www.threads.com/@me/post/X1",
        "posted_at": "2026-09-01T10:00:00+09:00", "text": "ポーチの中", "post_type": "casual",
    }, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "cleanup"

    for module in list(sys.modules):
        if module.startswith("playwright"):
            del sys.modules[module]
    assert cleanup.main(["--history", str(history), "--out-dir", str(out)]) == 0

    assert not [m for m in sys.modules if m.startswith("playwright")]
    assert cleanup.latest_candidates(out) is not None
    assert not (out / cleanup.DELETED_LOG).exists()
    assert "何も消していません" in capsys.readouterr().out


def _history_with_old_name(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({
        "status": "success", "permalink": "https://www.threads.com/@cosme_memo_jp/post/X1",
        "posted_at": "2026-09-01T10:00:00+09:00", "text": "ポーチの中", "post_type": "casual",
    }, ensure_ascii=False), encoding="utf-8")
    return history


def test_the_current_username_wins_over_the_old_permalinks(cleanup, tmp_path):
    """2026-09-15 に @cosme_memo_jp → @career_powerup。履歴のパーマリンクは旧名のまま残る。"""
    posts = cleanup.load_history_posts(_history_with_old_name(tmp_path))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"threads_username": "career_powerup"}), encoding="utf-8")

    assert cleanup.current_username(None, state, posts) == "career_powerup"
    assert cleanup.current_username("@someone", state, posts) == "someone"
    assert cleanup.current_username(None, tmp_path / "missing.json", posts) == "cosme_memo_jp"


def test_the_dry_run_lists_urls_under_the_current_username(cleanup, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"threads_username": "career_powerup"}), encoding="utf-8")
    out = tmp_path / "cleanup"
    assert cleanup.main(["--history", str(_history_with_old_name(tmp_path)),
                         "--state", str(state), "--out-dir", str(out)]) == 0

    data = json.loads(cleanup.latest_candidates(out).read_text(encoding="utf-8"))
    assert [e["url"] for e in data["targets"]] == ["https://www.threads.com/@career_powerup/post/X1"]


def test_delete_opens_the_post_under_the_given_username(cleanup):
    """候補ファイルのURLが旧名でも、開くのは今の名前のURL。

    旧名のURLが開けず「すでに無い」と記録されると、その投稿は二度と消されない。
    """
    class Page:
        url = ""

        def wait_for_timeout(self, ms):
            pass

        def query_selector(self, css):
            return None

        def wait_for_selector(self, css, **kwargs):
            raise TimeoutError(css)

        def get_by_role(self, *args, **kwargs):
            raise RuntimeError("no role")

    class Session:
        def __init__(self):
            self.opened = []

        def goto(self, url):
            self.opened.append(url)
            return Page()

        def is_logged_in(self, page):
            return True

    session = Session()
    entry = {"shortcode": "X1", "url": "https://www.threads.com/@cosme_memo_jp/post/X1"}
    assert cleanup.delete_one(session, entry, "career_powerup") == "already_gone"
    assert session.opened == ["https://www.threads.com/@career_powerup/post/X1"]


def test_a_slow_page_is_not_mistaken_for_an_already_deleted_post(cleanup):
    """描画が遅れて後から出たパーマリンクは「ある」。数秒で見切って already_gone にしない。"""
    class SlowPage:
        def __init__(self):
            self.waited = []

        def wait_for_selector(self, css, *, timeout, state):
            self.waited.append((css, timeout))

    page = SlowPage()
    assert cleanup._post_is_present(page, "X1") is True
    assert page.waited[0][0] == 'a[href*="/post/X1"]' and page.waited[0][1] >= 10000


def test_execute_without_the_env_flag_stops_before_the_browser(cleanup, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DELETE_OLD_THREADS_POSTS", raising=False)
    assert cleanup.main(["--execute", "--out-dir", str(tmp_path)]) == 1
    assert "DELETE_OLD_THREADS_POSTS" in capsys.readouterr().out
    assert not [m for m in sys.modules if m.startswith("playwright")]


# ======================================================================
# 5. 自動運用に組み込まない
# ======================================================================
def test_no_workflow_runs_the_cleanup():
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "cleanup_old_posts" not in text, path
        assert "DELETE_OLD_THREADS_POSTS" not in text, path


def test_the_cleanup_output_is_not_committed():
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "data/engage/cleanup/candidates-20260915.json",
         "data/engage/cleanup/deleted.jsonl"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, "削除候補（自分の投稿の本文）が gitignore されていない"


# ======================================================================
# 削除の反映待ち（2026-10-13）
#
# 確認を押してから、開いているページは15秒以上そのまま投稿を表示していた。
# 開き直せば消えている。4秒待って1回だけ見ていたので、反映の遅い投稿を
# «削除できていない» と判定し、そこで運用全体が止まっていた。
# ======================================================================

class _DeletePage:
    """⋯ → 削除 → 確認 を素通りさせ、パーマリンクの有無だけを切り替える板。"""

    def __init__(self, present: bool):
        self.present = present

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, css, **kwargs):
        if not self.present:
            raise TimeoutError(css)

    def query_selector(self, css):
        return None

    def evaluate(self, *args, **kwargs):
        return None

    def screenshot(self, **kwargs):
        pass


class _DeleteSession:
    """何回目の開き直しかで «まだある / もう無い» を変える。"""

    def __init__(self, present_until: int):
        self.present_until = present_until
        self.opened: list[str] = []
        self.clicked: list[str] = []

    def goto(self, url):
        self.opened.append(url)
        # 1回目は «最初に開いて削除する» ぶん。以降が確認の開き直し
        return _DeletePage(present=len(self.opened) <= self.present_until)

    def is_logged_in(self, page):
        return True


def _patch_delete_steps(cleanup, monkeypatch, session):
    """⋯ とメニューと確認のクリックを «押せたことにする»（DOM は別のテストで見ている）。"""
    class Handle:
        def click(self):
            session.clicked.append("more")

    class Element:
        def click(self):
            session.clicked.append("item")

    monkeypatch.setattr(cleanup, "CARD_ELEMENT_JS", "x")
    monkeypatch.setattr(cleanup, "_resolve_in_page", lambda page, key: Element())
    monkeypatch.setattr(cleanup, "_throttled", lambda page: False)

    def fake_handle(js, args):
        class H:
            def as_element(self):
                return Handle()
        return H()

    monkeypatch.setattr(_DeletePage, "evaluate_handle", lambda self, js, args: fake_handle(js, args),
                        raising=False)


def test_a_slow_deletion_is_confirmed_on_a_later_reload(cleanup, monkeypatch):
    """1回目の開き直しでまだ見えても、消えるまで何回か開き直す。"""
    session = _DeleteSession(present_until=2)   # 最初の1回＋確認1回目は «まだある»
    _patch_delete_steps(cleanup, monkeypatch, session)

    entry = {"shortcode": "X1", "url": "https://www.threads.com/@career_powerup/post/X1"}
    assert cleanup.delete_one(session, entry, "career_powerup") == "deleted"
    assert len(session.opened) == 3, "消えるまで開き直していない"


def test_a_post_that_never_disappears_stops_the_run(cleanup, monkeypatch):
    """**消えたことにして先へ進まない。** 残った投稿が二度と消されなくなる。"""
    session = _DeleteSession(present_until=99)
    _patch_delete_steps(cleanup, monkeypatch, session)

    entry = {"shortcode": "X1", "url": "https://www.threads.com/@career_powerup/post/X1"}
    with pytest.raises(cleanup.DeletionUnverified, match="削除を確認できません"):
        cleanup.delete_one(session, entry, "career_powerup")
    assert len(session.opened) == 1 + cleanup.VERIFY_ATTEMPTS


def test_the_number_of_reloads_is_bounded(cleanup):
    """無限に待たない。間隔をあけて数回まで。"""
    assert 2 <= cleanup.VERIFY_ATTEMPTS <= 5
    assert cleanup.VERIFY_WAIT_MS >= 3000


def test_the_intermediate_checks_are_quick_but_the_last_one_waits(cleanup, monkeypatch):
    """消えている投稿で、毎回15秒を丸ごと待たない。

    実測（2026-10-16）では1件あたり約6分かかっていた。«まだある» と見切っても
    もう一度開き直すだけなので、途中の回を長く待つ意味がない。
    最後の回だけは長く待つ（描画の遅れを «消えた» と取り違えないため）。
    """
    waits: list[int] = []

    class Page(_DeletePage):
        def wait_for_selector(self, css, **kwargs):
            waits.append(kwargs.get("timeout"))
            if not self.present:
                raise TimeoutError(css)

    class Session(_DeleteSession):
        def goto(self, url):
            self.opened.append(url)
            return Page(present=len(self.opened) <= self.present_until)

    session = Session(present_until=1)   # 最初に開いた回だけ «ある»
    _patch_delete_steps(cleanup, monkeypatch, session)
    entry = {"shortcode": "X1", "url": "https://www.threads.com/@career_powerup/post/X1"}
    assert cleanup.delete_one(session, entry, "career_powerup") == "deleted"

    # 1回目は削除前の在庫確認（長い）、2回目は確認の1回目（短い）
    assert waits[0] >= 10000, "削除前の在庫確認を短くしてはいけない"
    assert waits[1] == cleanup.VERIFY_QUICK_MS


def test_the_last_check_still_waits_long_before_giving_up(cleanup, monkeypatch):
    waits: list[int] = []

    class Page(_DeletePage):
        def wait_for_selector(self, css, **kwargs):
            waits.append(kwargs.get("timeout"))

    class Session(_DeleteSession):
        def goto(self, url):
            self.opened.append(url)
            return Page(present=True)

    session = Session(present_until=99)
    _patch_delete_steps(cleanup, monkeypatch, session)
    entry = {"shortcode": "X1", "url": "https://www.threads.com/@career_powerup/post/X1"}
    with pytest.raises(cleanup.DeletionUnverified, match="削除を確認できません"):
        cleanup.delete_one(session, entry, "career_powerup")
    assert waits[-1] >= 10000, "最後の判定が短い。描画の遅れを «消えた» と取り違える"



# ======================================================================
# 1件の失敗で全体を止めない（2026-10-16）
#
# 実測では5件に1件くらい «確認できない» が出る。止めていたぶん1回で4件しか
# 進まず、残り170件が片付かなかった。記録しないので次の実行でもう一度見る
# （10/13 に失敗した投稿が 10/16 にふつうに消せた）。
# ======================================================================

def test_unverified_is_its_own_error_type(cleanup):
    """«確認できない» と «仕組みが壊れた» を取り違えない。"""
    assert issubclass(cleanup.DeletionUnverified, RuntimeError)
    assert cleanup.DeletionUnverified is not RuntimeError


def test_a_single_miss_does_not_stop_the_batch(cleanup, tmp_path, monkeypatch, capsys):
    plan = [{"shortcode": f"X{i}", "class": "B", "excerpt": f"本文{i}",
             "url": f"https://www.threads.com/@career_powerup/post/X{i}"} for i in range(4)]
    seen: list[str] = []

    def fake_delete(session, entry, username):
        seen.append(entry["shortcode"])
        if entry["shortcode"] == "X1":
            raise cleanup.DeletionUnverified("削除を確認できません（X1）。")
        return "deleted"

    _run_plan(cleanup, tmp_path, monkeypatch, plan, fake_delete)
    out = capsys.readouterr().out
    assert seen == ["X0", "X1", "X2", "X3"], "失敗したところで止まっている"
    assert "次の実行でもう一度見ます" in out
    assert "今回の処理: 3件" in out

    # 失敗したものは記録しない（次の実行で拾い直せる）
    logged = (tmp_path / cleanup.DELETED_LOG).read_text(encoding="utf-8")
    assert "X1" not in logged


def test_many_misses_in_a_row_stop_the_batch(cleanup, tmp_path, monkeypatch, capsys):
    """続けて出るなら、押し損ねではなく仕組みが変わっている。"""
    plan = [{"shortcode": f"X{i}", "class": "B", "excerpt": "本文",
             "url": f"https://www.threads.com/@career_powerup/post/X{i}"} for i in range(8)]
    seen: list[str] = []

    def always_miss(session, entry, username):
        seen.append(entry["shortcode"])
        raise cleanup.DeletionUnverified("削除を確認できません。")

    _run_plan(cleanup, tmp_path, monkeypatch, plan, always_miss)
    assert len(seen) == cleanup.MAX_CONSECUTIVE_MISSES
    assert "止めます" in capsys.readouterr().out


def _run_plan(cleanup, tmp_path, monkeypatch, plan, fake_delete):
    """実削除の本体（_execute 相当）を、ブラウザ無しで回す。"""
    import argparse

    monkeypatch.setattr(cleanup, "delete_one", fake_delete)
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)
    monkeypatch.setattr(cleanup.random, "randint", lambda a, b: 0)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def require_login(self):
            pass

    # **関数の中で import しているので、モジュール側を差し替える。**
    import src.engage.browser.session as browser_session

    monkeypatch.setattr(browser_session, "ThreadsSession", lambda *a, **kw: Session())

    class Lock:
        def close(self):
            pass

    args = argparse.Namespace(out_dir=tmp_path, headed=False)
    cleanup._execute_plan(None, plan, "career_powerup", args, Lock())
