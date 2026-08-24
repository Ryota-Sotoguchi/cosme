"""リンク投稿が毎回同じに見えないことのテスト。

リンク投稿は1日1〜3本しか出ない。同じ剤形の商品が続いたときに
まったく同じ箇条書きが出ると、見ている側には
「同じ投稿を繰り返している」ようにしか見えない。

実際、`ctx.fact_style` が `closing` グループの履歴長だったせいで、
リンク投稿では永久に 0 のままだった（リンク投稿は `recommend_closing`
を使うため）。同じ剤形なら毎回同じ箇条書きになっていた。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from src.content.benefits import BENEFITS, TIPS_HEADINGS, benefit_for, tips_block
from src.content.builder import ContentBuilder
from src.content.parts import RECOMMEND_OPENINGS, THREAD_HOOKS
from src.storage.state import State
from tests.conftest import make_item


def _builder() -> ContentBuilder:
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ContentBuilder(State(tmp / "state.json"))


# ======================================================================
# 箇条書きが商品ごとに変わること
# ======================================================================
def test_same_form_different_products_get_different_tips():
    """同じ剤形でも、商品が違えば箇条書きが変わること。"""
    builder = _builder()
    seen: set[str] = set()
    for i in range(5):
        item = make_item(
            item_code=f"shop{i}:{i}",
            item_name=f"テスト シャンプー {i}",
            shop_code=f"shop{i}",
        )
        draft = builder.build("product", [item], template_id="objective")
        # 箇条書きの段（2本目）
        seen.add(draft.segments[1])

    assert len(seen) >= 3, (
        f"5商品で箇条書きが {len(seen)} 種類しか出ていない。"
        "同じ投稿の繰り返しに見える"
    )


def test_tips_block_rotates_with_cursor():
    benefit = benefit_for("シャンプー")
    assert benefit is not None
    blocks = {tips_block(benefit, cursor=c) for c in range(len(benefit.tips))}
    assert len(blocks) >= 3, "cursor を変えても箇条書きが変わらない"


def test_every_form_has_enough_tips_to_rotate():
    """回す母数があること。3件しか無いと、3件表示では常に同じになる。"""
    for benefit in BENEFITS:
        assert len(benefit.tips) >= 5, (
            f"{benefit.id}: tips が {len(benefit.tips)} 件しかない。"
            "回転させても同じ組み合わせになる"
        )


def test_tips_headings_are_distinct():
    assert len(set(TIPS_HEADINGS)) == len(TIPS_HEADINGS)
    assert len(TIPS_HEADINGS) >= 4


# ======================================================================
# フックの型が偏っていないこと
# ======================================================================
def test_recommend_openings_are_not_all_the_same_shape():
    """書き出しが「{category}＋指示」一辺倒になっていないこと。

    参考アカウントは1行目の型自体を変えている。
    逆張り・断言・名指し・共有、といった別の型を混ぜる。
    """
    texts = [p.text for p in RECOMMEND_OPENINGS]
    assert len(RECOMMEND_OPENINGS) >= 12, "書き出しの数が足りない"

    # 「〜てください」「〜てほしい」で終わる指示形が過半を占めていないこと
    imperative = [t for t in texts if t.rstrip("👀📝🤍💭。").endswith(("ください", "ほしい", "みて"))]
    assert len(imperative) <= len(texts) // 2, (
        f"指示形が {len(imperative)}/{len(texts)} 件。型が偏っている"
    )

    # 言い切り（句点で終わる）の型が入っていること
    assertive = [t for t in texts if t.rstrip("👀📝🤍💭").endswith("。")]
    assert assertive, "言い切り型の書き出しが1つも無い"


# ======================================================================
# フックが剤形と食い違わないこと
# ======================================================================
# フックはジャンル（スキンケア/ヘアケア…）で選ぶのに、箇条書きは
# 剤形（化粧水/シャンプー…）で選ぶ。フックが特定の剤形を名指しすると、
# 別の剤形の商品に付いたときに 食い違う。
#
#   商品: シャンプー（ヘアケア）
#   フック: 「化粧水って、こだわる人と…」  ← 別物の話をしている
FORM_WORDS = ("化粧水", "美容液", "乳液", "シャンプー", "トリートメント",
              "クレンジング", "洗顔", "日焼け止め", "リップ", "ファンデ")


def test_thread_hooks_do_not_name_a_specific_form():
    for category, hooks in THREAD_HOOKS.items():
        for hook in hooks:
            hits = [w for w in FORM_WORDS if w in hook.text]
            assert not hits, (
                f"{category}.{hook.id} が剤形「{'/'.join(hits)}」を名指ししている。"
                f"別の剤形の商品に付くと食い違う:\n  {hook.text}"
            )


# ======================================================================
# 価格帯が漏れないこと
# ======================================================================
def test_band_focus_does_not_leak_a_price():
    """band_focus の書き出しに価格帯が出ないこと。

    「1,500円前後のヘアケア探してたときのメモ」が本番に出ていた。
    リンク投稿から数値を全部外す方針に反する。
    """
    builder = _builder()
    item = make_item(item_name="テスト シャンプー", item_price=1580)
    draft = builder.build("product", [item], template_id="band_focus")
    for i, segment in enumerate(draft.segments):
        assert not any(c.isdigit() for c in segment), (
            f"band_focus の {i+1}本目に数値がある:\n{segment}"
        )


@pytest.mark.parametrize("template_id", ["short", "thread", "objective", "checklist", "band_focus"])
def test_link_posts_have_no_digits_anywhere(template_id):
    builder = _builder()
    item = make_item(item_name="テスト クレンジングジェル", item_price=2480)
    draft = builder.build("product", [item], template_id=template_id)
    joined = "\n".join(draft.segments)
    assert not any(c.isdigit() for c in joined), f"{template_id} に数値:\n{joined}"
