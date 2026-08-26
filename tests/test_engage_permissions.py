"""権限診断のテスト。

Threads にはトークンの内省エンドポイントが無いので、
各エンドポイントを叩いた結果から判断する。その判断が正しいことを固定する。
"""

from __future__ import annotations

import pytest

from src.engage.permissions import PermissionProbe


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("本文がありません")
        return self._payload


@pytest.fixture
def probe(config):
    return PermissionProbe(config)


def test_empty_500_is_read_as_a_missing_permission(probe, monkeypatch):
    """権限の無いエンドポイントは本文の無い 500 を返す。

    実測した挙動。keyword_search で content-length: 0 の 500 が返った。
    """
    monkeypatch.setattr(probe.http, "get", lambda *a, **k: FakeResponse(500))
    checks = probe.run()
    assert all(not c.ok for c in checks)
    assert any("権限" in c.detail for c in checks)


def test_all_ok_when_every_endpoint_answers(probe, monkeypatch):
    monkeypatch.setattr(probe.http, "get",
                        lambda *a, **k: FakeResponse(200, {"data": []}))
    assert all(c.ok for c in probe.run())


def test_error_payload_is_reported(probe, monkeypatch):
    monkeypatch.setattr(
        probe.http, "get",
        lambda *a, **k: FakeResponse(400, {"error": {"message": "権限がありません"}}))
    checks = probe.run()
    assert all(not c.ok for c in checks)
    assert all("権限がありません" in c.detail for c in checks)


def test_keyword_search_is_checked(probe, monkeypatch):
    """検索の権限を必ず見ること。他人への返信の入口なので。"""
    monkeypatch.setattr(probe.http, "get",
                        lambda *a, **k: FakeResponse(200, {"data": []}))
    scopes = {c.scope for c in probe.run()}
    assert "threads_keyword_search" in scopes


def test_each_check_says_what_it_is_for(probe, monkeypatch):
    """何に使う権限かを書くこと。落ちたときに影響が分かるように。"""
    monkeypatch.setattr(probe.http, "get",
                        lambda *a, **k: FakeResponse(200, {"data": []}))
    assert all(c.needed_for for c in probe.run())
