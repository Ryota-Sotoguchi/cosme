"""返信生成の検査。

見るのは:
  * 機械の検査（無料・確定的）が LLM の審査より先に効いているか
  * 落ちた案を材料にして書き直しているか
  * **無限に書き直さないか**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.config import load_config
from src.engage.llm import LlmResponse
from src.engage.store import EngageStore
from src.engage.writer import ReplyWriter
from src.errors import TransientError


@dataclass
class FakeCandidate:
    username: str = "someone"
    shortcode: str = "ABC123"
    text: str = "無印の化粧水しか使ってないけど、そろそろ何か足したほうがいいのかな"
    likes: int = 120
    replies: int = 8
    age_hours: float | None = 3.0


CANDIDATE = FakeCandidate()

GOOD = "無印だけでも十分だと思います〜。足すなら日焼け止めからがいいって聞きました"
ANOTHER_GOOD = "朝はいつもぎりぎりなので、増やすほど続かなくなっちゃうんですよね〜"


class FakeLlm:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def ask(self, prompt: str, *, timeout_s: float | None = None) -> LlmResponse:
        self.prompts.append(prompt)
        if not self._replies:
            raise AssertionError("想定より多く LLM が呼ばれました")
        nxt = self._replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LlmResponse(text="", data={"reply": nxt, "touches": "無印"})


@pytest.fixture
def config(tmp_path):
    return load_config(data_dir=tmp_path)


@pytest.fixture
def store(tmp_path):
    with EngageStore(tmp_path / "engage.sqlite3") as s:
        yield s


# ======================================================================
# 成功
# ======================================================================
def test_returns_a_draft_on_the_first_good_attempt(config):
    draft = ReplyWriter(config, FakeLlm([GOOD])).write(CANDIDATE)
    assert draft is not None
    assert draft.text == GOOD
    assert draft.attempts == 1
    assert draft.touches == "無印"


def test_uses_the_shape_it_is_given(config):
    llm = FakeLlm([GOOD])
    draft = ReplyWriter(config, llm).write(CANDIDATE, shape="質問")
    assert draft.shape == "質問"
    assert "**質問**" in llm.prompts[0]


def test_rotates_the_shape_through_state(config):
    """**毎回同じ形になるのが、自動返信がAIに見える最大の原因。**"""
    class FakeState:
        def __init__(self):
            self.calls = []

        def next_rotation(self, key, options):
            self.calls.append(key)
            return options[len(self.calls) - 1]

    state = FakeState()
    writer = ReplyWriter(config, FakeLlm([GOOD, ANOTHER_GOOD]), state=state)
    first = writer.write(CANDIDATE)
    second = writer.write(CANDIDATE)
    assert first.shape != second.shape
    assert state.calls == ["autoreply_shape", "autoreply_shape"]


# ======================================================================
# 書き直し
# ======================================================================
def test_retries_past_a_template_only_reply(config):
    """定型句だけの返信は review() が落とす。LLM の審査を待たない。"""
    llm = FakeLlm(["わかります！", GOOD])
    draft = ReplyWriter(config, llm).write(CANDIDATE)
    assert draft.text == GOOD
    assert draft.attempts == 2
    assert "わかります！" in draft.rejected[0]


def test_retries_past_a_reply_containing_a_url(config):
    """楽天アフィリエイト規約でコメント欄へのリンクは禁止。"""
    llm = FakeLlm(["こちら参考になります https://example.com/x", GOOD])
    draft = ReplyWriter(config, llm).write(CANDIDATE)
    assert draft.text == GOOD


def test_retries_past_a_fabricated_usage_experience(config):
    """**このアカウントは商品を使っていない。** 機械が書く文では通さない。"""
    llm = FakeLlm(["わたしも使ってみたけど、朝の支度がすごく楽になりました〜", GOOD])
    draft = ReplyWriter(config, llm).write(CANDIDATE)
    assert draft.text == GOOD
    assert "NG表現" in draft.rejected[0]


def test_retries_past_an_efficacy_claim(config):
    llm = FakeLlm(["それ使うと乾燥が治るらしいですよ〜、肌荒れも改善するって", GOOD])
    draft = ReplyWriter(config, llm).write(CANDIDATE)
    assert draft.text == GOOD


def test_the_retry_prompt_carries_the_previous_failure(config):
    llm = FakeLlm(["わかります！", GOOD])
    ReplyWriter(config, llm).write(CANDIDATE)
    assert "わかります！" in llm.prompts[1]
    assert "前回の案は落ちました" in llm.prompts[1]


def test_retries_past_an_empty_reply(config):
    draft = ReplyWriter(config, FakeLlm(["", GOOD])).write(CANDIDATE)
    assert draft.text == GOOD


def test_a_transient_failure_does_not_end_the_attempt_loop(config):
    draft = ReplyWriter(config, FakeLlm([TransientError("落ちた"), GOOD])).write(CANDIDATE)
    assert draft.text == GOOD


# ======================================================================
# 打ち切り
# ======================================================================
def test_gives_up_after_the_configured_attempts(config):
    """**無限に書き直さない。** 1件の返信で CLI が何度も起動するのを防ぐ。"""
    attempts = int(config.autoreply_section("llm")["max_generation_attempts"])
    llm = FakeLlm(["わかります！"] * attempts)
    draft = ReplyWriter(config, llm).write(CANDIDATE)
    assert draft is None
    assert len(llm.prompts) == attempts


def test_gives_up_when_the_cli_never_answers(config):
    attempts = int(config.autoreply_section("llm")["max_generation_attempts"])
    llm = FakeLlm([TransientError("落ちた")] * attempts)
    assert ReplyWriter(config, llm).write(CANDIDATE) is None


# ======================================================================
# 重複回避
# ======================================================================
def test_rejects_a_reply_too_similar_to_a_recent_one(config, store):
    store.record_reply(shortcode="OLD", username="a", reply_text=GOOD)
    llm = FakeLlm([GOOD, ANOTHER_GOOD])
    draft = ReplyWriter(config, llm, store=store).write(CANDIDATE)
    assert draft.text == ANOTHER_GOOD
    assert "似すぎ" in draft.rejected[0]


def test_recent_replies_are_shown_to_the_model(config, store):
    store.record_reply(shortcode="OLD", username="a", reply_text=ANOTHER_GOOD)
    llm = FakeLlm([GOOD])
    ReplyWriter(config, llm, store=store).write(CANDIDATE)
    assert ANOTHER_GOOD in llm.prompts[0]


def test_works_without_a_store(config):
    """初回実行では履歴が無い。"""
    assert ReplyWriter(config, FakeLlm([GOOD]), store=None).write(CANDIDATE) is not None


def test_high_performing_replies_are_offered_as_examples(config, store):
    """成果ループを後付けしたとき、生成側が自動で使えること。"""
    reply_id = store.record_reply(shortcode="HIT", username="a", reply_text=ANOTHER_GOOD,
                                  our_reply_url="https://example.test/x")
    store.update_outcome(reply_id, likes=40, replies=6)

    llm = FakeLlm([GOOD])
    ReplyWriter(config, llm, store=store).write(CANDIDATE)
    assert "反応が良かった返信" in llm.prompts[0]
