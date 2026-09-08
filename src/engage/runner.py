"""自動返信の全体を回す。**工程を並べるだけ。**

    収集 → 既読を除く → 順位付け → 返信対象か → 返信を書く →
    出していいか → 投稿直前の再確認 → 投稿 → 記録

判断はそれぞれのモジュールが持つ。ここは順番と、止めどきだけを持つ。

## 投稿するには鍵が2つ要る

    config.toml の enabled = true  かつ  --live

どちらか片方では投稿しない。DRY_RUN が既定。

## 1件の問題で全体を止めない

`Pipeline` と同じ方針。候補1の検証が落ちたら候補2へ進む。
ただし **日次上限とキルスイッチだけは即座に全体を止める。**

## 日次の枠は手動返信と共有する

`EngagementLog` を通して数える。手で3件返した日は自動返信は動かない。
Meta のスパム規定が見るのは «そのアカウントが1日に何件返したか» であって、
どの経路から返したかではない。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..errors import AuthError, ThrottledError
from . import buzz as buzz_mod
from . import store as store_mod
from .budget import ReplyBudget
from .browser import actions
from .candidates import Candidate
from .executor import BrowserReplyExecutor, ExecuteResult
from .judge import ReplyJudge, TargetJudge, Verdict
from .sources import build_sources, collect_all
from .verify import PrePostVerifier
from .writer import Draft, ReplyWriter

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


@dataclass
class PlannedReply:
    candidate: Candidate
    buzz: buzz_mod.BuzzScore
    target_verdict: Verdict
    draft: Draft
    reply_verdict: Verdict


@dataclass
class RunReport:
    collected: int = 0
    after_seen: int = 0
    ranked: int = 0
    considered: int = 0
    llm_calls: int = 0
    ranked_candidates: list[tuple[Candidate, "buzz_mod.BuzzScore"]] = field(default_factory=list)
    """順位付けを通った候補。--collect-only が中身を見るために持つ。"""
    planned: list[PlannedReply] = field(default_factory=list)
    posted: list[tuple[PlannedReply, ExecuteResult]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    """(shortcode, 理由, 工程)。工程は rule / judge / verify。

    ルールで落ちたものは数が多く内容も似るが、LLM や再確認で落ちたものは
    1件ずつ意味が違う。混ぜて先頭から10件出すと、**知りたいほうが
    埋もれる**ので分けて持つ。
    """
    errors: list[str] = field(default_factory=list)
    stopped: str = ""
    """全体を止めた理由。空なら最後まで回った。"""

    paused: str = ""
    """返信だけを見送った理由（間隔が空いていない等）。

    **`stopped` と分ける。** `stopped` は _print_report が早期 return する
    ので、使い回すと候補一覧が消えてしまう。見送りの回も収集と観測は
    行っており、それは次の回の velocity を作る。

    budget: str = ""
    残枠の要約。人が読む用。
    """

    budget: str = ""

    def note_skip(self, candidate: Any, reason: str, stage: str = "judge") -> None:
        self.skipped.append((getattr(candidate, "shortcode", "?"), reason, stage))

    def skips(self, stage: str) -> list[tuple[str, str]]:
        return [(code, reason) for code, reason, s in self.skipped if s == stage]


class EngageRunner:
    def __init__(
        self,
        config: Config,
        *,
        store: Any,
        engagement_log: Any,
        llm: Any,
        state: Any | None = None,
        session_factory: Any | None = None,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        # テストが時刻を固定するための入口。既定は実時刻。
        self._now = now
        self.store = store
        self.log = engagement_log
        self.llm = llm
        self.state = state
        self._session_factory = session_factory

        auto = config.autoreply
        self.enabled = bool(auto.get("enabled", False))
        self.max_per_run = int(auto.get("max_per_run", 1))
        self.max_candidates = int(auto.get("max_candidates_per_run", 60))
        self.max_llm_calls = int(auto.get("max_llm_calls_per_run", 40))

        filt = config.autoreply_section("filter")
        self.seen_ttl_days = int(filt.get("seen_ttl_days", 45))

        engagement = config.engagement
        self.cooldown_days = int(engagement.get("same_account_cooldown_days", 7))
        self.beauty_ratio = float(engagement.get("beauty_ratio", 0.8))

        # 審査で落ちたときに書き直す回数。**0 にすると1発勝負になる。**
        self.max_judge_rounds = max(
            int(config.autoreply_section("llm").get("max_judge_rounds", 3)), 1)

        self.target_judge = TargetJudge(config, llm)
        self.reply_judge = ReplyJudge(config, llm)
        self.writer = ReplyWriter(config, llm, state=state, store=store)
        self.executor = BrowserReplyExecutor(config, PrePostVerifier(config))

        # 自分の投稿に返信しないための照合用。
        # state.json に記録済みのユーザー名を使う（無ければ照合を諦める）。
        self.own_username = str(state.get("threads_username", "") or "") if state else ""

    # ------------------------------------------------------------------
    def daily_cap(self) -> int:
        """今日返してよい全体の件数。ランプの解釈は ReplyBudget に委ねる。

        「今日のランプは何を言っているか」の実装を2か所に置かないため。
        """
        return ReplyBudget(self.config, store=self.store, log=self.log,
                           state=self.state, now=self._now).global_cap

    def now(self) -> datetime:
        return self._now or datetime.now(JST)

    def _back_off(self, exc: Exception) -> None:
        """絞られたことを記録し、しばらく発火させない。"""
        hours = int(self.config.autoreply.get("throttle_backoff_hours", 6))
        until = self.now() + timedelta(hours=hours)
        if self.state is not None:
            self.state.set("autoreply_throttled_until", until.isoformat(timespec="seconds"))
        logger.warning("Threads に絞られました。%d時間おきます（%s まで）: %s",
                       hours, until.strftime("%m/%d %H:%M"), exc)

    def throttled_until(self) -> datetime | None:
        if self.state is None:
            return None
        try:
            return datetime.fromisoformat(str(self.state.get("autoreply_throttled_until") or ""))
        except (ValueError, TypeError):
            return None

    def stop_reason(self, *, dry_run: bool) -> str:
        """全体を止めるべき理由。無ければ空。"""
        # 絞られた直後はブラウザを開く前に止める。
        until = self.throttled_until()
        if until is not None and self.now() < until:
            return (f"Threads に絞られたため待機中です"
                    f"（{until.strftime('%m/%d %H:%M')} まで）")
        # キルスイッチ。設定を編集せずに止められる手段を2つ持つ。
        if os.environ.get("AUTOREPLY_OFF", "").strip().lower() in {"1", "true", "yes", "on"}:
            return "AUTOREPLY_OFF が設定されています"
        if self.config.autoreply_stop_path.exists():
            return f"停止ファイルがあります: {self.config.autoreply_stop_path}"
        if not dry_run and not self.enabled:
            return ("config.toml の [autoreply] enabled が false です"
                    "（投稿するには enabled = true と --live の両方が要ります）")
        remaining = self.daily_cap() - self.log.replied_today()
        if remaining <= 0:
            return f"本日の上限に達しています（{self.daily_cap()}件）"
        return ""

    # ------------------------------------------------------------------
    def run(
        self,
        *,
        dry_run: bool = True,
        limit: int | None = None,
        sources: list[str] | None = None,
        collect_only: bool = False,
    ) -> RunReport:
        report = RunReport()

        stop = self.stop_reason(dry_run=dry_run)
        if stop:
            report.stopped = stop
            logger.info("実行しません: %s", stop)
            return report

        budget = ReplyBudget(self.config, store=self.store, log=self.log,
                             state=self.state, now=self._now)
        report.budget = budget.summary()

        want = min(limit or self.max_per_run, budget.remaining_total())
        pace = budget.pace()
        if not pace.ready:
            # **返信だけ見送る。収集は続ける。**
            # velocity は同じ投稿を2回見ないと出ないので、この回は無駄ではなく
            # 次の回の velocity を作る回になる。
            want = 0
            report.paused = pace.describe()
            if sources is None:
                # 返信できない回にプロフィールを開かない。開く回数そのものがリスク。
                sources = ["timeline"]

        session = self._open_session()
        try:
            candidates = self._collect(session, sources, report)
            ranked = self._rank(candidates, report)
            report.ranked_candidates = ranked
            if collect_only:
                # LLM を呼ばずにここで終わる。何が残ったかは呼び出し側が読む。
                self.store.prune(days=self.seen_ttl_days)
                return report
            self._consider(session, ranked, report, want=want,
                           dry_run=dry_run, budget=budget)
        except ThrottledError as exc:
            # **次の定期実行を止める。** 信号をここで消すと90分後に突っ込む。
            self._back_off(exc)
            report.stopped = str(exc)
            return report
        finally:
            self._close_session(session)
            report.budget = budget.summary()

        self.store.prune(days=self.seen_ttl_days)
        return report

    # ------------------------------------------------------------------
    def _open_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from .browser.session import ThreadsSession

        session = ThreadsSession(self.config).start()
        try:
            session.require_login()
        except AuthError:
            session.close()
            raise
        return session

    @staticmethod
    def _close_session(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------
    def _collect(self, session: Any, only: list[str] | None,
                 report: RunReport) -> list[Candidate]:
        found, errors = collect_all(
            session,
            build_sources(self.config, only=only, state=self.state),
            limit_per_source=self.max_candidates,
        )
        report.collected = len(found)
        report.errors += errors

        if actions.looks_like_silent_drift(found):
            report.errors.append(
                "収集した投稿のほぼ全件で反応数が0です。"
                " DOM が変わった可能性があります（probe_threads_dom.py で実測）。")

        self._retag_monitored(found)

        actionable = self.store.filter_actionable(found, within_days=self.seen_ttl_days)
        report.after_seen = len(actionable)
        return actionable

    def _retag_monitored(self, candidates: list[Candidate]) -> None:
        """監視対象の投稿は、どこで見つけても accounts 扱いにする。

        **「どこで見つけたか」ではなく「誰の投稿か」で扱いを決める。**

        collect_all() は enabled の順（timeline が先）に連結し、
        rank_candidates() は先勝ちで重複排除する。フォローしている監視対象の
        投稿はホームTLにも流れてくるので、放っておくと source="timeline" になり、
        厳しいフィルタと7日クールダウンが当たって「同じ相手に1日3件」が
        静かに壊れる。

        副産物として、**プロフィールを開かずに監視対象の投稿を拾える。**
        プロフィールを開く回数そのものが制限のリスクなので、これが効く。
        """
        monitored = {t.username.lower() for t in self.config.autoreply_targets}
        if not monitored:
            return
        for candidate in candidates:
            if candidate.username.lower() in monitored:
                candidate.source = "accounts"

    def _rank(self, candidates: list[Candidate],
              report: RunReport) -> list[tuple[Candidate, buzz_mod.BuzzScore]]:
        # **順位付けが先。** buzz.rank() が previous_sighting() を読むので、
        # ここで先に mark_seen すると、今回の数字で前回の数字を上書きしてしまい
        # 差分が 0 になる（＝ velocity が死ぬ）。観測の記録は必ずあとで行う。
        ranked, rejected = buzz_mod.rank(
            candidates,
            store=self.store,
            # 収集元ごとの上書きを含んだまま渡す。解決は候補ごと。
            filt=self.config.autoreply_section("filter"),
            weights=self.config.autoreply_section("buzz"),
            # クールダウンは passes_filter で見る（収集元ごとに長さが違う）。
            # ここで exclude_usernames に入れると、落ちた理由が記録に残らない。
            last_reply_at=self.log.last_replied_at_by_username(
                days=max(self.cooldown_days, 1)),
            now=self._now,
            exclude_shortcodes=self.log.replied_shortcodes() | self.store.replied_shortcodes(),
            beauty_ratio=self.beauty_ratio,
            limit=self.max_candidates,
        )

        # 観測を記録する。ここで残した数字が、次回の速度計算の分母になる。
        # ルールで落ちた投稿も記録する（伸びれば次回また候補に戻る）。
        for candidate, reason in rejected:
            self.store.mark_seen(candidate, decision=store_mod.DECISION_SKIPPED_RULE,
                                 reason=reason)
            report.note_skip(candidate, reason, stage="rule")
        for candidate, score in ranked:
            self.store.mark_seen(candidate, decision=store_mod.DECISION_OBSERVED,
                                 buzz_score=score.total)

        report.ranked = len(ranked)
        return ranked

    # ------------------------------------------------------------------
    def _consider(self, session: Any, ranked: list[tuple[Candidate, buzz_mod.BuzzScore]],
                  report: RunReport, *, want: int, dry_run: bool,
                  budget: ReplyBudget) -> None:
        for candidate, score in ranked:
            if len(report.posted) >= want:
                return
            if report.llm_calls >= self.max_llm_calls:
                report.errors.append("LLM の呼び出し上限に達しました")
                return

            # **LLM を呼ぶ前に枠を見る。** 使い切った収集元に費用をかけない。
            allowed, reason = budget.allows(candidate.source)
            if not allowed:
                report.note_skip(candidate, reason, stage="rule")
                continue

            report.considered += 1
            plan = self._plan(candidate, score, report, dry_run=dry_run)
            if plan is None:
                continue

            report.planned.append(plan)
            result = self.executor.post(session, candidate, plan.draft.text,
                                        dry_run=dry_run,
                                        own_username=self.own_username)
            if not result.ok:
                if not dry_run:
                    # 下見では記録しない。記録すると、その候補が本番から
                    # 永久に外れてしまう（verify_failed は終了状態なので）。
                    self.store.mark_seen(candidate,
                                         decision=store_mod.DECISION_VERIFY_FAILED,
                                         reason=result.reason, buzz_score=score.total)
                report.note_skip(candidate, result.reason, stage="verify")
                # **1件の問題で全体を止めない。** 次の候補へ進む。
                continue

            report.posted.append((plan, result))
            budget.charge(candidate.source)
            if not dry_run:
                self._record(plan, result)

    def _plan(self, candidate: Candidate, score: buzz_mod.BuzzScore,
              report: RunReport, *, dry_run: bool) -> PlannedReply | None:
        """返信案までを作る。まだ投稿しない。"""
        target = self.target_judge.judge(candidate)
        report.llm_calls += 1
        if not target.ok:
            if not dry_run:
                self.store.mark_seen(candidate,
                                     decision=store_mod.DECISION_SKIPPED_JUDGE,
                                     reason=target.reason, buzz_score=score.total)
            report.note_skip(candidate, f"対象外: {target.reason}")
            return None

        # **審査で落ちて終わりにしない。** 指摘と直し案を渡して書き直す。
        #
        # 審査は problems（何が悪いか）と rewrite（こう書けばいい）まで返す。
        # それを捨てて次の候補へ移ると、せっかく «返信してよい» と判断した
        # 相手を毎回逃すことになる。実測でも、生成まで到達した1件が
        # ai_smell 0.35 で落ちてそこで終わっていた。
        draft = None
        reply = None
        feedback = None
        for attempt in range(1, self.max_judge_rounds + 1):
            draft = self.writer.write(candidate, feedback=feedback)
            report.llm_calls += self.writer.last_call_count
            if draft is None:
                break

            reply = self.reply_judge.judge(candidate, draft.text)
            report.llm_calls += 1
            if reply.ok:
                break

            logger.info("審査で却下（%d/%d回目）: %s",
                        attempt, self.max_judge_rounds, reply.reason)
            # 落ちた本文を detail に載せて次の生成へ渡す
            reply.detail["_text"] = draft.text
            feedback = reply
            draft = None

        if draft is None:
            reason = (f"審査で却下: {reply.reason}" if reply is not None
                      else "返信案を作れませんでした")
            if not dry_run:
                self.store.mark_seen(
                    candidate,
                    decision=(store_mod.DECISION_SKIPPED_JUDGE if reply is not None
                              else store_mod.DECISION_NO_DRAFT),
                    reason=reason, buzz_score=score.total)
            report.note_skip(candidate, reason)
            return None

        return PlannedReply(candidate=candidate, buzz=score, target_verdict=target,
                            draft=draft, reply_verdict=reply)

    # ------------------------------------------------------------------
    def _record(self, plan: PlannedReply, result: ExecuteResult) -> None:
        """記録は sqlite と JSONL の両方へ。

        JSONL は手動返信との共有台帳。1日の上限を両経路で数えるために要る。
        """
        candidate = plan.candidate
        self.store.record_reply(
            shortcode=candidate.shortcode,
            username=candidate.username,
            permalink=candidate.permalink,
            reply_text=plan.draft.text,
            reply_shape=plan.draft.shape,
            source=candidate.source,
            buzz_score=plan.buzz.total,
            target_score=plan.target_verdict.score,
            target_reason=plan.target_verdict.reason,
            reply_score=plan.reply_verdict.score,
            reply_reason=plan.reply_verdict.reason,
            generation_attempts=plan.draft.attempts,
            our_reply_url=result.our_reply_url,
            confirmed=result.confirmed,
        )
        self.store.mark_seen(candidate, decision=store_mod.DECISION_REPLIED,
                             buzz_score=plan.buzz.total)
        self.log.append(candidate.username, candidate.shortcode, plan.draft.text)

        # ランプの起点は「初めて実際に返信した日」。
        # 実行しただけ・DRY_RUN しただけでは日数を消費しない。
        if self.state is not None:
            self.state.mark_autoreply_started()
