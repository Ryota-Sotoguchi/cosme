"""判定工程の検査。

いちばん大事なのは **判断できなかったときに通さないこと**。
返信は他人の投稿に残り、後から直せない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.config import load_config
from src.engage.judge import ReplyJudge, TargetJudge, Verdict
from src.engage.llm import LlmResponse
from src.errors import TransientError


@dataclass
class FakeCandidate:
    username: str = "someone"
    shortcode: str = "ABC123"
    text: str = "無印の化粧水しか使ってないけど、そろそろ何か足したほうがいいのかな"
    likes: int = 120
    replies: int = 8
    age_hours: float | None = 3.0


class FakeLlm:
    """応答を先に決めておくクライアント。subprocess を起こさない。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def ask(self, prompt: str, *, timeout_s: float | None = None) -> LlmResponse:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("想定より多く LLM が呼ばれました")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LlmResponse(text="", data=nxt)


@pytest.fixture
def config(tmp_path):
    return load_config(data_dir=tmp_path)


CANDIDATE = FakeCandidate()

GOOD_TARGET = {"relevance": 0.9, "opportunity": 0.85, "reach": 0.8,
               "risk": 0.05, "score": 0.88, "recommended": True, "reason": "コスメの相談"}

GOOD_REPLY = {"naturalness": 0.9, "context_match": 0.9, "engagement": 0.85,
              "spam_risk": 0.05, "ai_smell": 0.1, "specific": True,
              "score": 0.9, "publish": True, "problems": [], "rewrite": ""}


# ======================================================================
# 返信対象の判定
# ======================================================================
def test_accepts_a_relevant_low_risk_post(config):
    verdict = TargetJudge(config, FakeLlm([GOOD_TARGET])).judge(CANDIDATE)
    assert verdict.ok is True
    assert verdict.score == pytest.approx(0.88)


def test_rejects_when_the_model_says_not_recommended(config):
    verdict = TargetJudge(config, FakeLlm([
        {**GOOD_TARGET, "recommended": False, "reason": "コスメと関係ない"}
    ])).judge(CANDIDATE)
    assert verdict.ok is False
    assert "関係ない" in verdict.reason


def test_rejects_a_high_risk_post_even_with_a_high_score(config):
    """**炎上リスクは点の高さで帳消しにしない。**"""
    verdict = TargetJudge(config, FakeLlm([
        {**GOOD_TARGET, "score": 0.95, "risk": 0.8, "reason": "訃報への言及"}
    ])).judge(CANDIDATE)
    assert verdict.ok is False
    assert "リスク" in verdict.reason


def test_rejects_below_the_threshold(config):
    verdict = TargetJudge(config, FakeLlm([{**GOOD_TARGET, "score": 0.3}])).judge(CANDIDATE)
    assert verdict.ok is False


def test_target_judge_fails_closed_on_unparsable_output(config):
    """**判断できなかった = 返さない。**"""
    llm = FakeLlm([])
    llm._responses = [{}]
    verdict = TargetJudge(config, llm).judge(CANDIDATE)
    assert verdict.ok is False
    assert "解釈できません" in verdict.reason


def test_target_judge_fails_closed_when_the_cli_errors(config):
    verdict = TargetJudge(config, FakeLlm([TransientError("落ちた")])).judge(CANDIDATE)
    assert verdict.ok is False
    assert verdict.score == 0.0


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "no", "0", "いいえ"])
def test_a_stringified_false_is_not_read_as_true(config, value):
    """**fail-closed を謳う場所で fail-open にしない。**

    LLM は JSON のつもりで "recommended": "false" を返すことがある。
    素の bool() は非空文字列を True にするので、判断が反転する。
    """
    verdict = TargetJudge(config, FakeLlm([
        {**GOOD_TARGET, "recommended": value}
    ])).judge(CANDIDATE)
    assert verdict.ok is False


@pytest.mark.parametrize("value", ["false", "no", "0"])
def test_a_stringified_false_specific_is_not_read_as_true(config, value):
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "specific": value}
    ])).judge(CANDIDATE, "わかります！")
    assert verdict.ok is False


@pytest.mark.parametrize("value", ["true", "yes", "1", True])
def test_a_stringified_true_still_passes(config, value):
    verdict = TargetJudge(config, FakeLlm([
        {**GOOD_TARGET, "recommended": value}
    ])).judge(CANDIDATE)
    assert verdict.ok is True


@pytest.mark.parametrize("score", ["high", "0.8点", None, [], {"a": 1}])
def test_target_judge_survives_a_malformed_score(config, score):
    """型が崩れても落ちない。落ちないうえで、通しもしない。"""
    verdict = TargetJudge(config, FakeLlm([
        {**GOOD_TARGET, "score": score, "recommended": True}
    ])).judge(CANDIDATE)
    assert isinstance(verdict, Verdict)
    if score == "0.8点":
        assert verdict.ok is True  # 数値として読めるものは読む
    else:
        assert verdict.ok is False


# ======================================================================
# 返信の審査
# ======================================================================
def test_accepts_a_good_reply(config):
    verdict = ReplyJudge(config, FakeLlm([GOOD_REPLY])).judge(CANDIDATE, "返信文")
    assert verdict.ok is True


def test_rejects_a_reply_that_could_be_written_without_reading_the_post(config):
    """**これがいちばん効く基準。**"""
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "specific": False, "score": 0.95}
    ])).judge(CANDIDATE, "わかります！")
    assert verdict.ok is False
    assert "読まなくても書ける" in verdict.reason


def test_rejects_a_reply_that_smells_of_ai(config):
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "ai_smell": 0.8}
    ])).judge(CANDIDATE, "返信文")
    assert verdict.ok is False
    assert "AI" in verdict.reason


def test_rejects_a_spammy_reply(config):
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "spam_risk": 0.7}
    ])).judge(CANDIDATE, "返信文")
    assert verdict.ok is False


def test_rejects_below_the_threshold_and_reports_the_problems(config):
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "score": 0.4, "publish": False,
         "problems": ["投稿の言い換えで終わっている"]}
    ])).judge(CANDIDATE, "返信文")
    assert verdict.ok is False
    assert "言い換え" in verdict.reason


def test_reply_judge_fails_closed_on_unparsable_output(config):
    llm = FakeLlm([])
    llm._responses = [{}]
    verdict = ReplyJudge(config, llm).judge(CANDIDATE, "返信文")
    assert verdict.ok is False


def test_reply_judge_fails_closed_when_the_cli_errors(config):
    verdict = ReplyJudge(config, FakeLlm([TransientError("落ちた")])).judge(CANDIDATE, "返信文")
    assert verdict.ok is False


def test_the_judge_sees_the_reply_text(config):
    llm = FakeLlm([GOOD_REPLY])
    ReplyJudge(config, llm).judge(CANDIDATE, "これが審査対象です")
    assert "これが審査対象です" in llm.prompts[0]


def test_verdict_summary_is_readable(config):
    verdict = TargetJudge(config, FakeLlm([GOOD_TARGET])).judge(CANDIDATE)
    assert "通過" in verdict.summary()
    assert "0.88" in verdict.summary()


# ======================================================================
# 感情と中身を合わせる
# ======================================================================
def test_a_reply_claiming_an_emotion_the_post_does_not_earn_is_rejected(config):
    """**無い感情を書くのが、いちばん «AIが書いた» と分かる。**

    2026-09-07 に実際に出た:
        投稿「Threads開くのが完全に習慣になってきている…笑」
        返信「「完全に習慣」ってワードに笑った…」
    相手は «あるある» を言っただけで、笑える中身は無い。
    """
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "emotion_fit": 0.15}
    ])).judge(CANDIDATE, "「完全に習慣」ってワードに笑った…")
    assert verdict.ok is False
    assert "感情" in verdict.reason


def test_a_reply_with_matching_emotion_passes(config):
    verdict = ReplyJudge(config, FakeLlm([
        {**GOOD_REPLY, "emotion_fit": 0.9}
    ])).judge(CANDIDATE, "返信文")
    assert verdict.ok is True


def test_a_missing_emotion_fit_does_not_block(config):
    """感情語を含まない淡々とした返信を、欠損で落とさない。"""
    data = {k: v for k, v in GOOD_REPLY.items()}
    verdict = ReplyJudge(config, FakeLlm([data])).judge(CANDIDATE, "返信文")
    assert verdict.ok is True
