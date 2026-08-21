"""除外フィルタ。

スコアリングの前に、広告表現リスクの高いカテゴリーを機械的に落とす。
「安全側に倒す」ため、判断に迷う商品は落とす方向に倒している。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..rakuten.models import RakutenItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExclusionResult:
    excluded: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.excluded


def _normalize(text: str) -> str:
    """全角英数と大文字小文字の揺れを吸収して部分一致の取りこぼしを減らす。"""
    table = str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789",
    )
    return text.translate(table).lower()


# 化粧品の剤形を示す語。これが商品名にあれば「塗るもの」と判断できる。
_COSMETIC_FORM_WORDS = (
    "化粧水", "美容液", "乳液", "クリーム", "ローション", "ジェル", "エッセンス",
    "オイル", "パック", "マスク", "ミルク", "ファンデーション", "リップ",
    "シャンプー", "トリートメント", "コンディショナー", "ソープ", "洗顔",
    "クレンジング", "下地", "アイシャドウ", "マスカラ", "チーク", "ブラシ",
)

# 経口サプリでよく使われるが、化粧品の配合成分としても普通に使われる語。
# 単独では判定しない（誤除外が多いため）。剤形語が無いときだけ疑う。
_ORAL_SUSPECT_WORDS = (
    "アスタキサンチン", "エラスチン", "コエンザイムQ10", "NMN", "オルニチン",
    "乳酸菌", "青汁", "酵素ドリンク", "マルチビタミン", "葉酸",
)


def is_excluded(
    item: RakutenItem,
    exclusion: dict[str, Any],
    selection: dict[str, Any],
) -> ExclusionResult:
    """1件の商品が除外対象かを判定する。"""

    # --- 禁止キーワード（商品名・キャッチコピー） ---
    haystack = _normalize(f"{item.item_name} {item.catchcopy or ''}")
    for keyword in exclusion.get("keywords", []):
        if _normalize(keyword) in haystack:
            return ExclusionResult(True, f"禁止キーワード: {keyword}")

    # --- 禁止ジャンル ---
    if item.genre_id:
        blocked = {str(g) for g in exclusion.get("genre_ids", [])}
        if str(item.genre_id) in blocked:
            return ExclusionResult(True, f"禁止ジャンル: {item.genre_id}")

    # --- 禁止ショップ ---
    if item.shop_name:
        shop = _normalize(item.shop_name)
        for keyword in exclusion.get("shop_keywords", []):
            if _normalize(keyword) in shop:
                return ExclusionResult(True, f"禁止ショップ種別: {keyword}")

    # --- 価格帯 ---
    lo = selection.get("min_price")
    hi = selection.get("max_price")
    if lo is not None and item.item_price < lo:
        return ExclusionResult(True, f"価格が下限未満: {item.item_price} < {lo}")
    if hi is not None and item.item_price > hi:
        return ExclusionResult(True, f"価格が上限超過: {item.item_price} > {hi}")

    # --- 在庫 ---
    if selection.get("require_available", True) and item.is_available is False:
        return ExclusionResult(True, "在庫なし")

    # --- 画像 ---
    if selection.get("require_image", True) and not item.image_url:
        return ExclusionResult(True, "商品画像なし")

    # --- 剤形語なしの経口サプリ疑い成分 ---
    #
    # 「アスタキサンチン コラーゲン ヒアルロン酸エラスチン」のような
    # DHC系の商品名は、化粧水・美容液などの剤形語を伴わずに成分名だけが
    # 並ぶことが多く、経口サプリの可能性が高い。
    # 逆に「アスタリフト アドバンスドローション...コラーゲン」のように
    # 剤形語（ローション）が入っていれば化粧品と判断し、除外しない。
    # 成分名だけで機械的に弾くと、コラーゲン配合美容液のような
    # 正当な化粧品まで落としてしまうため、剤形語の有無で切り分ける。
    if not any(_normalize(w) in haystack for w in _COSMETIC_FORM_WORDS):
        for word in _ORAL_SUSPECT_WORDS:
            if _normalize(word) in haystack:
                return ExclusionResult(True, f"剤形語なしの経口サプリ疑い成分: {word}")

    # --- レビュー（客観情報を柱にするアカウントなので必須扱い） ---
    min_count = selection.get("min_review_count")
    if min_count is not None:
        if item.review_count is None:
            return ExclusionResult(True, "レビュー件数が取得できない")
        if item.review_count < min_count:
            return ExclusionResult(True, f"レビュー件数が少ない: {item.review_count} < {min_count}")

    min_avg = selection.get("min_review_average")
    if min_avg is not None:
        if item.review_average is None:
            return ExclusionResult(True, "レビュー平均が取得できない")
        if item.review_average < min_avg:
            return ExclusionResult(True, f"レビュー平均が低い: {item.review_average} < {min_avg}")

    return ExclusionResult(False)


def filter_items(
    items: Iterable[RakutenItem],
    exclusion: dict[str, Any],
    selection: dict[str, Any],
    *,
    excluded_item_codes: Sequence[str] | set[str] = (),
    excluded_url_hashes: Sequence[str] | set[str] = (),
) -> list[RakutenItem]:
    """候補プールから除外対象を落とす。

    excluded_item_codes / excluded_url_hashes には、履歴から作った
    「クールダウン中の商品」を渡す。
    """
    codes = set(excluded_item_codes)
    hashes = set(excluded_url_hashes)

    kept: list[RakutenItem] = []
    reasons: dict[str, int] = {}

    for item in items:
        if item.item_code in codes:
            reasons["再投稿クールダウン中"] = reasons.get("再投稿クールダウン中", 0) + 1
            continue
        if item.item_url_hash in hashes or (
            item.affiliate_url_hash and item.affiliate_url_hash in hashes
        ):
            reasons["URL重複"] = reasons.get("URL重複", 0) + 1
            continue

        result = is_excluded(item, exclusion, selection)
        if result.excluded:
            key = (result.reason or "不明").split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
            continue

        kept.append(item)

    if reasons:
        logger.info(
            "除外内訳: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])),
        )
    logger.info("フィルタ後の候補: %d件", len(kept))
    return kept
