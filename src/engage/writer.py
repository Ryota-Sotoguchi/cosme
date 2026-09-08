"""返信文を書く。

## 機械の検査を先に置く

LLM を呼ぶ前でも後でも、確定的で無料な検査は先に済ませる。

    1. review(include_experience=True)  URL・長さ・NG表現・自己宣伝・定型句
    2. similarity()                     直近の返信と似ていないか
    3. ReplyJudge                       ここで初めて LLM を使う

1 と 2 で落ちる案に LLM の審査を使わない。

## 型は呼び出し側が選ぶ

**返信が毎回同じ形になることが、自動返信がAIに見える最大の原因。**
`State.next_rotation()` で型を1つ選んで渡す。既存のローテーション機構を
そのまま使うので、状態の持ち方を新しく作らない。

## 無限に書き直さない

`max_generation_attempts` で打ち切る。1件の返信のために CLI が何度も
起動するのを防ぐ（`ComplianceChecker` の `max_regenerations` と同じ考え方）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..compliance.checker import similarity
from ..config import Config
from ..errors import TransientError
from . import prompts
from .llm import LlmClient
from .review import review

logger = logging.getLogger(__name__)


@dataclass
class Draft:
    """返信案ひとつ。"""

    text: str
    shape: str
    attempts: int = 1
    touches: str = ""
    rejected: list[str] = field(default_factory=list)
    """落ちた案とその理由。--explain で人が読む。"""


class ReplyWriter:
    def __init__(
        self,
        config: Config,
        llm: LlmClient,
        *,
        state: Any | None = None,
        store: Any | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.state = state
        self.store = store

        opts = config.autoreply_section("llm")
        self.max_attempts = max(int(opts.get("max_generation_attempts", 3)), 1)

        dedup = config.dedup if "dedup" in config.raw else {}
        self.max_similarity = float(dedup.get("max_similarity", 0.72))
        self.similarity_window = int(dedup.get("similarity_window", 140))

        self.last_call_count = 0
        """直前の write() で LLM を呼んだ回数。"""

    # ------------------------------------------------------------------
    def pick_shape(self) -> str:
        """今回の型を選ぶ。既存のローテーションカーソルを使う。"""
        if self.state is None:
            return prompts.REPLY_SHAPES[0]
        return self.state.next_rotation("autoreply_shape", list(prompts.REPLY_SHAPES))

    def _recent_texts(self) -> list[str]:
        if self.store is None:
            return []
        return self.store.recent_reply_texts(limit=self.similarity_window)

    def _examples(self) -> list[str]:
        """反応の良かった返信。まだ成果を取っていなければ空。"""
        if self.store is None:
            return []
        try:
            return [row.reply_text for row in self.store.best_replies(limit=3)]
        except AttributeError:
            return []

    # ------------------------------------------------------------------
    def _local_check(self, text: str, recent: list[str]) -> list[str]:
        """LLM を使わない検査。確定的で無料なので先に通す。"""
        result = review(text, include_experience=True)
        if not result.ok:
            return list(result.problems)

        for previous in recent:
            score = similarity(text, previous)
            if score >= self.max_similarity:
                return [f"最近の返信と似すぎています（{score:.2f}）: {previous[:30]}"]
        return []

    # ------------------------------------------------------------------
    def write(self, candidate: Any, *, shape: str | None = None,
              feedback: Any | None = None) -> Draft | None:
        """返信案を作る。作れなければ None。

        `feedback` は審査（ReplyJudge）が落としたときの指摘。
        **落ちた文と理由と直し案を渡して書き直させる。**
        審査は rewrite（こう書けばいい、という案）まで返しているので、
        それを捨てて最初から書き直すより速く収束する。
        """
        chosen = shape or self.pick_shape()
        recent = self._recent_texts()
        examples = self._examples()

        rejected: list[str] = []
        previous_attempt = ""
        previous_problems: list[str] = []
        hint = ""

        if feedback is not None:
            detail = getattr(feedback, "detail", {}) or {}
            previous_attempt = str(detail.get("_text", "") or getattr(feedback, "text", ""))
            previous_problems = [str(p) for p in (detail.get("problems") or [])]
            if not previous_problems and getattr(feedback, "reason", ""):
                previous_problems = [feedback.reason]
            hint = str(detail.get("rewrite", "") or "")
            rejected.append(f"[審査] {previous_attempt} ← {'、'.join(previous_problems)}")
        # 実際に LLM を呼んだ回数。呼び出し側が予算を数えるのに使う。
        # max_attempts で数えると、1回で通ったときも上限ぶん使ったことになる。
        self.last_call_count = 0

        for attempt in range(1, self.max_attempts + 1):
            self.last_call_count = attempt
            prompt = prompts.reply_prompt(
                candidate,
                shape=chosen,
                recent_replies=recent,
                examples=examples,
                previous_attempt=previous_attempt,
                previous_problems=previous_problems,
                hint=hint,
            )
            try:
                response = self.llm.ask(prompt)
            except TransientError as exc:
                logger.warning("返信の生成に失敗しました（%d回目）: %s", attempt, exc)
                rejected.append(f"[{attempt}] 生成できませんでした: {str(exc)[:80]}")
                continue

            text = str(response.data.get("reply", "")).strip()
            if not text:
                rejected.append(f"[{attempt}] 返信文が空でした")
                previous_attempt, previous_problems = "", ["空の応答"]
                continue

            problems = self._local_check(text, recent)
            if problems:
                logger.info("返信案を却下しました（%d回目）: %s", attempt, problems)
                rejected.append(f"[{attempt}] {text} ← {'、'.join(problems)}")
                previous_attempt, previous_problems = text, problems
                continue

            return Draft(
                text=text,
                shape=chosen,
                attempts=attempt,
                touches=str(response.data.get("touches", ""))[:120],
                rejected=rejected,
            )

        logger.info("返信案を作れませんでした（%d回試行）", self.max_attempts)
        return None
