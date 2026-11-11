"""書ききる長文の連投（型 guide / guide_site）を見張る。2026-11-04 追加。

## なぜこの型を足したか

1日10本の短文（本文の中央値17〜38字）を出していた3週間、枠ごとの表示中央値は2〜18だった。
本人の指示「短文・つぶやきばかりで収益化を狙えていない。有益な発信を長文で」で、
1日3本の長文の連投に替えた（config の [rotation]）。

## 事故り方が決まっているので、機械で止める

* 1本500字を超える（Threads の上限）。合計は超えてよい（checker は1本ずつ見る）
* 数字・統計・法律の断定・保証が混ざる（読んだ人が損をする向きに効く）
* 設定（転職二回・面接官）と食い違う
* guide_site で、**紹介が始まる1本より前にサイトの話が漏れる**。
  #PR は紹介する1本にしか無いので、前に漏れると広告表示より前に宣伝が出る
* guide_site で、作者を名乗る／他人のサイトを見つけた体にする（test_site_link.py と同じ）
"""

from __future__ import annotations

import re
import tempfile
from datetime import date
from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content import persona
from src.content.builder import ContentBuilder
from src.content.guides import GUIDES, SITE_GUIDES
from src.content.parts import SITE_URL
from src.content.templates import PR_TAG
from src.storage.state import State
from tests.test_genre import _cosme_hits
from tests.test_new_post_types import LEGAL_ASSERTIONS
from tests.test_site_link import AUTHOR_WORDS, DISCOVERY_WORDS, OVERCLAIM_WORDS

ALL = [("guide", p) for p in GUIDES] + [("guide_site", p) for p in SITE_GUIDES]
IDS = [f"{k}-{p.id}" for k, p in ALL]

# サイトの紹介が始まったと読める語。guide_site の本文（最後の1本より前）に出さない。
SITE_WORDS = ("ページ", "サイト", "チェッカー", "http", ".dev", "リンク")


def _builder() -> ContentBuilder:
    tmp = Path(tempfile.mkdtemp())
    return ContentBuilder(State(tmp / "state.json"), voices_path=tmp / "voices.json")


def _build_all(post_type: str, pool) -> list:
    """プールの全記事を1本ずつ組み上げる。

    選び方（builder._pick）は直近を避けて回すだけで、件数ぶん回しても一巡するとは限らない。
    ここで見たいのは «どの記事を引いても通るか» なので、引く記事を指定して組む。
    """
    builder = _builder()
    drafts = []
    for part in pool:
        builder._pick = lambda group, parts, _part=part, **flags: _part
        drafts.append(
            builder.build(post_type, [], with_affiliate_link=False, today=date(2026, 11, 4))
        )
    return drafts


# ======================================================================
# 在庫
# ======================================================================
def test_the_pools_outlast_the_similarity_window(config):
    """**類似度の窓（2週間）より長く回せること。** 足りないと窓の中で尽きて短文へ逃げる。"""
    per_week = {"guide": 0.0, "guide_site": 0.0}
    for options in config.rotation.values():
        for post_type in per_week:
            per_week[post_type] += options.count(post_type) / len(options) * 7
    assert len(GUIDES) >= 32
    assert len(GUIDES) > per_week["guide"] * 2, per_week
    # guide_a8 は guide_site と同じ記事を同じ選び方のグループで引く（2026-11-04）
    per_week["guide_site"] += sum(
        options.count("guide_a8") / len(options) * 7 for options in config.rotation.values()
    )
    assert len(SITE_GUIDES) > per_week["guide_site"] * 2, per_week


def test_ids_and_texts_are_unique():
    ids = [p.id for _, p in ALL]
    assert len(ids) == len(set(ids))
    assert len({p.text for _, p in ALL}) == len(ALL)


# ======================================================================
# 形
# ======================================================================
@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_every_post_fits_the_platform_limit(pool, part):
    # guide_site は最後の1本に URL と #PR が付く。その分を足しても収まること
    suffix = len(f"\n{SITE_URL}\n\n{PR_TAG}") if pool == "guide_site" else 0
    for index, segment in enumerate(part.segments):
        extra = suffix if index == len(part.segments) - 1 else 0
        assert 0 < len(segment) + extra <= 500, f"{part.id} {index + 1}本目: {len(segment) + extra}字"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_it_is_actually_long(pool, part):
    """**長文であること。** 連投4本以上・合計400字以上（短文の中央値は17〜38字だった）。"""
    assert 4 <= len(part.segments) <= 8, f"{part.id}: {len(part.segments)}本"
    assert len(part.text) >= 400, f"{part.id}: {len(part.text)}字"


# ======================================================================
# 形の種類（2026-11-06）
#
# 本人から「長文系が全く同じで THE・AI って感じ。指の絵文字も乱用」と言われた。
# 当時は42本すべてが «1本目の最後に👇 → ①見出し → 本文» の同じ骨組みで、
# «» が30か所あった。**形の種類が少ないこと** が問題だったので、種類の数を見張る。
# ======================================================================
# 記号で形が分かるものは、記号が本文にあることも確かめる（名前だけ付けて中身が同じ、を防ぐ）
SHAPE_MARKS = {
    "言い換え": ("×", "○"),
    "チェックリスト": ("☐",),
    "一問一答": ("Q.", "A."),
    "時系列": ("【",),
    "セリフ例": ("「",),
    "自分への質問": ("？",),
}
# 全部の記事の決まり文句になっていた言い回し
STOCK_PHRASES = ("👇", "👉", "«", "»", "置いておく")


def test_every_article_has_a_shape():
    from src.content.guides import SHAPE_OF

    ids = {p.id for p in (*GUIDES, *SITE_GUIDES)}
    assert set(SHAPE_OF) == ids, f"形の名前が無い記事: {ids - set(SHAPE_OF)} / 余分: {set(SHAPE_OF) - ids}"


def test_the_shapes_are_mixed():
    """**形は8種類以上。一つの形が全体の4分の1を超えないこと。**"""
    from collections import Counter

    from src.content.guides import SHAPE_OF

    shapes = Counter(SHAPE_OF.values())
    assert len(shapes) >= 8, dict(shapes)
    total = sum(shapes.values())
    for name, count in shapes.items():
        assert count / total <= 0.25, f"{name} が {count}/{total} 本: {dict(shapes)}"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_the_shape_is_real(pool, part):
    from src.content.guides import SHAPE_OF

    body = "\n".join(part.segments[1:])
    for mark in SHAPE_MARKS.get(SHAPE_OF.get(part.id, ""), ()):
        assert mark in body, f"{part.id} は {SHAPE_OF[part.id]} なのに {mark} が無い"


def test_numbered_headings_are_the_exception():
    """①見出しの骨組みは全記事で使っていた。使うのは一部だけにする。"""
    numbered = [p.id for p in (*GUIDES, *SITE_GUIDES) if "①" in p.text]
    assert len(numbered) <= len(GUIDES) // 5, numbered


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_no_stock_phrases(pool, part):
    """👇・«»・「置いておく」を使わない。どれも全記事の決まり文句だった。"""
    hits = [w for w in STOCK_PHRASES if w in part.text]
    assert not hits, f"{part.id}: {hits}"


def test_the_openings_do_not_repeat():
    """書き出しが似ないこと（最初の8字が重ならない・1本目の終わり方が偏らない）。"""
    from collections import Counter

    openings = [p.segments[0][:8] for p in (*GUIDES, *SITE_GUIDES)]
    assert len(openings) == len(set(openings)), Counter(openings).most_common(3)
    endings = Counter(p.segments[0].rstrip("。")[-3:] for p in (*GUIDES, *SITE_GUIDES))
    assert endings.most_common(1)[0][1] <= 3, endings.most_common(3)


DOING = ("確認", "確かめ", "聞", "書", "メモ", "読", "見て", "開", "録音", "直す", "足す",
         "調べ", "入れておく", "入っておく", "作っておく", "やっておく", "一覧", "更新",
         "済ませ", "埋め", "並べ")
# 自分の話・オチで終わる形（くだけた本音の口調。2026-11-06 に本人が選んだ）
PERSONAL = ("わたし", "自分", "笑", "思う", "です。", "でした", "ない。", "なる。")


def _last_block(part):
    return [block for block in part.segments[-1].split("\n\n") if block.strip()][-1]


def _ending_kind(part) -> str:
    last = _last_block(part)
    if "？" in last:
        return "問いかけ"
    if any(w in last for w in PERSONAL):
        return "自分の話"
    if any(w in last for w in DOING):
        return "宿題"
    return "説明"


@pytest.mark.parametrize("part", GUIDES, ids=[p.id for p in GUIDES])
def test_the_guide_does_not_end_on_a_dry_explanation(part):
    """**最後の段落が説明で終わらないこと。** 調べれば出てくる話になる。

    行き先は «次に何をするか» だけではない（2026-11-11）。問いかけでも、
    自分の話やオチでもいい。下の `test_the_endings_are_mixed` が偏りを見る。
    """
    assert _ending_kind(part) != "説明", f"{part.id}: 最後が説明で終わっている → {_last_block(part)}"


def test_the_endings_are_mixed():
    """**締めの形を混ぜる。**

    2026-11-11 に本人から「長文やスレッド型はいいけど、それしかなくて
    AIにやらせてる・堅苦しいイメージ」と言われた。数えたら、
    **42本のうち37本が «〜してみて» の宿題で終わっていた**。

    原因はこのテストの前の版で、«最後は次に何をするか» を**全記事に要求して
    いた**こと。役に立たせるための決まりが、プリントの読後感を作っていた。
    いまは «どれか一つに寄らないこと» を見る（全部をオチにするのも同じ失敗）。
    """
    from collections import Counter

    kinds = Counter(_ending_kind(p) for p in GUIDES)
    assert len(kinds) >= 3, f"締めの形が少ない: {kinds}"
    top, count = kinds.most_common(1)[0]
    assert count <= len(GUIDES) * 0.6, f"«{top}» に偏っている: {kinds}"
    assert kinds["宿題"] >= 4, f"宿題で終わる記事が無さすぎる: {kinds}"


# ======================================================================
# 中身
# ======================================================================
@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_no_ng_expressions(pool, part):
    """リンクなしの型でも、景表法系（リンク投稿用）のルールまで当てておく。"""
    assert not scan(part.text, has_link=False), [h.label for h in scan(part.text, has_link=False)]
    if pool == "guide_site":
        assert not scan(part.text, has_link=True), [h.label for h in scan(part.text, has_link=True)]


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_no_statistics_or_deadlines(pool, part):
    """**本人の数字は書く。統計の数字と手続きの期限は書かない**（2026-11-06）。

    もとは «数字を一切書かない» にしていたが、それが文を無機質にしていた
    （「〇〇万円」の伏せ字、年齢の無い失敗談）。人は «27歳のとき» «年収420万» と書く。
    止めるのは、読んだ人が損をする向きに効く数字だけ。
    """
    text = part.text
    for word in ("平均", "中央値", "調査によると", "統計では", "割の人", "%の人", "％の人"):
        assert word not in text, f"{part.id}: 統計の言い方（{word}）"
    deadline = re.search(r"[0-9０-９]+\s*(日|週間|か月|ヶ月|カ月)\s*(以内|前まで|までに)", text)
    assert not deadline, f"{part.id}: 手続きの期限を書いている（{deadline.group()}）"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_the_ages_match_the_persona(pool, part):
    """記事に出てくる年齢が、中の人の設定（persona.PERSONA_AGES）と食い違わないこと。"""
    from src.content.persona import PERSONA_AGES

    ages = {int(n) for n in re.findall(r"([0-9]{2})歳", part.text)}
    assert ages <= PERSONA_AGES, f"{part.id}: 設定に無い年齢 {ages - PERSONA_AGES}"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_own_salary_matches_the_persona(pool, part):
    """«わたしの年収» として出てくる数字は、設定の 380 / 420 / 520 万だけ。"""
    from src.content.persona import PERSONA_SALARIES

    own = re.findall(r"(?:今の年収は|今の年収が|わたしの|年収は)([0-9]{3})万", part.text)
    for amount in own:
        assert int(amount) in PERSONA_SALARIES.values(), f"{part.id}: 設定に無い自分の年収 {amount}万"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_never_asserts_the_law(pool, part):
    for word in LEGAL_ASSERTIONS:
        assert word not in part.text, f"{part.id}: 法律を断定している（{word}）"


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_fits_the_persona_and_the_genre(pool, part):
    assert not persona.contradicts(part.text), persona.contradicts(part.text)
    assert not _cosme_hits(part.text), _cosme_hits(part.text)


@pytest.mark.parametrize("pool,part", ALL, ids=IDS)
def test_the_data_carries_no_url_and_no_ad_mark(pool, part):
    """URL と #PR はテンプレートが付ける。文面に書くと置き場所の検査をすり抜ける。"""
    assert "http" not in part.text and ".dev" not in part.text
    assert PR_TAG not in part.text


# ======================================================================
# guide_site — 紹介は最後の1本だけ
# ======================================================================
@pytest.mark.parametrize("part", SITE_GUIDES, ids=[p.id for p in SITE_GUIDES])
def test_the_site_is_not_mentioned_before_the_last_post(part):
    """**紹介が始まる1本より前に、サイトの話を出さない。** #PR はその1本にしか無い。"""
    for index, segment in enumerate(part.segments[:-1]):
        hits = [w for w in SITE_WORDS if w in segment]
        assert not hits, f"{part.id} {index + 1}本目にサイトの話: {hits}"


@pytest.mark.parametrize("part", SITE_GUIDES, ids=[p.id for p in SITE_GUIDES])
def test_the_invite_never_claims_or_disguises_who_made_the_site(part):
    hits = [w for w in (*AUTHOR_WORDS, *DISCOVERY_WORDS, *OVERCLAIM_WORDS) if w in part.text]
    assert not hits, f"{part.id}: {hits}"


def test_the_site_guide_puts_the_url_and_the_ad_mark_on_the_last_post():
    for draft in _build_all("guide_site", SITE_GUIDES):
        *body, last = draft.segments
        assert re.findall(r"https?://\S+", draft.text) == [SITE_URL]
        assert SITE_URL in last
        # 広告表示は紹介する1本の最後の行。後ろに行を続けない
        assert last.rstrip().endswith(f"\n{PR_TAG}"), last
        assert draft.text.count(PR_TAG) == 1
        for segment in body:
            assert "http" not in segment and PR_TAG not in segment, segment
        # 商品を持たないので、楽天のリンク枠にも «アフィリエイトリンクあり» にも数えない
        assert not draft.items and not draft.link_attachment and not draft.has_affiliate_link


def test_the_plain_guide_carries_no_link_at_all():
    for draft in _build_all("guide", GUIDES):
        assert "http" not in draft.text
        assert PR_TAG not in draft.text
        assert not draft.link_attachment


# ======================================================================
# 検査を通ること
# ======================================================================
@pytest.mark.parametrize("post_type,pool", [("guide", GUIDES), ("guide_site", SITE_GUIDES)])
def test_every_built_post_passes_compliance(config, post_type, pool):
    checker = ComplianceChecker(config.compliance, config.dedup)
    drafts = _build_all(post_type, pool)
    assert len({d.part_ids[post_type] for d in drafts}) == len(pool), "一巡で全部使われていない"
    for draft in drafts:
        result = checker.check(draft, recent_texts=[])
        assert result.passed, f"{result.summary()}\n{draft.text}"
        # 合計は500字を超える。1本ずつの上限で見ていることの確認にもなる
        assert all(len(seg) <= 500 for seg in draft.segments)


def test_the_site_guide_without_the_ad_mark_is_stopped(config):
    """#PR を外したら落ちること（検査側が本当に要求しているか）。"""
    import dataclasses

    checker = ComplianceChecker(config.compliance, config.dedup)
    draft = _build_all("guide_site", SITE_GUIDES)[0]
    segments = [seg.replace(f"\n\n{PR_TAG}", "") for seg in draft.segments]
    stripped = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = checker.check(stripped, recent_texts=[])
    assert not result.passed
    assert any(v.category == "pr" for v in result.violations), result.summary()


def test_a_single_post_over_the_limit_is_still_stopped(config):
    """1本ずつ見るようにしても、1本が500字を超えたら止まること。"""
    import dataclasses

    checker = ComplianceChecker(config.compliance, config.dedup)
    draft = _build_all("guide", GUIDES)[0]
    segments = [*draft.segments[:-1], "あ" * 501]
    long = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = checker.check(long, recent_texts=[])
    assert any(v.category == "format" for v in result.violations), result.summary()


def test_a_runaway_thread_is_stopped(config):
    """本数の上限（max_thread_posts）を超えた連投は止まること。自己返信も1投稿に数えられる。"""
    import dataclasses

    checker = ComplianceChecker(config.compliance, config.dedup)
    draft = _build_all("guide", GUIDES)[0]
    segments = [f"{seg}" for seg in draft.segments] * 3
    long = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = checker.check(long, recent_texts=[])
    assert any("連投が長すぎます" in v.detail for v in result.violations), result.summary()
