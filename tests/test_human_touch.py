"""つぶやきに「いつ」と「何について」が入っていることのテスト。

語尾も型も直したのに、まだAIアカウント臭が残っていた。
数えたら、語彙ではなく**投稿が何にも紐づいていないこと**が原因だった。

    つぶやき85件のうち
      固有名詞（ブランド名）    0件 ( 0%)
      過去の投稿への言及        0件 ( 0%)
      時間の言及               12件 (14%)

誰でもない人が、いつでもない時に、何についてでもない話をしている。
だから誰にでも当てはまり、誰のものでもなく読める。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from src.compliance.rules import scan
from src.content.brand_murmurs import BRAND_MURMURS, murmur_for
from src.content.builder import ContentBuilder
from src.content.parts import CASUAL_MURMURS, time_band_for
from src.storage.state import State
from tests.conftest import make_item


def _builder() -> ContentBuilder:
    return ContentBuilder(State(pathlib.Path(tempfile.mkdtemp()) / "s.json"))


# ======================================================================
# 時間帯
# ======================================================================
def test_every_slot_maps_to_a_band():
    """config.toml の全スロットに時間帯があること。

    漏れると time_band が空になり、絞り込みが黙って効かなくなる。
    """
    from src.config import load_config

    for slot in load_config().schedule:
        assert time_band_for(slot.slot), f"{slot.slot} の時間帯が未定義"


@pytest.mark.parametrize("slot,forbidden", [
    ("morning", ("寝る前", "今日もおつかれ", "湯船")),
    ("midmorning", ("寝る前", "今日もおつかれ")),
    ("late", ("朝の支度", "朝、鏡の前")),
    ("night", ("朝の支度",)),
])
def test_posts_match_the_time_of_day(slot, forbidden):
    """時間と内容が食い違わないこと。

    「朝の支度、あと五分だけ時間がほしい」が22:30に出るのは、
    人間なら起きない。実際に起きていた（flags に time_band を
    渡し忘れていて、絞り込みが一度も効いていなかった）。
    """
    builder = _builder()
    for _ in range(12):
        draft = builder.build("casual", [], template_id="casual", slot=slot)
        builder.state.record_part_ids(draft.part_ids)
        for word in forbidden:
            assert word not in draft.text, (
                f"{slot}({time_band_for(slot)}) に「{word}」が出た:\n  {draft.text}"
            )


def test_most_murmurs_stay_untagged():
    """大半は時間帯を指定しないこと。

    全部にタグを付けると、枠ごとに選べるパーツが減って
    同じ投稿が戻ってくる。
    """
    tagged = [p for p in CASUAL_MURMURS if p.time_bands]
    ratio = len(tagged) / len(CASUAL_MURMURS)
    assert ratio <= 0.35, f"タグ付きが {ratio:.0%}。枠ごとの母数が枯れる"
    assert tagged, "時間帯タグが1件も無い"


def test_unknown_slot_does_not_restrict():
    assert time_band_for("なにこれ") == ""


# ======================================================================
# ブランド名
# ======================================================================
def test_brand_name_is_extracted_from_real_patterns():
    cases = [
        ("モイストリファイン 化粧液【ファンケル 公式】 [FANCL 化粧水", "ファンケル"),
        ("サロンプレミアム トリートメント【アテニア 公式】[ ヘア", "アテニア"),
        ("【公式】キールズ レチノール美容液 30mL", "キールズ"),
    ]
    for name, expected in cases:
        assert make_item(item_name=name).brand_name == expected, name


@pytest.mark.parametrize("name", [
    "メール便 送料無料 マカダミ屋 茶ボトル20ml",
    "【10％OFF】シャンプー ノンシリコン ボタニカル アミノ酸",
    "【11種類】NEKODORONO メイクブラシ スリムアイライナーブラシ",
])
def test_garbage_is_never_returned_as_a_brand(name):
    """ブランド名が取れないときは黙ること。

    間違ったブランド名を投稿するより、触れないほうが害が小さい。
    「メール便の化粧水、詰め替えあるんだ」は意味不明で、
    かつ実在しない情報を出すことになる。
    """
    brand = make_item(item_name=name).brand_name
    assert brand is None or brand not in ("メール便", "最大", "シャンプー", "送料無料")


def test_brand_key_is_left_alone():
    """brand_key は重複防止で使われているので触らないこと。"""
    item = make_item(item_name="モイストリファイン 化粧液【ファンケル 公式】")
    assert item.brand_key == "モイストリファイン"
    assert item.brand_name == "ファンケル"


# ======================================================================
# ブランド由来のつぶやき
# ======================================================================
def test_murmur_needs_a_verifiable_fact():
    """商品名から確かめられる事実が無ければ作らないこと。"""
    item = make_item(item_name="【公式】キールズ 美容液")
    assert murmur_for(item) is None, "根拠が無いのにつぶやきを作っている"


def test_murmur_uses_only_what_the_name_says():
    item = make_item(item_name="【アテニア 公式】シャンプー 詰め替え")
    murmur = murmur_for(item)
    assert murmur is not None
    assert "アテニア" in murmur
    assert "詰め替え" in murmur


def test_murmur_is_none_without_a_brand():
    """ブランドが取れなければ、事実があっても作らない。"""
    item = make_item(item_name="メール便 送料無料 シャンプー 詰め替え 大容量")
    assert murmur_for(item) is None


def test_murmurs_never_claim_usage_or_quality():
    """使用体験も評価も書かないこと。

    このアカウントは商品を使っていない。
    「良かった」「おすすめ」はこちらで裏を取れない。
    """
    banned = ("使っ", "良か", "よかった", "おすすめ", "コスパ", "しっとり",
              "効く", "人気", "神", "最強")
    for spec in BRAND_MURMURS:
        for template in spec.templates:
            text = template.format(brand="テストブランド")
            hits = [w for w in banned if w in text]
            assert not hits, f"{spec.id}: {hits} → {text}"


def test_murmurs_pass_compliance():
    for spec in BRAND_MURMURS:
        for template in spec.templates:
            text = template.format(brand="キールズ")
            assert not scan(text, has_link=False), text


def test_murmurs_contain_no_digits():
    for spec in BRAND_MURMURS:
        for template in spec.templates:
            assert not any(c.isdigit() for c in template)


def test_murmurs_are_short():
    for spec in BRAND_MURMURS:
        for template in spec.templates:
            assert len(template.format(brand="オルナオーガニック")) <= 40


# ======================================================================
# 組み立て
# ======================================================================
def test_brand_hint_is_used_when_available():
    builder = _builder()
    draft = builder.build("casual", [], template_id="casual",
                          brand_hint="アテニア、詰め替えあるんだ")
    assert draft.text == "アテニア、詰め替えあるんだ"


def test_falls_back_when_no_material():
    """素材が無い日でも投稿が出ること。

    商品が取れない・ブランドが取れない日に枠が飛ぶのは避ける。
    """
    builder = _builder()
    draft = builder.build("casual", [], template_id="casual", brand_hint="")
    assert draft.text.strip()
    assert draft.part_ids.get("casual"), "定型プールに戻っていない"
