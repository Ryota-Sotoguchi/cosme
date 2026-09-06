"""書ききる型。

上限500字に対して、実測（101投稿）の文字数は中央値60字・最大214字だった。
半分も使っておらず、「並べただけ」で終わって読み手が選べていない。

longform は上限を使い切る型。ただし **水増しはしない**。
埋めるのは全部データから出るもの（観点・数値・並べたときの位置づけ）。
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.content.builder import ContentBuilder
from src.content.facts import comparative_notes, extract_numbers
from src.content.templates import LINK_FIRST, LINK_LAST, PR_TAG
from src.storage.state import State

from conftest import make_item

# 「書ききる」と言える下限。既存の最長投稿（214字）を大きく超えること。
MIN_LENGTH = 330
MAX_LENGTH = 500

GROUPS = (
    (("クレンジングバーム ホットクレンジング 90g", 1480, 3200, 4.21, 1.0, 0),
     ("クレンジングジェル まつエクOK 200mL", 2480, 180, 4.80, 2.0, 0),
     ("クレンジングオイル 大容量 480mL", 5200, 900, 4.52, 10.0, 1)),
    (("保湿化粧水 大容量 500mL", 990, 12400, 4.63, 1.0, 0),
     ("高保湿ローション しっとり 150mL", 3740, 1852, 4.69, 2.0, 0),
     ("エマルジョン 乳液 150mL", 3300, 189, 4.71, 20.0, 0)),
    (("シャンプー ノンシリコン 400mL", 1320, 2577, 4.70, 1.0, 0),
     ("トリートメント サロン 250g", 1980, 640, 4.40, 3.0, 0),
     ("ヘアオイル アウトバス 100mL", 2280, 55, 4.10, 1.0, 0)),
)


def build(tmp_path: Path, group, position=LINK_FIRST, with_link=True):
    builder = ContentBuilder(
        State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json"
    )
    items = [
        make_item(item_code=f"s{i}:{i}", item_name=n, item_price=p, review_count=c,
                  review_average=a, point_rate=pt, postage_flag=pf, shop_code=f"s{i}")
        for i, (n, p, c, a, pt, pf) in enumerate(group)
    ]
    draft = builder.build(
        "longform", items, template_id="longform", with_affiliate_link=with_link,
        today=datetime.date(2026, 9, 8), link_position=position,
    )
    return draft, items


# ======================================================================
@pytest.mark.parametrize("group", GROUPS)
def test_longform_actually_uses_the_limit(tmp_path, group):
    """既存の最長（214字）を大きく超え、上限は超えないこと。"""
    draft, _ = build(tmp_path, group)
    assert MIN_LENGTH <= len(draft.text) <= MAX_LENGTH, (
        f"{len(draft.text)}字:\n{draft.text}"
    )


@pytest.mark.parametrize("group", GROUPS)
def test_longform_is_one_post(tmp_path, group):
    """書ききる型なので連投にしない。1本で読み切ってもらう。"""
    draft, _ = build(tmp_path, group)
    assert len(draft.segments) == 1


@pytest.mark.parametrize("group", GROUPS)
def test_longform_marks_the_ad_at_the_top(tmp_path, group):
    draft, _ = build(tmp_path, group)
    assert draft.text.startswith(PR_TAG)


@pytest.mark.parametrize("group", GROUPS)
def test_longform_passes_compliance(tmp_path, config, group):
    checker = ComplianceChecker(config.compliance, config.dedup, max_length=MAX_LENGTH)
    draft, _ = build(tmp_path, group)
    result = checker.check(draft, recent_texts=[])
    assert result.passed, f"{result.summary()}\n{draft.text}"


@pytest.mark.parametrize("group", GROUPS)
def test_longform_names_every_product(tmp_path, group):
    """3件すべてが本文に出ること。並べる型なのに落ちていたら意味が無い。"""
    draft, items = build(tmp_path, group)
    for item in items:
        head = item.display_name_without_volume(30)[:6]
        assert head in draft.text, f"{head} が出ていない:\n{draft.text}"


@pytest.mark.parametrize("group", GROUPS)
def test_longform_declares_its_numbers(tmp_path, group):
    """本文の数値が全部許可リストに入っていること。"""
    draft, _ = build(tmp_path, group)
    for token in extract_numbers(draft.text):
        assert token in draft.allowed_numbers, f"{token} が許可されていない"


@pytest.mark.parametrize("group", GROUPS)
def test_longform_reports_where_the_link_is(tmp_path, group):
    """常に1本なので、A/B には first と記録すること。

    ctx.link_position を無視する型が "last" と記録すると、
    A/B の集計が意味を失う。
    """
    for position in (LINK_FIRST, LINK_LAST):
        draft, _ = build(tmp_path, group, position=position)
        assert draft.link_position == LINK_FIRST


def test_longform_without_a_link_has_no_pr_tag(tmp_path):
    draft, _ = build(tmp_path, GROUPS[0], with_link=False)
    assert PR_TAG not in draft.text


# ======================================================================
# 並べたときの位置づけ
# ======================================================================
def test_every_note_passes_compliance():
    """位置づけの文が NG表現に当たらないこと。

    「いちばん多くの人が買ってる」が「多くの人が」（第三者の声の創作）に
    一致して、本番データで longform が1本も作れなかった。
    longform はテンプレートが1つしか無いので、再生成で逃げられない。
    """
    from src.compliance.rules import scan

    combos = [
        [make_item(item_code="a:1", item_price=990, review_count=3200, review_average=4.2),
         make_item(item_code="b:1", item_price=2480, review_count=180, review_average=4.8),
         make_item(item_code="c:1", item_price=5200, review_count=900, review_average=4.5,
                   point_rate=10.0)],
        [make_item(item_code="d:1", item_price=1000, review_count=0, review_average=None,
                   postage_flag=1),
         make_item(item_code="e:1", item_price=2000, review_count=0, review_average=None,
                   postage_flag=0),
         make_item(item_code="f:1", item_price=3000, review_count=0, review_average=None,
                   postage_flag=1)],
    ]
    for items in combos:
        for note in comparative_notes(items):
            assert not scan(note, has_link=True), f"{note}: {scan(note, has_link=True)}"


def test_notes_pick_the_distinguishing_fact():
    items = [
        make_item(item_code="a:1", item_price=990, review_count=3200, review_average=4.2),
        make_item(item_code="b:1", item_price=2480, review_count=180, review_average=4.8),
        make_item(item_code="c:1", item_price=5200, review_count=900, review_average=4.5,
                  point_rate=10.0),
    ]
    notes = comparative_notes(items)
    assert notes[0] == "この中ではいちばん安い"
    assert "評価" in notes[1]
    assert notes[2], "3件目に位置づけが無い"
    assert len(set(n for n in notes if n)) == len([n for n in notes if n]), (
        f"同じ位置づけが重複している: {notes}"
    )


def test_postage_note_needs_a_difference():
    """全部が送料無料なら、それは並べたときの違いにならない。"""
    items = [
        make_item(item_code=f"x{i}:1", item_price=1000 + i * 10, review_count=0,
                  review_average=None, point_rate=1.0, postage_flag=0)
        for i in range(3)
    ]
    assert "送料がかからない" not in comparative_notes(items)


def test_notes_are_silent_without_a_comparison():
    """1件だけなら比べようがない。無理に何か言わせない。"""
    assert comparative_notes([make_item()]) == [""]


def test_ties_are_not_called_the_best():
    """同点なら「いちばん」と言わないこと。"""
    items = [make_item(item_code=f"t{i}:1", item_price=1000, review_count=100,
                       review_average=4.5, point_rate=1.0) for i in range(3)]
    assert "いちばん安い" not in comparative_notes(items)


# ======================================================================
def test_longform_is_in_the_affiliate_rotation(config):
    """書ききる型が実際に出る枠に入っていること。"""
    affiliate = [s for s in config.schedule if s.allow_affiliate]
    assert affiliate, "リンク枠が無い"
    options = config.rotation.get(affiliate[0].slot, [])
    assert "longform" in options, f"longform が枠に入っていない: {options}"


def test_longform_needs_three_items():
    from src.pipeline import ITEMS_NEEDED

    assert ITEMS_NEEDED["longform"] == 3


def test_close_values_are_not_called_the_best():
    """僅差で「いちばん」と言わないこと。

    実データで 345/331/310件 が3件とも「レビュー300件超え」と表示される
    のに、1件だけ「レビューがいちばん多い」と書いていた。
    表示が同じに見えるのに片方だけ特別扱いになる。
    """
    items = [
        make_item(item_code="p:1", item_price=6050, review_count=345, review_average=4.88),
        make_item(item_code="q:1", item_price=4400, review_count=310, review_average=4.84),
        make_item(item_code="r:1", item_price=4180, review_count=331, review_average=4.68),
    ]
    notes = comparative_notes(items)
    assert "レビューがいちばん多い" not in notes, notes
    assert "評価はこの中で一番高い" not in notes, notes
    # 4,180円 と 4,400円 は5%差。ここも「いちばん安い」とは言わない
    assert "この中ではいちばん安い" not in notes, notes
    # 6,050円 は2番手より3割高いので、そこは言ってよい
    assert notes[0] == "この中ではいちばん高い", notes


def test_clear_gaps_are_still_reported():
    """開きがあるときは、ちゃんと言うこと。差を消しすぎない。"""
    items = [
        make_item(item_code="s:1", item_price=990, review_count=12400, review_average=4.6),
        make_item(item_code="t:1", item_price=3740, review_count=180, review_average=4.2),
        make_item(item_code="u:1", item_price=3300, review_count=200, review_average=4.3),
    ]
    notes = comparative_notes(items)
    assert notes[0] in ("この中ではいちばん安い", "レビューがいちばん多い"), notes
    assert any(notes), notes
