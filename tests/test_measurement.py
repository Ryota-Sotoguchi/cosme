"""成績の読み方。

Threads の insights.replies は **自分の連投も1件として数える**。
2026-08-20〜09-03 の実測では、返信60件のうち51件が自分の連投だった
（Threads API で返信の投稿者を照合して確認）。

この取り違えを根拠に config の枠配分を組み替えた前例があるので、
「人の反応」と「自分の連投」を混ぜないことをテストで固定する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.storage.history import History, PostRecord
from src.storage.revenue import Revenue, RevenueDay

JST = timezone(timedelta(hours=9))


def record(**kwargs) -> PostRecord:
    base = dict(
        posted_at=datetime.now(JST).isoformat(timespec="seconds"),
        slot="noon",
        post_type="product",
        template_id="band_focus",
        status="success",
        text="本文",
        has_affiliate_link=True,
    )
    base.update(kwargs)
    return PostRecord(**base)


# ======================================================================
def test_self_replies_come_from_the_segment_count():
    """連投3本なら、自己リプライは2件。"""
    r = record(extra={"segments": 3}, insights={"replies": 3})
    assert r.self_replies == 2
    assert r.human_replies == 1


def test_single_post_has_no_self_replies():
    r = record(extra={"segments": 1}, insights={"replies": 4})
    assert r.self_replies == 0
    assert r.human_replies == 4


def test_stored_value_wins_over_the_segment_count():
    """古い投稿は連投本数が履歴に無いので、照合結果を保存して使う。"""
    r = record(extra={}, insights={"replies": 3, "replies_self": 2})
    assert r.self_replies == 2
    assert r.human_replies == 1


def test_human_replies_never_go_negative():
    """返信が消された等でずれても、負の反応数を出さない。"""
    r = record(extra={"segments": 5}, insights={"replies": 1})
    assert r.human_replies == 0


def test_unknown_history_does_not_invent_self_replies():
    """情報が無いときに勝手な補正をしない。0件として扱う。"""
    r = record(extra={}, insights={"replies": 2})
    assert r.self_replies == 0
    assert r.human_replies == 2


def test_human_engagements_exclude_own_replies():
    """自分の連投を「反応」に数えないこと。

    これを数えていたせいで、人の反応がゼロの型が
    「反応率がいちばん高い型」に見えていた。
    """
    r = record(
        extra={"segments": 3},
        insights={"views": 300, "likes": 1, "replies": 2, "reposts": 0, "shares": 0},
    )
    assert r.human_replies == 0
    assert r.human_engagements == 1


def test_insight_dataclass_reports_human_replies():
    from src.threads.insights import PostInsight

    insight = PostInsight(post_id="1", replies=3, replies_self=2)
    assert insight.human_replies == 1
    assert insight.as_dict()["replies_self"] == 2


def test_insight_without_breakdown_omits_the_field():
    """分からないものを 0 として書き込まない。上書きで情報が消える。"""
    from src.threads.insights import PostInsight

    assert "replies_self" not in PostInsight(post_id="1", replies=3).as_dict()


# ======================================================================
def test_revenue_round_trip(tmp_path: Path):
    store = Revenue(tmp_path / "revenue.jsonl")
    store.put(RevenueDay(date="2026-09-03", clicks=5, orders=1, reward=48))
    reloaded = Revenue(tmp_path / "revenue.jsonl")
    entry = reloaded.get("2026-09-03")
    assert entry is not None
    assert (entry.clicks, entry.orders, entry.reward) == (5, 1, 48)


def test_revenue_replaces_the_same_day(tmp_path: Path):
    """同じ日を二度入れても二重計上しないこと。"""
    store = Revenue(tmp_path / "revenue.jsonl")
    store.put(RevenueDay(date="2026-09-03", clicks=5, reward=48))
    store.put(RevenueDay(date="2026-09-03", clicks=9, reward=96))
    assert store.totals().clicks == 9
    assert store.totals().reward == 96


def test_revenue_csv_import(tmp_path: Path):
    csv_path = tmp_path / "report.csv"
    csv_path.write_text(
        "日付,クリック数,成果件数,成果報酬\n"
        "2026/09/01,12,1,\"1,200円\"\n"
        "2026/09/02,7,0,0円\n",
        encoding="utf-8",
    )
    entries = Revenue.parse_csv(csv_path)
    assert [e.date for e in entries] == ["2026-09-01", "2026-09-02"]
    assert entries[0].clicks == 12
    assert entries[0].reward == 1200


def test_revenue_csv_without_a_date_column_is_refused(tmp_path: Path):
    """読めない CSV を推測で埋めないこと。"""
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("クリック数,成果報酬\n5,48\n", encoding="utf-8")
    assert Revenue.parse_csv(csv_path) == []


def test_broken_revenue_line_does_not_stop_operations(tmp_path: Path):
    path = tmp_path / "revenue.jsonl"
    path.write_text(
        '{"date": "2026-09-01", "clicks": 3}\nこわれた行\n', encoding="utf-8"
    )
    assert Revenue(path).totals().clicks == 3


# ======================================================================
def test_history_keeps_reading_old_records(tmp_path: Path):
    """フィールドを足しても既存行が読めること。

    history.jsonl は運用の実績そのもので、重複防止とランプアップの根拠。
    後方互換を落とすと同じ商品を再投稿する。
    """
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"posted_at": "2026-08-20T12:00:00+09:00", "slot": "noon",'
        ' "post_type": "product", "template_id": "short", "status": "success",'
        ' "text": "むかしの投稿", "has_affiliate_link": true,'
        ' "insights": {"views": 100, "replies": 2}}\n',
        encoding="utf-8",
    )
    records = History(path).load()
    assert len(records) == 1
    assert records[0].views == 100
    # segments も replies_self も無い行。勝手な補正をしない。
    assert records[0].self_replies == 0
