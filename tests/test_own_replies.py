"""自分の投稿に付いたコメントに、一度だけ返す。

## なぜこのテストが要るのか

仕組み（`ReplyResponder`）は前からあったのに、**下書きを出すだけで
実際に返すのは人**という前提だったため、2026-10-16 時点で
**他人から付いた返信38件のうち答えたと記録があるのは1件**だった。
9/26 の投稿に付いた14件のコメントも放置されていた。

わざわざ書き込んだ人に返さないのは、いちばん安くていちばん効く機会を捨てている。
自動で返すようにしたので、守るべきことを機械で見張る。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.storage.state import State
from src.threads import reply_writer
from src.threads.replies import Reply, ReplyResponder

JST = timezone(timedelta(hours=9))


class FakeHttp:
    def __init__(self, replies_by_post=None):
        self.replies_by_post = replies_by_post or {}
        self.posted: list[dict] = []

    def get(self, url, params=None, **kwargs):
        class R:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        if url.endswith("/me"):
            return R({"username": "career_powerup"})
        for post_id, items in self.replies_by_post.items():
            if f"/{post_id}/replies" in url:
                return R({"data": items})
        return R({"data": []})

    def post(self, url, data=None, **kwargs):
        class R:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        self.posted.append(dict(data or {}))
        if url.endswith("/threads_publish"):
            return R({"id": f"published-{len(self.posted)}"})
        return R({"id": f"container-{len(self.posted)}"})


@pytest.fixture
def responder_factory(config, tmp_path, monkeypatch):
    def make(replies_by_post, *, writer=None, state_data=None):
        monkeypatch.setenv("THREADS_ACCESS_TOKEN", "t")
        object.__setattr__(config.credentials, "threads_access_token", "t")
        object.__setattr__(config.credentials, "threads_user_id", "me")
        state = State(tmp_path / "state.json")
        state.set("threads_username", "career_powerup")
        for k, v in (state_data or {}).items():
            state.set(k, v)
        http = FakeHttp(replies_by_post)
        r = ReplyResponder(config, state, http=http, writer=writer)
        r.publish_wait = 0
        return r, http, state

    return make


COMMENT = {"id": "c1", "text": "面接で退職理由を聞かれたとき、正直に話していいんでしょうか", "username": "someone"}


def test_a_comment_is_answered_once_and_never_again(responder_factory):
    r, http, state = responder_factory({"p1": [COMMENT]})
    planned = r.run(dry_run=False, post_ids=["p1"])
    assert len(planned) == 1 and planned[0]["posted"] is True

    # 2回目は同じコメントを拾わない
    again = r.run(dry_run=False, post_ids=["p1"])
    assert again == []
    assert "c1" in state.get("answered_replies", [])


def test_the_draft_run_never_posts_and_never_charges(responder_factory):
    r, http, state = responder_factory({"p1": [COMMENT]})
    planned = r.run(dry_run=True, post_ids=["p1"])
    assert planned and planned[0]["posted"] is False
    assert http.posted == []
    assert state.get("own_reply_log", []) == []
    assert r.remaining_today() == r.max_per_day


def test_own_replies_are_not_answered(responder_factory):
    """連投の2本目は自分の返信。ここに返すと自分と会話する。"""
    mine = {"id": "c2", "text": "続きです", "username": "career_powerup"}
    r, _, _ = responder_factory({"p1": [mine]})
    assert r.run(dry_run=True, post_ids=["p1"]) == []


def test_spam_comments_are_skipped_and_remembered(responder_factory):
    spam = {"id": "c3", "text": "副業で稼げます。DMください", "username": "spam"}
    r, http, state = responder_factory({"p1": [spam]})
    assert r.run(dry_run=False, post_ids=["p1"]) == []
    assert "c3" in state.get("answered_replies", []), "次の実行でまた見てしまう"
    assert http.posted == []


def test_the_daily_cap_counts_only_what_was_really_posted(responder_factory):
    comments = [{"id": f"c{i}", "text": f"質問です{i}？", "username": "u"} for i in range(8)]
    r, http, state = responder_factory({"p1": comments})
    r.max_per_day = 3
    r.max_per_run = 10
    planned = r.run(dry_run=False, post_ids=["p1"])
    assert len(planned) == 3, "1日の上限を超えて返している"
    assert r.remaining_today() == 0

    assert r.run(dry_run=False, post_ids=["p1"]) == []


def test_yesterdays_replies_do_not_count_against_today(responder_factory):
    yesterday = (datetime.now(JST) - timedelta(days=1)).isoformat(timespec="seconds")
    r, _, _ = responder_factory({"p1": [COMMENT]}, state_data={"own_reply_log": [yesterday] * 9})
    assert r.remaining_today() == r.max_per_day


def test_nothing_is_sent_while_throttled(responder_factory):
    """絞られている最中に別の口から押し込まない（経路は違ってもアカウントは同じ）。"""
    until = (datetime.now(JST) + timedelta(hours=3)).isoformat(timespec="seconds")
    r, http, _ = responder_factory({"p1": [COMMENT]},
                                   state_data={"autoreply_throttled_until": until})
    assert r.run(dry_run=False, post_ids=["p1"]) == []
    assert http.posted == []
    # 下書きは見られる（投稿しないので害がない）
    assert r.run(dry_run=True, post_ids=["p1"])


def test_an_expired_throttle_does_not_block(responder_factory):
    past = (datetime.now(JST) - timedelta(hours=1)).isoformat(timespec="seconds")
    r, _, _ = responder_factory({"p1": [COMMENT]},
                                state_data={"autoreply_throttled_until": past})
    assert r.run(dry_run=False, post_ids=["p1"])


# ======================================================================
# 返信文（reply_writer）
# ======================================================================

class FakeClient:
    available = True

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def ask(self, prompt, **kwargs):
        self.prompts.append(prompt)
        class R:
            def __init__(self, text):
                self.text = text
        return R(self.answers.pop(0) if self.answers else "{}")


def test_the_writer_answers_what_the_person_wrote(config):
    client = FakeClient('{"reply": "正直に話して大丈夫だと思います。不満ではなく'
                        '「次に何をしたいか」に言い換えると伝わりやすいです。"}')
    text = reply_writer.write_reply(client, comment=COMMENT["text"], post_text="退職理由の話")
    assert text and text.startswith("正直に")
    assert "退職理由の話" in client.prompts[0], "何に対するコメントかを渡していない"
    assert COMMENT["text"] in client.prompts[0]


def test_the_writer_refuses_a_reply_that_fails_review(config):
    """機械が書いた文は、使用体験や保証の言い方まで弾く。"""
    client = FakeClient('{"reply": "このエージェントを使えば必ず年収が上がります。"}',
                        '{"reply": "そこは人によります。わたしは条件を書き出して整理しました。"}')
    text = reply_writer.write_reply(client, comment=COMMENT["text"])
    assert text == "そこは人によります。わたしは条件を書き出して整理しました。"
    assert len(client.prompts) == 2
    assert "前回の文は次の理由で使えませんでした" in client.prompts[1]


def test_the_writer_gives_up_after_two_tries(config):
    bad = '{"reply": "誰でも必ず内定が取れます。"}'
    client = FakeClient(bad, bad, bad)
    assert reply_writer.write_reply(client, comment=COMMENT["text"]) is None
    assert len(client.prompts) == reply_writer.MAX_ATTEMPTS


def test_the_writer_returns_none_without_the_cli(config):
    class NoCli:
        available = False

        def ask(self, prompt, **kwargs):
            raise AssertionError("CLI が無いのに呼んでいる")

    assert reply_writer.write_reply(NoCli(), comment=COMMENT["text"]) is None


def test_the_responder_falls_back_to_a_fixed_reply(responder_factory):
    """書けなかった回も**返す**。書き込んだ人に何も返さないのがいちばん損。"""
    r, http, _ = responder_factory({"p1": [COMMENT]}, writer=lambda reply, post: None)
    planned = r.run(dry_run=False, post_ids=["p1"])
    assert len(planned) == 1 and planned[0]["posted"] is True
    assert planned[0]["reply"]


def test_the_fallback_replies_are_polite(config, tmp_path):
    from src.engage.review import impolite_fragment
    from src.threads.replies import _ANSWER_REPLIES, _THANKS_REPLIES

    for text in _ANSWER_REPLIES + _THANKS_REPLIES:
        assert not impolite_fragment(text), f"タメ口が混ざっている: {text}"
        assert "です" in text or "ます" in text, text


def test_the_same_kill_switch_stops_it(config, tmp_path, monkeypatch, capsys):
    """止めたいときに覚える場所を増やさない（自動返信と同じ STOP ファイル）。"""
    import argparse

    from src.main import cmd_replies

    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "t")
    stop = config.autoreply_stop_path
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("", encoding="utf-8")
    try:
        code = cmd_replies(config, argparse.Namespace(live=True))
    finally:
        stop.unlink()
    assert code == 0
    assert "停止ファイル" in capsys.readouterr().out


def test_the_env_kill_switch_also_stops_it(config, monkeypatch, capsys):
    import argparse

    from src.main import cmd_replies

    monkeypatch.setenv("AUTOREPLY_OFF", "true")
    assert cmd_replies(config, argparse.Namespace(live=True)) == 0
    assert "AUTOREPLY_OFF" in capsys.readouterr().out


# ======================================================================
# 古いコメントには返さない（2026-10-28）
#
# 動かしたら、9月のコスメ時代の投稿に付いた **62日前**の
# 「何時にお風呂入る？」への返信を書こうとした。1か月以上遅れて返すのは
# 会話として不自然だし、ジャンルも違う投稿に紐づいている。
# ======================================================================

def _comment(cid, text, days_ago=None, username="someone"):
    d = {"id": cid, "text": text, "username": username}
    if days_ago is not None:
        d["timestamp"] = (datetime.now(JST) - timedelta(days=days_ago)).isoformat()
    return d


def test_an_old_comment_is_left_alone(responder_factory):
    r, http, state = responder_factory({"p1": [_comment("c9", "18時20分ぐらいです", days_ago=62)]})
    r.max_comment_age_days = 14
    assert r.run(dry_run=False, post_ids=["p1"]) == []
    assert http.posted == []
    # **«見た» 扱いにしない。** 時刻の読み方を直したときに拾い直せなくなる
    assert "c9" not in state.get("answered_replies", [])


def test_a_fresh_comment_is_answered(responder_factory):
    r, http, _ = responder_factory({"p1": [_comment("c10", "面接の話、参考になりました", days_ago=2)]})
    r.max_comment_age_days = 14
    planned = r.run(dry_run=False, post_ids=["p1"])
    assert len(planned) == 1 and planned[0]["posted"] is True


def test_a_comment_without_a_timestamp_is_still_answered(responder_factory):
    """時刻が読めないものまで落とすと、取りこぼしが増える。安全側は «返す» 。"""
    r, _, _ = responder_factory({"p1": [_comment("c11", "そうなんですね")]})
    assert len(r.run(dry_run=False, post_ids=["p1"])) == 1


def test_the_age_is_read_from_the_api_timestamp():
    from src.threads.replies import Reply

    now = datetime.now(JST)
    old = Reply("x", "t", "u", created_at=(now - timedelta(days=30)).isoformat())
    assert 29 < old.age_days(now) < 31
    assert Reply("x", "t", "u").age_days(now) is None
    assert Reply("x", "t", "u", created_at="きのう").age_days(now) is None
    # Threads は Z 付きの UTC で返す
    utc = Reply("x", "t", "u", created_at="2026-10-01T00:00:00Z")
    assert utc.age_days(now) is not None
