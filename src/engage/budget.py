"""いま返信してよいか、だけを答える。

## 3つの制約を1か所にまとめる

    全体の上限   1日に何件返してよいか（手動返信と共有）
    経路別の枠   タイムライン5件 / 監視対象3件 のように分ける
    返信の間隔   前の返信から最低どれだけ空けるか

runner に散らばらせると、どれか1つを直したときに他が置いていかれる。

## 台帳が2つあるのは意図的

    全体の上限   data/engagements.jsonl（EngagementLog）
                 **手動返信も入る。** Meta が見るのは経路ではなく
                 アカウント単位の合計なので、ここは分けない
    経路別の枠   data/engage/engage.sqlite3（threads_replies.source）
                 自動返信だけが入る

手動で1件返すと全体の残りは減るが、経路別の残りは減らない。ズレるが、
**全体の上限が先に効く**ので実害は無い。ここを「揃える」方向に直さないこと。

## 暦日で数える。ローリング24時間ではない

固定時刻の cron でローリング窓にすると、前日の同時刻の返信がまだ窓に残り、
毎日1枠ずつ食われて実行時刻が後ろへずれていく。
バーストを防ぐのは枠ではなく**間隔**の仕事。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 当日の返信を漏れなく拾うための窓。
# 「今日」の最も古い行でも 23時間59分 前なので、25時間見れば必ず含まれる。
_TODAY_WINDOW_HOURS = 25


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Pace:
    """次に返信してよい時刻。"""

    last_reply_at: datetime | None
    next_allowed_at: datetime | None
    ready: bool

    def describe(self) -> str:
        if self.ready or self.next_allowed_at is None:
            return ""
        last = self.last_reply_at.strftime("%H:%M") if self.last_reply_at else "?"
        return (f"前の返信から間隔が足りません"
                f"（前回 {last} / 次は {self.next_allowed_at.strftime('%H:%M')} 以降）")


class ReplyBudget:
    """返信の予算。`run()` で1回だけ組む。"""

    def __init__(
        self,
        config: Any,
        *,
        store: Any,
        log: Any,
        state: Any | None = None,
        now: datetime | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.now = now or datetime.now(JST)
        self._rng = rng

        auto = config.autoreply
        self.global_cap = int(config.engagement.get("max_per_day", 3))
        self.quotas = dict(config.autoreply_section("daily_quota"))
        self.default_quota = int(self.quotas.pop("default", 1))

        self.min_interval = timedelta(
            minutes=float(auto.get("min_reply_interval_minutes", 45)))
        self.jitter_minutes = float(auto.get("reply_interval_jitter_minutes", 45))

        self._stage = self._ramp_stage(state)
        if self._stage:
            self.quotas = {
                source: min(int(limit), int(self._stage.get(source, limit)))
                for source, limit in self.quotas.items()
            }
            self.global_cap = min(self.global_cap, sum(self.quotas.values()))

        self._used_today = self._count_by_source(store)
        self._replied_today = int(log.replied_today())
        self._last_reply_at = self._latest_reply(store, log)

        # 実行中に返信したぶんは、その場で減らす（max_per_run > 1 のときに要る）
        self._charged: dict[str, int] = {}
        self._charged_total = 0

    # ------------------------------------------------------------------
    def _ramp_stage(self, state: Any | None) -> dict[str, Any]:
        """今日のランプ段階。該当が無ければ空（＝制限しない）。

        **自動返信が初めて返信した日から数える。** 投稿パイプラインの
        運用開始日を見ていたときは、日数が先行しすぎてランプが一度も
        効いていなかった（2026-09-06 に判明）。
        """
        ramp = self.config.autoreply_section("ramp_up")
        if not ramp.get("enabled") or state is None:
            return {}
        day = state.autoreply_days_since_start()
        if day is None:
            # まだ一度も返信していない。初日として扱い、最も狭い段階を当てる。
            day = 1
        for stage in sorted(ramp.get("stages", []),
                            key=lambda s: int(s.get("until_day", 0))):
            if day <= int(stage.get("until_day", 0)):
                return dict(stage)
        return {}

    def _count_by_source(self, store: Any) -> dict[str, int]:
        """今日すでに返信した件数を経路ごとに数える。"""
        today = self.now.strftime("%Y-%m-%d")
        counts: dict[str, int] = {}
        for row in store.replied_since(hours=_TODAY_WINDOW_HOURS):
            if not str(row.replied_at).startswith(today):
                continue
            source = row.source or ""
            counts[source] = counts.get(source, 0) + 1
        return counts

    def _latest_reply(self, store: Any, log: Any) -> datetime | None:
        """最後に返信した時刻。**両方の台帳の新しいほう。**

        state.json に持たせない。実績から引けば真実とずれないし、
        投稿できたのに保存前に落ちた場合でも間隔が効く。
        """
        candidates: list[datetime] = []
        for row in store.replied_since(hours=_TODAY_WINDOW_HOURS * 2):
            when = _parse(row.replied_at)
            if when is not None:
                candidates.append(when)
        manual = log.last_replied_at()
        if manual is not None:
            candidates.append(manual)
        return max(candidates) if candidates else None

    # ------------------------------------------------------------------
    @property
    def ramp_stage(self) -> dict[str, Any]:
        return dict(self._stage)

    def quota(self, source: str) -> int:
        return int(self.quotas.get(source, self.default_quota))

    def used(self, source: str) -> int:
        return self._used_today.get(source, 0) + self._charged.get(source, 0)

    def remaining(self, source: str) -> int:
        return max(self.quota(source) - self.used(source), 0)

    def remaining_total(self) -> int:
        return max(self.global_cap - self._replied_today - self._charged_total, 0)

    def summary(self) -> str:
        """人が読む残枠。--history と実行結果に出す。"""
        parts = [f"{s} {self.used(s)}/{self.quota(s)}" for s in sorted(self.quotas)]
        head = f"本日 {self._replied_today + self._charged_total}/{self.global_cap}"
        if self._stage:
            head += f"（ランプ {self._stage.get('until_day')}日目まで）"
        return head + ("  ·  " + " · ".join(parts) if parts else "")

    # ------------------------------------------------------------------
    def pace(self) -> Pace:
        """次に返信してよい時刻。

        ばらつきは**最終返信時刻をシードにした乱数**で出す。プロセスを
        またいでも同じ値になるので、10分違いの2回の発火が同じ目標時刻を
        計算する。外からは不規則に見える。
        """
        last = self._last_reply_at
        if last is None:
            return Pace(None, None, True)

        rng = self._rng or random.Random(last.isoformat())
        gap = self.min_interval + timedelta(
            minutes=rng.uniform(0, max(self.jitter_minutes, 0)))
        allowed = last + gap
        return Pace(last, allowed, self.now >= allowed)

    def paced_out(self) -> bool:
        return not self.pace().ready

    # ------------------------------------------------------------------
    def allows(self, source: str) -> tuple[bool, str]:
        """その収集元でいま返信してよいか。(可否, 理由) を返す。"""
        if self.remaining_total() <= 0:
            return False, f"本日の上限に達しています（{self.global_cap}件）"
        if self.remaining(source) <= 0:
            return False, (f"{source} の本日の枠を使い切りました"
                           f"（{self.used(source)}/{self.quota(source)}）")
        return True, ""

    def charge(self, source: str) -> None:
        """1件返信した。実行中の残りを減らす。"""
        self._charged[source] = self._charged.get(source, 0) + 1
        self._charged_total += 1
        self._last_reply_at = self.now


def build(config: Any, *, store: Any, log: Any, state: Any | None = None,
          now: datetime | None = None, rng: random.Random | None = None) -> ReplyBudget:
    return ReplyBudget(config, store=store, log=log, state=state, now=now, rng=rng)
