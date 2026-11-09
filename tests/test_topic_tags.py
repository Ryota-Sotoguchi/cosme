"""トピックタグは記事の話題に合わせる。

## なぜこのテストが要るのか

**2026-11-09 まで、全投稿に同じタグ「キャリア」が付いていた。**

`facts.topic_tag_for` のカーソルが `state.recent_part_ids("closing")` の件数で、
長文の型（guide）は "closing" を記録しないため **8 で凍結**していた。
`8 // 2 % 3 = 1` なので、毎回 `TOPIC_TAGS["コスメ"][1]` = 「キャリア」。

しかも**履歴にタグを記録していなかった**ので、2か月気づかなかった。
フォロワーが9人のアカウントにとって、タグはおすすめ以外でほぼ唯一の
発見導線なのに、1つの話題にしか出ていなかったことになる。
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


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_every_article_has_a_tag(article):
    """**記事を足したらタグも足す。** 無いとタグ無しで投稿される。"""
    assert article.id in F.GUIDE_TAGS, f"{article.id} のタグが GUIDE_TAGS に無い"
    assert F.GUIDE_TAGS[article.id].strip(), f"{article.id} のタグが空"


@pytest.mark.parametrize("article", ALL_GUIDES, ids=[g.id for g in ALL_GUIDES])
def test_the_tag_is_a_plain_word(article):
    """Threads のタグは1投稿に1つ。記号や空白を混ぜない。"""
    tag = F.GUIDE_TAGS[article.id]
    for bad in ("#", " ", "　", "\n", ",", "、"):
        assert bad not in tag, f"{article.id}: タグに {bad!r} が入っている（{tag}）"
    assert len(tag) <= 10, f"{article.id}: タグが長い（{tag}）"


def test_the_article_decides_the_tag_not_the_category():
    """カテゴリー（商品のジャンル）から引くと、長文は全部同じタグになる。"""
    assert F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_taishoku_tsutaekata"}) == "退職"
    assert F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_keirekisho"}) == "職務経歴書"
    # 凍結していたカーソル（8）でも、記事が違えばタグは変わる
    assert (F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_online"})
            != F.topic_tag_for("コスメ", 8, "guide", {"guide": "g_fukugyou"}))


def test_the_site_and_a8_articles_are_tagged_too():
    """収益の導線にもタグが要る。ここが無タグだと誰にも見つからない。"""
    assert F.topic_tag_for("コスメ", 8, "guide_site", {"guide_site": "gs_koushou"}) == "年収交渉"
    assert F.topic_tag_for("コスメ", 8, "guide_a8",
                           {"guide_site": "gs_agaranai", "a8": "uzuz"}) == "年収"


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
