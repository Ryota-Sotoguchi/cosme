"""本（転職・資格）の商品リンク投稿（2026-09-15 に楽天のリンク投稿を再開）。

見張ること:

1. 本の文面に、NG表現・コスメの語・読んだ体験の主張・数字が無い
2. 本のカテゴリーでは本のプールが選ばれ、化粧品では従来どおり（休止中のまま壊さない）
3. 書名から「どんな本か」を、具体的なものから判定する
4. 本の商品投稿が compliance を通り、#PR が最後の独立行にある
5. 本の投稿に、化粧品の語で数えた「使った人の声」が混ざらない
6. 「読んでみた」系はリンク投稿で止まり、リンクなしの経験談は止まらない
7. レビューの使用感の収集（化粧品の語）が、本を対象にしない
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import make_item

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content import books as B
from src.content import persona
from src.content.appeals import AppealContext, _surprise, _timing, _value
from src.content.builder import ContentBuilder
from src.content.facts import TOPIC_TAGS
from src.content.templates import BOOK_POOLS, COSME_POOLS, LINK_FIRST, LINK_LAST, PR_TAG, _pools
from src.storage.state import State

ROOT = Path(__file__).resolve().parent.parent

# 本の投稿に出てはいけない語。
# 「送料」「楽天」は楽天ブックスの事実行（送料無料）とリンク先なので、本の投稿では出てよい。
COSME_WORDS = (
    "コスメ", "美容", "スキンケア", "化粧", "メイク", "ヘアケア", "ボディケア",
    "シャンプー", "トリートメント", "日焼け止め", "リップ", "クレンジング", "美容液",
    "乳液", "ファンデ", "ポーチ", "詰め替え", "プチプラ", "デパコス", "肌", "容量",
    "つけ心地", "香り", "成分",
)

BOOK_TEXT_POOLS = {
    "BOOK_RECOMMEND_OPENINGS": B.BOOK_RECOMMEND_OPENINGS,
    "BOOK_RECOMMEND_CLOSINGS": B.BOOK_RECOMMEND_CLOSINGS,
    "BOOK_CTA_PARTS": B.BOOK_CTA_PARTS,
    "BOOK_DISCLAIMERS": B.BOOK_DISCLAIMERS,
    "BOOK_ROUNDUP_CLOSINGS": B.BOOK_ROUNDUP_CLOSINGS,
    **{f"BOOK_ROUNDUP_OPENINGS[{k}]": v for k, v in B.BOOK_ROUNDUP_OPENINGS.items()},
}


def _benefit_texts():
    for benefit in B.BOOK_BENEFITS:
        yield benefit.id, (benefit.line, benefit.concern, benefit.pain, benefit.future, *benefit.tips)


def _all_book_texts():
    for name, pool in BOOK_TEXT_POOLS.items():
        for part in pool:
            yield f"{name}:{part.id}", part.text
    for benefit_id, texts in _benefit_texts():
        for text in texts:
            yield benefit_id, text
    for tip in B.BOOK_GENERIC_TIPS:
        yield "BOOK_GENERIC_TIPS", tip


# ======================================================================
# 1. 文面
# ======================================================================
@pytest.mark.parametrize("where,text", list(_all_book_texts()))
def test_book_texts_are_free_of_ng_expressions(where, text):
    """リンク投稿に出るので、リンク投稿のルールを全部かける。"""
    hits = scan(text, has_link=True)
    assert not hits, f"{where} に NG表現: {[h.label for h in hits]} / {text}"


@pytest.mark.parametrize("where,text", list(_all_book_texts()))
def test_book_texts_carry_no_cosme_words(where, text):
    assert not [w for w in COSME_WORDS if w in text], f"{where}: {text}"


@pytest.mark.parametrize("where,text", list(_all_book_texts()))
def test_book_texts_do_not_claim_to_have_read_the_book(where, text):
    """このアカウントは紹介する本を読んでいない。読んだ体にも、中身を知っている体にもしない。"""
    for phrase in ("読んだ", "読んでみ", "読み終", "読了", "愛読", "この本は", "この本には", "載ってた"):
        assert phrase not in text, f"{where}: 「{phrase}」 / {text}"


@pytest.mark.parametrize("where,text", list(_all_book_texts()))
def test_book_texts_have_no_numbers(where, text):
    """数値は商品データ（facts.py）からだけ入る。文面に数字を書くとデータ整合性チェックが落ちる。"""
    assert not any(ch.isdigit() for ch in text), f"{where}: {text}"


@pytest.mark.parametrize("where,text", list(_all_book_texts()))
def test_book_texts_do_not_contradict_the_persona(where, text):
    assert not persona.contradicts(text), f"{where}: {text}"


def test_book_part_ids_are_unique_and_do_not_collide_with_cosme():
    """同じグループ（cta など）で使用履歴を共有するので、ID が重なるとクールダウンが狂う。"""
    book_ids = [p.id for pool in BOOK_TEXT_POOLS.values() for p in pool]
    cosme_ids = {
        p.id
        for pool in (COSME_POOLS.openings, COSME_POOLS.recommend_openings, COSME_POOLS.closings,
                     COSME_POOLS.recommend_closings, COSME_POOLS.cta, COSME_POOLS.disclaimers,
                     COSME_POOLS.roundup_closings, *COSME_POOLS.roundup_openings.values())
        for p in pool
    }
    # postage_free は comparison の入りを流用しているので、重複はそのぶんだけ
    shared = len(B.BOOK_ROUNDUP_OPENINGS["comparison"])
    assert len(book_ids) - len(set(book_ids)) == shared
    assert not set(book_ids) & cosme_ids


def test_every_book_category_has_topic_tags():
    for category in B.BOOK_CATEGORIES:
        assert TOPIC_TAGS.get(category), category
        assert not [w for tag in TOPIC_TAGS[category] for w in COSME_WORDS if w in tag]


def test_book_categories_match_the_configured_genres(config):
    """楽天のジャンルの label と BOOK_CATEGORIES を一致させる。

    2026-09-28 に実APIで確かめたジャンル（208962 / 208967 / 101937 / 208679）へ
    差し替えた。化粧品のジャンルは config にコメントで残してあるだけ。
    """
    labels = {genre["label"] for genre in config.genres}
    assert labels, "ジャンルが1つも設定されていない"
    assert labels <= B.BOOK_CATEGORIES, f"本以外のジャンルが残っている: {labels - B.BOOK_CATEGORIES}"


# ======================================================================
# 2. プールの切り替え
# ======================================================================
class _Ctx:
    def __init__(self, category):
        self.category = category


@pytest.mark.parametrize("category", sorted(B.BOOK_CATEGORIES))
def test_book_categories_use_the_book_pools(category):
    assert _pools(_Ctx(category)) is BOOK_POOLS
    assert BOOK_POOLS.uses_voices is False


@pytest.mark.parametrize("category", ["スキンケア", "ヘアケア", "コスメ"])
def test_cosme_categories_keep_the_paused_pools(category):
    assert _pools(_Ctx(category)) is COSME_POOLS


# ======================================================================
# 3. 書名からの判定
# ======================================================================
@pytest.mark.parametrize("title,expected", [
    ("転職面接の質問と答え方", "book_mensetsu"),
    ("未経験からの転職の教科書", "book_mikeiken"),
    ("転職の職務経歴書 書き方の基本", "book_keirekisho"),
    ("年収交渉の進め方", "book_koushou"),
    ("日商簿記3級 テキスト", "book_boki"),
    ("FP技能士2級 問題集", "book_fp"),
    ("宅建士 過去問題集", "book_takken"),
    ("ITパスポート 合格教本", "book_it"),
    ("TOEIC L&R 公式問題集", "book_eigo"),
    ("キャリアコンサルタント試験 対策テキスト", "book_shikaku"),
    ("自己分析のやり方", "book_career"),
    ("転職の思考法", "book_tenshoku"),
])
def test_book_kind_is_judged_from_the_specific_to_the_general(title, expected):
    benefit = B.book_benefit_for(title)
    assert benefit is not None and benefit.id == expected, (title, benefit and benefit.id)


def test_an_unknown_title_is_not_forced_into_a_kind():
    """判定できない本に、無関係な種類の選び方を付けない（汎用の選び方に戻る）。"""
    assert B.book_benefit_for("はたらく人の読書案内") is None


def test_every_book_kind_has_what_the_appeals_need():
    for benefit in B.BOOK_BENEFITS:
        assert benefit.keywords and benefit.pain and benefit.future and benefit.concern, benefit.id
        assert len(benefit.tips) >= 3, benefit.id


# ======================================================================
# 4. 生成した本の投稿
# ======================================================================
def _book(code="book:0001", name="転職面接の質問と答え方 [ 山田 太郎 ]", price=1650, label="転職本", **kw):
    params = dict(
        item_code=code, item_name=name, item_price=price, review_count=48, review_average=4.3,
        postage_flag=0, point_rate=1.0, affiliate_rate=2.0, genre_id="001",
        shop_code="book", shop_name="楽天ブックス", genre_label=label,
    )
    params.update(kw)
    return make_item(**params)


BOOKS_FOR_ROUNDUP = [
    ("book:0001", "転職面接の質問と答え方 [ 山田 太郎 ]", 1650),
    ("book:0002", "職務経歴書の書き方 [ 佐藤 花子 ]", 1540),
    ("book:0003", "転職の進め方がわかる本 [ 鈴木 一郎 ]", 1980),
]


def _builder(tmp_path, voices=None):
    voices_path = tmp_path / "voices.json"
    if voices is not None:
        voices_path.write_text(json.dumps(voices, ensure_ascii=False), encoding="utf-8")
    return ContentBuilder(State(tmp_path / "state.json"), voices_path=voices_path)


def _draft(tmp_path, template_id, position, *, items=None, post_type="product", voices=None):
    items = items or [_book()]
    return _builder(tmp_path, voices).build(
        post_type, items, template_id=template_id, with_affiliate_link=True,
        today=datetime.date(2026, 9, 16), link_position=position,
    )


PRODUCT_TEMPLATES = ("short", "band_focus", "checklist", "objective", "thread")
CASES = [
    *[("product", t, p) for t in PRODUCT_TEMPLATES for p in (LINK_FIRST, LINK_LAST)],
    ("comparison", "comparison", LINK_FIRST), ("comparison", "comparison", LINK_LAST),
    ("longform", "longform", LINK_FIRST),
]


def _items_for(post_type):
    if post_type == "comparison":
        return [_book(code, name, price) for code, name, price in BOOKS_FOR_ROUNDUP[:2]]
    if post_type == "longform":
        return [_book(code, name, price) for code, name, price in BOOKS_FOR_ROUNDUP]
    return [_book()]


@pytest.mark.parametrize("post_type,template_id,position", CASES)
def test_book_posts_pass_compliance(tmp_path, config, post_type, template_id, position):
    draft = _draft(tmp_path, template_id, position, items=_items_for(post_type), post_type=post_type)
    result = ComplianceChecker(config.compliance, config.dedup).check(draft)
    assert result.passed, f"{template_id}/{position}: {result.violations}\n---\n{draft.text}"


@pytest.mark.parametrize("post_type,template_id,position", CASES)
def test_book_posts_end_with_the_ad_marker(tmp_path, post_type, template_id, position):
    draft = _draft(tmp_path, template_id, position, items=_items_for(post_type), post_type=post_type)
    assert draft.link_attachment, "リンクカードが付いていない"
    assert PR_TAG in draft.text
    # 連投なら、#PR はいずれかの本の最後の独立行（checker が位置を強制している）
    assert any(seg.rstrip().endswith(f"\n{PR_TAG}") for seg in draft.segments), draft.segments


@pytest.mark.parametrize("post_type,template_id,position", CASES)
def test_book_posts_carry_no_cosme_words_and_use_book_parts(tmp_path, post_type, template_id, position):
    draft = _draft(tmp_path, template_id, position, items=_items_for(post_type), post_type=post_type)
    assert not [w for w in COSME_WORDS if w in draft.text], draft.text
    book_ids = {p.id for pool in BOOK_TEXT_POOLS.values() for p in pool}
    for group in ("cta", "disclaimer", "recommend_opening", "recommend_closing", "roundup_closing"):
        if group in draft.part_ids:
            assert draft.part_ids[group] in book_ids, (group, draft.part_ids[group])


@pytest.mark.parametrize("template_id", PRODUCT_TEMPLATES)
def test_book_posts_ignore_voices_counted_with_cosme_words(tmp_path, template_id):
    """voices.json に化粧品の語の声が入っていても、本の投稿には出さない。"""
    voices = {"book:0001": {"voices": ["軽いつけ心地", "香りがいい"],
                            "counts": {"軽いつけ心地": 40, "香りがいい": 30}}}
    for position in (LINK_FIRST, LINK_LAST):
        draft = _draft(tmp_path, template_id, position, voices=voices)
        assert "つけ心地" not in draft.text and "香り" not in draft.text, draft.text


def test_book_appeals_skip_price_and_consumable_timing():
    """本はほぼ定価なので値段で押さない。「買い替える」「切らしてから」は消耗品の言い方。"""
    item = _book()
    benefit = B.book_benefit_for(item.item_name)
    for cursor in range(8):
        ctx = AppealContext(item=item, benefit=benefit, category="転職本", cursor=cursor,
                            allowed_numbers=set())
        assert _value(ctx) is None
        for text in (_timing(ctx) or "", _surprise(ctx) or ""):
            for word in ("買い替", "切らして", "値段", "高い", "新作", "効いて"):
                assert word not in text, text


# ======================================================================
# 6. 読書体験の判定
# ======================================================================
@pytest.mark.parametrize("text", [
    "この本、読んでみたけど分かりやすかった",
    "わたしも読んだけど、面接の前に役立った",
    "この本のおかげで内定が出た",
    "このテキストで合格できた",
    "何度も読み返してる一冊",
])
def test_reading_claims_are_stopped_in_link_posts_only(text):
    assert "experience" in {r.category for r in scan(text, has_link=True)}, text
    assert "experience" not in {r.category for r in scan(text, has_link=False)}, text


@pytest.mark.parametrize("text", ["この一冊で合格保証", "合格確実のテキスト", "誰でも合格できる勉強法"])
def test_pass_guarantees_are_always_stopped(text):
    assert "career_guarantee" in {r.category for r in scan(text, has_link=False)}, text


def test_advising_the_reader_to_read_is_not_a_reading_claim():
    """読み手への助言（「読んでおくといい」）は、こちらが読んだ主張ではない。"""
    assert not scan("面接の前に一冊だけ読んでおくと、流れがつかめる", has_link=True)


# ======================================================================
# 7. 使用感の収集は本を対象にしない
# ======================================================================
def test_review_collection_skips_books(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("collect_reviews", ROOT / "scripts" / "collect_reviews.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    (tmp_path / "data").mkdir()
    rows = [
        {"item_code": "cosme:1", "item_url": "https://item.rakuten.co.jp/a/1/", "category": "スキンケア"},
        {"item_code": "book:1", "item_url": "https://item.rakuten.co.jp/b/1/", "category": "転職本"},
        {"item_code": "book:2", "item_url": "https://item.rakuten.co.jp/b/2/", "category": "資格本"},
    ]
    (tmp_path / "data" / "history.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    fetched: list[str] = []

    def fake_fetch(url, **kwargs):
        fetched.append(url)
        return [], "テストなので取らない"

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "OUT", tmp_path / "data" / "voices.json")
    monkeypatch.setattr(module, "fetch_reviews", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["collect_reviews.py", "--interval", "0"])

    assert module.main() == 0
    assert fetched == ["https://item.rakuten.co.jp/a/1/"]


# ======================================================================
# 6. 書名で発信ジャンルを見分ける（2026-10-13）
#
# 本のジャンル（ビジネス実用・PC資格）は広く、禁止語に当たらないのに
# 転職・年収・キャリアから外れた本がスコア1位に来ていた。
# **実際に投稿された14冊のうち6冊がそれだった。** その14冊をそのまま
# ここに置いて、同じものが二度と通らないことを見張る。
# ======================================================================

# (書名, 通ってよいか)。コメントは実測の出典
TITLES_SEEN_IN_PRODUCTION = [
    # --- 残すべきもの（発信ジャンルの中にある） ---
    ("コミュニケーションの達人・超人 人間関係改善の秘訣 [ 林雄介 ]", True),
    ("大量に覚えて絶対忘れない「紙1枚」勉強法 [ 棚田　健大郎 ]", True),
    ("人生を変える！「コーチング脳」のつくり方 [ 宮越大樹 ]", True),
    ("このまま今の会社にいていいのか？と一度でも思ったら読む転職の思考法 [ 北野 唯我 ]", True),
    # --- 落とすべきもの（実際に投稿されてしまった） ---
    ("第二級陸上特殊無線技士 試験問題集 （合格精選240題） [ 吉川 忠久 ]", False),
    ("研究発表のためのスライドデザイン （ブルーバックス） [ 宮野 公樹 ]", False),
    ("無料で使える！ 60歳からの最新AI ChatGPT&Gemini 世界一わかりやすい", False),
    ("40代からは「稼ぎ口」を2つにしなさい [ 坂下　仁 ]", False),
    ("育ちのきほん はじめての子育て [ 神田英雄 ]", False),
    ("できるWindows 11 2025年 改訂4版 Copilot対応 （できるシリーズ）", False),
    ("今すぐ使えるかんたん　YouTube入門 ［改訂2版］ [ AYURA ]", False),
    ("マンガでわかる人間関係の心理学 [ 渋谷昌三 ]", False),
    # 書名だけでは何の本か分からないもの。
    # 「書名から種類が分からない本に別の種類の話を付ける」失敗を防ぐため落とす
    ("一緒にいると楽しい人、疲れる人 [ 有川真由美 ]", False),
    ("人の話は、ただ聞けばいい [ 石川 有生 ]", False),
]

# 選定の背骨。これが落ちるようになったら要求語を削りすぎている
TITLES_THAT_MUST_PASS = [
    "日商簿記3級 テキスト＆問題集 2026年度版",
    "いちばんやさしいITパスポート 絶対合格の教科書",
    "MOS Excel 365 対策テキスト＆問題集",
    "職務経歴書の書き方 完全ガイド",
    "面接の達人 2026",
    "社会人1年目の仕事のきほん",
    "FP3級 合格のトリセツ",
    "TOEIC L&Rテスト 文法問題 でる1000問",
    "転職エージェントとの付き合い方",
    "年収交渉の教科書",
]


def _book_item(name):
    return _book(name=name)


@pytest.mark.parametrize("title,should_pass", TITLES_SEEN_IN_PRODUCTION)
def test_titles_seen_in_production_are_judged_as_intended(config, title, should_pass):
    from src.selector.filters import is_excluded

    result = is_excluded(_book_item(title), config.exclusion, config.selection)
    assert result.excluded is not should_pass, f"{title} → {result.reason}"


@pytest.mark.parametrize("title", TITLES_THAT_MUST_PASS)
def test_the_core_of_the_genre_still_passes(config, title):
    from src.selector.filters import is_excluded

    result = is_excluded(_book_item(title), config.exclusion, config.selection)
    assert not result.excluded, f"要求語を削りすぎている: {title} → {result.reason}"


def test_a_title_without_a_genre_word_is_dropped(config):
    """キャッチコピーでは通らない。店が自由に書くので «何の本か» の根拠にならない。"""
    from src.selector.filters import is_excluded

    item = _book(name="きほんのきほん", catchcopy="転職に効く！面接対策の決定版")
    result = is_excluded(item, config.exclusion, config.selection)
    assert result.excluded
    assert result.reason == "ジャンルの語が書名に無い"


def test_the_required_keywords_are_only_for_books(config):
    """化粧品の商品投稿を再開するときに空へ戻せること（剤形語の仕組みとは別）。"""
    from src.selector.filters import is_excluded

    exclusion = dict(config.exclusion)
    exclusion["require_any_keywords"] = []
    item = _book(name="きほんのきほん")
    assert not is_excluded(item, exclusion, config.selection).excluded


# ======================================================================
# 7. 名乗る «何の本か» は書名から決める（2026-10-16）
#
# ジャンルラベルは **楽天のどのジャンルで取ってきたか** でしかない。
# 「考える人のメモの技術 手を動かして答えを出す『万能の問題解決術』」が
# PC・IT資格のジャンルに入っていて、**«資格本は、名前を聞いたことがあるかより
# 自分の状況に合うかで選んだほうがいい» と名乗る投稿が実際に出た**（表示4）。
# CLAUDE.md §5-4「書名から種類が分からない本に、別の種類の話を付けない」。
# ======================================================================

# (書名, 付いていたジャンルラベル)。どれも実際に候補に出たもの
MISLABELLED = [
    ("考える人のメモの技術 手を動かして答えを出す「万能の問題解決術」 [ 下地寛也 ]", "資格本"),
    ("今すぐ使えるかんたん　Access 2021　[Office 2021/Microsoft 365 両対応]", "資格本"),
    ("不格好経営 チームDeNAの挑戦 [ 南場智子 ]", "働き方の本"),
]


def _text_of(tmp_path, item):
    draft = _builder(tmp_path).build(
        "product", [item], template_id="band_focus", with_affiliate_link=True,
        today=datetime.date(2026, 9, 16), link_position=LINK_LAST,
    )
    return draft.text


@pytest.mark.parametrize("title,label", MISLABELLED)
def test_a_title_that_does_not_show_its_kind_never_claims_one(tmp_path, title, label):
    """書名が種類を示していないなら、ジャンルラベルを名乗らない。"""
    text = _text_of(tmp_path, _book(name=title, label=label))
    assert label not in text, f"書名が示していない «{label}» を名乗っている:\n{text}"
    assert B.NEUTRAL_CATEGORY in text or label not in text


@pytest.mark.parametrize("title,label", [
    ("転職面接の質問と答え方 [ 山田 太郎 ]", "転職本"),
    ("日商簿記3級 テキスト＆問題集 2026年度版", "資格本"),
    ("伝え方が9割（2） [ 佐々木圭一 ]", "働き方の本"),
])
def test_a_title_that_shows_its_kind_keeps_the_label(tmp_path, title, label):
    """書名から分かるときは、今までどおりラベルを名乗る（中立の語に逃げない）。"""
    text = _text_of(tmp_path, _book(name=title, label=label))
    assert label in text or B.NEUTRAL_CATEGORY not in text


def test_the_neutral_word_fits_all_three_genres():
    """3つのジャンルどれにも当てはまる語であること（嘘にならない）。"""
    assert B.NEUTRAL_CATEGORY
    assert B.NEUTRAL_CATEGORY not in B.BOOK_CATEGORIES


# ======================================================================
# 8. 働き方の本（ビジネス実用）の種類を書名から判定する（2026-10-16）
#
# それまで BOOK_BENEFITS は転職と資格だけだったので、実際に選ばれた16冊のうち
# 書名から種類が分かったのは2冊だけで、残りはラベルを名乗るだけになっていた。
# ======================================================================

TITLES_AND_KINDS = [
    ("伝え方が9割（2） [ 佐々木圭一 ]", "book_tsutaekata"),
    ("話し方で損する人 得する人", "book_tsutaekata"),
    ("おもろい話し方 芸人だけが知っているウケる会話の法則", "book_tsutaekata"),
    ("よくわかるアサーション　自分の気持ちの伝え方", "book_tsutaekata"),
    ("コミュニケーションの達人・超人 人間関係改善の秘訣 [ 林雄介 ]", "book_ningenkankei"),
    ("お前は私じゃないし、私はお前じゃない 悩みが0になる人間関係術", "book_ningenkankei"),
    ("誰もが人を動かせる!　あなたの人生を変えるリーダーシップ革命 [ 森岡毅 ]", "book_leader"),
    ("考える人のメモの技術 手を動かして答えを出す「万能の問題解決術」", "book_shiryo"),
    ("残業ゼロの段取り術", "book_jikan"),
    # 転職・資格の判定は先に置いてあるので、こちらが勝つ
    ("転職面接の質問と答え方", "book_mensetsu"),
    ("日商簿記3級 テキスト＆問題集", "book_boki"),
]


@pytest.mark.parametrize("title,kind", TITLES_AND_KINDS)
def test_the_kind_comes_from_the_title(title, kind):
    benefit = B.book_benefit_for(title)
    assert benefit is not None, f"種類が判定できない: {title}"
    assert benefit.id == kind, f"{title} → {benefit.id}（期待 {kind}）"


# ======================================================================
# 9. 訴求文は本の確認項目（疑問形）と噛み合う形だけ使う（2026-10-16）
#
# 確認項目は「職場の話か、家族や友人の話か」のような**疑問形**なので、
# 化粧品向けの枠に入れると文にならなかった。実測で出たのがこれ:
#   「働き方の本、『職場の話か、家族や友人の話か』派と…派で選ぶものが変わる」
# あわせて「弱点もある」は、読んでいない本については言えない（§5-4）。
# ======================================================================

from src.content.appeals import APPEALS, AppealContext  # noqa: E402

# 本の確認項目は疑問形。この形を名詞のように扱う言い方は使わない
BROKEN_FRAMES = ("」派", "」重視", "派と「")
# 読んでいない本の中身についての断定
UNFOUNDED = ("弱点もある", "弱点はある")


def _book_appeal_texts(category, title):
    item = _book(name=title, label=category)
    benefit = B.book_benefit_for(title)
    out = []
    for cursor, appeal in enumerate(APPEALS):
        ctx = AppealContext(item=item, benefit=benefit, category=category,
                            cursor=cursor, allowed_numbers=set())
        for shift in range(4):
            ctx = AppealContext(item=item, benefit=benefit, category=category,
                                cursor=cursor + shift * len(APPEALS), allowed_numbers=set())
            text = appeal.build(ctx)
            if text:
                out.append((appeal.id, text))
    return out


BOOK_SAMPLES = [
    ("働き方の本", "お前は私じゃないし、私はお前じゃない 悩みが0になる人間関係術"),
    ("転職本", "採用される履歴書・職務経歴書はこう書く 実例付き"),
    ("資格本", "日商簿記3級 テキスト＆問題集 2026年度版"),
]


@pytest.mark.parametrize("category,title", BOOK_SAMPLES)
def test_book_appeals_never_use_a_frame_that_needs_a_noun(category, title):
    for appeal_id, text in _book_appeal_texts(category, title):
        for frame in BROKEN_FRAMES:
            assert frame not in text, f"{appeal_id}: 疑問形が名詞の枠に入っている\n{text}"


@pytest.mark.parametrize("category,title", BOOK_SAMPLES)
def test_book_appeals_never_claim_a_weakness(category, title):
    """中身を読んでいないので、欠点があるとは言えない。"""
    for appeal_id, text in _book_appeal_texts(category, title):
        for word in UNFOUNDED:
            assert word not in text, f"{appeal_id}: 読んでいない本の欠点を断定している\n{text}"


def test_the_cosme_frames_are_kept_for_the_paused_genre():
    """化粧品側は従来どおり。剤形の確認項目は名詞なので枠に入る。"""
    from src.content.benefits import benefit_for

    item = make_item(
        item_code="cosme:1", item_name="モイストクレンジングバーム 90g", item_price=1980,
        review_count=120, review_average=4.4, postage_flag=0, point_rate=1.0,
        affiliate_rate=4.0, genre_id="100939", shop_code="s", shop_name="店",
        genre_label="スキンケア",
    )
    benefit = benefit_for(item.item_name)
    texts = []
    for cursor, appeal in enumerate(APPEALS):
        ctx = AppealContext(item=item, benefit=benefit, category="スキンケア",
                            cursor=cursor, allowed_numbers=set())
        text = appeal.build(ctx)
        if text:
            texts.append(text)
    assert texts, "化粧品の訴求文が1つも作れていない"


# ======================================================================
# 10. 書き出しと箇条書きで同じ項目を二度出さない（2026-10-16）
#
# 「比較・対立」の軸は確認項目を2つ鍵括弧で引用する。そのすぐ下に
# 同じ項目から始まる箇条書きが並んでいた（実測の band_focus / checklist）。
# 箇条書きは**続きの項目へずらす**（段ごと消すと本文が薄くなる）。
# ======================================================================

@pytest.mark.parametrize("template_id", ["checklist", "band_focus", "short", "objective"])
def test_the_bullets_do_not_repeat_what_the_opening_quoted(tmp_path, template_id):
    item = _book(name="お前は私じゃないし、私はお前じゃない 悩みが0になる人間関係術",
                 label="働き方の本")
    benefit = B.book_benefit_for(item.item_name)
    assert benefit is not None

    draft = _builder(tmp_path).build(
        "product", [item], template_id=template_id, with_affiliate_link=True,
        today=datetime.date(2026, 9, 16), link_position=LINK_LAST,
    )
    text = draft.text
    lines = text.splitlines()
    bullets = [ln.lstrip("・").strip() for ln in lines if ln.startswith("・")]
    if not bullets:
        return  # 箇条書きの無い型は対象外

    # 鍵括弧で引用された項目
    quoted = [tip for tip in benefit.tips if tip and f"「{tip}」" in text]
    overlap = sorted(set(quoted) & set(bullets))
    assert not overlap, f"引用と箇条書きで重複している: {overlap}\n{text}"


def test_the_shift_counts_only_tips_that_are_really_quoted(tmp_path):
    """引用が無いときはずらさない（毎回同じ項目から始まるのを避ける仕組みを壊さない）。"""
    from src.content.templates import _tips_used_in

    class Ctx:
        category = "働き方の本"
        items = [_book(name="伝え方が9割（2） [ 佐々木圭一 ]", label="働き方の本")]
        item = items[0]

    assert _tips_used_in(Ctx(), "ぜんぜん関係のない書き出し") == 0
    benefit = B.book_benefit_for(Ctx.item.item_name)
    lead = f"「{benefit.tips[0]}」と「{benefit.tips[1]}」"
    assert _tips_used_in(Ctx(), lead) == 2
