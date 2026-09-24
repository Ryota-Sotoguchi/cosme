"""アカウント全体の指標（フォロワー数の推移）。

`insights` はフォロワー数を取得していたのに画面へ出すだけで捨てていた。
増えているのか止まっているのかを後から見られるように、1日1行で残す。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.storage import account

JST = timezone(timedelta(hours=9))


def at(day: int, hour: int = 20) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=JST)


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ======================================================================
def test_one_row_per_day(tmp_path):
    path = tmp_path / "account.jsonl"
    account.record(path, {"followers_count": 100, "views": 1000}, when=at(20))
    account.record(path, {"followers_count": 104, "views": 1200}, when=at(21))
    assert [r["date"] for r in rows(path)] == ["2026-09-20", "2026-09-21"]


def test_the_same_day_is_replaced_not_appended(tmp_path):
    """手で動かしても行が増えないこと。1日の値は最後の取得で上書きする。"""
    path = tmp_path / "account.jsonl"
    account.record(path, {"followers_count": 100}, when=at(20, hour=9))
    account.record(path, {"followers_count": 103}, when=at(20, hour=21))
    assert rows(path) == [{"date": "2026-09-20", "followers_count": 103}]


def test_rows_stay_sorted_by_date(tmp_path):
    path = tmp_path / "account.jsonl"
    account.record(path, {"followers_count": 3}, when=at(22))
    account.record(path, {"followers_count": 1}, when=at(20))
    assert [r["date"] for r in rows(path)] == ["2026-09-20", "2026-09-22"]


def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    """読めない1行で履歴全部を失わない。"""
    path = tmp_path / "account.jsonl"
    path.write_text('{"date": "2026-09-19", "followers_count": 90}\n壊れた行\n', encoding="utf-8")
    account.record(path, {"followers_count": 95}, when=at(20))
    assert [r["followers_count"] for r in rows(path)] == [90, 95]


def test_growth_reports_the_change(tmp_path):
    path = tmp_path / "account.jsonl"
    for day, followers in ((18, 80), (19, 84), (20, 90)):
        account.record(path, {"followers_count": followers}, when=at(day))
    text = account.growth(path, days=7)
    assert "90" in text and "+10" in text


def test_growth_is_quiet_until_there_are_two_days(tmp_path):
    path = tmp_path / "account.jsonl"
    assert account.growth(path) == ""
    account.record(path, {"followers_count": 10}, when=at(20))
    assert account.growth(path) == ""


def test_non_numeric_values_are_dropped(tmp_path):
    """API が想定外の形を返しても、行を壊さない。"""
    path = tmp_path / "account.jsonl"
    account.record(path, {"followers_count": 12, "note": "x"}, when=at(20))  # type: ignore[dict-item]
    assert rows(path) == [{"date": "2026-09-20", "followers_count": 12}]


def test_the_path_is_under_the_data_directory(config):
    assert config.account_path.name == "account.jsonl"
    assert config.account_path.parent == config.data_dir
