"""実際に使った人の声を材料にするときの制約。

## 何を守っているか

**本文を転載しない。** レビューは書いた人の著作物。
語の出現傾向という事実にすれば、著作物ではなくなる。

**使用感だけ。** 薬機法で、化粧品の広告に体験談を効能効果の
証明として使うことはできない。

    ×  「シミが消えたという声が多い」   効能の保証。違反
    ○  「伸びがいいという声が多い」     使用感。効能ではないので可
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from src.compliance.rules import scan
from src.content.voices import (
    FORBIDDEN_IN_VOICES,
    TEXTURE_WORDS,
    extract_voices,
    load_voices,
    voice_phrase,
)


# ======================================================================
# 拾う語が使用感に限られていること
# ======================================================================
def test_texture_words_contain_no_efficacy_claims():
    """拾う語に効能を入れないこと。

    ここに「シミが薄くなった」を入れた瞬間、
    体験談による効能の保証になって薬機法違反になる。
    """
    for label, variants in TEXTURE_WORDS.items():
        for word in (label, *variants):
            hits = [w for w in FORBIDDEN_IN_VOICES if w in word]
            assert not hits, f"{label}: 効能の語が混ざっている {hits}"


def test_texture_labels_pass_compliance():
    for label in TEXTURE_WORDS:
        assert not scan(label, has_link=True), label


# ======================================================================
# 効能に触れたレビューは使わない
# ======================================================================
def test_reviews_mentioning_efficacy_are_dropped_whole():
    """効能に触れたレビューは、丸ごと集計から外すこと。

    使用感の語が入っていても、そのレビューを根拠にはしない。
    「伸びが良くてシミも薄くなった」を「伸びがいい」の1件として
    数えると、実質その体験談を引いていることになる。
    """
    reviews = [
        "伸びが良い。シミが薄くなりました",   # 効能に触れている → 丸ごと除外
        "伸びが良くて使いやすい",
        "伸びが良い",
    ]
    summary = extract_voices("x:1", reviews)
    counts = dict(summary.counts)
    assert counts.get("伸びがいい") == 2, "効能に触れたレビューが数えられている"


@pytest.mark.parametrize("body", [
    "毛穴が目立たなくなりました",
    "ニキビが減った気がする",
    "肌質が変わりました",
    "効果を実感しています",
])
def test_efficacy_reviews_produce_nothing(body):
    summary = extract_voices("x:1", [body, body])
    assert not summary.counts


# ======================================================================
# 「多かった」と言える根拠があること
# ======================================================================
def test_single_mention_is_not_counted():
    """1件しか言っていないものは「多かった」と書けない。"""
    summary = extract_voices("x:1", ["しっとり"])
    assert not summary.counts


def test_two_mentions_are_enough():
    summary = extract_voices("x:1", ["しっとりする", "しっとりして良い"])
    assert dict(summary.counts).get("しっとり") == 2


# ======================================================================
# 出力
# ======================================================================
def test_phrase_quotes_the_labels_not_the_reviews():
    """出すのは決めた言い方であって、レビュー本文ではないこと。"""
    reviews = ["伸びが良くてベタつかないので毎日愛用しています"] * 2
    summary = extract_voices("x:1", reviews)
    phrase = summary.phrase()
    assert "毎日愛用" not in phrase, "レビュー本文が漏れている"
    assert "伸びがいい" in phrase


def test_phrase_has_no_counts():
    """件数を出さないこと。

    こちらの集計方法に依存する数字なので、
    「10人が言った」とは書けない。
    """
    summary = extract_voices("x:1", ["しっとり"] * 9)
    phrase = summary.phrase()
    assert not any(c.isdigit() for c in phrase), phrase


def test_voice_phrase_rotates():
    words = ("伸びがいい", "べたつかない", "香りがいい")
    seen = {voice_phrase(words, cursor=c) for c in range(3)}
    assert len(seen) >= 2, "cursor を変えても同じ文になる"


def test_voice_phrase_is_empty_without_material():
    assert voice_phrase(()) == ""


# ======================================================================
# 保存形式
# ======================================================================
def test_saved_file_never_contains_review_bodies():
    """保存するのは語のリストだけ。本文を残さないこと。

    data/ は公開リポジトリに入る。
    """
    path = pathlib.Path("data/voices.json")
    if not path.exists():
        pytest.skip("まだ収集していない")
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = set(TEXTURE_WORDS)
    for code, entry in raw.items():
        assert set(entry) <= {"updated_at", "reviews_seen", "voices"}, code
        for word in entry["voices"]:
            assert word in allowed, f"{code}: 決めた語以外が保存されている「{word}」"


def test_load_voices_survives_a_missing_file():
    """ファイルが無くても落ちないこと。声が無くても投稿は成立する。"""
    missing = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    assert load_voices(missing) == {}


def test_load_voices_survives_broken_json():
    path = pathlib.Path(tempfile.mkdtemp()) / "broken.json"
    path.write_text("{ これは壊れている", encoding="utf-8")
    assert load_voices(path) == {}


# ======================================================================
# 組み上がった投稿
# ======================================================================
def test_generated_voice_appeals_pass_compliance():
    from src.content.appeals import APPEALS, AppealContext
    from src.content.benefits import BENEFITS
    from tests.conftest import make_item

    voiced = [a for a in APPEALS if a.id in ("voices", "pain_voices")]
    assert voiced, "声の軸が無い"

    words = tuple(TEXTURE_WORDS)
    for appeal in voiced:
        for benefit in BENEFITS:
            for cursor in range(6):
                ctx = AppealContext(
                    item=make_item(), benefit=benefit, category="スキンケア",
                    cursor=cursor, allowed_numbers=set(), voices=words,
                )
                text = appeal.build(ctx)
                assert text
                assert not scan(text, has_link=True), f"{appeal.id}:\n  {text}"
                assert not any(c.isdigit() for c in text), text


# ======================================================================
# 出典を投稿に出さないこと
# ======================================================================
# 「レビューで多かったのは」と毎回言うと、投稿が調査報告に見える。
# 出典は投稿の中身ではないので出さない（根拠は data/voices.json に残る）。
SOURCE_WORDS = ("レビュー", "口コミ", "クチコミ", "評価では", "使った人",
                "みんな", "声が多い", "という声", "調べた", "集計")


def test_posts_never_name_the_source():
    """投稿に出典を書かないこと。"""
    from src.content.appeals import APPEALS, AppealContext
    from src.content.benefits import BENEFITS
    from tests.conftest import make_item

    voiced = [a for a in APPEALS if a.id in ("voices", "pain_voices")]
    for appeal in voiced:
        for benefit in BENEFITS:
            for cursor in range(6):
                ctx = AppealContext(
                    item=make_item(), benefit=benefit, category="スキンケア",
                    cursor=cursor, allowed_numbers=set(),
                    voices=tuple(TEXTURE_WORDS),
                )
                text = appeal.build(ctx)
                assert text
                hits = [w for w in SOURCE_WORDS if w in text]
                assert not hits, f"{appeal.id} が出典を書いている {hits}:\n  {text}"


# ======================================================================
# 出典を消しても、自分の体験にはしないこと
# ======================================================================
# 出典を消したうえで「さっぱりした」と書くと、こちらが使った話に読める。
# このアカウントは商品を使っていない。
#
# 体験ではなく **商品の性質** として書く。
#
#   ×  さっぱりして良かった   使った話になる
#   ○  さっぱりのタイプ       商品の性質。誰の体験でもない
#   ○  さっぱりらしい         伝聞。自分の話ではない
OWN_EXPERIENCE = ("してみた", "してみて", "した感じ", "でした", "だった",
                  "良かった", "よかった", "気に入", "使ってる", "使った")


def test_posts_do_not_read_as_our_own_experience():
    from src.content.appeals import APPEALS, AppealContext
    from src.content.benefits import BENEFITS
    from tests.conftest import make_item

    voiced = [a for a in APPEALS if a.id in ("voices", "pain_voices")]
    for appeal in voiced:
        for benefit in BENEFITS:
            for cursor in range(6):
                ctx = AppealContext(
                    item=make_item(), benefit=benefit, category="スキンケア",
                    cursor=cursor, allowed_numbers=set(),
                    voices=tuple(TEXTURE_WORDS),
                )
                text = appeal.build(ctx)
                hits = [w for w in OWN_EXPERIENCE if w in text]
                assert not hits, (
                    f"{appeal.id} が自分の体験に読める {hits}:\n  {text}"
                )


def test_voice_phrase_stays_single():
    """1語だけにすること。

    2語並べると「さっぱり、伸びがいいのタイプ」のように繋ぎが崩れる。
    地の文に溶かす前提なので、列挙にしない。
    """
    words = ("さっぱり", "伸びがいい", "香りがいい")
    for cursor in range(3):
        phrase = voice_phrase(words, cursor=cursor)
        assert "、" not in phrase, f"複数語が並んでいる: {phrase}"
        assert phrase in words


def test_labels_connect_to_a_noun():
    """ラベルが連体形であること。

    「さっぱりする」だと「さっぱりするのタイプ」になって崩れる。
    """
    for label in TEXTURE_WORDS:
        assert not label.endswith("する"), f"{label} が連体形になっていない"
