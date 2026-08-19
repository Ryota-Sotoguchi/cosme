"""除外フィルタとスコアリングのテスト。"""

from __future__ import annotations

from src.selector.filters import filter_items, is_excluded
from src.selector.scoring import score_items
from tests.conftest import make_item


# ======================================================================
# 除外カテゴリー
# ======================================================================
def test_excludes_otc_medicine(config):
    item = make_item(item_name="【第2類医薬品】かゆみ止めクリーム 20g")
    result = is_excluded(item, config.exclusion, config.selection)
    assert result.excluded
    assert "禁止キーワード" in result.reason


def test_excludes_quasi_drug_and_yakuyou(config):
    """医薬部外品は安全に扱える実装ができるまで除外する。"""
    for name in ("薬用美白クリーム", "【医薬部外品】デオドラント"):
        assert is_excluded(make_item(item_name=name), config.exclusion, config.selection).excluded


def test_excludes_supplements_and_diet(config):
    for name in ("コラーゲンサプリ 90粒", "ダイエット サポート ドリンク", "健康食品 青汁"):
        assert is_excluded(make_item(item_name=name), config.exclusion, config.selection).excluded


def test_excludes_hair_growth(config):
    for name in ("育毛トニック 200ml", "発毛促進エッセンス"):
        assert is_excluded(make_item(item_name=name), config.exclusion, config.selection).excluded


def test_excludes_blocked_genre(config):
    item = make_item(genre_id="551167")  # 医薬品・コンタクト・介護
    assert is_excluded(item, config.exclusion, config.selection).excluded


def test_excludes_drugstore_shops(config):
    item = make_item(shop_name="楽天ドラッグストア")
    assert is_excluded(item, config.exclusion, config.selection).excluded


def test_matches_keyword_in_fullwidth(config):
    """全角表記でも取りこぼさない。"""
    item = make_item(item_name="ＡＧＡ 対策ローション")
    assert is_excluded(item, config.exclusion, config.selection).excluded


def test_excludes_out_of_price_range(config):
    assert is_excluded(make_item(item_price=200), config.exclusion, config.selection).excluded
    assert is_excluded(make_item(item_price=9800), config.exclusion, config.selection).excluded


def test_excludes_out_of_stock_and_no_image(config):
    assert is_excluded(make_item(availability=0), config.exclusion, config.selection).excluded
    assert is_excluded(make_item(image=False), config.exclusion, config.selection).excluded


def test_excludes_low_or_missing_reviews(config):
    assert is_excluded(make_item(review_count=1), config.exclusion, config.selection).excluded
    assert is_excluded(make_item(review_count=None), config.exclusion, config.selection).excluded
    assert is_excluded(make_item(review_average=2.0), config.exclusion, config.selection).excluded


def test_keeps_ordinary_cosmetic(config, item):
    assert not is_excluded(item, config.exclusion, config.selection).excluded


def test_filter_items_applies_cooldowns(config, items):
    kept = filter_items(
        items,
        config.exclusion,
        config.selection,
        excluded_item_codes={items[0].item_code},
    )
    assert items[0] not in kept
    assert len(kept) == 2


def test_filter_items_blocks_by_url_hash(config, items):
    kept = filter_items(
        items,
        config.exclusion,
        config.selection,
        excluded_url_hashes={items[1].item_url_hash},
    )
    assert items[1] not in kept


# ======================================================================
# スコアリング
# ======================================================================
def test_reviews_outrank_affiliate_rate(config):
    """料率が高いだけの商品が、レビューが厚い商品に勝たないこと。"""
    high_rate = make_item(item_code="a:1", shop_code="a", review_count=6,
                          review_average=3.6, affiliate_rate=8.0, postage_flag=1)
    well_reviewed = make_item(item_code="b:2", shop_code="b", review_count=4000,
                              review_average=4.6, affiliate_rate=1.0, postage_flag=0)

    ranked = score_items([high_rate, well_reviewed], config.scoring, config.selection)
    assert ranked[0].item.item_code == "b:2"


def test_affiliate_rate_contribution_is_capped(config, item):
    """料率の寄与が上限を超えないこと。"""
    ranked = score_items([item], config.scoring, config.selection)
    scored = ranked[0]
    base = sum(v for k, v in scored.breakdown.items() if k != "affiliate_rate")
    cap = base * config.scoring["affiliate_rate_contribution_cap"]
    assert scored.breakdown["affiliate_rate"] <= cap + 1e-9


def test_shop_bias_is_penalised(config):
    a = make_item(item_code="a:1", shop_code="samestore")
    b = make_item(item_code="b:2", shop_code="otherstore")

    ranked = score_items(
        [a, b], config.scoring, config.selection,
        recent_shop_codes=["samestore", "samestore", "samestore"],
    )
    assert ranked[0].item.shop_code == "otherstore"
    penalised = next(s for s in ranked if s.item.shop_code == "samestore")
    assert penalised.penalties["shop"] < 0


def test_cheaper_item_scores_higher_when_otherwise_equal(config):
    cheap = make_item(item_code="a:1", item_price=900, shop_code="a")
    pricey = make_item(item_code="b:2", item_price=4800, shop_code="b")
    ranked = score_items([cheap, pricey], config.scoring, config.selection)
    assert ranked[0].item.item_code == "a:1"


def test_ordering_is_deterministic(config, items):
    first = [s.item.item_code for s in score_items(items, config.scoring, config.selection)]
    second = [s.item.item_code for s in score_items(items, config.scoring, config.selection)]
    assert first == second
