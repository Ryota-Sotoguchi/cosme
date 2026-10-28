"""このアカウントにしか書けない型（insider / practical）を見張る。

## なぜこの2つを足したか

2か月半で560本出して、フォロワーは9人・いいねは1日1〜3件だった。
**届いていないのではなく、届いた人が «また読みたい» と思っていない。**

型ごとの表示中央値（2026-10-14〜）:

    question      182   ← 表示は出るが、伸びた上位4本とも いいね0
    no_link       165
    thread_topic  119
    casual         49   ← いちばん低いのに19枠を持っていた
    product         3   （楽天リンク。10/16 に休止）
    site_link       1

`casual`（誰でも書ける雑談）と `question`（見られても反応が無い）から枠を取り、
このアカウントだけの材料に移した。

* `insider`  … 採用する側の席に座ったことがある、という一点
* `practical`… 調べないと分からない手続きの話

どちらも**事故り方が決まっている**ので、そこを機械で止める。
"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content import persona
from src.content.builder import ContentBuilder
from src.content.facts import extract_numbers
from src.content.parts import INSIDER_NOTES, PRACTICAL_NOTES
from src.storage.state import State

ALL = [("insider", p) for p in INSIDER_NOTES] + [("practical", p) for p in PRACTICAL_NOTES]


@pytest.mark.parametrize("pool,part", ALL, ids=[f"{k}-{p.id}" for k, p in ALL])
def test_no_ng_expressions(pool, part):
    hits = scan(part.text)
    assert not hits, f"{part.id}: {[h.label for h in hits]}"


@pytest.mark.parametrize("pool,part", ALL, ids=[f"{k}-{p.id}" for k, p in ALL])
def test_nothing_contradicts_the_persona(pool, part):
    assert not persona.contradicts(part.text), f"{part.id}: {persona.contradicts(part.text)}"


# 法律の断定。`LEGAL_RULES` は «必ず取れる» と «訴えれば勝てる» しか見ていないので、
# 引っかからない言い方も、手続きの話をする型では自分で止める。
LEGAL_ASSERTIONS = (
    "権利があります", "権利がある", "請求できます", "請求できる",
    "義務があります", "違法です", "法律違反", "訴え", "労基署に言え",
    "もらえます", "もらえる権利",
)


@pytest.mark.parametrize("part", PRACTICAL_NOTES, ids=[p.id for p in PRACTICAL_NOTES])
def test_practical_notes_never_assert_the_law(part):
    """**制度の «仕組み» までにとどめる。** 断定は、読んだ人が損をする向きに効く。"""
    for word in LEGAL_ASSERTIONS:
        assert word not in part.text, f"{part.id}: 法律を断定している（{word}）"


@pytest.mark.parametrize("part", PRACTICAL_NOTES, ids=[p.id for p in PRACTICAL_NOTES])
def test_practical_notes_carry_no_amounts_or_day_counts(part):
    """金額と日数は人と時期で変わる。間違えたときに読んだ人が損をする。"""
    numbers = extract_numbers(part.text)
    assert not numbers, f"{part.id}: 数字が入っている {numbers}"


@pytest.mark.parametrize("part", PRACTICAL_NOTES, ids=[p.id for p in PRACTICAL_NOTES])
def test_practical_notes_land_on_what_to_check(part):
    """**答えではなく確認先を渡す。** ここが抜けると «聞きかじり» になる。

    «確かめる» 側の動詞が1つも無い文は、制度の説明で終わっている。
    制度の説明だけなら、調べれば出てくるので、このアカウントで読む理由が無い。
    """
    # **最後の一文で見る。** 本文のどこかに «見ている» があればいい、にすると
    # 「見ているところが違う」のような説明文まで通ってしまい、検査にならない。
    checking = ("確認", "聞い", "聞く", "聞いて", "見ておく", "読んで", "残しておく",
                "出してもら", "調べ")
    last = [line for line in part.text.split("\n") if line.strip()][-1]
    tail = last.rstrip("。").split("。")[-1]
    assert any(w in tail for w in checking), (
        f"{part.id}: 最後が «何を確認すればいいか» で終わっていない → {tail}")


def test_insider_notes_stay_one_short_line():
    """**形は短い独白1本。** 説明を足したくなったら practical / thread_topic に置く。

    «採用する側の席から見えること» かどうかは語では判定できないので、
    ここでは形だけを見る。中身は `parts.INSIDER_NOTES` のコメントが決まり。
    """
    for part in INSIDER_NOTES:
        assert "\n" not in part.text, f"{part.id}: 複数行になっている"
        assert len(part.text) <= 70, f"{part.id}: 長すぎる（{len(part.text)}字）"


def test_the_two_pools_do_not_repeat_each_other():
    """同じ話を両方に置かない（窓の中で同じ趣旨が二度出る）。"""
    import itertools

    def head(text):
        return text.split("。")[0][:14]

    heads = [head(p.text) for p in INSIDER_NOTES] + [head(p.text) for p in PRACTICAL_NOTES]
    dup = [h for h, n in __import__("collections").Counter(heads).items() if n > 1]
    assert not dup, f"書き出しが重なっている: {dup}"


def test_the_pool_ids_are_unique_and_do_not_collide():
    ids = [p.id for p in INSIDER_NOTES] + [p.id for p in PRACTICAL_NOTES]
    assert len(ids) == len(set(ids))
    prefixes = {p.id.split("_")[0] for p in INSIDER_NOTES} | {p.id.split("_")[0] for p in PRACTICAL_NOTES}
    assert prefixes == {"in", "pn"}, f"接頭辞が混ざっている: {prefixes}"


def test_both_pools_are_deep_enough_for_two_weeks():
    """**2週間ぶんの枠より多いこと。** 少ないと同じ文が窓の中で再登場する。"""
    import tomllib

    config = tomllib.load(open("config/config.toml", "rb"))
    share = {}
    for types in config["rotation"].values():
        for t in types:
            share[t] = share.get(t, 0) + 1
    assert len(INSIDER_NOTES) > share["insider"] * 2, "insider の在庫が2週間ぶんに足りない"
    assert len(PRACTICAL_NOTES) > share["practical"] * 2, "practical の在庫が2週間ぶんに足りない"


@pytest.mark.parametrize("post_type", ["insider", "practical"])
def test_the_generated_post_passes_compliance(config, post_type):
    tmp = Path(tempfile.mkdtemp())
    builder = ContentBuilder(State(tmp / "state.json"), voices_path=tmp / "voices.json")
    checker = ComplianceChecker(config.compliance, config.dedup)
    for day in range(6):
        draft = builder.build(post_type, [], with_affiliate_link=False,
                              today=datetime.date(2026, 10, 28) + datetime.timedelta(days=day))
        result = checker.check(draft, recent_texts=[])
        assert result.passed, f"{post_type}: {result.reasons}\n{draft.text}"
        assert "#PR" not in draft.text, "リンクの無い型に広告の印が付いている"


@pytest.mark.parametrize("post_type", ["insider", "practical"])
def test_the_type_needs_no_product(post_type):
    """**ITEMS_NEEDED に無いと .get(post_type, 1) が 1 を返し、商品を取りに行く。**"""
    from src.pipeline import ITEMS_NEEDED

    assert ITEMS_NEEDED.get(post_type) == 0, f"{post_type} が ITEMS_NEEDED に無い"


# ======================================================================
# 仕事の道具（2026-10-28）
#
# 本と違って **レビューが桁違いに多い**（実測で手帳 中央87件 / 本 中央4件）。
# 使った人の声を «誰の感想か分かる形» で引用できるのが、ここを足した理由。
# **こちらが使った体にはしない**（CLAUDE.md §4）。
# ======================================================================

from src.content import tools as T  # noqa: E402
from src.content.voices import BOOK_WORDS, TEXTURE_WORDS, TOOL_WORDS, vocabulary_for  # noqa: E402

TOOL_TEXTS = [
    (b.id, t)
    for b in T.TOOL_BENEFITS
    for t in (b.line, b.concern, b.pain, b.future, *b.tips)
]


@pytest.mark.parametrize("pid,text", TOOL_TEXTS, ids=[f"{i}-{n}" for n, (i, _) in enumerate(TOOL_TEXTS)])
def test_tool_texts_are_clean(pid, text):
    assert not scan(text), f"{pid}: {[h.label for h in scan(text)]}"
    assert not persona.contradicts(text), f"{pid}: {persona.contradicts(text)}"


FIRST_PERSON_USE = ("使ってみた", "愛用", "リピ確定", "買ってよかった",
                    "持ってます", "使ってます", "届いた", "開封")


@pytest.mark.parametrize("pid,text", TOOL_TEXTS, ids=[f"{i}-{n}" for n, (i, _) in enumerate(TOOL_TEXTS)])
def test_tool_texts_never_claim_we_own_it(pid, text):
    """**このアカウントは商品を持っていない。** 出すのは «使った人はこう書いている» まで。"""
    for word in FIRST_PERSON_USE:
        assert word not in text, f"{pid}: 自分が使った体になっている（{word}）"


def test_each_category_counts_reviews_with_its_own_words():
    """**語彙を取り違えると «軽くて読みやすい» が «軽いつけ心地» になる。**"""
    assert vocabulary_for("手帳") is TOOL_WORDS
    assert vocabulary_for("ノート・文具") is TOOL_WORDS
    assert vocabulary_for("ビジネスバッグ") is TOOL_WORDS
    assert vocabulary_for("転職本") is BOOK_WORDS
    assert vocabulary_for("働き方の本") is BOOK_WORDS
    assert vocabulary_for("スキンケア") is TEXTURE_WORDS
    assert vocabulary_for("") is TEXTURE_WORDS


def test_every_configured_genre_has_a_vocabulary(config):
    """**ジャンルを足したら語彙も足す。** 忘れると化粧品の語で数えてしまう。"""
    from src.content.voices import VOCABULARIES

    for genre in config.genres:
        label = genre["label"]
        assert label in VOCABULARIES, f"{label} の語彙が voices.VOCABULARIES に無い"


def test_the_tool_words_say_nothing_about_results():
    """**成果の話を入れない。** 「はかどる」は使った人の解釈で、こちらは裏を取れない。"""
    banned = ("はかどる", "効率", "生産性", "仕事が早く", "年収", "評価が上が", "変わった")
    for label in TOOL_WORDS:
        for word in banned:
            assert word not in label, f"{label}: 成果の語が入っている"
    for label in BOOK_WORDS:
        for word in ("受かった", "合格", "年収", "成功", "人生が"):
            assert word not in label, f"{label}: 成果の語が入っている"


def test_the_roundup_openings_have_every_key_the_templates_ask_for():
    """**欠けると KeyError で投稿が落ちる**（2026-10-28 に実際に落ちた）。"""
    from src.content.books import BOOK_ROUNDUP_OPENINGS
    from src.content.parts import ROUNDUP_OPENINGS

    needed = set(ROUNDUP_OPENINGS)
    assert set(BOOK_ROUNDUP_OPENINGS) >= needed, "本のまとめ形式にキーが足りない"
    assert set(T.TOOL_ROUNDUP_OPENINGS) >= needed, "道具のまとめ形式にキーが足りない"


@pytest.mark.parametrize("name,expected", [
    ("高橋 手帳 2026年 A5 ウィークリー リングダイアリー", "tool_techou_weekly"),
    ("ハイタイド 手帳 2027 マンスリー", "tool_techou_monthly"),
    ("ロルバーン ダイアリー 2026年3月始まり L", "tool_techou"),
    ("方眼ノート A5 5mm ドット方眼", "tool_note_hougan"),
    ("コクヨ キャンパスノート セミB5 B罫", "tool_note"),
    ("ビジネスリュック メンズ 大容量 3way", "tool_bag_ryukku"),
    ("ブリーフケース 本革 トートバッグ", "tool_bag"),
])
def test_the_kind_of_tool_comes_from_the_product_name(name, expected):
    benefit = T.tool_benefit_for(name)
    assert benefit is not None, f"種類が判定できない: {name}"
    assert benefit.id == expected, f"{name} → {benefit.id}（期待 {expected}）"
