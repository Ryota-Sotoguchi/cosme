"""アカウント全体の指標は「どの日の数字か」が分かる形で取る。

## なぜこのテストが要るのか

`data/account.jsonl` の views は **9/25〜10/13 のあいだ日ごとに比較できない値**だった。
`threads_insights` に since / until を渡していなかったので、API は直近の短い窓を
日ごとの桶で返し、`_values()` が最後の桶（= 測った時点までの当日ぶん）を拾っていた。
insights の発火は混雑で何時間もずれるため、何時までの値かが日によって違い、
9/28 が 5294、翌日が 67 という比べられない並びになっていた。

`followers_count` は total_value なので、この問題とは無関係に正しかった。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.storage import account as account_metrics
from src.threads.insights import ACCOUNT_METRICS, AccountInsight, ThreadsInsights

JST = timezone(timedelta(hours=9))


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """呼ばれた params を全部覚える。窓を渡しているかを見るため。"""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(dict(params or {}))
        payload = self.payloads.pop(0) if self.payloads else {}
        return FakeResponse(payload)


SERIES = {"data": [
    {"name": "views", "values": [{"value": 111}, {"value": 222}]},
    {"name": "likes", "values": [{"value": 3}]},
    {"name": "followers_count", "total_value": {"value": 42}},
]}
NOW = datetime(2026, 10, 13, 22, 5, tzinfo=JST)


def _insights(config, http):
    obj = ThreadsInsights(config, http=http)
    object.__setattr__(obj.credentials, "threads_access_token", "t")
    return obj


@pytest.fixture(autouse=True)
def _credentials(config):
    """認証を要求する箇所を通すためのダミー。実際の通信はしない。"""
    object.__setattr__(config.credentials, "threads_access_token", "t")
    object.__setattr__(config.credentials, "threads_user_id", "me")


def test_the_window_is_the_previous_jst_day():
    window = ThreadsInsights._previous_day_window(NOW)
    since = datetime.fromtimestamp(window["since"], JST)
    until = datetime.fromtimestamp(window["until"], JST)
    assert (since.year, since.month, since.day) == (2026, 10, 12)
    assert since.hour == since.minute == 0
    assert until - since == timedelta(days=1)


def test_the_account_call_asks_for_one_whole_day(config):
    http = FakeHttp(SERIES)
    result = _insights(config, http).for_account(when=NOW)

    assert result.complete is True
    assert result.day == "2026-10-12", "前日ぶんなのに当日の日付を返している"
    assert result.metrics == {"views": 222, "likes": 3, "followers_count": 42}
    assert set(http.calls[0]) >= {"metric", "since", "until"}
    assert http.calls[0]["metric"] == ACCOUNT_METRICS


def test_it_falls_back_without_the_window_so_followers_are_never_lost(config):
    """窓付きで取れない日でも、フォロワー数だけは残す。"""
    http = FakeHttp({"error": {"message": "unsupported"}}, SERIES)
    result = _insights(config, http).for_account(when=NOW)

    assert len(http.calls) == 2
    assert "since" not in http.calls[1]
    assert result.metrics["followers_count"] == 42
    # 窓なしは «その日の途中まで» なので、当日の日付で、印を付ける
    assert result.complete is False
    assert result.day == "2026-10-13"


def test_nothing_is_recorded_when_both_calls_fail(config):
    http = FakeHttp({"error": {"message": "x"}}, {"error": {"message": "x"}})
    result = _insights(config, http).for_account(when=NOW)
    assert not result
    assert result.metrics == {}


def test_a_partial_row_is_marked_so_it_is_not_compared(tmp_path: Path):
    path = tmp_path / "account.jsonl"
    account_metrics.record(path, {"views": 9, "followers_count": 10},
                           day="2026-10-13", partial=True)
    account_metrics.record(path, {"views": 480, "followers_count": 11},
                           day="2026-10-12")

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["date"] for r in rows] == ["2026-10-12", "2026-10-13"], "日付順に並べる"
    assert rows[0].get("partial") is None
    assert rows[1]["partial"] is True


def test_the_day_label_wins_over_the_clock(tmp_path: Path):
    """前日ぶんを今日の行に書くと1日ずれる。日付は取得側が決める。"""
    path = tmp_path / "account.jsonl"
    account_metrics.record(path, {"views": 1}, when=NOW, day="2026-10-12")
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["date"] == "2026-10-12"


def test_the_same_day_is_replaced_not_appended(tmp_path: Path):
    path = tmp_path / "account.jsonl"
    account_metrics.record(path, {"views": 1}, day="2026-10-12")
    account_metrics.record(path, {"views": 2}, day="2026-10-12")
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["views"] == 2


def test_account_insight_is_falsy_without_metrics():
    assert not AccountInsight({}, day="2026-10-12", complete=True)
    assert AccountInsight({"views": 1}, day="2026-10-12", complete=True)
