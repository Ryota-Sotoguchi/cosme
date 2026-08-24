"""リンクなし連投のテスト。

タイムラインに出るのは1本目だけ。そこで完結させると2本目が読まれない。
「で、どういうこと？」と思わせて進ませるのが狙い。

参考アカウントはこの型でリーチを伸ばしている。

  1本目  死ぬまで言うけど、たるみは皮膚が伸びたんじゃない。
  2本目  ハイフはSMASの層に熱を入れて…（理屈の分解）

リンクが無いので Threads に表示回数を落とされない。
景表法系のルールも効かない（商品を売っていないため）。
ただし薬機法は効くので、効能は語らない。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from src.compliance.rules import scan
from src.content.builder import ContentBuilder
from src.content.parts import THREAD_TOPICS
from src.storage.state import State


def _builder() -> ContentBuilder:
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ContentBuilder(State(tmp / "state.json"))


# ======================================================================
# 形
# ======================================================================
def test_every_thread_topic_has_at_least_two_segments():
    for part in THREAD_TOPICS:
        assert len(part.segments) >= 2, f"{part.id} が連投になっていない"


def test_first_segment_is_short_enough_to_read_in_the_feed():
    """1本目は一目で読める長さにする。

    タイムラインで長文を見せられると、そこで止まる。
    """
    for part in THREAD_TOPICS:
        first = part.segments[0]
        assert len(first) <= 45, f"{part.id} の1本目が長い（{len(first)}字）:\n{first}"
        assert "\n" not in first.strip(), f"{part.id} の1本目が複数行になっている"


def test_first_segment_asserts_something():
    """1本目が言い切りであること。

    「〜かも」で始まる連投は、続きを読む理由が無い。
    """
    hedge = ("かも", "気がする", "かもしれ")
    for part in THREAD_TOPICS:
        first = part.segments[0]
        assert not any(h in first for h in hedge), (
            f"{part.id} の1本目がぼかしている:\n{first}"
        )
        assert first.rstrip().endswith(("。", "！", "？")), (
            f"{part.id} の1本目が言い切りで終わっていない:\n{first}"
        )


def test_second_segment_adds_substance():
    """2本目が1本目より厚いこと。

    2本目が短いと、分けた意味が無い。
    """
    for part in THREAD_TOPICS:
        assert len(part.segments[1]) > len(part.segments[0]), (
            f"{part.id} の2本目が1本目より短い。分ける意味が無い"
        )


def test_segments_fit_the_post_limit():
    for part in THREAD_TOPICS:
        for i, seg in enumerate(part.segments, 1):
            assert len(seg) <= 500, f"{part.id} の{i}本目が500字超"


# ======================================================================
# 中身
# ======================================================================
def test_thread_topics_pass_compliance_for_no_link_posts():
    """リンクが無い前提の検査を通ること。薬機法は外していない。"""
    for part in THREAD_TOPICS:
        hits = scan(part.text, has_link=False)
        assert not hits, f"{part.id}: {[h.label for h in hits]}"


def test_thread_topics_contain_no_digits():
    """商品データを持たないので、数値の裏取りができない。

    データ整合性チェックの抜け道を作らないため、数値は書かせない。
    """
    for part in THREAD_TOPICS:
        assert not any(c.isdigit() for c in part.text), f"{part.id} に数値"


def test_thread_topic_ids_are_unique():
    ids = [p.id for p in THREAD_TOPICS]
    assert len(set(ids)) == len(ids)


def test_there_are_enough_thread_topics_to_rotate():
    assert len(THREAD_TOPICS) >= 8


# ======================================================================
# 組み立て
# ======================================================================
def test_builder_produces_a_thread_without_a_link():
    builder = _builder()
    draft = builder.build("thread_topic", [], template_id="topic_thread")

    assert len(draft.segments) >= 2, "連投になっていない"
    assert not draft.has_affiliate_link
    assert not draft.link_attachment
    assert not draft.items
    assert "#PR" not in draft.text, "リンクが無いのに広告表示が付いている"


def test_consecutive_thread_topics_differ():
    builder = _builder()
    seen = set()
    for _ in range(4):
        draft = builder.build("thread_topic", [], template_id="topic_thread")
        seen.add(draft.segments[0])
        builder.state.record_part_ids(draft.part_ids)
    assert len(seen) >= 3, f"4本で {len(seen)} 種類しか出ていない"


@pytest.mark.parametrize("post_type", ["thread_topic"])
def test_thread_topic_is_a_known_post_type(post_type):
    from src.pipeline import ITEMS_NEEDED

    assert post_type in ITEMS_NEEDED
    assert ITEMS_NEEDED[post_type] == 0, "商品を必要としない投稿タイプであること"
