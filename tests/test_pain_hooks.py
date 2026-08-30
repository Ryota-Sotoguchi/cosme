"""悩みから入るフックのテスト。

もとの書き出しは全部「{category}＋助言」だった。

    スキンケア探してる人、これ見てみてほしい👀

誰の何の話か分からないまま流れる。読む人が「え、それわたしだ」と
思う状況を先に置くと、そこで指が止まる。

    夕方になると顔がつっぱってくる人、これちょっと気になった

## 守っていること

**pain は必ず role（56項目）の範囲内。**
範囲外の悩み（シミ・シワ・毛穴・ニキビ）を名指しして商品を並べると、
効能の暗示になる。化粧品で言えるのは56項目だけ。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from src.compliance.rules import scan
from src.content.benefits import BENEFITS, PAIN_HOOKS, benefit_for, pain_hook
from src.content.builder import ContentBuilder
from src.storage.state import State
from tests.conftest import make_item


def _builder() -> ContentBuilder:
    return ContentBuilder(State(pathlib.Path(tempfile.mkdtemp()) / "s.json"))


# ======================================================================
# 悩みが56項目の範囲に収まっていること
# ======================================================================
# 化粧品で標ぼうできない症状。これを名指しして商品を並べると、
# 「この商品がそれを解決する」という暗示になる。
OUT_OF_SCOPE = ("シミ", "しみ", "シワ", "しわ", "たるみ", "ニキビ", "にきび",
                "毛穴", "くすみ", "肌荒れ", "赤み", "アトピー", "湿疹")


def test_pains_stay_within_the_permitted_claims():
    for benefit in BENEFITS:
        hits = [w for w in OUT_OF_SCOPE if w in benefit.pain]
        assert not hits, (
            f"{benefit.id} の pain が56項目の範囲外: {hits}\n  {benefit.pain}\n"
            f"  （この剤形で言えるのは「{benefit.role}」まで）"
        )


def test_every_form_has_a_pain():
    for benefit in BENEFITS:
        assert benefit.pain, f"{benefit.id} に pain が無い"


def test_pains_are_specific_enough_to_recognise():
    """「〜な人が多い」ではなく、その人の状況を名指しすること。

    一般論だと自分ごとにならず、フックにならない。
    """
    for benefit in BENEFITS:
        assert "人が多い" not in benefit.pain, f"{benefit.id} が一般論になっている"
        assert len(benefit.pain) >= 10, f"{benefit.id} の pain が短すぎる"


# ======================================================================
# 生成されるフック
# ======================================================================
def test_hooks_pass_compliance():
    for benefit in BENEFITS:
        for cursor in range(len(PAIN_HOOKS)):
            text = pain_hook(benefit, cursor=cursor)
            assert text
            hits = scan(text, has_link=True)
            assert not hits, f"{benefit.id}/{cursor}: {[h.label for h in hits]}\n  {text}"


def test_hooks_never_claim_a_cure():
    """「これで解決する」と書かないこと。効能の断定になる。"""
    banned = ("解決する", "治る", "改善する", "なくなる", "消える", "効きます")
    for benefit in BENEFITS:
        for cursor in range(len(PAIN_HOOKS)):
            text = pain_hook(benefit, cursor=cursor)
            hits = [w for w in banned if w in text]
            assert not hits, f"{benefit.id}: {hits}\n  {text}"


def test_hooks_never_claim_usage():
    """使ってもいないので「良かった」とは書かないこと。

    書けるのは「気になった」「良さそう」まで。
    """
    banned = ("使ってみた", "使ったら", "良かった", "よかった", "愛用", "リピ")
    for template in PAIN_HOOKS:
        hits = [w for w in banned if w in template]
        assert not hits, f"{hits}: {template}"


def test_hooks_are_varied():
    assert len(PAIN_HOOKS) >= 12
    assert len(set(PAIN_HOOKS)) == len(PAIN_HOOKS)


def test_hooks_contain_no_digits():
    """リンク投稿から数値を外す方針は維持する。"""
    for benefit in BENEFITS:
        for cursor in range(len(PAIN_HOOKS)):
            text = pain_hook(benefit, cursor=cursor)
            assert not any(c.isdigit() for c in text), text


# ======================================================================
# 剤形の判定が悩みと食い違わないこと
# ======================================================================
@pytest.mark.parametrize("name,expected", [
    ("日焼け止め ジェル SPF50", "sunscreen"),
    ("リップクリーム 無香料", "lip"),
    ("ハンドクリーム チューブ", "hand"),
    ("ボディクリーム 200g", "body"),
    ("クレンジングバーム 90g", "cleansing"),
    ("ナイトクリーム 50g", "cream"),
])
def test_specific_forms_win_over_generic_cream(name, expected):
    """「クリーム」「ジェル」は広すぎるので、具体的な剤形を先に判定すること。

    実際に「日焼け止め ジェル」が cream と判定され、
    「朝塗ったのに、昼にはもう乾いてる」という別物の悩みが付いていた。
    """
    benefit = benefit_for(name)
    assert benefit is not None, name
    assert benefit.id == expected, f"{name} が {benefit.id} と判定された"


# ======================================================================
# 実際の投稿
# ======================================================================
def test_link_posts_use_varied_appeals():
    """リンク投稿の書き出しが、複数の訴求軸に散っていること。

    悩み型だけに寄せると、結局また同じ形になる。
    人がクリックする理由は一つではない。
    """
    builder = _builder()
    names = ["化粧水 200mL", "シャンプー 詰め替え", "ヘアオイル", "クレンジングジェル",
             "ハンドクリーム", "日焼け止め SPF50", "トリートメント", "リップクリーム",
             "ボディクリーム", "美容液 30mL"]
    used: list[str] = []
    for tid in ("objective", "thread", "short"):
        for i, name in enumerate(names):
            item = make_item(item_code=f"{tid}:{i}", item_name=name, shop_code=f"s{i}")
            draft = builder.build("product", [item], template_id=tid)
            builder.state.record_part_ids(draft.part_ids)
            if "appeal" in draft.part_ids:
                used.append(draft.part_ids["appeal"])

    assert len(used) >= len(names) * 2, f"訴求型が {len(used)} 本しか出ていない"
    assert len(set(used)) >= 6, f"軸が {len(set(used))} 種類しか使われていない: {set(used)}"


def test_no_single_appeal_dominates():
    """ひとつの軸に偏らないこと。

    材料の揃いやすい軸ばかりが出ると、それはそれで型になる。
    """
    from collections import Counter

    builder = _builder()
    used = []
    for i in range(30):
        item = make_item(item_code=f"v:{i}", item_name="化粧水 200mL", shop_code=f"s{i}")
        draft = builder.build("product", [item], template_id="objective")
        builder.state.record_part_ids(draft.part_ids)
        if "appeal" in draft.part_ids:
            used.append(draft.part_ids["appeal"])

    counts = Counter(used)
    top, n = counts.most_common(1)[0]
    assert n / len(used) <= 0.45, f"「{top}」が {n}/{len(used)} 本を占めている"


# ======================================================================
# 剤形が分からない商品
# ======================================================================
def test_unknown_form_does_not_borrow_another_forms_claim():
    """剤形が判定できない商品に、無関係な効能を付けないこと。

    以前は benefit_by_cursor で適当な剤形を引いていたため、
    メイクブラシの投稿に「体を洗うものなので香りの強さが…」と出ていた。
    読む人に意味が通らないうえ、その剤形に許された効能を
    別の商品に付けることになる。
    """
    from src.content.benefits import BENEFITS, benefit_for

    builder = _builder()
    item = make_item(item_name="【11種類】メイクブラシ アイライナーブラシ セット")
    assert benefit_for(item.display_name(60)) is None, "この商品は剤形不明であること"

    draft = builder.build("product", [item], template_id="objective")
    body = "\n".join(draft.segments)
    for benefit in BENEFITS:
        assert benefit.line not in body, (
            f"剤形不明なのに {benefit.id} の効能が出ている:\n{body}"
        )


def test_unknown_form_still_gets_a_tips_block():
    """効能に触れないだけで、箇条書き自体は出すこと。空にしない。"""
    from src.content.templates import GENERIC_TIPS

    builder = _builder()
    item = make_item(item_name="メイクブラシ セット")
    draft = builder.build("product", [item], template_id="objective")
    body = "\n".join(draft.segments)
    assert any(tip in body for tip in GENERIC_TIPS), body


# ======================================================================
# 箇条書きの重複
# ======================================================================
def test_bullets_are_not_listed_twice():
    """訴求文が箇条書きを持つとき、tips 段を重ねないこと。

    「省力化」「チェック・診断」「ランキング」の軸は本文に箇条書きを含む。
    そこへ tips 段を重ねると、同じ項目が二度並ぶ。
    """
    builder = _builder()
    for i in range(25):
        item = make_item(item_code=f"d:{i}", item_name="化粧水 200mL",
                         shop_code=f"s{i}", item_price=1500)
        draft = builder.build("product", [item], template_id="objective")
        builder.state.record_part_ids(draft.part_ids)
        for segment in draft.segments:
            lines = [ln for ln in segment.split("\n") if ln.startswith("・")]
            assert len(lines) == len(set(lines)), f"同じ項目が並んでいる:\n{segment}"
        # 前振り全体でも重複しない
        bullets = [ln for seg in draft.segments[:-1]
                   for ln in seg.split("\n") if ln.startswith("・")]
        assert len(bullets) == len(set(bullets)), f"箇条書きが重複:\n{draft.text}"


# ======================================================================
# 「使うとどうなるか」（future）
# ======================================================================
# 56項目は「化粧品で言ってよい効能のリスト」であり、
# つまり**唯一許された「いい未来」**でもある。
# それを辞書の文のまま使っていたので、何も伝わっていなかった。
#
#   role    毛髪にはり、こしを与えるもの      ← 定義
#   future  ぺたんこの髪に、はりとこしが出る   ← 変化
#
# ただし範囲を超えると薬機法違反になる。ここで機械的に止める。

# 56項目の外。持続性・治療・体質変化の主張。
BEYOND_56 = (
    # 持続性（56項目に時間の保証は無い）
    "翌朝まで", "一日中", "朝まで", "何時間", "持続し", "キープし続け",
    # 治療・改善
    "治る", "治す", "改善", "解消", "なくなる", "消える", "無くなる",
    "生まれ変わ", "再生",
    # 体質・構造の変化
    "肌質が変わ", "体質が変わ", "細胞", "真皮", "角質層を超え",
    # 範囲外の部位・症状
    "シミ", "シワ", "たるみ", "ニキビ", "毛穴", "くすみ", "肌荒れ",
    # 程度の誇張
    "劇的", "圧倒的", "別人", "見違え",
)


def test_futures_stay_within_the_permitted_claims():
    """future が56項目を超えないこと。

    ここを超えると薬機法違反になる。**この検査は緩めないこと。**
    """
    for benefit in BENEFITS:
        hits = [w for w in BEYOND_56 if w in benefit.future]
        assert not hits, (
            f"{benefit.id} の future が56項目の外: {hits}\n"
            f"  {benefit.future}\n"
            f"  （この剤形で言えるのは「{benefit.role}」まで）"
        )


def test_every_form_has_a_future():
    for benefit in BENEFITS:
        assert benefit.future, f"{benefit.id} に future が無い"


def test_futures_describe_a_change_not_a_definition():
    """定義ではなく変化として書くこと。

    「うるおいを与えるもの」は辞書の文で、何も伝わらない。
    「乾いた肌に、うるおいが戻る」は変化なので伝わる。
    """
    for benefit in BENEFITS:
        assert not benefit.future.endswith("もの"), (
            f"{benefit.id} が定義のまま: {benefit.future}"
        )
        assert benefit.future != benefit.role


def test_futures_pass_compliance():
    for benefit in BENEFITS:
        hits = scan(benefit.future, has_link=True)
        assert not hits, f"{benefit.id}: {[h.label for h in hits]}\n  {benefit.future}"


def test_generated_appeals_never_exceed_the_permitted_claims():
    """組み上がった訴求文でも範囲を超えないこと。

    future と pain を組み合わせたときに、思わぬ言い回しが
    生まれていないかを見る。
    """
    from src.content.appeals import APPEALS, AppealContext

    for benefit in BENEFITS:
        for appeal in APPEALS:
            for cursor in range(6):
                ctx = AppealContext(
                    item=make_item(item_price=1500), benefit=benefit,
                    category="スキンケア", cursor=cursor, allowed_numbers=set(),
                )
                text = appeal.build(ctx)
                if not text:
                    continue
                hits = [w for w in BEYOND_56 if w in text]
                assert not hits, f"{appeal.id}/{benefit.id}: {hits}\n  {text}"
                assert not scan(text, has_link=True), f"{appeal.id}/{benefit.id}:\n  {text}"


def test_future_appeal_is_actually_used():
    """未来の軸が実際に出ること。持っているだけでは意味がない。"""
    builder = _builder()
    used = []
    for i in range(30):
        item = make_item(item_code=f"fu:{i}", item_name="化粧水 200mL",
                         shop_code=f"s{i}", item_price=1500)
        draft = builder.build("product", [item], template_id="objective")
        builder.state.record_part_ids(draft.part_ids)
        used.append(draft.part_ids.get("appeal", ""))
    assert "future" in used or "future_effortless" in used, f"未来の軸が出ていない: {set(used)}"
