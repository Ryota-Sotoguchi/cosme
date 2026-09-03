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
# 誰の感想なのかを言うこと
# ======================================================================
# 2026-09-03 に方針を反転した。
#
# それまでは「調査報告に見える」という理由で出典を外していた。
# だが出典を消すと「さっぱりのタイプ」という、**誰の感想か分からない文**
# になり、読み手にはこちらが使ったように読める。使っていないので事実と違う。
#
# 出典を言っても硬くならない。人がふだん書くのはこの形。
#
#   ×  レビューによると、さっぱりという評価が多いようです
#   ○  さっぱりって書いてる人が多かった
#
# しかも「使った人がそう言っている」ほうが強い。使っていない人間の
# 「いいと思う」には根拠が無いが、何百人が同じことを書いているのは
# 事実として重い。
ATTRIBUTION_MARKERS = ("書いてる人", "書いてる", "書かれてる", "声が多い",
                       "使った人", "言ってる", "感想", "らしい", "レビュー")


def test_posts_say_whose_impression_it_is():
    """声の軸は、誰の感想かが分かる形で書くこと。"""
    from src.content.appeals import APPEALS, AppealContext
    from src.content.benefits import BENEFITS
    from tests.conftest import make_item

    voiced = [a for a in APPEALS if a.id in ("voices", "pain_voices")]
    assert voiced, "声の軸が無い"
    for appeal in voiced:
        for benefit in BENEFITS:
            for cursor in range(8):
                ctx = AppealContext(
                    item=make_item(), benefit=benefit, category="スキンケア",
                    cursor=cursor, allowed_numbers=set(),
                    voices=tuple(TEXTURE_WORDS),
                )
                text = appeal.build(ctx)
                assert text
                assert any(m in text for m in ATTRIBUTION_MARKERS), (
                    f"{appeal.id} が誰の感想か言っていない:\n  {text}"
                )


# ======================================================================
# ただし、こちらが使った話にはしないこと
# ======================================================================
# 他人の感想を引くのと、自分が使ったことにするのは別。
# このアカウントは商品を使っていない。
#
#   ○  さっぱりって書いてる人が多かった   他人の感想。事実
#   ○  さっぱりらしい                     伝聞。自分の話ではない
#   ×  使ってみたらさっぱりした           使った話。事実と違う
#   ×  わたしも使ってる                   同上
OWN_EXPERIENCE = ("使ってみた", "使ってみて", "試してみた", "塗ってみた",
                  "つけてみた", "わたしも使", "私も使", "自分で使",
                  "買ってよかった", "リピ", "愛用")


def test_posts_do_not_claim_we_used_it():
    from src.content.appeals import APPEALS, AppealContext
    from src.content.benefits import BENEFITS
    from tests.conftest import make_item

    voiced = [a for a in APPEALS if a.id in ("voices", "pain_voices")]
    for appeal in voiced:
        for benefit in BENEFITS:
            for cursor in range(8):
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


def test_voice_phrase_takes_at_most_two_words():
    """2語まで。3語並べるとスペック表になって人の言葉に見えない。"""
    words = ("さっぱり", "伸びがいい", "香りがいい", "長持ちする")
    for cursor in range(4):
        phrase = voice_phrase(words, cursor=cursor, limit=2)
        assert phrase.count("、") <= 1, f"語を並べすぎ: {phrase}"
        for word in words:
            phrase = phrase.replace(word, "")
        assert phrase.replace("し、", "").replace("、", "") == "", "知らない語が混ざった"


def test_voice_sentence_reads_as_someone_elses_words():
    from src.content.voices import voice_sentence

    for cursor in range(8):
        sentence = voice_sentence(("さっぱり", "伸びがいい"), cursor=cursor)
        assert sentence
        assert any(m in sentence for m in ATTRIBUTION_MARKERS), sentence
        assert not any(w in sentence for w in OWN_EXPERIENCE), sentence


def test_voice_sentence_is_empty_without_material():
    """声が無い商品では黙ること。無いものを作らない。"""
    from src.content.voices import voice_sentence

    assert voice_sentence(()) == ""


def test_labels_read_naturally_before_tte():
    """ラベルが「〜って」に繋がる形であること。

    文型が「{語}って書いてる人が多かった」なので、
    語尾は終止形でよい（連体形の制約は 2026-09-03 に外した）。
    「さっぱりだ」のような断定形だけは繋がらない。
    """
    for label in TEXTURE_WORDS:
        assert not label.endswith(("だ", "です", "ます")), (
            f"{label} が「って」に繋がらない"
        )
        assert not label.endswith("、"), f"{label} の末尾が読点"


