"""LLM に判断させる工程。

## 判断できなかったときは返さない（fail-closed）

LLM の応答から JSON が取れないことはある。そのとき「たぶん大丈夫だろう」で
通すと、判断されていない返信が他人の投稿に残る。返信は後から直せない。
**空の応答は却下**として扱う。

## 書き手と審査は別プロセス

`claude -p` は毎回まっさらなコンテキストで起動するので、審査側は
書き手の意図も試行回数も知らない。自分の答案を採点する構図にならない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import TransientError
from . import prompts
from .llm import LlmClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    score: float = 0.0
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{'通過' if self.ok else '却下'} {self.score:.2f} / {self.reason}"


def _number(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    """数値を取り出す。型が崩れていても落ちない。

    LLM は score に "high" や "0.8点" を入れてくることがある。
    """
    value = data.get(key, default)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("点%"))
        except ValueError:
            return default
    return default


def _text(data: dict[str, Any], key: str, limit: int = 200) -> str:
    value = data.get(key)
    return str(value)[:limit] if value is not None else ""


def _flag(data: dict[str, Any], key: str, default: bool) -> bool:
    """真偽値を取り出す。**文字列の "false" を True にしない。**

    LLM は JSON のつもりで `"recommended": "false"` を返すことがある。
    素の bool() は非空文字列を True にするので、そのまま使うと
    「対象外」という判断が「対象」に反転する。fail-closed を謳う場所で
    fail-open になるので、明示的に読む。
    """
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "no", "0", "n", "いいえ", ""}:
            return False
        if normalized in {"true", "yes", "1", "y", "はい"}:
            return True
        return default
    return default


class TargetJudge:
    """この投稿に返信する価値があるか。"""

    def __init__(self, config: Config, llm: LlmClient) -> None:
        self.config = config
        self.llm = llm
        opts = config.autoreply_section("llm")
        self.threshold = float(opts.get("target_score_threshold", 0.7))

    def judge(self, candidate: Any) -> Verdict:
        try:
            response = self.llm.ask(prompts.target_prompt(candidate))
        except TransientError as exc:
            logger.warning("返信対象の判定に失敗しました（@%s）: %s", candidate.username, exc)
            return Verdict(False, reason=f"判定できませんでした: {str(exc)[:80]}")

        data = response.data
        if not data:
            return Verdict(False, reason="判定の応答を解釈できませんでした")

        score = _number(data, "score")
        risk = _number(data, "risk")
        recommended = _flag(data, "recommended", score >= self.threshold)
        reason = _text(data, "reason", 120)

        if not recommended:
            return Verdict(False, score, reason or "対象外と判断されました", data)
        if score < self.threshold:
            return Verdict(False, score, f"点が足りません（{score:.2f} < {self.threshold}）", data)
        if risk >= 0.5:
            return Verdict(False, score, f"リスクが高い（{risk:.2f}）: {reason}", data)
        return Verdict(True, score, reason, data)


class ReplyJudge:
    """書いた返信を出していいか。**落とす理由を探す側。**"""

    def __init__(self, config: Config, llm: LlmClient) -> None:
        self.config = config
        self.llm = llm
        opts = config.autoreply_section("llm")
        self.threshold = float(opts.get("reply_score_threshold", 0.8))
        self.max_ai_smell = float(opts.get("max_ai_smell", 0.3))
        self.min_emotion_fit = float(opts.get("min_emotion_fit", 0.5))

    def judge(self, candidate: Any, reply_text: str) -> Verdict:
        try:
            response = self.llm.ask(prompts.judge_prompt(candidate, reply_text))
        except TransientError as exc:
            logger.warning("返信の審査に失敗しました: %s", exc)
            return Verdict(False, reason=f"審査できませんでした: {str(exc)[:80]}")

        data = response.data
        if not data:
            return Verdict(False, reason="審査の応答を解釈できませんでした")

        score = _number(data, "score")
        ai_smell = _number(data, "ai_smell")
        spam_risk = _number(data, "spam_risk")
        specific = _flag(data, "specific", False)
        publish = _flag(data, "publish", score >= self.threshold)
        problems = data.get("problems") or []
        problem_text = "、".join(str(p) for p in problems)[:160]

        # **投稿を読まずに書ける返信は出さない。** これがいちばん効く基準。
        if not specific:
            return Verdict(False, score, "投稿を読まなくても書ける内容です", data)

        # **無い感情を書かない。**
        # 2026-09-07: 「Threads開くのが完全に習慣になってきている…笑」に対して
        # 「「完全に習慣」ってワードに笑った…」と返した。笑える中身が無いのに
        # 笑ったと書くのが、いちばん «AIが書いた» と分かる。
        emotion_fit = _number(data, "emotion_fit", 1.0)
        if emotion_fit < self.min_emotion_fit:
            return Verdict(False, score,
                           f"書かれている感情が中身と合っていません（{emotion_fit:.2f}）",
                           data)
        if ai_smell > self.max_ai_smell:
            return Verdict(False, score,
                           f"AIらしさが強い（{ai_smell:.2f} > {self.max_ai_smell}）", data)
        if spam_risk >= 0.5:
            return Verdict(False, score, f"宣伝臭・定型句（{spam_risk:.2f}）", data)
        if not publish or score < self.threshold:
            return Verdict(False, score,
                           problem_text or f"点が足りません（{score:.2f} < {self.threshold}）",
                           data)
        return Verdict(True, score, problem_text, data)
