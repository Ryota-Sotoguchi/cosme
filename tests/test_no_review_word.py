"""**投稿文に「レビュー」を出さない。**

## なぜ

このアカウントの仕事は «日常で役に立つこと» を言って、有益だと見なされる
こと。アフィリエイトリンクは「誰が貼ったか」で押されるので、そこが崩れると
収益目標そのものが崩れる。レビューの要約は、その «役に立つこと» の形を
していない。

## 言い換えもしない

「買った人が多い」「たくさんの人が選んでる」への置換は**採らない**。
compliance は通るが、レビュー ⊆ 購入者なので API から取れる事実を超えた
推測になり、CLAUDE.md §4 の «API から取得していない事実の補完» に触れる。

## データは残す

`scoring.weights.review_count` / `min_review_count` / `min_review_average` は
そのまま。**客観性の背骨は、読者から見えない場所に残す。**
消すのは文言だけ。

このテストが無いと、次に触る人が scoring の重みを見て
「レビュー2,000件」を CTA に書き戻す。
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from src.content import facts as F
from src.content import parts as P
from src.content import templates as T
from src.content.builder import ContentBuilder
from src.content.parts import Part
from src.storage.state import State
from tests.conftest import make_item

# 「レビュー」そのものと、同じことを別の字で言ったもの。
FORBIDDEN = ("レビュー", "レヴュー", "口コミ", "クチコミ", "くちこみ")


def offending(text: str) -> list[str]:
    return [w for w in FORBIDDEN if w in text]


# ======================================================================
# 文章パーツの全プール
#
# **プールを列挙せず、モジュールから拾う。** 新しいプールを足した人が
# このテストに追記し忘れても、勝手に検査対象になる。
# ======================================================================
def all_parts() -> list[tuple[str, Part]]:
    found: list[tuple[str, Part]] = []
    for name, value in vars(P).items():
        if name.startswith("_"):
            continue
        if isinstance(value, tuple) and value and all(isinstance(v, Part) for v in value):
            found.extend((f"{name}.{p.id}", p) for p in value)
        elif isinstance(value, dict):
            for key, pool in value.items():
                if isinstance(pool, tuple) and pool and all(isinstance(v, Part) for v in pool):
                    found.extend((f"{name}[{key}].{p.id}", p) for p in pool)
    return found


def test_the_scan_actually_finds_the_pools():
    """列挙が空振りしていたら、このファイルは何も見ていない。"""
    found = all_parts()
    assert len(found) > 300, f"プールを拾えていない（{len(found)}件）"
    ids = {name for name, _ in found}
    for expected in ("NO_LINK_TOPICS", "HOWTO_POSTS", "CTA_PARTS",
                     "PRODUCT_OPENINGS", "PRODUCT_CLOSINGS", "QUESTION_POSTS",
                     "THREAD_TOPICS", "CASUAL_MURMURS", "ROUNDUP_OPENINGS"):
        assert any(i.startswith(expected) for i in ids), f"{expected} を見ていない"


def test_no_phrase_part_mentions_reviews():
    for name, part in all_parts():
        for text in (part.text, *part.segments):
            hit = offending(text)
            assert not hit, f"{name} に {hit}: {text[:60]}"


# ======================================================================
# テンプレート側
# ======================================================================
def test_the_generic_tips_do_not_mention_reviews():
    """剤形が分からない商品で使う、買い方だけのノウハウ段。"""
    for tip in T.GENERIC_TIPS:
        assert not offending(tip), tip


def test_the_benefit_pools_do_not_mention_reviews():
    from src.content import benefits as B

    for name, value in vars(B).items():
        if name.startswith("_"):
            continue
        for text in _strings(value):
            assert not offending(text), f"benefits.{name}: {text[:60]}"


def _strings(value, depth: int = 0):
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, (tuple, list, set)):
        for v in value:
            yield from _strings(v, depth + 1)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)


# ======================================================================
# 事実の描画
#
# ここが本丸だった。build_facts は「・レビュー1,284件、平均4.4」を
# **まとめ系投稿では必ず**出していた。
# ======================================================================
@pytest.mark.parametrize("count,average", [
    (0, None), (1, 3.0), (12, 4.05), (300, 4.5), (1284, 4.42), (99999, 5.0),
])
def test_the_fact_block_never_renders_reviews(count, average):
    item = make_item(review_count=count, review_average=average)
    rendered = "\n".join(F.build_facts(item).lines)
    assert not offending(rendered), rendered


@pytest.mark.parametrize("style", range(8))
def test_the_one_line_facts_never_render_reviews(style):
    for count, average in ((0, None), (1284, 4.42), (300, 4.0)):
        item = make_item(review_count=count, review_average=average)
        text, _ = F.sentence_facts(item, style)
        assert not offending(text), text


def test_the_numbers_stay_allowed_even_though_they_are_not_shown():
    """**選定とスコアリングでは今までどおり使う。**

    allowed_numbers は «この商品について語ってよい数字» の集合。
    表示しなくなっても外さない（データ整合性の検査がここを見る）。
    """
    item = make_item(review_count=1284, review_average=4.4)
    allowed = F.build_facts(item).allowed_numbers
    assert "1284" in allowed
    assert any("4.4" in a for a in allowed)


# ======================================================================
# 実際に出来上がる投稿
# ======================================================================
# **型を列挙しない。** TEMPLATES から拾うので、型を足した人が
# ここに追記し忘れても勝手に検査対象になる。
SHIPPED_TEMPLATES = [(t.id, t.post_types[0], t.item_count) for t in T.TEMPLATES]


def test_the_template_sweep_is_not_empty():
    """列挙が空振りしていたら、下のテストは何も見ていない。"""
    assert len(SHIPPED_TEMPLATES) >= 10
    ids = {t[0] for t in SHIPPED_TEMPLATES}
    for expected in ("product", "casual", "question", "topic", "howto"):
        assert expected in ids or any(expected == t[1] for t in SHIPPED_TEMPLATES)


@pytest.mark.parametrize("template_id,post_type,needed", SHIPPED_TEMPLATES)
def test_no_generated_post_mentions_reviews(tmp_path, template_id, post_type, needed):
    builder = ContentBuilder(State(tmp_path / "state.json"))
    items = [
        make_item(item_code=f"s{i}:{i}", item_name=f"テストコスメ{i}",
                  item_price=800 + i * 300, review_count=200 + i * 400,
                  review_average=4.0 + (i % 5) * 0.1, postage_flag=i % 2,
                  shop_code=f"s{i}")
        for i in range(8)
    ]
    seen = 0
    for turn in range(30):
        selection = items[turn % 4: turn % 4 + needed] if needed else []
        if len(selection) < needed:
            selection = items[:needed]
        for link in ((True, False) if needed else (False,)):
            try:
                draft = builder.build(post_type, selection, template_id=template_id,
                                      with_affiliate_link=link, today=date.today())
            except ValueError:
                continue
            seen += 1
            for text in (draft.text, *draft.segments):
                hit = offending(text)
                assert not hit, f"{template_id} に {hit}:\n{text}"
    assert seen > 0, f"{template_id} を一度も生成できていない"


# ======================================================================
# 選定側は触っていないこと
# ======================================================================
def test_the_selection_still_uses_review_data():
    """**文言から消しただけで、判断材料としては残っていること。**

    ここが緑のまま「レビューを見なくなった」と誤解されると、
    根拠のない商品を並べるアカウントになる。
    """
    from src.config import load_config

    config = load_config()
    assert float(config.scoring["weights"]["review_count"]) > 0
    assert int(config.selection["min_review_count"]) > 0
    assert float(config.selection["min_review_average"]) > 0


def test_no_string_literal_in_the_content_package_mentions_reviews():
    """**コードの文字列リテラルを直接見る。**

    プール（データ）の走査では、f文字列に埋め込まれた
    `f"レビュー{cnt}"` のような書き方を捕まえられない。
    実際それが build_facts に居て、まとめ系投稿では必ず描画されていた。

    説明のコメントと docstring は対象外。方針を書き残すために
    「レビュー」という語が要るのはむしろ当然なので。
    """
    import src.content.appeals
    import src.content.benefits
    import src.content.builder
    import src.content.facts
    import src.content.parts
    import src.content.templates
    import src.content.voices

    modules = (
        src.content.facts, src.content.templates, src.content.appeals,
        src.content.builder, src.content.benefits, src.content.parts,
        src.content.voices,
    )
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            hit = offending(node.value)
            assert not hit, (
                f"{module.__name__}:{node.lineno} にレビュー語のリテラル "
                f"{hit}: {node.value[:60]!r}"
            )


# ======================================================================
# 返信文（src/engage/）も同じ方針
#
# 返信は LLM が書くので、プールを検査するだけでは足りない。
# プロンプトで指示したうえで、機械側でも止める。
# ======================================================================
def test_a_reply_mentioning_reviews_is_rejected():
    from src.engage.review import review

    for text in ("レビューたくさんついてたので気になってます〜",
                 "口コミ見てから決めるタイプです、わかる〜",
                 "クチコミ気にしちゃうのわかります"):
        result = review(text, include_experience=True)
        assert not result.ok, f"通ってしまった: {text}"
        assert "レビュー" in result.summary()


def test_a_normal_reply_still_passes():
    """語を禁じたせいで、ふつうの返信まで落ちないこと。"""
    from src.engage.review import review

    assert review("詰め替えあるかどうかで結構変わりますよね、わかる〜",
                  include_experience=True).ok


def test_the_reply_persona_does_not_advertise_reviews():
    """返信の «中の人» 設定にも残さない。

    ここに「価格やレビューなど公開情報だけで紹介しています」と書いてあると、
    LLM は素直にレビューの話をする。
    """
    from src.engage import prompts

    block = prompts._voice_block()
    identity = block.split("一人称")[0]
    assert "レビュー" not in identity, f"設定文がレビューを名乗っている: {identity}"
    assert "「レビュー」「口コミ」という言葉は使わない" in block, "禁止が書かれていない"
