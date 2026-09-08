"""返信の予算の検査。ブラウザも LLM も使わない。

見たいのは3つ:
  * 経路ごとに枠を持ち、片方の枠切れが他方を止めないこと
  * 枠は**暦日**で数えること（ローリング窓だと毎日1枠ずつ食われる）
  * 間隔が実行をまたいで効くこと
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.config import load_config
from src.engage.budget import JST, ReplyBudget
from src.engage.review import EngagementLog
from src.engage.store import EngageStore

# **今日を基準にする。** 固定日付にすると、日をまたいだ翌日に
# store.replied_since()（実時刻で窓を切る）と噛み合わなくなる。
NOW = datetime.now(JST).replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture
def config(tmp_path):
    return load_config(data_dir=tmp_path)


@pytest.fixture
def store(tmp_path):
    with EngageStore(tmp_path / "engage.sqlite3") as s:
        yield s


@pytest.fixture
def log(tmp_path):
    return EngagementLog(tmp_path / "engagements.jsonl")


def record(store, *, source: str, when: datetime, shortcode: str = "") -> None:
    """指定時刻に返信したことにする。"""
    code = shortcode or f"{source}-{when.isoformat()}"
    store.record_reply(shortcode=code, username="someone",
                       reply_text="返信", source=source)
    with store._conn:
        store._conn.execute(
            "UPDATE threads_replies SET replied_at = ? WHERE shortcode = ?",
            (when.isoformat(timespec="seconds"), code))


def manual(log, *, when: datetime, username: str = "person") -> None:
    """手で返信した記録を JSONL に置く。"""
    log.path.parent.mkdir(parents=True, exist_ok=True)
    with log.path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps({
            "username": username,
            "shortcode": f"m-{when.isoformat()}",
            "replied_at": when.isoformat(timespec="seconds"),
            "text": "手で書いた返信",
        }, ensure_ascii=False) + "\n")


def build(config, store, log, *, state=None, now=NOW) -> ReplyBudget:
    return ReplyBudget(config, store=store, log=log, state=state, now=now)


# ======================================================================
# 経路ごとの枠
# ======================================================================
def test_each_source_has_its_own_daily_quota(config, store, log):
    """数字は config を正とする。ここで二重に持つと配分を変えるたびに落ちる。"""
    quota = config.autoreply_section("daily_quota")
    budget = build(config, store, log)
    for source in ("timeline", "accounts", "search"):
        assert budget.quota(source) == quota[source]


def test_an_unknown_source_gets_the_default_quota(config, store, log):
    """REGISTRY に収集元を足しただけで無制限にならないこと。

    枠を書き忘れたまま収集元が増えても、既定値で頭打ちになる。
    （test_every_enabled_source_has_a_quota が書き忘れ自体も見張る）
    """
    default = config.autoreply_section("daily_quota")["default"]
    assert build(config, store, log).quota("followers") == default


def test_one_source_running_out_does_not_block_the_other(config, store, log):
    """**これが経路別に分ける理由。**"""
    for i in range(config.autoreply_section("daily_quota")["accounts"]):
        record(store, source="accounts", when=NOW - timedelta(hours=i + 1),
               shortcode=f"A{i}")

    budget = build(config, store, log)
    assert budget.allows("accounts")[0] is False
    assert "accounts の本日の枠" in budget.allows("accounts")[1]
    assert budget.allows("timeline")[0] is True


def test_the_shared_cap_still_binds_when_manual_replies_used_it(config, store, log):
    """手動返信は経路枠を食わないが、全体枠は食う。

    Meta が見るのは経路ではなくアカウント単位の合計。
    """
    for i in range(8):
        manual(log, when=NOW - timedelta(minutes=i * 10))

    budget = build(config, store, log)
    assert budget.remaining_total() == 0
    assert budget.allows("timeline")[0] is False
    assert "本日の上限" in budget.allows("timeline")[1]


def test_quotas_count_calendar_days_not_a_rolling_window(config, store, log):
    """**暦日で数える。**

    固定時刻の cron でローリング24時間にすると、前日の同時刻の返信が
    まだ窓に残り、毎日1枠ずつ食われて実行時刻が後ろへずれていく。
    """
    yesterday = NOW - timedelta(hours=23)   # 23時間前だが «昨日»
    assert yesterday.date() != NOW.date()
    record(store, source="timeline", when=yesterday)

    assert build(config, store, log).used("timeline") == 0


def test_todays_earliest_reply_is_still_counted(config, store, log):
    """当日の最も古い返信（23時間59分前）も拾えること。"""
    record(store, source="timeline", when=NOW.replace(hour=0, minute=1))
    assert build(config, store, log).used("timeline") == 1


def test_charging_reduces_the_remaining_within_one_run(config, store, log):
    budget = build(config, store, log)
    before = budget.remaining("timeline")
    budget.charge("timeline")
    assert budget.remaining("timeline") == before - 1
    assert budget.remaining_total() < budget.global_cap


# ======================================================================
# 間隔
# ======================================================================
def test_no_interval_applies_before_the_first_reply(config, store, log):
    assert build(config, store, log).pace().ready is True


def test_the_interval_blocks_a_reply_that_is_too_soon(config, store, log):
    record(store, source="timeline", when=NOW - timedelta(minutes=5))
    pace = build(config, store, log).pace()
    assert pace.ready is False
    assert "間隔が足りません" in pace.describe()


def test_the_interval_clears_once_enough_time_has_passed(config, store, log):
    record(store, source="timeline", when=NOW - timedelta(hours=3))
    assert build(config, store, log).pace().ready is True


def test_the_interval_is_randomised_but_stable_for_a_given_last_reply(config, store, log):
    """**実行をまたいで同じ目標時刻になること。**

    10分違いの2回の発火が別々の目標を計算すると、片方だけが通って
    間隔の意味が消える。
    """
    record(store, source="timeline", when=NOW - timedelta(minutes=50))

    first = build(config, store, log).pace().next_allowed_at
    second = build(config, store, log, now=NOW + timedelta(minutes=10)).pace().next_allowed_at
    assert first == second


def test_the_jitter_stays_inside_the_configured_range(config, store, log):
    base = config.autoreply["min_reply_interval_minutes"]
    jitter = config.autoreply["reply_interval_jitter_minutes"]

    for minutes in range(0, 60, 7):
        last = NOW - timedelta(minutes=minutes)
        record(store, source="timeline", when=last, shortcode=f"S{minutes}")
        pace = build(config, store, log).pace()
        gap = (pace.next_allowed_at - pace.last_reply_at).total_seconds() / 60
        assert base <= gap <= base + jitter


def test_a_manual_reply_also_paces_the_automatic_path(config, store, log):
    """手で返した直後に機械が返したら、外から見れば連投。"""
    manual(log, when=NOW - timedelta(minutes=5))
    assert build(config, store, log).pace().ready is False


# ======================================================================
# ランプアップ
# ======================================================================
class FakeState:
    def __init__(self, day: int | None) -> None:
        self._day = day

    def autoreply_days_since_start(self) -> int | None:
        return self._day


def test_the_ramp_clamps_every_source_on_early_days(config, store, log):
    stage = config.autoreply_section("ramp_up")["stages"][0]
    budget = build(config, store, log, state=FakeState(day=3))
    for source, limit in stage.items():
        if source != "until_day":
            assert budget.quota(source) == limit
    assert budget.global_cap == sum(v for k, v in stage.items() if k != "until_day")


def test_the_ramp_widens_on_the_second_stage(config, store, log):
    stage = config.autoreply_section("ramp_up")["stages"][1]
    budget = build(config, store, log, state=FakeState(day=10))
    for source, limit in stage.items():
        if source != "until_day":
            assert budget.quota(source) == limit
    assert budget.global_cap == sum(v for k, v in stage.items() if k != "until_day")


def test_the_ramp_stops_clamping_once_it_is_over(config, store, log):
    quota = config.autoreply_section("daily_quota")
    budget = build(config, store, log, state=FakeState(day=30))
    for source in ("timeline", "accounts", "search"):
        assert budget.quota(source) == quota[source]
    assert budget.global_cap == config.engagement["max_per_day"]


def test_the_ramp_counts_from_the_first_automatic_reply(config, store, log):
    """**まだ一度も返していないなら初日として扱う。**

    以前は投稿パイプラインの運用開始日を見ており、日数が18日先行していたので
    ランプが一度も効いていなかった（2026-09-06 に判明）。同じ間違いを繰り返さない。
    """
    budget = build(config, store, log, state=FakeState(day=None))
    assert budget.global_cap == 3


def test_without_state_the_ramp_does_not_apply(config, store, log):
    """state を持たない経路（テスト等）でも落ちないこと。"""
    assert build(config, store, log, state=None).global_cap == 8


# ======================================================================
# 表示
# ======================================================================
def test_the_summary_shows_each_source(config, store, log):
    record(store, source="timeline", when=NOW - timedelta(hours=2))
    quota = config.autoreply_section("daily_quota")
    summary = build(config, store, log).summary()
    assert f"timeline 1/{quota['timeline']}" in summary
    assert f"accounts 0/{quota['accounts']}" in summary
    assert "search" in summary
