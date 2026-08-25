"""定型文になっていないことのテスト。

投稿を毎日見る人にとっては、同じ言い回しが数日おきに戻ってくるだけで
「これ機械が書いてるな」と分かる。

崩れ方は3つある。

  1. 人が書かない言い方をしている（辞書の文・見出しっぽい見出し）
  2. ベタ書きしていて回らない
  3. プールはあるのに、クールダウンが効いていない
"""

from __future__ import annotations

import pathlib
import re
import tempfile
from collections import defaultdict

import pytest

from src.content import parts as P
from src.content.benefits import BENEFITS, TIPS_HEADINGS
from src.content.builder import ContentBuilder
from src.storage.state import State
from tests.conftest import make_item


def _builder() -> ContentBuilder:
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ContentBuilder(State(tmp / "state.json"))


# ======================================================================
# 1. 人が書かない言い方をしていないこと
# ======================================================================
def test_the_role_sentence_is_not_shown_verbatim():
    """`role` をそのまま本文に出さないこと。

    「シャンプーは髪と地肌を清浄にするもの」は薬機法の言い回しであって、
    人が投稿の最後にこう書くことはない。
    判定の基準としては持つが、表示するのは `line` のほう。
    """
    from src.content.benefits import tips_block

    for benefit in BENEFITS:
        for cursor in range(len(benefit.tips)):
            block = tips_block(benefit, cursor=cursor)
            assert benefit.role not in block, (
                f"{benefit.id}: 辞書の文がそのまま出ている\n{block}"
            )


def test_every_benefit_has_a_human_line():
    for benefit in BENEFITS:
        assert benefit.line, f"{benefit.id} に表示用の文が無い"
        assert benefit.line.endswith("。"), f"{benefit.id} の文が言い切りで終わっていない"


def test_the_role_line_is_not_appended_every_time():
    """役割の一文を毎回付けないこと。

    箇条書き＋一文、が固定されると、それ自体が型として見える。
    """
    from src.content.benefits import tips_block

    benefit = BENEFITS[0]
    with_line = sum(
        1 for c in range(len(benefit.tips)) if benefit.line in tips_block(benefit, cursor=c)
    )
    assert with_line < len(benefit.tips), "毎回付いている"
    assert with_line > 0, "一度も付かないなら、持っている意味が無い"


def test_headings_do_not_read_like_headings():
    """見出しっぽい見出しを置かないこと。

    「比べるときの軸」「見るのはこのへんだけ」は資料の見出しで、
    人が投稿に書く言い方ではない。話しかけている形にする。
    """
    for heading in TIPS_HEADINGS:
        assert not heading.rstrip("👀📝💭").endswith(("軸", "一覧", "項目", "基準")), (
            f"見出しが硬い: {heading}"
        )


# ======================================================================
# 2. ベタ書きしていないこと
# ======================================================================
def test_templates_do_not_hardcode_japanese_sentences():
    """テンプレートに日本語の文をベタ書きしないこと。

    ベタ書きは回らないので、そのテンプレートが選ばれるたびに同じ文が出る。
    実測で checklist の1本目が5日おきに戻ってきていた。
    文章はすべて parts.py / benefits.py のプールから取る。
    """
    source = (pathlib.Path("src/content/templates.py")).read_text(encoding="utf-8")
    # コメントと docstring を落とす
    source = re.sub(r'""".*?"""', "", source, flags=re.S)
    source = "\n".join(
        line for line in source.split("\n") if not line.lstrip().startswith("#")
    )
    # 文字列リテラルのうち、話し言葉らしいもの
    suspicious = [
        lit for lit in re.findall(r'"([^"\n]{6,})"', source)
        if re.search(r"[ぁ-んァ-ヶ]", lit)
        and re.search(r"[👀📝💭🤍😌🥺。！〜]", lit)
    ]
    assert not suspicious, f"テンプレートにベタ書きの文がある: {suspicious}"


# ======================================================================
# 3. クールダウンが効いていること
# ======================================================================
def test_link_post_openings_do_not_return_within_two_weeks():
    """同じ書き出しが2週間以内に戻ってこないこと。

    pick は "recommend_opening" グループで選ぶのに、記録は "opening" に
    入れていたため、クールダウンが一度も効いていなかった。
    実測で同じ書き出しが2〜3日おきに出ていた。
    """
    builder = _builder()
    names = ["シャンプー", "化粧水", "クレンジング", "ヘアオイル", "ボディクリーム",
             "日焼け止め", "リップ", "美容液", "洗顔フォーム", "トリートメント",
             "乳液", "シートマスク", "ハンドクリーム", "ボディソープ"]
    templates = ["objective", "short", "checklist"]

    seen: dict[str, int] = {}
    for day, name in enumerate(names):
        item = make_item(item_code=f"s{day}:{day}", shop_code=f"sh{day}",
                         item_name=f"テスト {name}")
        draft = builder.build("product", [item], template_id=templates[day % 3])
        builder.state.record_part_ids(draft.part_ids)

        part_id = draft.part_ids.get("recommend_opening")
        if part_id is None:
            continue
        if part_id in seen:
            gap = day - seen[part_id]
            pytest.fail(f"書き出し {part_id} が {gap}日で再登場した")
        seen[part_id] = day


def test_part_history_keeps_enough_to_space_things_out():
    """履歴の保存件数が、プールの最大件数より大きいこと。

    ここが小さいと、避けたくても履歴が残っていないので避けられない。
    """
    import inspect

    from src.storage.state import State as S

    keep = inspect.signature(S.record_part_ids).parameters["keep"].default
    biggest = max(
        len(pool) for pool in (
            P.NO_LINK_TOPICS, P.CASUAL_MURMURS, P.HOWTO_POSTS,
            P.RECOMMEND_OPENINGS, P.RECOMMEND_CLOSINGS, P.CTA_PARTS,
            P.PRODUCT_OPENINGS, P.PRODUCT_CLOSINGS, P.THREAD_TOPICS,
        )
    )
    assert keep >= biggest, (
        f"履歴の保存件数 {keep} が、最大プール {biggest} 件より小さい。"
        "これでは一巡を避けられない"
    )


def test_cta_pool_is_large_enough():
    """CTAは全リンク投稿に出る。少ないと一番早く飽きられる。"""
    assert len(P.CTA_PARTS) >= 10


def test_no_two_parts_share_the_same_text():
    """同じ文言が別IDで重複していないこと。

    重複していると、クールダウンをすり抜けて同じ文が続けて出る。
    """
    by_text: dict[str, list[str]] = defaultdict(list)
    for pool in (P.NO_LINK_TOPICS, P.CASUAL_MURMURS, P.HOWTO_POSTS,
                 P.RECOMMEND_OPENINGS, P.RECOMMEND_CLOSINGS, P.CTA_PARTS,
                 P.PRODUCT_OPENINGS, P.PRODUCT_CLOSINGS, P.THREAD_TOPICS):
        for part in pool:
            by_text[part.text].append(part.id)
    dupes = {t: ids for t, ids in by_text.items() if len(ids) > 1}
    assert not dupes, f"同じ文言が重複: {dupes}"
