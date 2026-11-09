"""フォロー（2026-11-09 に本人がルールを変えて解禁した）。

## なぜ慎重に縛るのか

フォロワーが9人で、投稿が届く先がほぼ無い。フォローはこちらから相手の画面に
出る数少ない手段だが、**自動フォローは «業者» の典型的な signal** でもある。

この口座は原因不明の表示低下の最中（10/22 以降、1日で1/20になって戻っていない）。
ここで数を出すと、戻る見込みを自分で潰す。だから上限と «押さない条件» を
機械で縛る。

なお**プロフィール（表示名・自己紹介）の変更はいまも禁止**。解禁されたのは
フォローだけ。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.engage import follow as F

JST = timezone(timedelta(hours=9))


def _db(tmp_path: Path, rows):
    path = tmp_path / "engage.sqlite3"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE threads_seen_posts
                   (shortcode TEXT, username TEXT, source TEXT, first_seen_at TEXT,
                    likes INTEGER, decision TEXT)""")
    con.executemany("INSERT INTO threads_seen_posts VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_only_accounts_found_by_career_search_are_picked(tmp_path):
    """**タイムライン由来は入れない。** コスメ時代のフォロー先が混ざる。"""
    db = _db(tmp_path, [
        ("a", "career_person", "search", "2026-11-01", 50, "observed"),
        ("b", "cosme_person", "timeline", "2026-11-01", 900, "observed"),
    ])
    names = [c.username for c in F.candidates(db)]
    assert names == ["career_person"], names


def test_accounts_dropped_by_the_rules_are_not_followed(tmp_path):
    """センシティブ・宣伝で落とした投稿の相手は見ない。"""
    db = _db(tmp_path, [
        ("a", "ok_person", "search", "2026-11-01", 10, "observed"),
        ("b", "spam_person", "search", "2026-11-01", 900, "skipped_rule"),
    ])
    assert [c.username for c in F.candidates(db)] == ["ok_person"]


def test_the_most_read_accounts_come_first(tmp_path):
    db = _db(tmp_path, [
        ("a", "small", "search", "2026-11-01", 5, "observed"),
        ("b", "big", "search", "2026-11-01", 500, "observed"),
        ("c", "mid", "search", "2026-11-01", 80, "observed"),
    ])
    assert [c.username for c in F.candidates(db)] == ["big", "mid", "small"]


def test_quiet_accounts_can_be_filtered_out(tmp_path):
    db = _db(tmp_path, [
        ("a", "quiet", "search", "2026-11-01", 1, "observed"),
        ("b", "read", "search", "2026-11-01", 40, "observed"),
    ])
    assert [c.username for c in F.candidates(db, min_likes=3)] == ["read"]


def test_an_account_is_never_touched_twice(tmp_path):
    """**フォローと解除を繰り返すのが、いちばん悪い形。** 一度触ったら二度と見ない。"""
    db = _db(tmp_path, [
        ("a", "already", "search", "2026-11-01", 50, "observed"),
        ("b", "fresh", "search", "2026-11-01", 40, "observed"),
    ])
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    F.record(data, "already", "followed")
    assert [c.username for c in F.candidates(db, exclude=F.already_handled(data))] == ["fresh"]


def test_a_failed_attempt_is_also_remembered(tmp_path):
    """押せなかった相手も記録する。同じプロフィールを何度も開かない。"""
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    F.record(data, "x", "not_found")
    assert "x" in F.already_handled(data)
    # ただし «今日フォローした数» には入れない
    assert F.followed_today(data) == 0


def test_todays_count_only_counts_what_actually_went_through(tmp_path):
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    F.record(data, "a", "followed")
    F.record(data, "b", "already")
    F.record(data, "c", "error")
    assert F.followed_today(data) == 1


def test_yesterdays_follows_do_not_count_against_today(tmp_path):
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    path = data / "engage" / F.FOLLOWED_LOG
    old = (datetime.now(JST) - timedelta(days=1)).isoformat(timespec="seconds")
    path.write_text(json.dumps({"username": "a", "outcome": "followed", "at": old}) + "\n",
                    encoding="utf-8")
    assert F.followed_today(data) == 0


def test_the_running_total_stops_at_the_cap(tmp_path):
    """**一旦100人まで**（2026-11-10 に本人が決めた）。

    日の上限だけだと止まらない。通算で数えて、達したら止める。
    押せなかったぶんは数えない（«触った» と «フォローできた» は別）。
    """
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    for i in range(7):
        F.record(data, f"ok{i}", "followed")
    F.record(data, "ng", "not_found")
    F.record(data, "ng2", "error")
    assert F.followed_total(data) == 7
    # 触った数（二度と開かない相手）は、できなかったぶんも含む
    assert len(F.already_handled(data)) == 9


def test_the_running_total_survives_a_day_change(tmp_path):
    """通算は日付で消えない。ここを today で数えると上限が効かない。"""
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    path = data / "engage" / F.FOLLOWED_LOG
    old = (datetime.now(JST) - timedelta(days=30)).isoformat(timespec="seconds")
    path.write_text(json.dumps({"username": "a", "outcome": "followed", "at": old}) + "\n",
                    encoding="utf-8")
    assert F.followed_today(data) == 0
    assert F.followed_total(data) == 1


def test_the_cap_is_configured_not_hardcoded():
    """上限は config にある（本人が動かすのはそこだけ）。"""
    import tomllib

    raw = tomllib.loads(pathlib.Path("config/config.toml").read_text(encoding="utf-8"))
    section = raw["follow"]
    assert section["max_total"] == 100, "通算の上限が config から消えている"
    assert section["max_per_day"] <= section["max_total"]
    # **数は出しすぎない。** 1日の上限が大きいと «業者» の signal になる。
    # 2026-11-09 に本人の指示で 6 → 15 に上げた（それでも通算100で止まる）。
    # ここを20より上にするなら、Claude の判断ではなく本人の指示で
    assert section["max_per_day"] <= 20, "1日の上限が大きすぎる"


def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    path = data / "engage" / F.FOLLOWED_LOG
    path.write_text('{"username": "a", "outcome": "followed"}\nこわれた行\n'
                    '{"username": "b", "outcome": "followed"}\n', encoding="utf-8")
    assert F.already_handled(data) == {"a", "b"}


# ======================================================================
# 押すかどうかの判断
# ======================================================================

class _Page:
    """FIND_FOLLOW_JS の戻り値を順に返す板。"""

    def __init__(self, *states):
        self.states = list(states)
        self.clicked: list[str] = []

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        return self.states.pop(0) if self.states else {"state": "not_found"}

    def click(self, selector):
        self.clicked.append(selector)


class _Session:
    def __init__(self, page):
        self.page = page
        self.opened: list[str] = []

    def goto(self, url):
        self.opened.append(url)
        return self.page


def test_it_clicks_only_when_the_button_is_clearly_the_profiles_own():
    page = _Page({"state": "found", "label": "フォローする"}, {"state": "already"})
    assert F.follow_one(_Session(page), "someone") == "followed"
    assert page.clicked == ['[data-cosme-follow="1"]']


def test_it_does_not_click_when_the_page_cannot_be_read():
    """**見分けがつかないなら押さない。** 知らない人を勝手にフォローするほうが悪い。

    プロフィールには «おすすめのアカウント» のフォローボタンが10個ほど並ぶ。
    目印（メッセージボタン）が無い画面で押すと、見てもいない人をフォローする。
    """
    for state in ({"state": "no_anchor"}, {"state": "not_found", "labels": []}):
        page = _Page(state)
        assert F.follow_one(_Session(page), "someone") in ("no_anchor", "not_found")
        assert page.clicked == [], "見分けがつかないのに押している"


def test_an_already_followed_account_is_not_clicked():
    page = _Page({"state": "already", "label": "フォロー中"})
    assert F.follow_one(_Session(page), "someone") == "already"
    assert page.clicked == []


def test_a_click_that_changes_nothing_is_not_recorded_as_followed():
    """押しても変わらなければ «できた» と言わない。"""
    page = _Page({"state": "found"}, {"state": "found"})
    assert F.follow_one(_Session(page), "someone") == "unconfirmed"


def test_the_dry_run_never_touches_the_browser(tmp_path):
    db = _db(tmp_path, [("a", "x", "search", "2026-11-01", 50, "observed")])
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    planned = F.run(None, db_path=db, data_dir=data, limit=3, dry_run=True)
    assert planned and planned[0]["outcome"] == "（下書き）"
    assert not (data / "engage" / F.FOLLOWED_LOG).exists(), "下書きなのに記録している"


def test_the_live_run_spaces_the_accounts_out(tmp_path):
    db = _db(tmp_path, [(f"s{i}", f"u{i}", "search", "2026-11-01", 100 - i, "observed")
                        for i in range(3)])
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    waits: list[float] = []

    class Session:
        def goto(self, url):
            return _Page({"state": "found"}, {"state": "already"})

    F.run(Session(), db_path=db, data_dir=data, limit=3, dry_run=False, sleep=waits.append)
    assert len(waits) == 2, "間隔をあけずに続けて押している"
    assert all(F.INTERVAL_SECONDS[0] <= w <= F.INTERVAL_SECONDS[1] for w in waits), waits


def test_the_interval_is_not_shortened_by_accident():
    assert F.INTERVAL_SECONDS[0] >= 30, "間隔が短すぎる"


def test_one_failure_does_not_stop_the_run(tmp_path):
    db = _db(tmp_path, [(f"s{i}", f"u{i}", "search", "2026-11-01", 100 - i, "observed")
                        for i in range(3)])
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)

    class Session:
        def __init__(self):
            self.n = 0

        def goto(self, url):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("timeout")
            return _Page({"state": "found"}, {"state": "already"})

    out = F.run(Session(), db_path=db, data_dir=data, limit=3, dry_run=False, sleep=lambda s: None)
    assert [e["outcome"] for e in out] == ["followed", "error", "followed"]
