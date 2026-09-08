"""DOM から投稿を取り出す部分の検査。

ブロックの形は `src/engage/candidates.py` の docstring にある実測例に合わせる。
**推測で作った形でテストしない。**
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.engage.browser.extract import (
    EXTRACT_JS,
    age_hours_from_datetime,
    parse_row,
    parse_rows,
)
from src.engage.buzz import JST

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=JST)

# candidates.py の docstring にある実測の形。
# 著者名 / 日付 / 本文 / いいね / 返信 / リポスト / シェア
REAL_BLOCK = """sana_oniku
2026/09/03
まつげ下がる人向けのマスカラ、結局どれがいいのか分からなくなってきた
216
9
1
5"""


def row(**kwargs):
    base = {
        "href": "/@sana_oniku/post/DTVoI4xlSTZ",
        "text": REAL_BLOCK,
        "datetime": None,
        "hasMedia": False,
    }
    base.update(kwargs)
    return base


# ======================================================================
# 投稿時刻
# ======================================================================
def test_reads_age_from_the_datetime_attribute():
    """表示テキストより機械可読で、表示形式の変更に影響されない。"""
    assert age_hours_from_datetime("2026-09-04T06:00:00+09:00", now=NOW) == 6.0


def test_handles_a_utc_timestamp():
    assert age_hours_from_datetime("2026-09-04T00:00:00.000Z", now=NOW) == pytest.approx(3.0)


def test_a_future_timestamp_clamps_to_zero():
    assert age_hours_from_datetime("2026-09-05T00:00:00+09:00", now=NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "こわれた日付", "昨日"])
def test_an_unreadable_timestamp_returns_none(value):
    """**推測で埋めない。** 読めなかったことを None で伝える。"""
    assert age_hours_from_datetime(value, now=NOW) is None


# ======================================================================
# 1件のパース
# ======================================================================
def test_parses_a_real_looking_block():
    candidate = parse_row(row(), source="timeline", now=NOW)
    assert candidate.username == "sana_oniku"
    assert candidate.shortcode == "DTVoI4xlSTZ"
    assert "マスカラ" in candidate.text
    assert candidate.likes == 216
    assert candidate.replies == 9
    assert candidate.source == "timeline"
    assert candidate.collected_at.startswith("2026-09-04")


def test_the_author_name_is_dropped_from_the_body():
    candidate = parse_row(row(), now=NOW)
    assert not candidate.text.startswith("sana_oniku")


def test_the_datetime_attribute_overrides_the_text_age():
    candidate = parse_row(
        row(datetime="2026-09-04T10:00:00+09:00"), now=NOW)
    assert candidate.age_hours == 2.0


def test_query_parameters_are_stripped_from_the_href():
    candidate = parse_row(
        row(href="/@sana_oniku/post/DTVoI4xlSTZ?xmt=abcdef"), now=NOW)
    assert candidate.shortcode == "DTVoI4xlSTZ"
    assert "?" not in candidate.href


def test_media_presence_is_recorded():
    assert parse_row(row(hasMedia=True), now=NOW).has_media is True


@pytest.mark.parametrize(
    "bad_href",
    ["", "/settings", "https://example.com/x", "/@user/media/ABC"],
)
def test_a_non_post_link_is_skipped(bad_href):
    assert parse_row(row(href=bad_href), now=NOW) is None


def test_an_empty_body_is_skipped():
    assert parse_row(row(text="sana_oniku\n2026/09/03\n216\n9"), now=NOW) is None


# ======================================================================
# 一覧のパース
# ======================================================================
def test_parse_rows_keeps_going_past_a_broken_entry():
    """**1件で全部を落とさない。**"""
    rows = [row(href="/@a/post/AAA"), row(href="broken"), row(href="/@b/post/BBB")]
    found = parse_rows(rows, source="timeline", now=NOW)
    assert [c.shortcode for c in found] == ["AAA", "BBB"]


def test_parse_rows_handles_an_empty_input():
    assert parse_rows([], now=NOW) == []
    assert parse_rows(None, now=NOW) == []


def test_parse_rows_tags_every_candidate_with_its_source():
    found = parse_rows([row()], source="accounts", now=NOW)
    assert all(c.source == "accounts" for c in found)


# ======================================================================
# 取り出す JS
# ======================================================================
def test_the_extract_script_does_not_depend_on_obfuscated_classes():
    from src.engage.browser.selectors import OBFUSCATED_CLASS

    assert not OBFUSCATED_CLASS.search(EXTRACT_JS)


def test_the_extract_script_starts_from_the_proven_selector():
    """scripts/collect_candidates.py が実データで動作確認済みの起点。"""
    assert 'a[href*="/post/"]' in EXTRACT_JS


def test_the_extract_script_reads_the_machine_readable_timestamp():
    assert "time[datetime]" in EXTRACT_JS


def test_the_extract_script_stops_at_the_measured_post_container():
    """2026-09-04 実測: article は 0個。data-pressable-container が投稿カード。"""
    assert "pressableContainer" in EXTRACT_JS
