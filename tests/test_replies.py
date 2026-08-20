"""自分の投稿へのコメント返信のテスト。

他人の投稿への自動返信とは別物。ここで扱うのは自分の投稿に
付いたコメントへの返しだけ。
"""

from __future__ import annotations

import json

import pytest

from src.http import HttpClient
from src.storage.state import State
from src.threads.replies import Reply, ReplyResponder


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {},
                           "data": data or {}})
        for fragment, payload in self.routes:
            if fragment in url:
                return _Resp(payload)
        return _Resp({"data": []})


def _responder(config, tmp_path, routes):
    object.__setattr__(config.credentials, "threads_access_token", "T")
    object.__setattr__(config.credentials, "threads_user_id", "999")
    http = HttpClient(session=_Session(routes), max_retries=0)
    return ReplyResponder(config, State(tmp_path / "state.json"), http=http)


# ======================================================================
def test_does_not_reply_to_its_own_thread_posts(config, tmp_path):
    """スレッドの2本目以降は自分の返信なので、返してはいけない。

    実装当初これを取りこぼし、自分自身に「ありがとうございます」と
    返信しかけた。
    """
    responder = _responder(config, tmp_path, [
        ("/me", {"username": "cosme_memo_jp"}),
        ("/replies", {"data": [
            {"id": "R1", "text": "そう思ってヘアケア見てたら…", "username": "cosme_memo_jp"},
            {"id": "R2", "text": "これ気になります！", "username": "someone"},
        ]}),
        ("/threads", {"data": [{"id": "P1"}]}),
    ])

    planned = responder.run(dry_run=True)
    usernames = [p["username"] for p in planned]
    assert "cosme_memo_jp" not in usernames
    assert usernames == ["someone"]


def test_skips_promotional_comments(config, tmp_path):
    responder = _responder(config, tmp_path, [
        ("/me", {"username": "me"}),
        ("/replies", {"data": [
            {"id": "R1", "text": "副業で稼げます DMください", "username": "spam"},
            {"id": "R2", "text": "https://example.com 見て", "username": "spam2"},
            {"id": "R3", "text": "相互フォローしませんか", "username": "spam3"},
            {"id": "R4", "text": "かわいい〜", "username": "real"},
        ]}),
        ("/threads", {"data": [{"id": "P1"}]}),
    ])

    planned = responder.run(dry_run=True)
    assert [p["username"] for p in planned] == ["real"]


def test_does_not_answer_the_same_comment_twice(config, tmp_path):
    routes = [
        ("/me", {"username": "me"}),
        ("/replies", {"data": [{"id": "R1", "text": "いいですね", "username": "a"}]}),
        ("/threads", {"data": [{"id": "P1"}]}),
    ]
    responder = _responder(config, tmp_path, routes)
    assert len(responder.run(dry_run=True)) == 1

    responder.state.set("answered_replies", ["R1"])
    assert responder.run(dry_run=True) == []


def test_caps_the_number_of_replies_per_run(config, tmp_path):
    responder = _responder(config, tmp_path, [
        ("/me", {"username": "me"}),
        ("/replies", {"data": [
            {"id": f"R{i}", "text": "いいですね", "username": f"u{i}"} for i in range(20)
        ]}),
        ("/threads", {"data": [{"id": "P1"}]}),
    ])
    planned = responder.run(dry_run=True)
    assert len(planned) <= responder.max_per_run


def test_dry_run_does_not_post(config, tmp_path):
    responder = _responder(config, tmp_path, [
        ("/me", {"username": "me"}),
        ("/replies", {"data": [{"id": "R1", "text": "いいですね", "username": "a"}]}),
        ("/threads", {"data": [{"id": "P1"}]}),
    ])
    planned = responder.run(dry_run=True)

    assert planned[0]["posted"] is False
    posts = [c for c in responder.http.session.calls if c["method"] == "POST"]
    assert posts == []


def test_question_and_plain_comments_get_different_replies():
    question = Reply(reply_id="1", text="これいくらですか？", username="a")
    plain = Reply(reply_id="2", text="かわいい", username="b")
    assert question.is_question
    assert not plain.is_question


def test_waits_before_publishing_a_reply(config, tmp_path, monkeypatch):
    """コンテナ作成から公開までの待機を飛ばさないこと。

    待たずに公開すると Threads が
    "The requested resource does not exist" を返して失敗する（実際に起きた）。
    """
    slept = []
    monkeypatch.setattr("src.threads.replies.time.sleep", lambda s: slept.append(s))

    responder = _responder(config, tmp_path, [
        ("/threads_publish", {"id": "PUBLISHED"}),
        ("/threads", {"id": "CONTAINER"}),
    ])
    result = responder.post_reply("R1", "ありがとうございます")

    assert result == "PUBLISHED"
    assert slept and slept[0] >= 10, "公開前に待機していない"
