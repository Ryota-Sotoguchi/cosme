"""リンクなし専用の語彙が、リンク投稿に混入しないことのテスト。

`src/compliance/rules.py` で景表法系のルールをリンク投稿だけに絞った。
その前提で、つぶやき・トピック・ノウハウには
「使ってる」「お気に入り」「みんな」「神」を書けるようにしてある。

**この前提が崩れると本物の規制違反になる。**
崩れ方は2つ:

  1. これらのパーツがリンク付きの投稿に混ざる
  2. リンクなしのはずの投稿が、いつのまにか商品を持つようになる

どちらもテンプレートを触ったときに静かに起きうるので、ここで固定する。
"""

from __future__ import annotations

import pytest

from src.compliance.rules import LINK_ONLY_RULES, scan
from src.content import parts as P
from src.content.builder import ContentBuilder
from src.storage.state import State

NO_LINK_POOLS = {
    "CASUAL_MURMURS": P.CASUAL_MURMURS,
    "NO_LINK_TOPICS": P.NO_LINK_TOPICS,
    "HOWTO_POSTS": P.HOWTO_POSTS,
}


@pytest.mark.parametrize("post_type,template_id", [
    ("casual", "casual"),
    ("no_link", "topic"),
    ("howto", "howto"),
])
def test_no_link_post_types_never_carry_a_product(post_type, template_id, tmp_path):
    """リンクなし投稿が商品もリンクも持たないこと。

    景表法系のルールを外している根拠がこれ。
    商品を持った瞬間、外している前提が崩れる。
    """
    builder = ContentBuilder(State(tmp_path / "state.json"))
    draft = builder.build(post_type, [], template_id=template_id)

    assert not draft.items, f"{template_id} が商品を持っている"
    assert not draft.link_attachment, f"{template_id} がリンクを持っている"
    assert not draft.has_affiliate_link, f"{template_id} がアフィリエイトリンク扱いになっている"


def test_link_only_vocabulary_stays_out_of_link_post_parts():
    """リンク投稿で使うパーツ側に、解放した語彙が入っていないこと。

    書き出し・締め・CTA・注記は商品と同じ投稿に出るので、
    ここに「使ってる」「みんな」が入ったら本物の違反になる。
    """
    link_pools = {
        "PRODUCT_OPENINGS": P.PRODUCT_OPENINGS,
        "PRODUCT_CLOSINGS": P.PRODUCT_CLOSINGS,
        "RECOMMEND_OPENINGS": P.RECOMMEND_OPENINGS,
        "RECOMMEND_CLOSINGS": P.RECOMMEND_CLOSINGS,
        "CTA_PARTS": P.CTA_PARTS,
        "DISCLAIMERS": P.DISCLAIMERS,
        "FACT_INTROS": P.FACT_INTROS,
        "THREAD_BRIDGES": P.THREAD_BRIDGES,
        "ROUNDUP_CLOSINGS": P.ROUNDUP_CLOSINGS,
    }
    for name, pool in link_pools.items():
        for part in pool:
            text = part.text.replace("{category}", "スキンケア")
            hits = [r for r in LINK_ONLY_RULES if r.pattern.search(text)]
            assert not hits, f"{name}.{part.id}: {[h.label for h in hits]}"


def test_no_link_pools_still_respect_the_pharmaceutical_act():
    """解放したのは景表法系だけ。薬機法は外していないこと。

    薬機法66条は何人も規制で、「広告し、記述し、又は流布して」が対象。
    リンクを外せば効能を語ってよい、ということにはならない。
    """
    for name, pool in NO_LINK_POOLS.items():
        for part in pool:
            hits = scan(part.text, has_link=False)
            assert not hits, f"{name}.{part.id}: {[h.label for h in hits]}"
