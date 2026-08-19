"""商品スコアリング。

設計上の要点:
  * 重みは config.toml から変更できる
  * **アフィリエイト料率が総合スコアに占める寄与率に上限を設ける**。
    料率の高い商品だけを選び続ける構造を、重み設定ミスがあっても防ぐため。
  * 直近履歴に多いショップ/ブランド/ジャンルは減点し、偏りを抑える
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..rakuten.models import RakutenItem

logger = logging.getLogger(__name__)

# 正規化の基準値。実データの分布に合わせた素朴な定数。
_REVIEW_COUNT_SATURATION = 2000.0   # これ以上は差をつけない
_REVIEW_AVERAGE_FLOOR = 3.0         # 3.0 を 0点、5.0 を 1点とする
_REVIEW_AVERAGE_CEIL = 5.0
_POINT_RATE_SATURATION = 10.0
_AFFILIATE_RATE_SATURATION = 8.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _norm_review_count(count: int | None) -> float:
    """レビュー件数を対数正規化。件数は桁で効くので線形にはしない。"""
    if not count or count <= 0:
        return 0.0
    return _clamp(math.log10(1 + count) / math.log10(1 + _REVIEW_COUNT_SATURATION))


def _norm_review_average(average: float | None) -> float:
    if average is None:
        return 0.0
    span = _REVIEW_AVERAGE_CEIL - _REVIEW_AVERAGE_FLOOR
    return _clamp((average - _REVIEW_AVERAGE_FLOOR) / span)


def _norm_affordability(price: int, lo: int, hi: int) -> float:
    """コスパ重視コンセプトなので、価格帯の中では安いほど高得点。

    ただし最安が常に勝つと品質が下がるので、下限付近は満点にせず緩やかにする。
    """
    if hi <= lo:
        return 0.5
    position = _clamp((price - lo) / (hi - lo))
    return _clamp(1.0 - position ** 0.75)


def _norm_point_rate(rate: float | None) -> float:
    if not rate or rate <= 1:
        return 0.0
    return _clamp((rate - 1.0) / (_POINT_RATE_SATURATION - 1.0))


def _norm_affiliate_rate(rate: float | None) -> float:
    if not rate or rate <= 0:
        return 0.0
    return _clamp(rate / _AFFILIATE_RATE_SATURATION)


@dataclass
class ScoredItem:
    item: RakutenItem
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        parts = [f"{k}={v:+.3f}" for k, v in sorted(self.breakdown.items(), key=lambda kv: -abs(kv[1]))]
        pens = [f"{k}={v:+.3f}" for k, v in self.penalties.items() if v]
        line = f"score={self.score:.4f} [" + " ".join(parts) + "]"
        if pens:
            line += " penalty[" + " ".join(pens) + "]"
        return line


def score_items(
    items: Iterable[RakutenItem],
    scoring: dict[str, Any],
    selection: dict[str, Any],
    *,
    recent_shop_codes: Sequence[str] = (),
    recent_brand_keys: Sequence[str] = (),
    recent_genre_ids: Sequence[str] = (),
) -> list[ScoredItem]:
    """候補をスコア降順で返す。"""
    weights: dict[str, float] = dict(scoring.get("weights", {}))
    total_weight = sum(abs(w) for w in weights.values()) or 1.0

    lo = int(selection.get("min_price", 0))
    hi = int(selection.get("max_price", 100000))

    cap = float(scoring.get("affiliate_rate_contribution_cap", 0.10))
    shop_penalty = float(scoring.get("shop_penalty", 0.35))
    brand_penalty = float(scoring.get("brand_penalty", 0.30))
    genre_penalty = float(scoring.get("genre_penalty", 0.20))

    shop_counts = _count(recent_shop_codes)
    brand_counts = _count(recent_brand_keys)
    genre_counts = _count(recent_genre_ids)

    results: list[ScoredItem] = []

    for item in items:
        features = {
            "review_count": _norm_review_count(item.review_count),
            "review_average": _norm_review_average(item.review_average),
            "price_affordability": _norm_affordability(item.item_price, lo, hi),
            "postage_free": 1.0 if item.is_postage_free else 0.0,
            "point_rate": _norm_point_rate(item.point_rate),
            "affiliate_rate": _norm_affiliate_rate(item.affiliate_rate),
            "has_image": 1.0 if item.image_url else 0.0,
        }

        breakdown = {
            name: (weights.get(name, 0.0) / total_weight) * value
            for name, value in features.items()
        }

        # アフィリエイト料率の寄与に上限をかける。
        # 重みを大きく設定してしまっても、料率だけで順位が決まらないようにする。
        base_total = sum(v for k, v in breakdown.items() if k != "affiliate_rate")
        affiliate_cap = abs(base_total) * cap if base_total else cap
        if breakdown["affiliate_rate"] > affiliate_cap:
            breakdown["affiliate_rate"] = affiliate_cap

        score = sum(breakdown.values())

        # --- 偏り抑制 ---
        penalties: dict[str, float] = {}
        if item.shop_code and (n := shop_counts.get(item.shop_code, 0)):
            penalties["shop"] = -shop_penalty * min(n, 3) / 3
        if (n := brand_counts.get(item.brand_key, 0)):
            penalties["brand"] = -brand_penalty * min(n, 3) / 3
        if item.genre_id and (n := genre_counts.get(str(item.genre_id), 0)):
            penalties["genre"] = -genre_penalty * min(n, 5) / 5

        score += sum(penalties.values())

        results.append(ScoredItem(item=item, score=score, breakdown=breakdown, penalties=penalties))

    # 同点時は item_code で決定的に並べる（実行ごとに結果が揺れないように）
    results.sort(key=lambda s: (-s.score, s.item.item_code))

    if results:
        logger.info(
            "スコアリング完了: %d件 / 1位 %s | %s",
            len(results),
            results[0].item.display_name(28),
            results[0].explain(),
        )
    return results


def _count(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts
