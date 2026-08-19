"""楽天APIレスポンスの正規化。

方針: **取得できなかった値は None のまま保持する。**
0 や空文字で埋めると「レビュー0件」と「レビュー情報なし」が区別できなくなり、
投稿本文に存在しない事実を書いてしまう。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# 商品名からブランドらしき語を推定するための区切り
_BRAND_SPLIT = re.compile(r"[\s【】\[\]（）()「」/｜|・,、]+")
# 商品名の先頭によく付く販促ノイズ
_NOISE_PREFIX = re.compile(
    r"^(?:【[^】]{0,30}】|\[[^\]]{0,30}\]|＼[^／]{0,30}／|»|▼|◆|★|☆|■|◇|●|○)+"
)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _first_image(value: Any) -> str | None:
    """mediumImageUrls は formatVersion によって形が変わるので両方を吸収する。

    formatVersion=1: [{"imageUrl": "..."}]
    formatVersion=2: ["..."]
    """
    if not value:
        return None
    first = value[0] if isinstance(value, list) else value
    if isinstance(first, dict):
        url = first.get("imageUrl")
    else:
        url = first
    if not isinstance(url, str) or not url:
        return None
    # サムネイル指定 (?_ex=128x128) を外して大きい画像にする
    return url.split("?")[0]


@dataclass(frozen=True)
class RakutenItem:
    """楽天市場の商品1件。APIから取れた事実だけを保持する。"""

    item_code: str
    item_name: str
    item_price: int
    item_url: str
    affiliate_url: str | None
    shop_code: str | None
    shop_name: str | None
    genre_id: str | None
    review_count: int | None
    review_average: float | None
    postage_flag: int | None       # 0=送料込み(無料), 1=送料別
    availability: int | None       # 1=在庫あり
    affiliate_rate: float | None
    point_rate: float | None
    image_url: str | None
    catchcopy: str | None
    tax_flag: int | None
    credit_card_flag: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------
    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "RakutenItem":
        """API の Item オブジェクト（formatVersion 1/2 どちらでも可）から生成する。"""
        item = payload.get("Item", payload) if "Item" in payload else payload

        item_code = str(item.get("itemCode") or "").strip()
        item_name = str(item.get("itemName") or "").strip()
        price = _to_int(item.get("itemPrice"))
        item_url = str(item.get("itemUrl") or "").strip()

        if not item_code or not item_name or price is None or not item_url:
            raise ValueError(
                "必須フィールドが欠けています "
                f"(itemCode={item_code!r}, itemName={bool(item_name)}, "
                f"itemPrice={price!r}, itemUrl={bool(item_url)})"
            )

        return cls(
            item_code=item_code,
            item_name=item_name,
            item_price=price,
            item_url=item_url,
            affiliate_url=(str(item["affiliateUrl"]).strip() or None)
            if item.get("affiliateUrl")
            else None,
            shop_code=str(item["shopCode"]) if item.get("shopCode") else None,
            shop_name=str(item["shopName"]) if item.get("shopName") else None,
            genre_id=str(item["genreId"]) if item.get("genreId") else None,
            review_count=_to_int(item.get("reviewCount")),
            review_average=_to_float(item.get("reviewAverage")),
            postage_flag=_to_int(item.get("postageFlag")),
            availability=_to_int(item.get("availability")),
            affiliate_rate=_to_float(item.get("affiliateRate")),
            point_rate=_to_float(item.get("pointRate")),
            image_url=_first_image(item.get("mediumImageUrls") or item.get("smallImageUrls")),
            catchcopy=str(item["catchcopy"]).strip() if item.get("catchcopy") else None,
            tax_flag=_to_int(item.get("taxFlag")),
            credit_card_flag=_to_int(item.get("creditCardFlag")),
            raw=item,
        )

    # ------------------------------------------------------------------
    @property
    def is_postage_free(self) -> bool | None:
        """送料無料か。postageFlag が取れていなければ None（＝書かない）。"""
        if self.postage_flag is None:
            return None
        return self.postage_flag == 0

    @property
    def is_available(self) -> bool | None:
        if self.availability is None:
            return None
        return self.availability == 1

    @property
    def has_review(self) -> bool:
        return bool(self.review_count) and self.review_average is not None

    @property
    def clean_name(self) -> str:
        """販促ノイズを落とした商品名。投稿本文では使わず、ブランド推定と表示用に使う。"""
        name = _NOISE_PREFIX.sub("", self.item_name).strip()
        return name or self.item_name

    @property
    def brand_key(self) -> str:
        """ブランド偏りを抑制するための推定キー。厳密である必要はない。"""
        tokens = [t for t in _BRAND_SPLIT.split(self.clean_name) if t]
        return tokens[0].lower() if tokens else self.clean_name[:8].lower()

    @property
    def affiliate_url_hash(self) -> str | None:
        """公開リポジトリに保存するためのハッシュ。affiliate URL 自体は保存しない。"""
        if not self.affiliate_url:
            return None
        return hashlib.sha256(self.affiliate_url.encode("utf-8")).hexdigest()

    @property
    def item_url_hash(self) -> str:
        return hashlib.sha256(self.item_url.encode("utf-8")).hexdigest()

    def display_name(self, max_length: int = 42) -> str:
        """投稿本文に載せる商品名。長すぎる場合は自然な位置で切る。

        括弧の途中で切れると「[FANCL 化粧水」のように閉じ括弧が無い
        中途半端な表示になるので、括弧の開始位置まで戻して切る。
        """
        name = self.clean_name
        if len(name) <= max_length:
            return self._balance(name)

        cut = name[:max_length]
        # 区切り文字の直前で切ると読みやすい
        for sep in ("　", " ", "／", "/", "・"):
            idx = cut.rfind(sep)
            if idx >= max_length // 2:
                return self._balance(cut[:idx].strip())
        return self._balance(cut.rstrip()) + "…"

    @staticmethod
    def _balance(text: str) -> str:
        """閉じられていない括弧が残っていたら、その開き括弧以降を落とす。"""
        pairs = {"【": "】", "[": "]", "［": "］", "(": ")", "（": "）", "「": "」", "『": "』"}
        for opener, closer in pairs.items():
            last_open = text.rfind(opener)
            if last_open > 0 and text.find(closer, last_open) < 0:
                text = text[:last_open]
        return text.strip(" 　-–—/／・|｜") or text.strip()
