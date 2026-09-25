"""返信の着弾確認。**押しただけで成功にしない。**

2026-09-24 の実測: 返信は3件とも実際に付いていたのに、2件が
«着弾を確認できませんでした» として記録された。返信は投稿の**下**に
描かれるので、再読み込みして待つだけでは画面に入らないことがある。

確認できないと `our_reply_url` が残らず、あとから成果（いいね・返信）を
測れない。**取り違えても再送はしない**（二重投稿を作らない）ので、
直すべきは «見つけ方» のほう。
"""

from __future__ import annotations

from src.engage.executor import BrowserReplyExecutor

REPLY = "みなし残業の時間、求人票だと見落としやすいところですよね"


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def wheel(self, dx: int, dy: int) -> None:
        self.page.scrolled += 1


class FakePage:
    """本文が «あとから» 見えるようになるページ。

    visible_after_reloads / visible_after_scrolls を満たすまで返信を見せない。
    """

    def __init__(self, *, after_reloads: int = 0, after_scrolls: int = 0,
                 body: str = "元の投稿") -> None:
        self.after_reloads = after_reloads
        self.after_scrolls = after_scrolls
        self.base = body
        self.reloads = 0
        self.scrolled = 0
        self.waits = 0
        self.mouse = FakeMouse(self)

    def inner_text(self, _selector: str) -> str:
        if self.reloads >= self.after_reloads and self.scrolled >= self.after_scrolls:
            return f"{self.base}\n{REPLY}"
        return self.base

    def reload(self, **_kwargs) -> None:
        self.reloads += 1

    def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1

    def query_selector_all(self, _css: str):
        return []


class FakeSession:
    def dwell(self, _page) -> None:
        pass


def executor(config) -> BrowserReplyExecutor:
    return BrowserReplyExecutor(config)


# ======================================================================
def test_confirms_when_the_reply_is_already_there(config):
    page = FakePage()
    confirmed, _url = executor(config)._confirm(FakeSession(), page, REPLY)
    assert confirmed
    assert page.reloads == 0, "すでに見えているのに再読み込みしている"


def test_reloads_once_before_giving_up(config):
    """送信直後はコンポーザが閉じただけで、まだ描かれていないことがある。"""
    page = FakePage(after_reloads=1)
    confirmed, _url = executor(config)._confirm(FakeSession(), page, REPLY)
    assert confirmed
    assert page.reloads == 1


def test_scrolls_down_to_find_the_reply(config):
    """**返信は投稿の下にある。** 2026-09-24 に2件を取り逃した経路。"""
    page = FakePage(after_reloads=1, after_scrolls=2)
    confirmed, _url = executor(config)._confirm(FakeSession(), page, REPLY)
    assert confirmed, "下まで送れば見つかるはずの返信を取り逃している"
    assert page.scrolled >= 2


def test_a_truncated_reply_still_counts(config):
    """「もっと見る」で省略されても、前半が一致すれば着弾とみなす。"""
    page = FakePage(body=f"元の投稿\n{REPLY[:18]}…")
    confirmed, _url = executor(config)._confirm(FakeSession(), page, REPLY)
    assert confirmed


def test_gives_up_without_resending(config):
    """見つからなくても例外にしない。**再送しない**（二重投稿を作らない）。"""
    page = FakePage(after_reloads=99)
    confirmed, url = executor(config)._confirm(FakeSession(), page, REPLY, timeout_s=0.1)
    assert not confirmed
    assert url == ""


def test_an_unreadable_page_does_not_crash(config):
    class Broken(FakePage):
        def inner_text(self, _selector: str) -> str:
            raise RuntimeError("読めません")

    confirmed, _url = executor(config)._confirm(FakeSession(), Broken(), REPLY, timeout_s=0.1)
    assert not confirmed
