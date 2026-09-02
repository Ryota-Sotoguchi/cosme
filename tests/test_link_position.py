"""リンクをどこに置くかの切り替え。

## なぜこのテストが要るのか

2026-08-20〜09-03 の実測で、リンク投稿の表示 6,211 に対してクリックは5件
（CTR 0.081%）しか出ていない。原因は、商品名・リンク・#PR がすべて
連投の最後の1本にあり、おすすめ欄に流れる1本目には何も無かったこと。

「リンク付き投稿は表示が落ちる」という前提で作られた形だったが、
初日を除く80件では リンク付き 270 / リンクなし 161 で、前提が支持されない。
どちらが正しいかを A/B で決めるので、両方の形が壊れないよう固定する。
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.content.builder import ContentBuilder
from src.content.templates import LINK_FIRST, LINK_LAST, PR_TAG
from src.storage.state import State

from conftest import make_item

PRODUCT_TEMPLATES = ("short", "band_focus", "checklist", "objective", "thread")


def build(tmp_path: Path, template_id: str, position: str, **item_kwargs):
    builder = ContentBuilder(
        State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json"
    )
    item = make_item(**item_kwargs)
    return builder.build(
        "product",
        [item],
        template_id=template_id,
        with_affiliate_link=True,
        today=datetime.date(2026, 9, 4),
        link_position=position,
    ), item


# ======================================================================
@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_link_first_is_a_single_post(tmp_path, template_id):
    """1本で完結すること。連投にすると商品まで3手かかる。"""
    draft, _ = build(tmp_path, template_id, LINK_FIRST)
    assert len(draft.segments) == 1, f"{template_id}: 連投になっている"


@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_link_first_shows_the_product_in_the_timeline(tmp_path, template_id):
    """おすすめ欄に流れる本文に、商品名と数値があること。

    いちばん表示が伸びた投稿（2,802）は商品名・価格・レビューが1本に
    揃っていた。数字を含む投稿の表示中央値は 277 で、含まない 172 を上回る。
    """
    draft, item = build(tmp_path, template_id, LINK_FIRST)
    timeline = draft.segments[0]

    assert item.display_name_without_volume(38)[:8] in timeline, "商品名が出ていない"
    assert any(ch.isdigit() for ch in timeline), "数値が1つも出ていない"
    assert draft.link_attachment, "リンクカードが付いていない"


@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_link_first_marks_the_ad_where_people_see_it(tmp_path, template_id):
    """#PR が、実際に人が見る本文の冒頭にあること。

    景表法（ステマ規制）が求めるのは、広告と判別できる表示が
    その投稿を見た人に見えること。タイムラインに出る本人が広告なのだから、
    表示もそこに要る。
    """
    draft, _ = build(tmp_path, template_id, LINK_FIRST)
    timeline = draft.segments[0]
    assert timeline.startswith(PR_TAG), f"{template_id}: 冒頭に {PR_TAG} が無い"


@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_link_first_passes_compliance(tmp_path, config, template_id):
    checker = ComplianceChecker(config.compliance, config.dedup, max_length=500)
    draft, _ = build(tmp_path, template_id, LINK_FIRST, item_code=f"s:{template_id}")
    result = checker.check(draft, recent_texts=[])
    assert result.passed, f"{template_id}: {result.summary()}"


def test_link_first_keeps_the_templates_distinguishable(tmp_path):
    """型ごとの長さが残っていること。

    連投をやめると、どの型も「前振り＋商品＋CTA」になって1種類に潰れる。
    テンプレートが宣言している段数を長さに読み替えて、
    短い型・普通の型・長い型に分けている。
    """
    # 同じ商品で比べる。商品が変わると訴求文も変わって長さがぶれる。
    shapes = {}
    for template_id in PRODUCT_TEMPLATES:
        draft, _ = build(tmp_path, template_id, LINK_FIRST)
        shapes[template_id] = (len(draft.text), draft.text.count("・"))

    short_len, short_bullets = shapes["short"]
    mid_len, mid_bullets = shapes["band_focus"]
    long_len, long_bullets = shapes["checklist"]

    assert short_bullets == 0, "いちばん短い型に箇条書きが入っている"
    assert 0 < mid_bullets < long_bullets, f"箇条書きの量が潰れている: {shapes}"
    assert short_len < mid_len < long_len, f"型ごとの長さが潰れている: {shapes}"


@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_link_last_still_works(tmp_path, template_id):
    """従来の形も壊れていないこと。A/B の対照側なので消さない。"""
    draft, _ = build(tmp_path, template_id, LINK_LAST)
    assert len(draft.segments) >= 2, f"{template_id}: 連投になっていない"
    assert draft.segments[-1].startswith(PR_TAG)
    assert PR_TAG not in draft.segments[0], "前振りに広告表示が混ざっている"


def test_link_position_is_recorded(tmp_path):
    """どちらの形で出したかが履歴に残ること。残らないと A/B を集計できない。"""
    for position in (LINK_FIRST, LINK_LAST):
        draft, _ = build(tmp_path, "short", position)
        assert draft.link_position == position


def test_link_position_is_empty_without_a_link(tmp_path):
    """リンクなし投稿に A/B の印を付けない。集計を汚す。"""
    builder = ContentBuilder(
        State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json"
    )
    draft = builder.build("casual", [], with_affiliate_link=False, link_position=LINK_FIRST)
    assert draft.link_position == ""


# ======================================================================
def test_ab_alternates_by_day(config, tmp_path):
    """A/B が日ごとに交互になること。片側だけ出続けると比較にならない。"""
    from src.pipeline import Pipeline
    from src.storage.history import History

    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=object(),
    )
    seen = [
        pipeline.link_position(datetime.datetime(2026, 9, day, 12, 0))
        for day in range(1, 9)
    ]
    assert set(seen) == {LINK_FIRST, LINK_LAST}, f"片側に寄っている: {seen}"
    assert seen[0] != seen[1], "日ごとに切り替わっていない"


def test_ab_can_be_pinned(config, tmp_path, monkeypatch):
    """勝ちが決まったら固定できること。"""
    from src.pipeline import Pipeline
    from src.storage.history import History

    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=object(),
    )
    monkeypatch.setitem(pipeline.config.raw, "experiment", {"link_position": "first"})
    assert pipeline.link_position(datetime.datetime(2026, 9, 1, 12, 0)) == LINK_FIRST
    assert pipeline.link_position(datetime.datetime(2026, 9, 2, 12, 0)) == LINK_FIRST


def test_unknown_mode_falls_back_to_the_previous_behaviour(config, tmp_path, monkeypatch):
    """設定を書き間違えても、既知の形で出ること。"""
    from src.pipeline import Pipeline
    from src.storage.history import History

    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=object(),
    )
    monkeypatch.setitem(pipeline.config.raw, "experiment", {"link_position": "typo"})
    assert pipeline.link_position(datetime.datetime(2026, 9, 1, 12, 0)) == LINK_LAST

# ======================================================================
# 3件並べる型（実績で表示がいちばん伸びている）
# ======================================================================
ROUNDUPS = (("review_heavy", 3), ("postage_free", 3), ("comparison", 2), ("price_band", 3))


def build_roundup(tmp_path: Path, post_type: str, count: int, position: str):
    builder = ContentBuilder(
        State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json"
    )
    items = [
        make_item(item_code=f"s{i}:{i}", item_name=f"テスト美容液{i} 30ml",
                  item_price=1200 + i * 900, shop_code=f"s{i}")
        for i in range(count)
    ]
    return builder.build(
        post_type, items, template_id=post_type, with_affiliate_link=True,
        today=datetime.date(2026, 9, 4), link_position=position,
    )


@pytest.mark.parametrize("post_type,count", ROUNDUPS)
def test_roundup_link_first_is_a_single_post(tmp_path, post_type, count):
    """3件並べる型でも、商品と数値をタイムラインに残すこと。

    review_heavy は1件で722表示、postage_free は575表示と、
    実績では最も表示を取っている型。連投にすると商品も数値も
    2本目へ行き、その強みがタイムラインから消える。
    """
    draft = build_roundup(tmp_path, post_type, count, LINK_FIRST)
    assert len(draft.segments) == 1
    timeline = draft.segments[0]
    assert timeline.startswith(PR_TAG)
    assert "テスト美容液0" in timeline, "商品名が出ていない"
    assert "※リンクは1つ目のものです" in timeline, "どれのリンクか明示していない"


@pytest.mark.parametrize("post_type,count", ROUNDUPS)
def test_roundup_link_first_passes_compliance(tmp_path, config, post_type, count):
    checker = ComplianceChecker(config.compliance, config.dedup, max_length=500)
    draft = build_roundup(tmp_path, post_type, count, LINK_FIRST)
    result = checker.check(draft, recent_texts=[])
    assert result.passed, f"{post_type}: {result.summary()}"


@pytest.mark.parametrize("post_type,count", ROUNDUPS)
def test_roundup_link_last_still_works(tmp_path, post_type, count):
    draft = build_roundup(tmp_path, post_type, count, LINK_LAST)
    assert len(draft.segments) == 3
    assert PR_TAG not in draft.segments[0]


def test_the_only_affiliate_slot_can_reach_the_roundups(config):
    """リンク枠を1つに絞ったときに、実績の良い型を落とさないこと。"""
    affiliate_slots = [s for s in config.schedule if s.allow_affiliate]
    assert len(affiliate_slots) == 1, "クリックの帰属にはリンク投稿が1日1本であること"

    options = config.rotation.get(affiliate_slots[0].slot, [])
    assert options, "リンク枠のローテーションが空"
    assert {"review_heavy", "postage_free"} <= set(options), (
        f"表示実績の良い型がリンク枠から外れている: {options}"
    )
