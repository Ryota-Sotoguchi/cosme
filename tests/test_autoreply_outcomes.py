"""返信の成果を測る（`autoreply --outcomes`）。

`store.update_outcome` は前からあったのに**呼ぶ場所がどこにも無かった**
（2026-09-24 に判明）。そのため「反応が良かった返信を手本に回す」仕組みが
空のまま回っていた。ここが埋まって初めて、手本が自分の実績に入れ替わる。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.engage import outcomes
from src.engage.candidates import Candidate

OWN = "career_powerup"
REPLY = "みなし残業の時間、求人票だと見落としやすいところですよね"


@dataclass
class FakeRow:
    id: int = 1
    shortcode: str = "ABC"
    username: str = "someone"
    permalink: str = "https://www.threads.com/@someone/post/ABC"
    reply_text: str = REPLY
    our_reply_url: str = ""


def candidate(username: str, text: str, *, likes: int = 0, replies: int = 0) -> Candidate:
    return Candidate(username=username, shortcode="RPL", text=text, likes=likes, replies=replies)


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def wheel(self, _dx: int, dy: int) -> None:
        self.page.scrolled += 1


class FakePage:
    """スクロールした回数ぶんだけ、返信が «見える» ようになるページ。"""

    def __init__(self, rows_by_scroll: dict[int, list[Candidate]]) -> None:
        self.rows_by_scroll = rows_by_scroll
        self.scrolled = 0
        self.mouse = FakeMouse(self)

    def evaluate(self, _js: str, _limit: int | None = None):
        return self.rows_by_scroll.get(self.scrolled, [])

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def query_selector(self, _css: str):
        return object()


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.opened: list[str] = []

    def goto(self, url: str) -> FakePage:
        self.opened.append(url)
        return self.page


class FakeStore:
    def __init__(self, rows: list[FakeRow]) -> None:
        self.rows = rows
        self.outcomes: list[tuple[int, int, int]] = []
        self.urls: list[tuple[int, str]] = []

    def pending_outcomes(self, *, older_than_hours: int = 24) -> list[FakeRow]:
        return list(self.rows)

    def update_outcome(self, reply_id: int, *, likes: int, replies: int) -> None:
        self.outcomes.append((reply_id, likes, replies))

    def set_reply_url(self, reply_id: int, url: str) -> None:
        self.urls.append((reply_id, url))


def patch_parse(monkeypatch) -> None:
    """DOM の解釈は extract 側のテストに任せ、ここでは行の中身だけ見る。"""
    monkeypatch.setattr(outcomes, "parse_rows", lambda rows, **_kw: rows)
    monkeypatch.setattr(outcomes.actions, "wait_for_posts", lambda page, **_kw: True)


# ======================================================================
def test_reads_the_reaction_on_our_own_reply(monkeypatch):
    patch_parse(monkeypatch)
    page = FakePage({0: [
        candidate("someone", "元の投稿", likes=100),
        candidate(OWN, REPLY, likes=7, replies=2),
    ]})
    store = FakeStore([FakeRow()])
    done = outcomes.measure(FakeSession(page), store, own_username=OWN)

    assert [(o.likes, o.replies) for o in done] == [(7, 2)]
    assert store.outcomes == [(1, 7, 2)]


def test_ignores_other_peoples_replies(monkeypatch):
    """同じことを書いた別人を自分の返信と取り違えない。"""
    patch_parse(monkeypatch)
    page = FakePage({0: [candidate("someone_else", REPLY, likes=50)]})
    store = FakeStore([FakeRow()])
    assert outcomes.measure(FakeSession(page), store, own_username=OWN) == []
    assert store.outcomes == []


def test_scrolls_until_the_reply_is_rendered(monkeypatch):
    """返信は投稿の下にある。最初の画面に無くても諦めない。"""
    patch_parse(monkeypatch)
    page = FakePage({
        0: [candidate("someone", "元の投稿")],
        2: [candidate(OWN, REPLY, likes=3)],
    })
    store = FakeStore([FakeRow()])
    done = outcomes.measure(FakeSession(page), store, own_username=OWN)
    assert [o.likes for o in done] == [3]
    assert page.scrolled >= 2


def test_records_nothing_when_the_reply_is_gone(monkeypatch):
    """**0件と書かない。** «反応が無かった» と «見つからない» は別物。"""
    patch_parse(monkeypatch)
    page = FakePage({0: [candidate("someone", "元の投稿")]})
    store = FakeStore([FakeRow()])
    assert outcomes.measure(FakeSession(page), store, own_username=OWN) == []
    assert store.outcomes == []


def test_fills_in_the_reply_url_when_it_was_missing(monkeypatch):
    """着弾確認に失敗した返信でも、ここで permalink を埋められる。"""
    patch_parse(monkeypatch)
    page = FakePage({0: [candidate(OWN, REPLY, likes=1)]})
    store = FakeStore([FakeRow(our_reply_url="")])
    outcomes.measure(FakeSession(page), store, own_username=OWN)
    assert store.urls == [(1, f"https://www.threads.com/@{OWN}/post/RPL")]


def test_one_broken_page_does_not_stop_the_rest(monkeypatch):
    patch_parse(monkeypatch)

    class Flaky(FakeSession):
        def goto(self, url: str):
            self.opened.append(url)
            if len(self.opened) == 1:
                raise RuntimeError("開けません")
            return self.page

    page = FakePage({0: [candidate(OWN, REPLY, likes=5)]})
    store = FakeStore([FakeRow(id=1), FakeRow(id=2)])
    done = outcomes.measure(Flaky(page), store, own_username=OWN)
    assert [o.reply_id for o in done] == [2]


def test_limit_caps_how_many_posts_are_opened(monkeypatch):
    """1回の実行で開きすぎない。開く回数そのものがリスク。"""
    patch_parse(monkeypatch)
    page = FakePage({0: [candidate(OWN, REPLY)]})
    session = FakeSession(page)
    store = FakeStore([FakeRow(id=i) for i in range(1, 6)])
    outcomes.measure(session, store, own_username=OWN, limit=2)
    assert len(session.opened) == 2
