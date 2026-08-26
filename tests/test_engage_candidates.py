"""返信候補の解析と評価のテスト。

実際に Threads の検索ページから取れた形をそのまま使う。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.engage.candidates import (
    JST,
    Candidate,
    parse_search_block,
    rank_candidates,
    score,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=JST)

# 実測したブロック（そのまま）
REAL = (
    "/@sana_oniku/post/DTVoI4xlSTZ",
    "sana_oniku\n2026/01/11\nまつげ下がる人向けのマストバイマスカラはこれ👁️\n"
    "「マツパ？」と5回聞かれた神コスメ🥹\n根元から圧倒的に伸びてマジで盛れる。\n"
    "216\n9\n1\n5",
)
REAL_BIG = (
    "/@ningeniji/post/DccFb1BEb0N",
    "ningeniji\n1日\nなんで最近のコスメの名前って\n「ごめんね素肌」\n"
    "「うそつき下地」\nもうちょい分かりやすくないのかな\n3.4万\n251\n254\n2",
)


# ======================================================================
# 解析
# ======================================================================
def test_parses_a_real_search_block():
    c = parse_search_block(*REAL, keyword="コスメ", now=NOW)
    assert c is not None
    assert c.username == "sana_oniku"
    assert c.shortcode == "DTVoI4xlSTZ"
    assert c.likes == 216
    assert c.replies == 9
    assert "マスカラ" in c.text
    # 反応数は本文に混ざらない
    assert "216" not in c.text


def test_parses_abbreviated_counts():
    c = parse_search_block(*REAL_BIG, now=NOW)
    assert c is not None
    assert c.likes == 34_000, "「3.4万」を数値にできていない"
    assert c.replies == 251


def test_parses_relative_timestamps():
    c = parse_search_block(*REAL_BIG, now=NOW)
    assert c is not None
    assert c.age_hours == pytest.approx(24.0)


def test_parses_absolute_timestamps():
    c = parse_search_block(*REAL, now=NOW)
    assert c is not None
    assert c.age_hours is not None and c.age_hours > 24 * 200


def test_permalink_is_rebuilt():
    c = parse_search_block(*REAL, now=NOW)
    assert c.permalink == "https://www.threads.com/@sana_oniku/post/DTVoI4xlSTZ"


@pytest.mark.parametrize("href", ["", "/@user/", "/search?q=x", "not-a-link"])
def test_rejects_non_post_links(href):
    assert parse_search_block(href, "テキスト\n1\n2", now=NOW) is None


def test_rejects_empty_body():
    assert parse_search_block("/@u/post/ABC", "u\n1日\n100\n5", now=NOW) is None


# ======================================================================
# 返していい投稿かどうか
# ======================================================================
def _c(text: str, **kw) -> Candidate:
    return Candidate(username="u", shortcode="S", text=text, **kw)


def test_sensitive_topics_are_skipped():
    """荒れる・失礼になる話題には返さない。"""
    for text in ("整形のダウンタイム中です、つらい" * 2,
                 "ダイエットで体重が落ちなくて悩んでる" * 2,
                 "地震があって不安な夜です、みなさん無事でしょうか"):
        assert not _c(text).is_repliable, text


def test_promotional_posts_are_skipped():
    text = "詳しくは公式LINEから！プロフから飛べます" * 2
    assert not _c(text).is_repliable


def test_short_posts_are_skipped():
    """短すぎる投稿は、何に触れて返せばいいか分からない。"""
    assert not _c("かわいい").is_repliable


def test_ordinary_beauty_post_is_repliable():
    c = _c("新作の化粧水、乾燥する時期に良さそうだったので気になってる。使った人いる？")
    assert c.is_repliable
    assert c.is_beauty


# ======================================================================
# 評価
# ======================================================================
def test_replies_count_more_than_likes():
    """コメント欄が動いている投稿を上に置く。

    自分の返信が読まれるのは、people が既に会話している投稿。
    """
    quiet = _c("コスメの新作、気になってるんだけど誰か使った？", likes=300, replies=0, age_hours=5)
    lively = _c("コスメの新作、気になってるんだけど誰か使った？", likes=100, replies=40, age_hours=5)
    assert score(lively) > score(quiet)


def test_fresh_posts_outrank_stale_ones():
    fresh = _c("スキンケアの話。乾燥がつらい季節ですね", likes=200, replies=10, age_hours=3)
    stale = _c("スキンケアの話。乾燥がつらい季節ですね", likes=200, replies=10, age_hours=200)
    assert score(fresh) > score(stale)


def test_beauty_posts_outrank_unrelated_ones():
    beauty = _c("コスメのメイク、スキンケアの話。今日の購入品です", likes=50, replies=5, age_hours=5)
    other = _c("今日のランチがおいしかったという話をします。とてもよかった", likes=50, replies=5, age_hours=5)
    assert score(beauty) > score(other)


def test_score_records_its_breakdown():
    c = _c("コスメの話をします。新作の化粧水が気になっている", likes=10, replies=2, age_hours=2)
    score(c)
    assert set(c.scores) == {"beauty", "momentum", "freshness"}


# ======================================================================
# 並べ替えと比率
# ======================================================================
def _many(n: int, *, beauty: bool) -> list[Candidate]:
    word = "コスメの新作が気になってる話" if beauty else "今日の天気と電車の遅延の話"
    tag = "b" if beauty else "o"
    return [
        Candidate(username=f"{tag}user{i}", shortcode=f"{tag.upper()}{i}",
                  text=f"{word}。長さを足すための文章をここに入れておきます。{i}",
                  likes=100 + i, replies=5, age_hours=5)
        for i in range(n)
    ]


def test_beauty_ratio_is_respected():
    picked = rank_candidates(_many(20, beauty=True) + _many(20, beauty=False),
                             limit=10, beauty_ratio=0.8)
    beauty = [c for c in picked if c.is_beauty]
    assert len(picked) == 10
    assert len(beauty) == 8, f"美容が {len(beauty)}件。8割になっていない"


def test_other_topics_are_included():
    """美容だけに固定しない。露出先を狭めすぎないため。"""
    picked = rank_candidates(_many(20, beauty=True) + _many(20, beauty=False),
                             limit=10, beauty_ratio=0.8)
    assert any(not c.is_beauty for c in picked)


def test_beauty_fills_in_when_other_topics_run_out():
    """美容以外が足りなければ美容で埋める。逆はしない（軸を守る）。"""
    picked = rank_candidates(_many(20, beauty=True), limit=10, beauty_ratio=0.8)
    assert len(picked) == 10
    assert all(c.is_beauty for c in picked)


def test_excluded_accounts_are_dropped():
    """同じ相手に張り付かない。"""
    pool = _many(10, beauty=True)
    picked = rank_candidates(pool, limit=10, exclude_usernames={"buser0", "buser1"})
    assert {c.username for c in picked}.isdisjoint({"buser0", "buser1"})


def test_already_replied_posts_are_dropped():
    pool = _many(10, beauty=True)
    picked = rank_candidates(pool, limit=10, exclude_shortcodes={"B3"})
    assert all(c.shortcode != "B3" for c in picked)


def test_duplicate_posts_appear_once():
    """同じ投稿が複数キーワードで拾えることがある。"""
    dup = _many(1, beauty=True) * 3
    assert len(rank_candidates(dup, limit=10)) == 1


def test_unrepliable_posts_never_rank():
    pool = _many(5, beauty=True)
    pool.append(_c("整形のダウンタイムがつらいという話をしています。とても長い本文",
                   likes=99999, replies=999, age_hours=1))
    picked = rank_candidates(pool, limit=10)
    assert all("整形" not in c.text for c in picked)
