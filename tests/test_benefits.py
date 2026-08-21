"""剤形ごとの効能表現のテスト。

このアカウントは商品を使っていないので、体験は書けない。
一方で薬機法の56項目にある「カテゴリー一般の役割」は、
その剤形の商品であれば言ってよい。その線引きを固定する。
"""

from __future__ import annotations

import pytest

from src.compliance.rules import scan
from src.content.benefits import BENEFITS, benefit_for


def test_every_benefit_phrase_passes_compliance():
    """効能表現そのものがNG表現辞書に触れないこと。"""
    for benefit in BENEFITS:
        for text in (benefit.role, benefit.concern):
            hits = scan(text)
            assert not hits, f"{benefit.id}: {[h.label for h in hits]} — {text}"


def test_benefit_ids_are_unique():
    ids = [b.id for b in BENEFITS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Dr.ウィラード・ウォーター 化粧水 220mL", "lotion"),
        ("モイストクレンジングバーム 90g", "cleansing"),
        ("アミノ酸シャンプー 詰め替え 400ml", "shampoo"),
        ("サロンプレミアム トリートメント", "treatment"),
        ("ヘアオイル 洗い流さないトリートメント 100ml", "hair_oil"),
        ("ハンドクリーム 3本セット", "hand"),
        ("ティントリップ 3.5g", "lip"),
        ("KISO CARE ナイアシンアミド 美容液 30ml", "serum"),
    ],
)
def test_detects_the_right_product_form(name, expected):
    benefit = benefit_for(name)
    assert benefit is not None, name
    assert benefit.id == expected


def test_cleansing_wins_over_generic_oil():
    """「クレンジングオイル」はオイルではなくクレンジングとして扱う。

    剤形の判定順を間違えると、その剤形に許されていない効能を書く。
    """
    assert benefit_for("クレンジングオイル 200ml").id == "cleansing"


def test_returns_none_for_non_cosmetic_items():
    """剤形が判定できないものには効能を紐づけない。

    ブラシに「うるおいを与える」と書いたら、その剤形に
    許されていない効能を標ぼうすることになる。
    """
    for name in ("メイクブラシ 5本セット", "化粧ポーチ 小物入れ", "スポンジパフ 6個入り"):
        assert benefit_for(name) is None, name


def test_no_benefit_claims_certainty():
    """効果の断定をしないこと。

    「シワが消えていくのを実感」のような断定はNG。
    役割の説明にとどめる。
    """
    banned = ("実感", "必ず", "確実", "improve", "治", "消える", "生まれ変わ")
    for benefit in BENEFITS:
        for text in (benefit.role, benefit.concern):
            for word in banned:
                assert word not in text, f"{benefit.id} に断定表現: {word}"


def test_concern_does_not_stoke_anxiety():
    """不安を煽る形の悩み提起はしない。

    「シミを放置すると取り返しがつかない」はNG。
    「乾燥が気になる時期に見直す人が多い」はOK、という線引き。
    """
    banned = ("放置", "手遅れ", "取り返し", "恐怖", "危険", "後悔する", "손해")
    for benefit in BENEFITS:
        for word in banned:
            assert word not in benefit.concern, f"{benefit.id} に煽り: {word}"


@pytest.mark.parametrize(
    "name,expected",
    [
        # 複合語は、より具体的な剤形が勝つこと。
        # 順序を間違えると、その剤形に許されていない効能を書く。
        ("ボディソープ 詰め替え 800ml", "body_soap"),
        ("ボディミルク 大容量 500ml", "body"),
        ("ボディクリーム 200g", "body"),
        ("ハンドクリーム 3本セット", "hand"),
        ("クレンジングオイル 200ml", "cleansing"),
        ("ヘアオイル 洗い流さないトリートメント", "hair_oil"),
    ],
)
def test_compound_names_pick_the_most_specific_form(name, expected):
    benefit = benefit_for(name)
    assert benefit is not None, name
    assert benefit.id == expected, f"{name} → {benefit.id}（期待: {expected}）"


def test_cleansing_agents_do_not_claim_moisture():
    """洗い流すものに「うるおいを与える」と書かない。

    ボディソープ・洗顔・シャンプーは「清浄にする」が許された効能で、
    保湿は剤形が対応しない。
    """
    for name in ("ボディソープ 800ml", "洗顔フォーム 120g", "シャンプー 400ml"):
        benefit = benefit_for(name)
        assert benefit is not None, name
        assert "うるおい" not in benefit.role, f"{name}: {benefit.role}"
        assert "清浄" in benefit.role, f"{name}: {benefit.role}"


@pytest.mark.parametrize(
    "name,expected",
    [
        # 楽天の商品名の末尾には検索用キーワードが羅列されていることが多い。
        # 「ヘアーパック ヘアマスク」を拾ってフェイスパックと誤判定していた。
        (
            "サロンプレミアム トリートメント [ ヘアトリートメント 洗い流す "
            "ヘアーパック ヘアマスク 美髪 インバス ]",
            "treatment",
        ),
        ("ヘアマスク 200g", "treatment"),
        ("シートマスク 30枚", "mask"),
        ("フェイスパック 5枚入り", "mask"),
    ],
)
def test_hair_pack_is_not_confused_with_face_mask(name, expected):
    benefit = benefit_for(name)
    assert benefit is not None, name
    assert benefit.id == expected, f"{name} → {benefit.id}（期待: {expected}）"


def test_tips_contain_no_numbers():
    """箇条書きにも数値を出さない。

    リンク投稿では値段・レビュー・容量を含め数値を出さない方針なので、
    ノウハウ側にも「1本でどのくらいもつか」のような表記を残さない。
    """
    for benefit in BENEFITS:
        for tip in benefit.tips:
            assert not any(c.isdigit() for c in tip), f"{benefit.id}: {tip}"
        for text in (benefit.role, benefit.concern):
            assert not any(c.isdigit() for c in text), f"{benefit.id}: {text}"
