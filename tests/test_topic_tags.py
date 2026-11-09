"""トピックタグは記事の話題に合わせる。主軸は1つ、副ジャンルは広く。

## なぜこのテストが要るのか

**2026-11-09 まで、全投稿に同じタグ「キャリア」が付いていた。**

`facts.topic_tag_for` のカーソルが `state.recent_part_ids("closing")` の件数で、
長文の型（guide）は "closing" を記録しないため **8 で凍結**していた。
`8 // 2 % 3 = 1` なので、毎回 `TOPIC_TAGS["コスメ"][1]` = 「キャリア」。

しかも**履歴にタグを記録していなかった**ので、2か月気づかなかった。
フォロワーが9人のアカウントにとって、タグはおすすめ以外でほぼ唯一の
発見導線なのに、1つの話題にしか出ていなかったことになる。

## 2026-11-09（2回目）

本人の指示: 「主軸は仕事や転職、年収ですが、副ジャンルは超幅広く」。
記事1本にタグの候補を複数持たせ、**先頭を主軸、2つめ以降を副ジャンル**にして
日替わりで回す。副ジャンルも**その記事に本当に書いてあることだけ**。
"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

import pytest

from src.content import facts as F
from src.content.builder import ContentBuilder
from src.content.guides import GUIDES, SITE_GUIDES
from src.storage.state import State

ALL_GUIDES = list(GUIDES) + list(SITE_GUIDES)

# 日付で回すので、確かめるときは日を固定する
DAY = datetime.date(2026, 11, 10).toordinal()


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_every_article_has_a_tag(article):
    """**記事を足したらタグも足す。** 無いとタグ無しで投稿される。"""
    assert article.id in F.GUIDE_TAGS, f"{article.id} のタグが GUIDE_TAGS に無い"
    tags = F.GUIDE_TAGS[article.id]
    assert isinstance(tags, tuple) and tags, f"{article.id} のタグが空"
    assert len(set(tags)) == len(tags), f"{article.id}: 同じタグが重複している（{tags}）"


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_the_tag_is_a_plain_word(article):
    """Threads のタグは1投稿に1つ。記号や空白を混ぜない。"""
    for tag in F.GUIDE_TAGS[article.id]:
        for bad in ("#", " ", "　", "\n", ",", "、"):
            assert bad not in tag, f"{article.id}: タグに {bad!r} が入っている（{tag}）"
        assert tag.strip(), f"{article.id}: 空のタグがある"
        assert len(tag) <= 10, f"{article.id}: タグが長い（{tag}）"


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_the_first_tag_is_the_main_axis(article):
    """**主軸は仕事・転職・年収**（2026-11-09 の本人の指示）。

    先頭が主軸でないと、幅を広げたぶんだけ «何のアカウントか» が消える。
    """
    first = F.GUIDE_TAGS[article.id][0]
    assert first in F.MAIN_AXIS_TAGS, f"{article.id}: 先頭が主軸でない（{first}）"


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_every_article_also_reaches_outside_the_main_axis(article):
    """**副ジャンルは超幅広く**（同じ指示）。

    主軸の語だけだと、いつも同じ層にしか出ない。
    記事に本当に書いてあることの範囲で、外へ1つは出す。
    """
    tags = F.GUIDE_TAGS[article.id]
    outside = [t for t in tags if t not in F.MAIN_AXIS_TAGS]
    assert outside, f"{article.id}: 副ジャンルが無い（{tags}）"


def test_the_sub_genres_are_actually_wide():
    """2週間ぶんを並べて、幅が出ていること。細いと指示を満たしていない。"""
    seen = set()
    for day in range(DAY, DAY + 14):
        for article in ALL_GUIDES:
            seen.add(F.guide_tag({"guide": article.id}, day))
    outside = {t for t in seen if t not in F.MAIN_AXIS_TAGS}
    assert len(seen) >= 50, f"タグの種類が少ない（{len(seen)}）: {sorted(seen)}"
    assert len(outside) >= 25, f"副ジャンルが狭い（{len(outside)}）: {sorted(outside)}"


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_the_same_article_changes_tag_by_day(article):
    """同じ記事を2度出すとき、同じタグで出さない。

    **カーソルは日付。** state の件数にすると、記録しない型があるだけで
    凍結する（それが «2か月ずっとキャリア» の原因だった）。
    """
    tags = F.GUIDE_TAGS[article.id]
    rolled = {F.guide_tag({"guide": article.id}, DAY + d) for d in range(len(tags))}
    assert rolled == set(tags), f"{article.id}: 日替わりで回っていない（{rolled}）"


def test_the_tag_always_comes_from_that_article():
    """**知らない話題のタグを付けない。** 中身と違うタグは離脱を生む。"""
    for day in range(DAY, DAY + 30):
        for article in ALL_GUIDES:
            tag = F.guide_tag({"guide": article.id}, day)
            assert tag in F.GUIDE_TAGS[article.id], f"{article.id}: {tag} は記事の候補に無い"


def test_the_article_decides_the_tag_not_the_category():
    """カテゴリー（商品のジャンル）から引くと、長文は全部同じタグになる。"""
    assert F.topic_tag_for(
        "コスメ", 8, "guide", {"guide": "g_taishoku_tsutaekata"}, DAY
    ) in F.GUIDE_TAGS["g_taishoku_tsutaekata"]
    # 凍結していたカーソル（8）でも、記事が違えばタグは変わる
    assert (F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_online"}, DAY)
            != F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_fukugyou"}, DAY))


def test_the_site_and_a8_articles_are_tagged_too():
    """収益の導線にもタグが要る。ここが無タグだと誰にも見つからない。"""
    assert F.topic_tag_for(
        "コスメ", 8, "guide_site", {"guide_site": "gs_koushou"}, DAY
    ) in F.GUIDE_TAGS["gs_koushou"]
    assert F.topic_tag_for(
        "コスメ", 8, "guide_a8", {"guide_site": "gs_agaranai", "a8": "uzuz"}, DAY
    ) in F.GUIDE_TAGS["gs_agaranai"]


@pytest.mark.parametrize("category", ["手帳", "ノート・文具", "ビジネスバッグ"])
def test_the_tool_categories_have_their_own_tags(category):
    """**手帳の投稿に「キャリア」が付いていた**（2026-11-09 まで）。

    道具のカテゴリーが TOPIC_TAGS に無いので、商品の無い投稿用の
    既定（転職・キャリア・年収）に落ちていた。
    """
    assert category in F.TOPIC_TAGS, f"{category} のタグが無い"
    tags = set(F.TOPIC_TAGS[category])
    assert not (tags & {"転職", "キャリア", "年収"}), f"{category}: 道具の投稿に転職のタグ（{tags}）"
    rolled = {F.topic_tag_for(category, 8, "product", {}, DAY + d) for d in range(len(tags))}
    assert rolled == tags, f"{category}: 日替わりで回っていない（{rolled}）"


def test_a_post_without_an_article_falls_back_to_the_category():
    """商品投稿は従来どおりカテゴリーから引く。"""
    assert F.topic_tag_for("転職本", 0, "product", {}) is not None
    assert F.topic_tag_for("コスメ", 0, "casual", {}) is None


def test_an_unknown_article_gets_no_tag_instead_of_a_wrong_one():
    """**知らない記事に «それらしいタグ» を付けない。** 中身と違うタグは離脱を生む。"""
    assert F.guide_tag({"guide": "g_not_registered"}) is None
    assert F.guide_tag({}) is None
    assert F.guide_tag(None) is None


def test_the_tags_spread_across_topics_in_a_real_run():
    """**同じタグばかりにならないこと。** これが2か月壊れていた中身。"""
    tmp = Path(tempfile.mkdtemp())
    state = State(tmp / "state.json")
    builder = ContentBuilder(state, voices_path=tmp / "voices.json")

    tags = []
    for i in range(12):
        draft = builder.build("guide", [], with_affiliate_link=False,
                              today=datetime.date(2026, 11, 9) + datetime.timedelta(days=i))
        state.record_part_ids(draft.part_ids)
        tags.append(draft.topic_tag)

    assert all(tags), f"タグの付かない投稿がある: {tags}"
    assert len(set(tags)) >= 5, f"タグが偏っている（{len(set(tags))}種類）: {tags}"


def test_the_tag_is_recorded_in_history():
    """**記録していないと、壊れていても気づけない。** 実際に2か月気づかなかった。"""
    import inspect

    from src import main

    source = inspect.getsource(main)
    assert '"topic_tag"' in source, "履歴の extra にタグを記録していない"
