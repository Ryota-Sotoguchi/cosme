"""Compliance Checker のテスト。

投稿してはいけないものを確実に止められることを検証する。
"""

from __future__ import annotations

import pytest

from src.compliance.checker import ComplianceChecker, similarity
from src.compliance.rules import scan
from src.content.builder import ContentBuilder, Draft
from tests.conftest import make_item


@pytest.fixture
def checker(config):
    return ComplianceChecker(config.compliance, config.dedup)


def draft_of(text: str, items=None, allowed=None) -> Draft:
    return Draft(
        text=text,
        template_id="test",
        post_type="product",
        items=items or [],
        allowed_numbers=allowed or set(),
    )


# ======================================================================
# NG表現
# ======================================================================
@pytest.mark.parametrize(
    "text",
    [
        "このクリームでニキビが治る",
        "シミが消えるという評価",
        "肌を根本から改善します",
        "若返りが期待できる",
        "発毛効果があります",
        "皮膚科医が推奨しています",
    ],
)
def test_detects_medical_claims(text):
    categories = {rule.category for rule in scan(text)}
    assert "medical" in categories


@pytest.mark.parametrize(
    "text",
    ["絶対に荒れない処方", "必ず効果が出ます", "100%満足できます", "副作用はありません", "即効性があります"],
)
def test_detects_guarantees(text):
    assert "guarantee" in {rule.category for rule in scan(text)}


@pytest.mark.parametrize(
    "text",
    [
        "使ってみたら良かった",
        "愛用しています",
        "リピ確定です",
        "買ってよかった",
        "私の肌には合っていました",
        "これで肌が変わりました",
    ],
)
def test_detects_fabricated_experience(text):
    """このアカウントの絶対禁止事項。実際に使っていない商品を使ったように書かない。"""
    assert "experience" in {rule.category for rule in scan(text)}


@pytest.mark.parametrize(
    "text",
    ["口コミでは高評価という声が多い", "みんな良いと言っている", "SNSで話題になっています", "評判が良いようです"],
)
def test_detects_fabricated_reviews(text):
    """取得していない口コミを生成しない。"""
    assert "fake_review" in {rule.category for rule in scan(text)}


@pytest.mark.parametrize("text", ["楽天No.1の実力", "ランキング第1位", "最強のクレンジング", "劇的に変わる"])
def test_detects_exaggeration(text):
    assert "exaggeration" in {rule.category for rule in scan(text)}


@pytest.mark.parametrize("text", ["第2類医薬品です", "医薬部外品の薬用クリーム", "サプリメントと併用"])
def test_detects_prohibited_categories(text):
    assert "category" in {rule.category for rule in scan(text)}


def test_clean_objective_text_passes(checker, item):
    text = (
        "【PR】2,000円前後でスキンケアを探してる人向けのメモ。\n\n"
        "モイストクレンジングバーム 90g\n\n"
        "・1,980円\n・レビュー1,284件／平均4.4\n・送料無料\n・ポイント2倍\n\n"
        "この価格帯で探してるなら、比較候補には入れやすい。\n\n"
        f"楽天市場 → {item.affiliate_url}\n\n※価格等は投稿時点"
    )
    assert checker.check(draft_of(text, [item])).passed


# ======================================================================
# PR表記
# ======================================================================
def test_rejects_affiliate_link_without_pr_marker(checker, item):
    text = f"いい感じの商品です。\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert not result.passed
    assert any(v.category == "pr" for v in result.violations)


def test_rejects_pr_marker_placed_too_late(checker, item):
    filler = "あ" * 60
    text = f"{filler}\n【PR】\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert any(v.category == "pr" for v in result.violations)


def test_no_link_post_does_not_need_pr_marker(checker):
    assert checker.check(draft_of("コスメを買うときの予算の決め方の話。")).passed


# ======================================================================
# URL
# ======================================================================
def test_rejects_url_outside_allowlist(checker, item):
    text = "【PR】メモ。\n\nhttps://evil.example.com/x"
    result = checker.check(draft_of(text, [item]))
    assert any(v.category == "url" for v in result.violations)


def test_rejects_url_not_matching_item_data(checker, item):
    other = "https://hb.afl.rakuten.co.jp/hgc/zzz/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fzzz%2F"
    result = checker.check(draft_of(f"【PR】メモ。\n\n{other}", [item]))
    assert any(v.category == "url" for v in result.violations)


def test_rejects_too_many_urls(checker, item):
    text = f"【PR】メモ。\n{item.affiliate_url}\n{item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert any("URLが多すぎ" in v.detail for v in result.violations)


# ======================================================================
# データ整合性
# ======================================================================
def test_rejects_price_that_does_not_match_item_data(checker, item):
    """テンプレートが価格を書き換えていたら止める。"""
    text = f"【PR】メモ。\n\n・1,780円\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert any(v.category == "data" for v in result.violations)


def test_rejects_review_count_that_does_not_match(checker, item):
    text = f"【PR】メモ。\n\n・1,980円\n・レビュー9,999件\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert any(v.category == "data" for v in result.violations)


def test_rejects_review_average_that_does_not_match(checker, item):
    text = f"【PR】メモ。\n\n・1,980円\n・平均4.9\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]))
    assert any(v.category == "data" for v in result.violations)


def test_accepts_numbers_present_in_item_name(checker, item):
    """商品名に含まれる容量などは事実なので許可する。"""
    text = f"【PR】メモ。\n\nモイストクレンジングバーム 90g\n・1,980円\n\n楽天市場 → {item.affiliate_url}"
    assert checker.check(draft_of(text, [item])).passed


# ======================================================================
# 重複
# ======================================================================
def test_rejects_item_in_cooldown(checker, item):
    text = f"【PR】メモ。\n・1,980円\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]), blocked_item_codes={item.item_code})
    assert any(v.category == "dedup" for v in result.violations)


def test_rejects_duplicate_affiliate_url(checker, item):
    text = f"【PR】メモ。\n・1,980円\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item]), blocked_url_hashes={item.affiliate_url_hash})
    assert any(v.category == "dedup" for v in result.violations)


def test_rejects_same_item_twice_in_one_post(checker, item):
    text = f"【PR】メモ。\n・1,980円\n\n楽天市場 → {item.affiliate_url}"
    result = checker.check(draft_of(text, [item, item]))
    assert any("同一投稿内" in v.detail for v in result.violations)


# ======================================================================
# 類似度
# ======================================================================
def test_rejects_near_duplicate_text(checker):
    past = "コスメを買うときの予算の決め方について整理してみた話。"
    now = "コスメを買うときの予算の決め方について整理してみた話です。"
    result = checker.check(draft_of(now), recent_texts=[past])
    assert any(v.category == "similarity" for v in result.violations)


def test_allows_different_products_with_same_template(checker):
    """同じ構造でも商品が違えば通す（数値とURLは類似度計算から除外される）。"""
    a = "【PR】まったく異なる話題その1。スキンケアの選び方の観点を整理する。"
    b = "【PR】別の観点で、ヘアケアを買うときに見る項目を並べておく話。"
    assert similarity(a, b) < 0.72


def test_similarity_ignores_numbers_and_urls():
    a = "【PR】2,000円前後のメモ。 https://hb.afl.rakuten.co.jp/a"
    b = "【PR】2,000円前後のメモ。 https://hb.afl.rakuten.co.jp/b"
    assert similarity(a, b) > 0.9


# ======================================================================
# 文字数
# ======================================================================
def test_rejects_text_over_limit(checker):
    assert not checker.check(draft_of("あ" * 501)).passed


def test_rejects_empty_text(checker):
    assert not checker.check(draft_of("   ")).passed


# ======================================================================
# 生成器との結合: 全テンプレートが合格すること
# ======================================================================
def test_every_template_passes_compliance(config, state, items):
    builder = ContentBuilder(state)
    checker_ = ComplianceChecker(config.compliance, config.dedup)

    cases = [
        ("product", items[:1], ["objective", "short", "checklist", "band_focus"]),
        ("price_band", items, ["price_band"]),
        ("review_heavy", items, ["review_heavy"]),
        ("postage_free", items, ["postage_free"]),
        ("comparison", items[:2], ["comparison"]),
        ("no_link", [], ["topic"]),
    ]
    for post_type, subset, template_ids in cases:
        for template_id in template_ids:
            draft = builder.build(post_type, subset, template_id=template_id)
            result = checker_.check(draft)
            assert result.passed, f"{template_id}: {result.summary()}"


def test_every_no_link_topic_passes_compliance(config, state):
    """すべてのリンクなしトピックが NG表現に触れないこと。"""
    from src.content.parts import NO_LINK_TOPICS

    checker_ = ComplianceChecker(config.compliance, config.dedup)
    for topic in NO_LINK_TOPICS:
        result = checker_.check(draft_of(topic.text))
        assert result.passed, f"{topic.id}: {result.summary()}"
        assert len(topic.text) <= 500, f"{topic.id} が長すぎます"
        # 商品データを持たない投稿なので、数値の裏取りができない。
        # トピック文には数値を書かせない方針にして、データ整合性チェックの
        # 抜け道を作らないようにする。
        assert not any(ch.isdigit() for ch in topic.text), f"{topic.id} に数値が含まれています"


def test_every_phrase_part_is_free_of_ng_expressions():
    """文章パーツ辞書そのものに NG表現が混ざっていないこと。"""
    from src.content import parts as P

    pools = [
        P.PRODUCT_OPENINGS, P.FACT_INTROS, P.PRODUCT_CLOSINGS,
        P.CTA_PARTS, P.DISCLAIMERS, P.ROUNDUP_CLOSINGS, P.NO_LINK_TOPICS,
        *P.ROUNDUP_OPENINGS.values(),
    ]
    for pool in pools:
        for part in pool:
            hits = scan(part.text)
            assert not hits, f"{part.id} に NG表現: {[h.label for h in hits]}"
