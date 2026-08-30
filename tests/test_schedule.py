"""スケジュール設定の整合性テスト。

ワークフローの cron と config.toml の [[schedule]] がずれると、
「投稿はされるが内容の種別が違う」「そもそもスロットが引けず落ちる」ため、
ここで機械的に突き合わせる。
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from src.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POST_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "post.yml"

CRON_LINE = re.compile(r"^\s*-\s*cron:\s*'([^']+)'", re.MULTILINE)


def workflow_crons() -> list[str]:
    text = POST_WORKFLOW.read_text(encoding="utf-8")
    # workflow_dispatch より前（schedule ブロック内）の cron だけを拾う
    schedule_part = text.split("workflow_dispatch:")[0]
    return [" ".join(m.split()) for m in CRON_LINE.findall(schedule_part)]


# ======================================================================
def test_workflow_exists():
    assert POST_WORKFLOW.is_file()


def test_workflow_crons_match_config(config):
    configured = sorted(" ".join(s.cron_utc.split()) for s in config.schedule)
    assert sorted(workflow_crons()) == configured


def test_every_workflow_cron_resolves_to_a_slot(config):
    for cron in workflow_crons():
        assert config.slot_for_cron(cron).slot


def test_unknown_cron_raises_config_error(config):
    with pytest.raises(ConfigError):
        config.slot_for_cron("59 23 * * *")


def test_ten_posts_per_day(config):
    assert len(config.schedule) == 10


def test_most_slots_are_link_free(config):
    """増やすのはリンクなしの枠だけ、という方針を固定する。

    露出を増やすのが目的で、広告を増やすのが目的ではない。
    枠を足すときにうっかり allow_affiliate を立てると、
    ランプアップの上限に届くまで広告が増えてしまう。
    """
    link_free = [s for s in config.schedule if not s.allow_affiliate]
    assert len(link_free) >= 5, "リンクなしの枠が少なすぎる"
    assert len(link_free) > len(config.schedule) / 2, "広告枠のほうが多い"


def test_daily_cap_covers_every_slot(config):
    """1日の上限が、スロット数を下回っていないこと。

    下回っていると、最後のスロットが毎日 daily_cap_reached() で
    黙って落ちる。上限自体は残す（手動実行が積み上がって
    1日18件投稿し、MetaにAPIアクセスをブロックされた経緯がある）。
    """
    cap = int(config.ramp_up["max_posts_per_day"])
    assert cap >= len(config.schedule), (
        f"上限 {cap} 件がスロット数 {len(config.schedule)} を下回っている"
    )


def test_cron_utc_matches_jst_time(config):
    """cron_utc が time_jst から9時間引いた値になっていること。"""
    for slot in config.schedule:
        hour, minute = (int(x) for x in slot.time_jst.split(":"))
        cron_minute, cron_hour = slot.cron_utc.split()[:2]

        expected = (timedelta(hours=hour, minutes=minute) - timedelta(hours=9)) % timedelta(days=1)
        expected_hour, remainder = divmod(int(expected.total_seconds()), 3600)
        expected_minute = remainder // 60

        assert int(cron_hour) == expected_hour, f"{slot.slot}: 時がずれています"
        assert int(cron_minute) == expected_minute, f"{slot.slot}: 分がずれています"


def test_slot_names_are_unique(config):
    names = [s.slot for s in config.schedule]
    assert len(names) == len(set(names))


def test_crons_are_unique(config):
    """cron が重複するとスロットを一意に解決できない。"""
    crons = [" ".join(s.cron_utc.split()) for s in config.schedule]
    assert len(crons) == len(set(crons))


def test_affiliate_slots_are_limited(config):
    """新規アカウントで広告投稿を連投しないよう、リンク許可スロットを抑える。"""
    allowed = [s for s in config.schedule if s.allow_affiliate]
    assert len(allowed) <= 3


def test_rotation_options_are_known_post_types(config):
    from src.pipeline import ITEMS_NEEDED

    for slot_name, options in config.rotation.items():
        for post_type in options:
            assert post_type in ITEMS_NEEDED, f"{slot_name}: 未知の投稿タイプ {post_type}"


def test_rotation_exists_for_every_rotation_slot(config):
    for slot in config.schedule:
        if slot.post_type == "rotation":
            assert config.rotation.get(slot.slot), f"{slot.slot} のローテーション設定がありません"
