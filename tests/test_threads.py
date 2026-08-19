"""Threads 投稿処理とトークン管理のテスト。

実APIは叩かない。公式仕様どおりのリクエストを組み立てているかを検証する。
"""

from __future__ import annotations

import json

import pytest

from src.errors import AuthError, MissingSecretError, PostRejectedError
from src.http import HttpClient
from src.storage.state import State
from src.threads.client import ThreadsClient
from src.threads.token import ThreadsTokenManager


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {}, "data": data or {}})
        return self._responses.pop(0)


def _http(responses):
    return HttpClient(session=_Session(responses), max_retries=0)


def _with_token(config, token="TOKEN123", user_id="9876"):
    object.__setattr__(config.credentials, "threads_access_token", token)
    object.__setattr__(config.credentials, "threads_user_id", user_id)
    return config


# ======================================================================
# 投稿
# ======================================================================
def test_post_follows_container_then_publish_then_verify(config):
    _with_token(config)
    http = _http([
        _Resp({"id": "CONTAINER1"}),
        _Resp({"id": "POST1"}),
        _Resp({"id": "POST1", "permalink": "https://www.threads.net/@u/post/abc",
               "timestamp": "2026-08-19T12:15:00+0000", "text": "本文"}),
    ])
    client = ThreadsClient(config, http=http)

    published = client.post_text("テスト本文", wait_seconds=0)

    calls = http.session.calls
    assert calls[0]["url"].endswith("/9876/threads")
    assert calls[0]["data"]["media_type"] == "TEXT"
    assert calls[0]["data"]["text"] == "テスト本文"
    assert calls[1]["url"].endswith("/9876/threads_publish")
    assert calls[1]["data"]["creation_id"] == "CONTAINER1"
    assert published.post_id == "POST1"
    assert published.verified is True
    assert published.permalink.startswith("https://www.threads.net/")


def test_publish_response_alone_is_not_treated_as_verified(config):
    """検証GETが失敗したら verified=False になる（成功レスポンスだけを信じない）。"""
    _with_token(config)
    http = _http([
        _Resp({"id": "C1"}),
        _Resp({"id": "P1"}),
        _Resp({"error": {"code": 100, "message": "not found"}}),
    ])
    client = ThreadsClient(config, http=http)

    published = client.post_text("本文", wait_seconds=0)
    assert published.post_id == "P1"
    assert published.verified is False


def test_expired_token_raises_auth_error(config):
    _with_token(config)
    http = _http([_Resp({"error": {"code": 190, "message": "Access token has expired"}})])
    client = ThreadsClient(config, http=http)

    with pytest.raises(AuthError):
        client.create_container("本文")


def test_graph_error_becomes_post_rejected(config):
    _with_token(config)
    http = _http([_Resp({"error": {"code": 4, "message": "rate limited by app"}})])
    client = ThreadsClient(config, http=http)

    with pytest.raises(PostRejectedError):
        client.create_container("本文")


def test_missing_token_raises_missing_secret(config):
    object.__setattr__(config.credentials, "threads_access_token", None)
    client = ThreadsClient(config, http=_http([]))
    with pytest.raises(MissingSecretError):
        client.create_container("本文")


def test_rejects_text_over_platform_limit(config):
    _with_token(config)
    client = ThreadsClient(config, http=_http([]))
    with pytest.raises(PostRejectedError):
        client.create_container("あ" * 501)


def test_rejects_empty_text(config):
    _with_token(config)
    client = ThreadsClient(config, http=_http([]))
    with pytest.raises(PostRejectedError):
        client.create_container("   ")


def test_resolves_user_id_from_me_when_not_configured(config):
    _with_token(config, user_id=None)
    object.__setattr__(config.credentials, "threads_user_id", None)
    http = _http([_Resp({"id": "555", "username": "cosme_kaimemo"}), _Resp({"id": "C1"})])
    client = ThreadsClient(config, http=http)

    assert client.get_user_id() == "555"
    assert http.session.calls[0]["url"].endswith("/me")


# ======================================================================
# トークン
# ======================================================================
def test_exchange_uses_official_grant_type(config, tmp_path):
    object.__setattr__(config.credentials, "threads_app_secret", "SECRET")
    state = State(tmp_path / "state.json")
    http = _http([_Resp({"access_token": "LONG", "expires_in": 5184000})])
    manager = ThreadsTokenManager(config, state, http=http)

    info = manager.exchange_for_long_lived("SHORT")

    call = http.session.calls[0]
    assert call["url"].endswith("/access_token")
    assert call["params"]["grant_type"] == "th_exchange_token"
    assert call["params"]["client_secret"] == "SECRET"
    assert info.access_token == "LONG"
    assert info.days == 60


def test_exchange_requires_app_secret(config, tmp_path):
    object.__setattr__(config.credentials, "threads_app_secret", None)
    manager = ThreadsTokenManager(config, State(tmp_path / "state.json"), http=_http([]))
    with pytest.raises(MissingSecretError):
        manager.exchange_for_long_lived("SHORT")


def test_refresh_uses_official_grant_type(config, tmp_path):
    state = State(tmp_path / "state.json")
    http = _http([_Resp({"access_token": "NEW", "expires_in": 5184000})])
    manager = ThreadsTokenManager(config, state, http=http)

    info = manager.refresh("OLD")

    call = http.session.calls[0]
    assert call["url"].endswith("/refresh_access_token")
    assert call["params"]["grant_type"] == "th_refresh_token"
    assert info.access_token == "NEW"
    # 期限が state に記録され、トークン本体は記録されない
    assert state.token_days_remaining() in (59, 60)
    assert "NEW" not in json.dumps(state.data)


def test_refresh_failure_raises_auth_error(config, tmp_path):
    http = _http([_Resp({"error": {"message": "invalid token"}})])
    manager = ThreadsTokenManager(config, State(tmp_path / "state.json"), http=http)
    with pytest.raises(AuthError):
        manager.refresh("OLD")


def test_warns_when_token_is_expiring(config, tmp_path, caplog):
    state = State(tmp_path / "state.json")
    state.record_token_refresh(5 * 86400)
    manager = ThreadsTokenManager(config, state, http=_http([]))

    with caplog.at_level("WARNING"):
        remaining = manager.warn_if_expiring()

    assert remaining in (4, 5)
    assert any("残り日数" in r.message for r in caplog.records)


def test_secret_writeback_is_skipped_without_pat(config, tmp_path, monkeypatch):
    """GH_PAT が無くても例外にせず False を返す（運用を止めない）。"""
    monkeypatch.delenv("GH_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    object.__setattr__(config.credentials, "github_pat", None)

    manager = ThreadsTokenManager(config, State(tmp_path / "state.json"), http=_http([]))
    assert manager.store_to_github_secret("TOKEN") is False


def test_non_numeric_user_id_is_ignored(config):
    """THREADS_USER_ID にユーザー名を入れてしまっても /me で引き直す。

    Graph API は username を渡すと
    「Object with ID '<username>' does not exist」という分かりにくい
    エラーを返すので、手前で弾く。
    """
    _with_token(config, user_id="cosme_memo_jp")
    http = _http([_Resp({"id": "28013906364885767", "username": "cosme_memo_jp"})])
    client = ThreadsClient(config, http=http)

    assert client.get_user_id() == "28013906364885767"
    assert http.session.calls[0]["url"].endswith("/me")


def test_numeric_user_id_is_used_directly(config):
    _with_token(config, user_id="28013906364885767")
    client = ThreadsClient(config, http=_http([]))
    assert client.get_user_id() == "28013906364885767"
