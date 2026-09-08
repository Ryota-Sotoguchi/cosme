"""自動返信の記録（sqlite）の検査。

特に見るところ:
  * 同じ投稿へ二度返せないことを **DB が保証している**か
  * 伸びの速度の分母になる first_seen_at が上書きされないか
  * 成果列があとから埋められるか（後付け機能が本当に additive か）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.engage.store import (
    DECISION_OBSERVED,
    DECISION_REPLIED,
    DECISION_SKIPPED_JUDGE,
    DECISION_SKIPPED_RULE,
    DECISION_VERIFY_FAILED,
    JST,
    SCHEMA_VERSION,
    EngageStore,
)


@dataclass
class FakeCandidate:
    shortcode: str = "ABC123"
    username: str = "someone"
    text: str = "本文"
    likes: int = 100
    replies: int = 5
    age_hours: float | None = 3.0
    source: str = "timeline"


@pytest.fixture
def store(tmp_path):
    with EngageStore(tmp_path / "engage" / "engage.sqlite3") as s:
        yield s


def _backdate(store, shortcode: str, *, days: float = 0, hours: float = 0) -> None:
    """行の時刻を過去にずらす。TTL や成果待ちの境界を試すため。"""
    when = (datetime.now(JST) - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")
    with store._conn:
        store._conn.execute(
            "UPDATE threads_seen_posts SET last_seen_at = ? WHERE shortcode = ?",
            (when, shortcode))
        store._conn.execute(
            "UPDATE threads_replies SET replied_at = ? WHERE shortcode = ?",
            (when, shortcode))


# ======================================================================
# スキーマ
# ======================================================================
def test_creates_its_parent_directory(tmp_path):
    """data/engage/ は .gitignore 済み。無ければ作る。"""
    path = tmp_path / "engage" / "engage.sqlite3"
    EngageStore(path).close()
    assert path.is_file()


def test_schema_creation_is_idempotent(tmp_path):
    path = tmp_path / "engage.sqlite3"
    first = EngageStore(path)
    first.mark_seen(FakeCandidate(), decision=DECISION_SKIPPED_RULE)
    first.close()

    second = EngageStore(path)
    assert second.schema_version == SCHEMA_VERSION
    assert second.seen_shortcodes(within_days=30) == {"ABC123"}
    second.close()


# ======================================================================
# 見た投稿
# ======================================================================
def test_a_settled_post_is_removed_from_the_candidates(store):
    store.mark_seen(FakeCandidate(shortcode="DONE"), decision=DECISION_REPLIED)
    remaining = store.filter_actionable(
        [FakeCandidate(shortcode="DONE"), FakeCandidate(shortcode="NEW")],
        within_days=30)
    assert [c.shortcode for c in remaining] == ["NEW"]


def test_merely_observing_a_post_does_not_remove_it(store):
    """**「見たことがある」だけでは外さない。**

    外してしまうと previous_sighting() が使われる場面が無くなり、
    伸びの速度が常に 0 になる。それはこのモジュールの中心が死ぬということ。
    """
    store.mark_seen(FakeCandidate(shortcode="WATCHED"), decision=DECISION_OBSERVED)
    remaining = store.filter_actionable(
        [FakeCandidate(shortcode="WATCHED")], within_days=30)
    assert [c.shortcode for c in remaining] == ["WATCHED"]


def test_a_rule_rejection_is_reconsidered_later(store):
    """投稿は時間とともに伸びる。

    「いいねが足りない」で落ちた投稿が1時間後に条件を満たすのはよくある。
    """
    store.mark_seen(FakeCandidate(shortcode="TOO_QUIET"),
                    decision=DECISION_SKIPPED_RULE, reason="反応が少ない")
    remaining = store.filter_actionable(
        [FakeCandidate(shortcode="TOO_QUIET")], within_days=30)
    assert [c.shortcode for c in remaining] == ["TOO_QUIET"]


def test_an_llm_rejection_is_reconsidered_later(store):
    """**LLM の判断はばらつくので、1回で永久に捨てない。**

    2026-09-06 実測: 同じ投稿を3回判定させたら 0.35 / 0.60 / 0.70 になった。
    投稿は max_age_hours でどのみち候補から外れるので、再判定は頭打ちになる。
    """
    store.mark_seen(FakeCandidate(shortcode="MAYBE"),
                    decision=DECISION_SKIPPED_JUDGE, reason="対象外")
    assert len(store.filter_actionable(
        [FakeCandidate(shortcode="MAYBE")], within_days=30)) == 1


@pytest.mark.parametrize("decision", [DECISION_REPLIED, DECISION_VERIFY_FAILED])
def test_terminal_decisions_are_never_reconsidered(store, decision):
    """返信済み・削除された。どちらも時間で変わらない。"""
    store.mark_seen(FakeCandidate(shortcode="SETTLED"), decision=decision)
    assert store.filter_actionable(
        [FakeCandidate(shortcode="SETTLED")], within_days=30) == []


def test_a_settled_post_returns_to_the_pool_after_the_window(store):
    store.mark_seen(FakeCandidate(shortcode="OLD"), decision=DECISION_REPLIED)
    _backdate(store, "OLD", days=50)
    assert len(store.filter_actionable([FakeCandidate(shortcode="OLD")],
                                       within_days=45)) == 1


def test_keeps_the_first_sighting_time_when_seen_again(store):
    """**速度は前回観測との差分で出す。** ここが上書きされると計算できない。"""
    store.mark_seen(FakeCandidate(likes=100), decision=DECISION_SKIPPED_RULE)
    first = store.previous_sighting("ABC123")

    store.mark_seen(FakeCandidate(likes=400), decision=DECISION_SKIPPED_RULE)
    second = store.previous_sighting("ABC123")

    assert second.first_seen_at == first.first_seen_at
    assert second.likes == 400  # 最新の数字には更新される


def test_previous_sighting_is_none_for_an_unknown_post(store):
    assert store.previous_sighting("NEVER_SEEN") is None


def test_prune_removes_only_old_sightings(store):
    store.mark_seen(FakeCandidate(shortcode="OLD"), decision=DECISION_SKIPPED_RULE)
    store.mark_seen(FakeCandidate(shortcode="FRESH"), decision=DECISION_SKIPPED_RULE)
    _backdate(store, "OLD", days=60)

    assert store.prune(days=45) == 1
    assert store.seen_shortcodes(within_days=365) == {"FRESH"}


def test_prune_does_not_touch_replies(store):
    """見た記録は忘れてよいが、返信した事実は消さない。"""
    store.mark_seen(FakeCandidate(), decision=DECISION_REPLIED)
    store.record_reply(shortcode="ABC123", username="someone", reply_text="返信")
    _backdate(store, "ABC123", days=100)

    store.prune(days=45)
    assert store.replied_shortcodes() == {"ABC123"}


def test_seen_shortcodes_respects_the_window(store):
    store.mark_seen(FakeCandidate(shortcode="OLD"), decision=DECISION_SKIPPED_RULE)
    _backdate(store, "OLD", days=50)
    assert store.seen_shortcodes(within_days=45) == set()
    assert store.seen_shortcodes(within_days=60) == {"OLD"}


# ======================================================================
# 返信
# ======================================================================
def test_records_a_reply(store):
    reply_id = store.record_reply(
        shortcode="ABC123", username="someone", reply_text="無印だけでも十分だと思います〜",
        reply_shape="共感", buzz_score=8.2, reply_score=0.9)
    assert reply_id > 0
    assert store.replied_shortcodes() == {"ABC123"}
    assert store.recent_reply_texts() == ["無印だけでも十分だと思います〜"]


def test_replying_twice_to_one_post_is_refused_by_the_database(store):
    """**アプリ側の判断だけに任せない。**

    取りこぼすと日次の枠を二重に食い、相手の投稿に返信が2つ並ぶ。
    """
    first = store.record_reply(shortcode="ABC123", username="someone", reply_text="1回目")
    second = store.record_reply(shortcode="ABC123", username="someone", reply_text="2回目")

    assert second == first  # 既存の id を返す
    assert store.recent_reply_texts() == ["1回目"]  # 上書きもしない
    assert len(store.recent_replies()) == 1


def test_replied_since_respects_the_window(store):
    store.record_reply(shortcode="OLD", username="a", reply_text="古い")
    store.record_reply(shortcode="NEW", username="b", reply_text="新しい")
    _backdate(store, "OLD", hours=30)

    assert [r.shortcode for r in store.replied_since(hours=24)] == ["NEW"]


def test_recent_reply_texts_are_newest_first(store):
    for i in range(3):
        store.record_reply(shortcode=f"S{i}", username="a", reply_text=f"返信{i}")
    assert store.recent_reply_texts(limit=2) == ["返信2", "返信1"]


# ======================================================================
# 成果（後付けできる形になっているか）
# ======================================================================
def test_outcome_columns_start_empty_and_can_be_filled_later(store):
    """成果ループは今回作らない。**あとで足せる形になっていることだけ確かめる。**"""
    reply_id = store.record_reply(
        shortcode="ABC123", username="someone", reply_text="返信",
        our_reply_url="https://www.threads.com/@me/post/XYZ")

    row = store.recent_replies()[0]
    assert row.outcome_likes is None
    assert row.outcome_replies is None

    store.update_outcome(reply_id, likes=12, replies=3)
    updated = store.recent_replies()[0]
    assert updated.outcome_likes == 12
    assert updated.outcome_replies == 3


def test_pending_outcomes_waits_for_the_delay(store):
    store.record_reply(shortcode="ABC123", username="a", reply_text="返信",
                       our_reply_url="https://example.test/x")
    assert store.pending_outcomes(older_than_hours=24) == []

    _backdate(store, "ABC123", hours=25)
    assert [r.shortcode for r in store.pending_outcomes(older_than_hours=24)] == ["ABC123"]


def test_pending_outcomes_skips_replies_without_a_url(store):
    """着弾を確認できなかった返信は開く先が無い。"""
    store.record_reply(shortcode="NOURL", username="a", reply_text="返信")
    _backdate(store, "NOURL", hours=25)
    assert store.pending_outcomes(older_than_hours=24) == []


def test_pending_outcomes_skips_already_fetched(store):
    reply_id = store.record_reply(shortcode="ABC123", username="a", reply_text="返信",
                                  our_reply_url="https://example.test/x")
    _backdate(store, "ABC123", hours=25)
    store.update_outcome(reply_id, likes=1, replies=0)
    assert store.pending_outcomes(older_than_hours=24) == []


def test_best_replies_ranks_by_reaction_weighting_replies_higher(store):
    """返信は「会話が続いた」証拠なので、いいねより重く見る。"""
    liked = store.record_reply(shortcode="LIKED", username="a", reply_text="いいねだけ",
                               our_reply_url="https://example.test/1")
    talked = store.record_reply(shortcode="TALKED", username="b", reply_text="会話になった",
                                our_reply_url="https://example.test/2")
    store.update_outcome(liked, likes=20, replies=0)
    store.update_outcome(talked, likes=8, replies=5)

    assert [r.shortcode for r in store.best_replies()] == ["TALKED", "LIKED"]


def test_best_replies_ignores_replies_without_outcomes(store):
    store.record_reply(shortcode="NOOUTCOME", username="a", reply_text="返信")
    assert store.best_replies() == []
