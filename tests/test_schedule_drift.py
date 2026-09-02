"""定期実行の遅延ガード。

GitHub Actions の定期実行は混雑時に大きく遅れる。実測（2026-08-20〜09-03）
では72件すべてが設定時刻とずれており、中央値43分・最大11時間半だった。
8/27以降は4〜11時間の遅延が続き、朝の投稿が12:28に、夜の投稿が翌06:30に
出ていた。そのとき表示回数1の投稿が3本出ている。

時間帯を設計していても、実行が数時間ずれると設計が成立しない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.main import _schedule_drift_minutes

JST = timezone(timedelta(hours=9))


def at(hour: int, minute: int = 0, day: int = 3) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=JST)


# ======================================================================
def test_on_time_is_zero_drift(config):
    # noon は 12:15
    assert _schedule_drift_minutes(config, "noon", at(12, 15)) == 0


def test_small_delay(config):
    assert _schedule_drift_minutes(config, "noon", at(12, 45)) == 30


def test_large_delay(config):
    """実際に起きた「12:15枠が23:09に出る」ケース。"""
    assert _schedule_drift_minutes(config, "noon", at(23, 9)) == pytest.approx(654, abs=1)


def test_drift_wraps_around_midnight(config):
    """22:30枠が翌00:30に走ったら、22時間ではなく2時間のずれとして見る。"""
    drift = _schedule_drift_minutes(config, "late", at(0, 30, day=4))
    assert drift == pytest.approx(120, abs=1)


def test_early_run_counts_as_drift_too(config):
    """早すぎる実行も同じくずれとして扱う。"""
    assert _schedule_drift_minutes(config, "noon", at(11, 15)) == 60


def test_unknown_slot_does_not_raise(config):
    """スロットが引けなくても投稿の流れを止めない。"""
    assert _schedule_drift_minutes(config, "存在しない枠", at(12, 15)) == 0.0


def test_guard_is_configured(config):
    """ガードが有効で、現実的な値であること。

    小さすぎると毎回見送りになる（実測の中央値は43分）。
    大きすぎると深夜に朝の投稿が出るのを止められない。
    """
    limit = float(config.schedule_settings.get("max_drift_minutes", 0))
    assert 60 <= limit <= 180, f"max_drift_minutes = {limit} は現実的でない"
