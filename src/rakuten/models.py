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
from urllib.parse import parse_qs, urlparse

# 商品名からブランドらしき語を推定するための区切り
_BRAND_SPLIT = re.compile(r"[\s【】\[\]（）()「」/｜|・,、]+")
# 商品名に混ざる販促文言。
# 楽天の商品名は「20％OFFクーポン有!★ベストコスメ殿堂入り★【公式】…」のように
# 販促文が前置されていることが多い。そのまま載せると
#   * 数値が増えて機械的な見た目になる
#   * 「ランキング1位」「殿堂入り」など、こちらで裏を取れない優良誤認表示を
#     再掲することになる（景表法。商品ページの文言でもSNSへそのまま
#     転用してよいとは限らない）
# ので、投稿に載せる前に落とす。
_PROMO_WORDS = (
    r"クーポン|OFF|ｏｆｆ|オフ|割引|セール|SALE|タイムセール|"
    r"ランキング|[0-9０-９]+\s*位|殿堂入り|受賞|大賞|[0-9０-９]+\s*冠|"
    r"送料無料|あす楽|即納|在庫|限定|数量限定|期間限定|"
    r"ポイント|[PpＰ][0-9０-９]+\s*倍|[0-9０-９]+\s*倍|"
    r"最大|還元|プレゼント|おまけ|特典|まとめ買い|"
    r"公式|正規品|新発売|リニューアル|話題|人気|売れ筋|注目|"
    r"[0-9０-９]{1,2}/[0-9０-９]{1,2}|[0-9０-９]+:[0-9０-９]+まで"
)

# 販促語を含む囲み（【】[]（）＼／★☆）をまるごと落とす
_PROMO_BRACKET = re.compile(
    r"(?:【[^】]*(?:" + _PROMO_WORDS + r")[^】]*】"
    r"|\[[^\]]*(?:" + _PROMO_WORDS + r")[^\]]*\]"
    r"|（[^）]*(?:" + _PROMO_WORDS + r")[^）]*）"
    r"|＼[^／]*／"
    r"|[★☆][^★☆]{0,40}[★☆])"
)
# 囲みの外に裸で書かれた販促文。短いものから順に消せるよう分けておく。
_PROMO_PHRASES = tuple(
    re.compile(pattern)
    for pattern in (
        r"[0-9０-９]+\s*[%％]\s*(?:OFF|off|オフ|割引|還元)",
        r"最大半額|半額",
        r"(?:楽天|総合|部門|LDK)?\s*ランキング[^\s]{0,8}[0-9０-９]+\s*位",
        r"[0-9０-９]+\s*冠",
        r"殿堂入り",
        r"クーポン(?:有|あり)?[！!]?",
        # 「9/2 09:59まで」「23:59まで」など期限表記
        r"[0-9０-９]{1,2}\s*[/／]\s*[0-9０-９]{1,2}",
        r"[0-9０-９]{1,2}\s*[:：]\s*[0-9０-９]{2}",
        r"まで[！!]?",
        r"[Pp][0-9０-９]+\s*倍",
        # 配送まわりの販促。囲みの外に裸で置かれることが多い。
        # 「メール便 送料無料 マカダミ屋…」のように商品名の頭に付く。
        r"(?:メール便|ネコポス|ゆうパケット|クリックポスト|宅配便)(?:対応|可)?",
        r"送料無料|送料込み?|あす楽|即日発送|翌日配送|年中無休",
        # 「ポイント5倍!」「人気急上昇中」など、囲みの外に裸で置かれる販促
        r"ポイント[0-9０-９]+\s*倍|[0-9０-９]+\s*倍[！!]",
        r"人気急上昇(?:中)?|話題沸騰|売れてる|大人気|新登場|限定価格",
        r"全[0-9０-９]+\s*種",
    )
)
# 容量・入数の表記。リンク投稿では数値を出さないので落とす。
# 「300mL/レフィル 500mL」「90g」「30枚入り」など。
_VOLUME = re.compile(
    r"[0-9０-９]+(?:\.[0-9０-９]+)?\s*"
    r"(?:mL|ml|ｍｌ|L|g|ｇ|kg|kｇ|個|枚|包|本|袋|回分|回|錠|粒|セット|P|ペア|双)"
    r"(?:入り?|入)?"
)

# 先頭・末尾に残った記号や区切り
_JUNK_EDGE = re.compile(r"^[\s　！!？?・/／|｜\-–—＊*+＋、,。.★☆■◆●○▼▲»▶【】\[\]（）]+")
_JUNK_TAIL = re.compile(r"[\s　！!・/／|｜\-–—＊*+＋、,★☆]+$")


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


def _canonical_item_url(url: str) -> str:
    """アフィリエイトURLなら、包まれている本来の商品URLを取り出す。

    affiliateId を指定すると楽天APIは itemUrl もアフィリエイトURLで返す。
    そのまま公開リポジトリへ保存するとアフィリエイトIDが残るので、
    pc= パラメータに入っている素の商品URLへ戻す。
    """
    if "hb.afl.rakuten.co.jp" not in url and "a.r10.to" not in url:
        return url
    query = parse_qs(urlparse(url).query)
    for key in ("pc", "m"):
        for candidate in query.get(key, []):
            if candidate.startswith("http"):
                return candidate
    return ""


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
            item_url=_canonical_item_url(item_url),
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
        """販促文言を落とした商品名。

        投稿に載せるのはこの結果。元の itemName は raw に残っているので、
        データ整合性チェック（本文の数値が商品データ由来か）は影響を受けない。

        入れ子や連続した販促ブロックがあるので、変化しなくなるまで繰り返す。
        """
        name = self.item_name
        for _ in range(4):
            before = name
            name = _PROMO_BRACKET.sub(" ", name)
            for pattern in _PROMO_PHRASES:
                name = pattern.sub(" ", name)
            name = _JUNK_EDGE.sub("", name)
            name = _JUNK_TAIL.sub("", name)
            # 中身が消えて空になった括弧、および対を失った括弧を落とす。
            # 「【全2種】」の中身だけ消すと「】」が孤立して残る。
            name = re.sub(r"[（(【\[]\s*[）)】\]]", " ", name)
            for opener, closer in (("【", "】"), ("[", "]"), ("（", "）"), ("(", ")")):
                if name.count(opener) != name.count(closer):
                    name = name.replace(opener, " ").replace(closer, " ")
            name = re.sub(r"[\s　]{2,}", " ", name).strip()
            if name == before:
                break
        # 削りすぎて短くなったら元に戻す（商品が特定できないほうが困る）
        return name if len(name) >= 4 else self.item_name

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

    def display_name_without_volume(self, max_length: int = 42) -> str:
        """容量・入数を落とした商品名。

        リンク投稿では数値を出さない方針なので、「90g」「300mL」
        「30枚入り」といった表記を削る。商品の特定に必要な情報は
        ブランド名と品目名なので、容量が無くても困らない。
        """
        name = _VOLUME.sub(" ", self.clean_name)
        name = re.sub(r"[／/]\s*(?:レフィル|詰め替え|つめかえ|替え)", " ", name)
        name = _JUNK_EDGE.sub("", name)
        name = _JUNK_TAIL.sub("", name)
        name = re.sub(r"[\s　]{2,}", " ", name).strip()
        if len(name) < 4:
            return self.display_name(max_length)
        return self._truncate(name, max_length)

    def display_name(self, max_length: int = 42) -> str:
        """投稿本文に載せる商品名。長すぎる場合は自然な位置で切る。

        括弧の途中で切れると「[FANCL 化粧水」のように閉じ括弧が無い
        中途半端な表示になるので、括弧の開始位置まで戻して切る。
        """
        return self._truncate(self.clean_name, max_length)

    def _truncate(self, name: str, max_length: int) -> str:
        """長すぎる名前を、区切りのよい位置で切る。"""
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
