"""おすすめの分析（2026-11-11 に追加）。

本人の指示: 「おすすめに乗ってる投稿を常に分析してほしい。特に伸びているものは
監視対象にしてもいいし、そこから取り入れるのはありかと」。

## このテストが見張っていること

* **取り入れていいのは «形» だけ。** 他人の本文を写さない（CLAUDE.md §4）
* **他人の本文を公開される場所に出さない。** `data/engage/` は gitignore 済み。
  `research/`（コミットされる）には書かない
* **測り方の嘘を出さない。** 2026-11-11 に2つ踏んだ:
  1. 1行目が7字 → 中を見たら「仕事」「Cats of Threads」で、本文ではなく
     Threads のトピックタグだった。«1行目を短くしろ» と読むと間違える
  2. 自分のタグが0% → タグは本文ではなく API の `topic_tag` に渡すメタ情報。
     本文から探せば当然0%になる
* **分析用のテーブルを返信の経路に混ぜない。** `threads_seen_posts` に入れると
  自動返信が «もう見た» として候補から外す
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engage import trends as T

JST = timezone(timedelta(hours=9))


@dataclass
class FakePost:
    shortcode: str
    username: str = "someone"
    text: str = "本文"
    likes: int = 0
    replies: int = 0
    age_hours: float | None = 1.0
    source: str = "timeline"


def _con(tmp_path: Path) -> sqlite3.Connection:
    return T.connect(tmp_path / "engage.sqlite3")


# ======================================================================
# トピックタグを本文と間違えない
# ======================================================================
def test_the_leading_topic_tag_is_split_off():
    """**1行目はトピックタグのことがある。**

    実測で伸びていた投稿の1行目は「仕事」「お弁当」「Cats of Threads」で、
    どれも Threads が投稿の上に出すタグだった。
    """
    tag, body = T.split_tag("仕事\nこっちは敬語なのにタメ口を使ってくる年上の新人。")
    assert tag == "仕事"
    assert body.startswith("こっちは敬語")


def test_a_real_first_line_is_not_mistaken_for_a_tag():
    """長い1行目・句点のある1行目・1行だけの投稿はタグにしない。"""
    assert T.split_tag("退職を伝える前の晩、心臓がうるさくて眠れなかった\n2回とも。")[0] == ""
    assert T.split_tag("仕事。\n本文")[0] == ""
    assert T.split_tag("これは質問ですか？\n本文")[0] == ""
    assert T.split_tag("タグだけ")[0] == ""
    assert T.split_tag("")[0] == ""


def test_the_shape_is_measured_on_the_body_not_the_tag():
    """タグを混ぜると «1行目が短い» という嘘が出る。"""
    tagged = T.shape_of(["仕事\n" + "あ" * 40])
    plain = T.shape_of(["あ" * 40])
    assert tagged.first_line == plain.first_line == 40
    assert tagged.tagged == 1.0
    assert plain.tagged == 0.0


def test_our_own_tags_come_from_the_metadata_not_the_text():
    """**自分のタグは本文に入らない**（API の topic_tag）。

    本文から探すと «自分はタグ0%» になり、«タグを付けろ» という
    すでに直してある指摘が出る（2026-11-09 に直した）。
    """
    texts = ["退職を伝える前の晩、眠れなかった。\n2回とも。"] * 4
    assert T.shape_of(texts).tagged == 0.0
    assert T.shape_of(texts, tags=["退職", "退職", "", "面接"]).tagged == 0.75


# ======================================================================
# 字数
# ======================================================================
def test_unknown_lengths_are_not_counted_as_short():
    """保存した本文は頭だけ。**切れた本文の長さを字数として数えない。**"""
    texts = ["あ" * 200, "い" * 200]
    assert T.shape_of(texts, lengths=[0, 0]).chars == 0        # 不明は 0（表示は «—»）
    assert T.shape_of(texts, lengths=[0, 900]).chars == 900    # 分かる行だけで出す
    assert T.shape_of(texts).chars == 200                      # 長さを渡さなければ本文で数える


def test_an_empty_pool_does_not_crash():
    shape = T.shape_of([])
    assert shape.n == 0
    assert "—" in shape.row("空")


# ======================================================================
# 集める
# ======================================================================
def test_the_trend_table_is_separate_from_the_reply_path(tmp_path):
    """**自動返信の «見た» 記録に混ぜない。**

    混ぜると、分析のために深くスクロールした結果で返信先を潰す
    （`store.seen_shortcodes` が候補から外す）。
    """
    con = _con(tmp_path)
    T.record(con, [FakePost("abc", likes=10)])
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "threads_trend_posts" in names
    assert "threads_seen_posts" not in names


def test_seeing_a_post_again_keeps_the_higher_like_count(tmp_path):
    """伸びは後から見たい。**数字を下げて上書きしない。**"""
    con = _con(tmp_path)
    T.record(con, [FakePost("abc", likes=500)])
    T.record(con, [FakePost("abc", likes=3)])
    assert con.execute("SELECT likes FROM threads_trend_posts").fetchone()[0] == 500


def test_a_post_without_a_shortcode_is_skipped(tmp_path):
    con = _con(tmp_path)
    assert T.record(con, [FakePost("")]) == 0


def test_the_true_length_is_recorded(tmp_path):
    """本文は400字で切って持つが、**字数は切る前のものを残す。**"""
    con = _con(tmp_path)
    T.record(con, [FakePost("abc", text="あ" * 900)])
    row = con.execute("SELECT text_len, text_head FROM threads_trend_posts").fetchone()
    assert row[0] == 900
    assert len(row[1]) == 400


def test_old_rows_go_but_the_big_ones_stay(tmp_path):
    """**よく伸びた投稿は残す**（後から手本として見たい）。"""
    con = _con(tmp_path)
    old = (datetime.now(JST) - timedelta(days=200)).isoformat(timespec="seconds")
    with con:
        con.executemany(
            "INSERT INTO threads_trend_posts (shortcode, username, first_seen_at,"
            " last_seen_at, likes) VALUES (?,?,?,?,?)",
            [("small", "a", old, old, 3), ("big", "b", old, old, 9000)],
        )
    T.prune(con, keep_days=120, keep_likes=500)
    left = {r[0] for r in con.execute("SELECT shortcode FROM threads_trend_posts")}
    assert left == {"big"}


def test_seeding_takes_only_the_home_timeline(tmp_path):
    """取り込むのはホームTLの行だけ。検索由来は別の材料として読む。"""
    con = _con(tmp_path)
    with con:
        con.executescript(
            "CREATE TABLE threads_seen_posts (shortcode TEXT, username TEXT,"
            " first_seen_at TEXT, last_seen_at TEXT, likes INTEGER, replies INTEGER,"
            " age_hours REAL, source TEXT, text_head TEXT);")
        con.executemany(
            "INSERT INTO threads_seen_posts VALUES (?,?,?,?,?,?,?,?,?)",
            [("h", "a", "2026-11-01", "2026-11-01", 10, 1, 2.0, "timeline", "本文"),
             ("s", "b", "2026-11-01", "2026-11-01", 20, 2, 3.0, "search", "本文")],
        )
    T.seed_from_seen(con)
    rows = con.execute(
        "SELECT shortcode, text_len FROM threads_trend_posts").fetchall()
    assert [r[0] for r in rows] == ["h"]
    # 取り込んだ行の本文は200字で切れている。字数は «不明» にしておく
    assert rows[0][1] == 0


def test_seeding_without_the_reply_tables_is_quiet(tmp_path):
    """自動返信を一度も動かしていない環境でも落ちない。"""
    assert T.seed_from_seen(_con(tmp_path)) == 0


# ======================================================================
# 監視対象
# ======================================================================
def _add(con, rows):
    now = datetime.now(JST).isoformat(timespec="seconds")
    with con:
        con.executemany(
            "INSERT INTO threads_trend_posts (shortcode, username, first_seen_at,"
            " last_seen_at, likes) VALUES (?,?,?,?,?)",
            [(sc, user, now, now, likes) for sc, user, likes in rows],
        )


def test_a_one_hit_account_is_not_watched(tmp_path):
    """**一発だけ伸びた人は入れない。** 何度も伸びている人を見る。"""
    con = _con(tmp_path)
    _add(con, [("a1", "once", 9000), ("b1", "often", 800), ("b2", "often", 600)])
    names = [r["username"] for r in T.risers(con, min_likes=300, min_posts=2)]
    assert names == ["often"]


def test_quiet_accounts_are_not_watched(tmp_path):
    con = _con(tmp_path)
    _add(con, [("a1", "quiet", 5), ("a2", "quiet", 8)])
    assert T.risers(con, min_likes=300, min_posts=2) == []


def test_the_watchlist_reports_only_the_new_names(tmp_path):
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    rows = [{"username": "a", "hits": 2, "best": 900, "last_seen": "2026-11-11"}]
    assert T.update_watchlist(data, rows) == ["a"]
    assert T.update_watchlist(data, rows) == []      # 2回目は新規ではない
    assert set(T.load_watchlist(data)) == {"a"}


def test_a_broken_watchlist_line_does_not_lose_the_rest(tmp_path):
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    path = data / "engage" / T.WATCHLIST
    path.write_text('{"username": "a"}\nこわれた行\n{"username": "b"}\n',
                    encoding="utf-8")
    assert set(T.load_watchlist(data)) == {"a", "b"}


# ======================================================================
# 自分の投稿
# ======================================================================
def test_own_posts_read_the_first_post_of_a_thread(tmp_path):
    """おすすめに出るのは連投の1本目。**全文をつないで数えない。**"""
    path = tmp_path / "history.jsonl"
    now = datetime.now(JST).isoformat(timespec="seconds")
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"status": "success", "posted_at": now, "text": "1本目\n\n\n2本目",
         "extra": {"topic_tag": "退職"}},
        {"status": "skipped", "posted_at": now, "text": "出していない"},
        {"status": "success", "posted_at": "2026-01-01T00:00:00+09:00", "text": "古い"},
    ]) + "\n", encoding="utf-8")
    posts = T.own_posts(path, days=14)
    assert posts == [("1本目", "退職")]


def test_own_posts_without_a_file_is_empty(tmp_path):
    assert T.own_posts(tmp_path / "none.jsonl") == []


# ======================================================================
# 発信ジャンルの中で伸びているもの
# ======================================================================
def test_the_genre_corpus_comes_from_the_search_rows(tmp_path):
    """**おすすめは興味とフォロー先で決まる。**

    2026-11-11 の実測で、おすすめの上位15のうち仕事の話は1本だけで、
    残りはお弁当・猫・アイドルだった。«このジャンルで何が伸びるか» は
    転職の語で検索した結果から見る。
    """
    con = _con(tmp_path)
    with con:
        con.executescript(
            "CREATE TABLE threads_seen_posts (shortcode TEXT, username TEXT,"
            " likes INTEGER, replies INTEGER, text_head TEXT, source TEXT,"
            " first_seen_at TEXT);")
        now = datetime.now(JST).isoformat(timespec="seconds")
        con.executemany(
            "INSERT INTO threads_seen_posts VALUES (?,?,?,?,?,?,?)",
            [("s", "career", 700, 15, "転職の話", "search", now),
             ("h", "cat", 9000, 50, "猫の話", "timeline", now)],
        )
    assert [r["username"] for r in T.genre_top(con)] == ["career"]


def test_the_genre_corpus_is_optional(tmp_path):
    """自動返信を動かしていない環境では黙って空を返す。"""
    assert T.genre_top(_con(tmp_path)) == []


# ======================================================================
# 差の出し方
# ======================================================================
def test_small_differences_are_not_reported():
    """標本が薄いうちに形を合わせにいって失敗した前例がある。

    幅の小さい差は出さない（出すと «直す» 側に引っぱられる）。
    """
    a = T.shape_of(["あ" * 100 + "？"])
    assert T.differences(a, a) == []


def test_a_difference_names_the_field_not_the_text():
    """**差として出すのは形の名前と数字だけ。** 他人の本文は入れない。"""
    hot = T.shape_of(["これは質問ですか？"] * 10)
    own = T.shape_of(["これは説明です。"] * 10)
    gaps = T.differences(hot, own)
    assert gaps and all("質問です" not in g for g in gaps)
    assert any("問いかけ" in g for g in gaps)


def test_nothing_is_reported_without_a_sample():
    assert T.differences(T.shape_of([]), T.shape_of(["あ"])) == []


# ======================================================================
# 他人の本文を公開される場所に出さない
# ======================================================================
def test_the_analysis_never_writes_to_a_committed_folder():
    """`research/` はコミットされる。**他人の本文の置き場にしない。**

    保存先は `data/engage/`（gitignore 済み）だけ。表示は端末に出すだけ。
    """
    import ast

    source = Path("src/engage/trends.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # docstring は «書かない» と説明しているので、**コードの文字列だけ**を見る
    docs = {id(ast.get_docstring(node, clean=False))
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))}
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in {ast.get_docstring(m, clean=False)
                                    for m in ast.walk(tree)
                                    if isinstance(m, (ast.Module, ast.ClassDef,
                                                      ast.FunctionDef))}]
    assert docs  # docstring を取れていること（取れないと検査が空振りする）
    for text in literals:
        assert "research" not in text, f"コードが research/ を指している: {text!r}"
    assert 'data_dir / "engage"' in source


def test_the_watchlist_holds_names_not_posts(tmp_path):
    """監視対象に残すのはアカウント名と数字。**本文は残さない。**"""
    data = tmp_path / "data"
    (data / "engage").mkdir(parents=True)
    T.update_watchlist(data, [{"username": "a", "hits": 2, "best": 900,
                               "last_seen": "2026-11-11", "text_head": "本文"}])
    saved = (data / "engage" / T.WATCHLIST).read_text(encoding="utf-8")
    assert "本文" not in saved
    assert "a" in saved
