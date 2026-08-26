"""返信の投稿と歯止めのテスト。

返信は他人の投稿に残る。自分の投稿と違って後から直せないので、
投稿する前に止まるべきものが確実に止まることを固定する。
"""

from __future__ import annotations

import pytest

from src.engage.responder import Responder
from src.engage.review import EngagementLog


@pytest.fixture
def responder(config, tmp_path):
    log = EngagementLog(tmp_path / "engagements.jsonl")
    return Responder(config, log)


GOOD = "無印だけでも十分だと思います〜。敏感肌だと足すほど荒れることあるので"
POST_ID = "17908396266285102"


# ======================================================================
# 投稿してよいかの判定
# ======================================================================
def test_accepts_a_specific_reply(responder):
    result = responder.check(username="someone", post_id=POST_ID,
                             shortcode="ABC", text=GOOD)
    assert result.ok, result.reason


def test_rejects_without_a_post_id(responder):
    """スクレイプだけで見つけた投稿には返信できない。

    短縮ID（DTVoI4xlSTZ）は reply_to_id が要求する数値IDとは別物。
    """
    result = responder.check(username="someone", post_id="",
                             shortcode="DTVoI4xlSTZ", text=GOOD)
    assert not result.ok
    assert "post_id" in result.reason


def test_rejects_a_template_reply(responder):
    result = responder.check(username="someone", post_id=POST_ID,
                             shortcode="ABC", text="わかります！")
    assert not result.ok
    assert "定型句" in result.reason


def test_rejects_a_reply_with_a_url(responder):
    result = responder.check(username="someone", post_id=POST_ID, shortcode="ABC",
                             text="これいいですよね https://example.com で買えます")
    assert not result.ok
    assert "URL" in result.reason


# ======================================================================
# 歯止め
# ======================================================================
def test_stops_at_the_daily_limit(responder):
    """1日の上限。数を追わない。"""
    for i in range(responder.max_per_day):
        responder.log.append(f"user{i}", f"S{i}", GOOD)
    result = responder.check(username="newuser", post_id=POST_ID,
                             shortcode="NEW", text=GOOD)
    assert not result.ok
    assert "上限" in result.reason


def test_does_not_reply_twice_to_the_same_post(responder):
    responder.log.append("someone", "SAME", GOOD)
    result = responder.check(username="another", post_id=POST_ID,
                             shortcode="SAME", text=GOOD)
    assert not result.ok
    assert "すでに返信" in result.reason


def test_does_not_stick_to_the_same_account(responder):
    """同じ相手に張り付かない。"""
    responder.log.append("someone", "S1", GOOD)
    result = responder.check(username="someone", post_id=POST_ID,
                             shortcode="S2", text=GOOD)
    assert not result.ok
    assert "返信済み" in result.reason


# ======================================================================
# 実際に投稿する経路
# ======================================================================
def test_dry_run_does_not_record(responder):
    """下見では記録しない。上限を無駄に食わないため。"""
    result = responder.reply(username="someone", post_id=POST_ID,
                             shortcode="ABC", text=GOOD, dry_run=True)
    assert result.ok
    assert responder.log.all() == []


def test_dry_run_still_runs_the_checks(responder):
    result = responder.reply(username="someone", post_id=POST_ID,
                             shortcode="ABC", text="わかります！", dry_run=True)
    assert not result.ok


def test_reply_uses_reply_to_id(responder, monkeypatch):
    """API の呼び方が、自分の投稿への返信とまったく同じであること。"""
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, data=None, **kwargs):
        calls.append((url, dict(data or {})))
        if url.endswith("/threads"):
            return FakeResponse({"id": "container-1"})
        return FakeResponse({"id": "published-1"})

    monkeypatch.setattr(responder.http, "post", fake_post)
    responder.publish_wait = 0

    result = responder.reply(username="someone", post_id=POST_ID,
                             shortcode="ABC", text=GOOD, dry_run=False)
    assert result.ok, result.reason
    assert result.reply_id == "published-1"

    container_call = calls[0][1]
    assert container_call["reply_to_id"] == POST_ID
    assert container_call["media_type"] == "TEXT"
    assert container_call["text"] == GOOD


def test_successful_reply_is_recorded(responder, monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, data=None, **kwargs):
        return FakeResponse({"id": "x"})

    monkeypatch.setattr(responder.http, "post", fake_post)
    responder.publish_wait = 0
    responder.reply(username="someone", post_id=POST_ID, shortcode="ABC",
                    text=GOOD, dry_run=False)
    assert len(responder.log.all()) == 1
    assert responder.log.all()[0].username == "someone"


def test_failed_reply_is_not_recorded(responder, monkeypatch):
    """失敗を記録すると、上限だけ減って返信できなくなる。"""
    class FakeResponse:
        def json(self):
            return {"error": {"message": "だめでした"}}

    monkeypatch.setattr(responder.http, "post",
                        lambda *a, **k: FakeResponse())
    responder.publish_wait = 0
    result = responder.reply(username="someone", post_id=POST_ID,
                             shortcode="ABC", text=GOOD, dry_run=False)
    assert not result.ok
    assert responder.log.all() == []
