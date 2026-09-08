"""投稿直前の再確認の検査。**誤爆防止がここに集約されている。**

返信は他人の投稿に残り、後から直せない。
1つでも合わなければ返さない、を条件ごとに固定する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from src.config import load_config
from src.engage.buzz import JST
from src.engage.candidates import Candidate
from src.engage.verify import PrePostVerifier, normalize

BODY = "無印の化粧水しか使ってないけど、そろそろ何か足したほうがいいのかな"
USERNAME = "someone"
SHORTCODE = "ABC123"


@dataclass
class FakeElement:
    text: str = ""
    attributes: dict = field(default_factory=dict)

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str):
        return self.attributes.get(name)


@dataclass
class FakePage:
    """query_selector と url だけ持つ最小のページ。"""

    url: str = f"https://www.threads.com/@{USERNAME}/post/{SHORTCODE}"
    body: str = BODY
    raw_card: bool = False
    """True にすると、実際の投稿カードと同じ「著者名 / 時刻 / 本文 / 反応数」を返す。"""
    posted_at: str | None = None
    deleted: bool = False
    replies_disabled: bool = False
    has_reply_button: bool = True

    def query_selector(self, css: str):
        if "利用できません" in css:
            return FakeElement("この投稿は利用できません") if self.deleted else None
        if "返信できません" in css:
            return FakeElement() if self.replies_disabled else None
        # 2026-09-04 実測: 投稿カードは data-pressable-container。
        # 本文だけを指すセレクタは無いので、カードの innerText を読む。
        if "pressable-container" in css:
            if self.raw_card:
                return FakeElement(f"{USERNAME}\n6時間\n{self.body}\n49\n35")
            return FakeElement(self.body)
        if css == "time[datetime]":
            return FakeElement(attributes={"datetime": self.posted_at}) if self.posted_at else None
        if "コメントする" in css:
            return self._reply_icon(css)
        return None

    def query_selector_all(self, css: str):
        return []

    def get_by_role(self, role: str, name: str = ""):
        page = self

        class Locator:
            def count(self):
                return 1 if (role == "button" and page.has_reply_button) else 0

            @property
            def first(self):
                return FakeElement()

        return Locator()

    def _reply_icon(self, css: str):
        """2026-09-04 実測: 返信ボタンは svg[aria-label="コメントする"]。"""
        return FakeElement() if self.has_reply_button else None


@pytest.fixture
def verifier(tmp_path):
    return PrePostVerifier(load_config(data_dir=tmp_path))


def candidate(**kwargs) -> Candidate:
    base = dict(username=USERNAME, shortcode=SHORTCODE, text=BODY,
                likes=100, replies=8, age_hours=3.0)
    base.update(kwargs)
    return Candidate(**base)


def hours_ago(n: float) -> str:
    return (datetime.now(JST) - timedelta(hours=n)).isoformat(timespec="seconds")


# ======================================================================
def test_normalize_ignores_whitespace_emoji_and_punctuation():
    """改行や絵文字の差だけで «別の投稿» と判定したくない。"""
    assert normalize("あ い\nう🥺！") == normalize("あいう")


def test_accepts_an_unchanged_post(verifier):
    assert verifier.verify(FakePage(), candidate()).ok is True


# ----------------------------------------------------------------------
# 1つでも合わなければ返さない
# ----------------------------------------------------------------------
def test_rejects_when_redirected_to_login(verifier):
    result = verifier.verify(FakePage(url="https://www.threads.com/login"), candidate())
    assert result.ok is False
    assert "別のページ" in result.reason


def test_rejects_when_the_url_is_a_different_post(verifier):
    page = FakePage(url=f"https://www.threads.com/@{USERNAME}/post/DIFFERENT")
    assert verifier.verify(page, candidate()).ok is False


def test_rejects_when_the_url_is_a_different_author(verifier):
    page = FakePage(url=f"https://www.threads.com/@someone_else/post/{SHORTCODE}")
    assert verifier.verify(page, candidate()).ok is False


def test_accepts_despite_query_parameters(verifier):
    page = FakePage(url=f"https://www.threads.com/@{USERNAME}/post/{SHORTCODE}?xmt=abc")
    assert verifier.verify(page, candidate()).ok is True


def test_rejects_a_deleted_post(verifier):
    result = verifier.verify(FakePage(deleted=True), candidate())
    assert result.ok is False
    assert "削除" in result.reason


def test_rejects_when_replies_are_disabled(verifier):
    result = verifier.verify(FakePage(replies_disabled=True), candidate())
    assert result.ok is False
    assert "返信を受け付けて" in result.reason


def test_rejects_our_own_post(verifier):
    result = verifier.verify(FakePage(), candidate(), own_username=USERNAME)
    assert result.ok is False
    assert "自分の投稿" in result.reason


def test_rejects_an_edited_post(verifier):
    """**Threads は投稿後5分の編集を許す。**

    伸び始めの投稿はまさにその時間帯にいる。ここを見ないと、
    別の内容になった投稿に的外れな返信を残すことになる。
    """
    page = FakePage(body="やっぱり全部書き直しました。今日は天気の話をします")
    result = verifier.verify(page, candidate())
    assert result.ok is False
    assert "本文が変わって" in result.reason


def test_accepts_a_post_that_only_gained_an_appendix(verifier):
    """冒頭が残っていれば、返信の根拠は失われていない。

    「追記: 〜」はよくある形なので、これを弾くと候補の大半を捨てることになる。
    追記部分の危険は、このあとのセンシティブ語の再走査で拾う。
    """
    page = FakePage(body=BODY + "\n\n追記: コメントありがとうございます")
    assert verifier.verify(page, candidate()).ok is True


def test_rejects_a_post_that_was_truncated(verifier):
    """短くされた場合も «変わった» として扱う。"""
    page = FakePage(body="無印の化粧水")
    result = verifier.verify(page, candidate())
    assert result.ok is False
    assert "本文が変わって" in result.reason


def test_rejects_when_the_opening_was_rewritten(verifier):
    """冒頭だけ差し替えて、元の文を後ろに残す編集も捕まえる。"""
    page = FakePage(body="【宣伝】フォローお願いします\n" + BODY)
    assert verifier.verify(page, candidate()).ok is False


def test_rejects_when_the_full_text_reveals_a_sensitive_topic(verifier):
    """**順位付けに使ったのは省略表示だった可能性がある。**"""
    page = FakePage(body=BODY + " ちなみに整形のダウンタイム中です")
    result = verifier.verify(page, candidate())
    assert result.ok is False
    assert "センシティブ" in result.reason


def test_rejects_when_the_full_text_reveals_spam(verifier):
    page = FakePage(body=BODY + " 詳しくは公式LINEから")
    result = verifier.verify(page, candidate())
    assert result.ok is False
    assert "宣伝" in result.reason


def test_rejects_when_the_body_cannot_be_read(verifier):
    """読めないまま «たぶん大丈夫» で進まない。"""
    result = verifier.verify(FakePage(body=""), candidate())
    assert result.ok is False
    assert "本文を読めません" in result.reason


def test_rejects_when_the_reply_button_is_missing(verifier):
    result = verifier.verify(FakePage(has_reply_button=False), candidate())
    assert result.ok is False
    assert "返信ボタン" in result.reason


def test_rejects_a_post_that_has_aged_out(verifier):
    """候補にしてから時間が経っていることがある。"""
    result = verifier.verify(FakePage(posted_at=hours_ago(30)), candidate())
    assert result.ok is False
    assert "古くなりました" in result.reason


def test_accepts_a_post_still_within_the_window(verifier):
    result = verifier.verify(FakePage(posted_at=hours_ago(2)), candidate())
    assert result.ok is True
    assert result.fresh_age_hours == pytest.approx(2.0, abs=0.1)


def test_a_missing_timestamp_does_not_reject_by_itself(verifier):
    """ここまで通ってきた候補を、時刻が読めないだけで捨てない。"""
    assert verifier.verify(FakePage(posted_at=None), candidate()).ok is True


def test_the_fresh_text_is_returned_for_the_record(verifier):
    page = FakePage(body=BODY + " 追記あり")
    assert "追記あり" in verifier.verify(page, candidate()).fresh_text


# ======================================================================
# 投稿カードの生テキストと突き合わせない
# ======================================================================
def test_the_body_is_parsed_the_same_way_as_the_candidate(verifier):
    """**候補と同じパーサを通すこと。**

    投稿カードの innerText は「著者名 / 時刻 / 本文 / 反応数」の形で、
    候補の text はそこから剥がした後の文字列。生のまま突き合わせると

        選定時 「Threads伸びないって…」
        現在   「ai.tem_studio 6時間 Thread…」

    となって構造上いつまでも一致しない。2026-09-07 に、審査を通った返信が
    これで投稿直前に止められた。
    """
    page = FakePage(raw_card=True)
    result = verifier.verify(page, candidate())
    assert result.ok is True, f"生テキストと比較している: {result.reason}"
    assert not result.fresh_text.startswith(USERNAME)
    assert result.fresh_text.startswith("無印")


def test_an_edited_post_is_still_caught_through_the_parser(verifier):
    """剥がしたうえで、本当に書き換わっていれば止めること。"""
    page = FakePage(raw_card=True, body="やっぱり全部書き直しました。今日は天気の話")
    result = verifier.verify(page, candidate())
    assert result.ok is False
    assert "本文が変わって" in result.reason
