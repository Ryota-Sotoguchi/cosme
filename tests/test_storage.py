"""投稿履歴・運用状態のテスト。

public リポジトリに置く前提なので、Secret が保存されないことも検証する。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from src.storage.history import JST, History, PostRecord
from src.storage.state import State


def record(**overrides) -> PostRecord:
    base = dict(
        posted_at=datetime.now(JST).isoformat(timespec="seconds"),
        slot="noon",
        post_type="product",
        template_id="objective",
        status="success",
        text="【PR】テスト本文",
        has_affiliate_link=True,
        item_code="shop:1",
        shop_code="shop",
        brand_key="brand",
        genre_id="216131",
    )
    base.update(overrides)
    return PostRecord(**base)


# ======================================================================
def test_append_and_reload(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.append(record())

    reloaded = History(tmp_path / "history.jsonl")
    assert len(reloaded) == 1
    assert reloaded.load()[0].item_code == "shop:1"


def test_survives_corrupted_line(tmp_path):
    """壊れた行があっても運用は止めない。"""
    path = tmp_path / "history.jsonl"
    path.write_text('{"posted_at":"x","slot":"noon"}\nこれは壊れた行\n', encoding="utf-8")
    assert len(History(path).load()) == 1


def test_recent_item_codes_respects_cooldown(tmp_path):
    history = History(tmp_path / "history.jsonl")
    old = (datetime.now(JST) - timedelta(days=45)).isoformat(timespec="seconds")
    recent = (datetime.now(JST) - timedelta(days=3)).isoformat(timespec="seconds")
    history.append(record(item_code="old:1", posted_at=old))
    history.append(record(item_code="new:1", posted_at=recent))

    codes = history.recent_item_codes(days=30)
    assert "new:1" in codes
    assert "old:1" not in codes


def test_failed_posts_do_not_block_reposting(tmp_path):
    """失敗した投稿はクールダウン対象にしない（商品を無駄に失わないため）。"""
    history = History(tmp_path / "history.jsonl")
    history.append(record(item_code="failed:1", status="failed"))
    assert "failed:1" not in history.recent_item_codes(days=30)


def test_counts_todays_affiliate_posts(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.append(record(item_code="a:1", has_affiliate_link=True))
    history.append(record(item_code="b:2", has_affiliate_link=False))
    yesterday = (datetime.now(JST) - timedelta(days=1)).isoformat(timespec="seconds")
    history.append(record(item_code="c:3", has_affiliate_link=True, posted_at=yesterday))

    assert history.affiliate_posts_today() == 1


def test_no_secrets_are_persisted(tmp_path):
    """履歴に affiliate URL の生値やトークンが入らないこと。"""
    history = History(tmp_path / "history.jsonl")
    history.append(
        record(
            affiliate_url_hash="a" * 64,
            item_url="https://item.rakuten.co.jp/shop/1/",
        )
    )
    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert "hb.afl.rakuten.co.jp" not in raw
    assert payload["affiliate_url_hash"] == "a" * 64
    assert "access_token" not in payload
    assert "accessKey" not in raw


# ======================================================================
def test_state_persists_and_reloads(tmp_path):
    state = State(tmp_path / "state.json")
    state.set("hello", "world")
    state.save()
    assert State(tmp_path / "state.json").get("hello") == "world"


def test_state_recovers_from_corrupted_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")
    assert State(path).data == {}


def test_rotation_advances_and_wraps(tmp_path):
    state = State(tmp_path / "state.json")
    options = ["a", "b", "c"]
    assert [state.next_rotation("late", options) for _ in range(4)] == ["a", "b", "c", "a"]


def test_peek_rotation_does_not_advance(tmp_path):
    state = State(tmp_path / "state.json")
    options = ["a", "b"]
    assert state.peek_rotation("late", options) == "a"
    assert state.peek_rotation("late", options) == "a"


def test_operation_start_date_is_stable(tmp_path):
    state = State(tmp_path / "state.json")
    first = state.operation_start_date()
    state.save()
    assert State(tmp_path / "state.json").operation_start_date() == first
    assert state.days_since_start() == 1


def test_part_history_is_capped(tmp_path):
    state = State(tmp_path / "state.json")
    for i in range(30):
        state.record_part_ids({"opening": f"p{i}"}, keep=12)
    assert len(state.data["part_history"]["opening"]) == 12


def test_token_expiry_tracking_stores_no_token(tmp_path):
    state = State(tmp_path / "state.json")
    state.record_token_refresh(60 * 86400)
    state.save()

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "access_token" not in raw
    assert state.token_days_remaining() in (59, 60)


def test_token_days_remaining_is_none_when_unknown(tmp_path):
    assert State(tmp_path / "state.json").token_days_remaining() is None


def test_affiliate_url_is_redacted_from_stored_text(tmp_path):
    """投稿本文に含まれるアフィリエイトURLが履歴に残らないこと。"""
    history = History(tmp_path / "history.jsonl")
    url = "https://hb.afl.rakuten.co.jp/hgc/abc123.def456/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fs%2F1%2F"
    history.append(record(text=f"【PR】メモ。\n\n楽天市場 → {url}\n\n※投稿時点"))

    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "hb.afl.rakuten.co.jp" not in raw
    assert "abc123" not in raw
    assert "[affiliate-url]" in raw
    # 本文の他の部分は残っていること（類似度判定に使うため）
    assert "楽天市場" in raw


def test_redaction_keeps_non_affiliate_text_intact():
    from src.storage.history import redact_affiliate_urls

    assert redact_affiliate_urls("リンクなしの投稿です。") == "リンクなしの投稿です。"
    assert redact_affiliate_urls("a.r10.to は伏せる https://a.r10.to/xyz").endswith("[affiliate-url]")


def test_item_url_is_also_redacted(tmp_path):
    """affiliateId 指定時は itemUrl もアフィリエイトURLで返るため伏せる。

    text だけ伏せていたせいで、実運用の1件目で item_url からIDが漏れた。
    """
    history = History(tmp_path / "history.jsonl")
    history.append(record(
        item_url="https://hb.afl.rakuten.co.jp/hgc/abc.def/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fs%2F1%2F"
    ))
    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "hb.afl.rakuten.co.jp" not in raw
    assert "abc.def" not in raw


def test_plain_item_url_is_kept(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.append(record(item_url="https://item.rakuten.co.jp/shop/1/"))
    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "https://item.rakuten.co.jp/shop/1/" in raw
