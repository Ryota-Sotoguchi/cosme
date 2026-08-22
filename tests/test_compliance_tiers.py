"""リンクの有無で NG表現の適用範囲が変わることのテスト。

規制の根拠が違うので分けている。詳しくは `src/compliance/rules.py` の
ALWAYS_RULES / LINK_ONLY_RULES のコメント。

  薬機法66条    … 何人も規制。「広告し、記述し、又は流布して」が対象。
                  リンクの有無に関係なく効く
  景表法・ステマ … 事業者が自己の供給する商品について行う表示が対象。
                  商品を売っていない投稿には効かない
"""

from __future__ import annotations

import pytest

from src.compliance.rules import ALWAYS_RULES, LINK_ONLY_RULES, scan


# ======================================================================
# リンクなし投稿で解放されるもの
# ======================================================================
# ここを縛っていたせいで、雑談で自分の生活を書くことすらできなかった。
# 商品名を出さない投稿は広告ではないので、普通に喋ってよい。
@pytest.mark.parametrize(
    "text",
    [
        "詰め替え買い忘れて、いま焦ってる",
        "この時期のヘアケア、ずっと同じの使ってる",
        "使ってみたけど、わたしにはちょっと重かった",
        "お気に入りです、この組み合わせ",
        "みんな何使ってる〜？",
        "SNSで話題になってるやつ、気になってる",
        "これは神アイテムだと思ってる",
        "バズってるの見て気になった",
        "買ってよかったって思えるのは、結局ちゃんと調べたやつ",
    ],
)
def test_no_link_posts_may_speak_freely(text):
    assert not scan(text, has_link=False), f"リンクなし投稿で止まるべきでない: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "使ってみたけど、わたしにはちょっと重かった",
        "買ってよかったって思えるのは、結局ちゃんと調べたやつ",
        "これは神アイテムだと思ってる",
        "みんな何使ってる〜？",
    ],
)
def test_link_posts_still_block_those(text):
    """同じ文でも、リンク投稿なら止まること。"""
    assert scan(text, has_link=True), f"リンク投稿では止まるべき: {text}"


# ======================================================================
# リンクの有無に関係なく止まるもの
# ======================================================================
@pytest.mark.parametrize(
    "text",
    [
        "この化粧水でシミが消える",       # 薬機法・効能の断定
        "肌荒れが治る",                   # 同上
        "必ず効きます",                   # 効能の保証
        "即効性があります",               # 同上
        "薬用クリームがおすすめ",         # 取扱い外カテゴリー
        "美容皮膚科でも使われている",     # 同上
        "アンチエイジングに効く",         # 薬機法
        "医師が推奨しています",           # 医療関与の示唆
    ],
)
def test_efficacy_claims_are_blocked_even_without_a_link(text):
    """薬機法66条は何人も規制。広告でなくても「記述・流布」が対象。

    リンクを外せば何でも書ける、ということにはならない。
    """
    assert scan(text, has_link=False), f"リンクの有無に関係なく止めるべき: {text}"
    assert scan(text, has_link=True)


# ======================================================================
# 分割そのものの健全性
# ======================================================================
def test_the_two_tiers_do_not_overlap():
    always = {r.label for r in ALWAYS_RULES}
    link_only = {r.label for r in LINK_ONLY_RULES}
    assert not (always & link_only), f"両方に入っているルール: {always & link_only}"


def test_every_rule_belongs_to_exactly_one_tier():
    from src.compliance.rules import ALL_RULES

    assert len(ALWAYS_RULES) + len(LINK_ONLY_RULES) == len(ALL_RULES), (
        "どちらのティアにも入っていないルールがある。"
        "新しいルールを足したら、どちらに属するか決めること"
    )


def test_scan_defaults_to_the_strict_side():
    """has_link を渡し忘れたら全部かかること。

    呼び出し側が指定を忘れたときに緩いほうへ倒れると、
    事故が静かに起きる。既定は厳しいほうにしておく。
    """
    text = "使ってみた"
    assert scan(text)
    assert scan(text, has_link=True)
    assert not scan(text, has_link=False)
