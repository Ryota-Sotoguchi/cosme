"""週次点検は「捨てられた投稿」を数える。

## なぜこのテストが要るのか

2026-09-29〜10-13 に、1日10本のうち実際に出ていたのは3〜5本だった。
残りは `ScheduleDrift` で見送られ、履歴には `status=skipped` で残っていた。

そのあいだ、週次点検の欄が言ったのは
**「直近30本のうち1本が360分以上ずれている」だけ**だった。
検査が `history.successful()` の投稿しか見ていなかったので、
**遅れすぎて捨てられた投稿は数に入らず、生き残ったものだけを数えていた**。
半分が捨てられている状態が、2週間ぶんの点検を素通りした。

点検は «何をすればいいか» を名指しするための欄なので、ここが鈍いと
運用全体が静かに壊れる。捨てられた側を数える。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.main import _review_flags, _within_days
from src.storage.history import PostRecord

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 13, 20, 0, tzinfo=JST)


def record(*, days_ago=1, status="success", error_type=None, slot="noon") -> PostRecord:
    return PostRecord(
        posted_at=(NOW - timedelta(days=days_ago)).isoformat(timespec="seconds"),
        slot=slot,
        post_type="casual",
        template_id="topic",
        status=status,
        text="本文",
        has_affiliate_link=False,
        error_type=error_type,
    )


def dropped(**kw) -> PostRecord:
    return record(status="skipped", error_type="ScheduleDrift", **kw)


class _NoRevenue:
    """実績の検査を黙らせる最小の板（ここで見たいのは遅延の検査だけ）。"""

    def load(self):
        return [object()]

    def daily(self):
        return [object()]

    def totals(self):
        class Totals:
            clicks = 1
        return Totals()


def _flags(config, all_records, posts=()):
    return _review_flags(config, list(posts), [], [], _NoRevenue(), all_records=all_records)


def test_dropped_posts_are_counted_and_the_share_is_shown(config):
    """実際に起きた形（10本中4本しか出ない）を入れる。"""
    records = [dropped(days_ago=d) for d in range(1, 8) for _ in range(6)]
    records += [record(days_ago=d) for d in range(1, 8) for _ in range(4)]

    flags = _flags(config, records)
    hit = [f for f in flags if "捨てられている" in f]
    assert hit, f"捨てられた投稿を数えていない: {flags}"
    assert "42/70本" in hit[0]
    assert "60%" in hit[0]


def test_no_flag_when_nothing_is_dropped(config):
    flags = _flags(config, [record(days_ago=d) for d in range(1, 8)])
    assert not [f for f in flags if "捨てられている" in f]


def test_only_schedule_drift_counts(config):
    """他の理由の見送り（在庫切れなど）はここでは数えない。"""
    records = [record(days_ago=1, status="skipped", error_type="OutOfStock") for _ in range(9)]
    records += [record(days_ago=1)]
    assert not [f for f in _flags(config, records) if "捨てられている" in f]


def test_old_drops_are_outside_the_window(config):
    """直近14日を見る。1か月前の事故をいつまでも警告しない。"""
    records = [dropped(days_ago=40) for _ in range(20)] + [record(days_ago=1)]
    assert not [f for f in _flags(config, records) if "捨てられている" in f]


def test_the_near_limit_warning_only_speaks_when_nothing_is_dropped(config):
    """捨てられているときは «近づいている» ではなく «捨てられている» と言う。

    両方出ると、どちらを先に直すのか分からなくなる。
    """
    records = [dropped(days_ago=1) for _ in range(5)] + [record(days_ago=1) for _ in range(5)]
    flags = _flags(config, records)
    assert [f for f in flags if "捨てられている" in f]
    assert not [f for f in flags if "85%" in f]


def test_within_days_ignores_unreadable_timestamps():
    assert _within_days(NOW.isoformat(), 14, now=NOW) is True
    assert _within_days("", 14, now=NOW) is False
    assert _within_days("きのう", 14, now=NOW) is False
    assert _within_days((NOW - timedelta(days=20)).isoformat(), 14, now=NOW) is False


def test_within_days_treats_a_naive_timestamp_as_jst():
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert _within_days(naive, 14, now=NOW) is True
