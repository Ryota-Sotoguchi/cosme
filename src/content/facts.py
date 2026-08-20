"""事実（数値）の描画を一箇所に集約する。

**テンプレート側に数値リテラルを書かない**というのがこのモジュールの存在理由。
本文に現れる数値はすべてここを通るので、
compliance 側で「本文の数値が商品データと一致しているか」を機械的に検証できる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..rakuten.models import RakutenItem

# 本文から数値を抜き出す（カンマ区切り・小数点に対応）
NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def normalize_number(token: str) -> str:
    """"1,980" -> "1980", "4.50" -> "4.5" のように比較用へ正規化する。"""
    cleaned = token.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned or "0"


def extract_numbers(text: str) -> list[str]:
    """本文に現れる数値トークンを正規化して返す。"""
    return [normalize_number(m.group()) for m in NUMBER_PATTERN.finditer(text)]


# ----------------------------------------------------------------------
def format_price(price: int) -> str:
    return f"{price:,}円"


def price_band_value(price: int) -> int:
    """価格帯の丸め値。「2,000円前後」のような表現に使う。"""
    if price < 1000:
        return max(100, round(price / 100) * 100)
    if price < 3000:
        return round(price / 500) * 500
    return round(price / 1000) * 1000


def format_price_band(price: int) -> str:
    return f"{price_band_value(price):,}円前後"


def format_review_average(average: float) -> str:
    """レビュー平均は小数第1位まで。切り上げず必ず切り捨て方向に丸めない
    （四捨五入だが、5.0を超えることはないので誇張にはならない）。"""
    return f"{average:.1f}"


def format_review_count(count: int) -> str:
    return f"{count:,}件"


def format_point_rate(rate: float) -> str:
    return f"{rate:g}倍"


# ----------------------------------------------------------------------
@dataclass
class FactSet:
    """1商品から取り出した「書いてよい事実」の集合。

    取得できなかった値は行ごと存在しない。存在しない事実を補完しない。
    """

    item: RakutenItem
    lines: list[str] = field(default_factory=list)
    allowed_numbers: set[str] = field(default_factory=set)

    def add(self, line: str) -> None:
        self.lines.append(line)

    def allow(self, *tokens: str | int | float) -> None:
        for token in tokens:
            self.allowed_numbers.add(normalize_number(str(token)))


def build_facts(item: RakutenItem, *, bullet: str = "・") -> FactSet:
    """商品から事実行を作る。取得できた項目だけを並べる。

    箇条書き記号は控えめにする。記号を並べると「Botの表」に見えて、
    人が書いたメモらしさが消えるため。
    """
    facts = FactSet(item=item)

    price_text = format_price(item.item_price)
    facts.add(f"{bullet}{price_text}")
    facts.allow(item.item_price, price_band_value(item.item_price))

    if item.review_count is not None and item.review_average is not None and item.review_count > 0:
        avg = format_review_average(item.review_average)
        cnt = format_review_count(item.review_count)
        facts.add(f"{bullet}レビュー{cnt}、平均{avg}")
        facts.allow(item.review_count, avg)
    elif item.review_count is not None and item.review_count > 0:
        cnt = format_review_count(item.review_count)
        facts.add(f"{bullet}レビュー{cnt}")
        facts.allow(item.review_count)

    if item.is_postage_free:
        facts.add(f"{bullet}送料無料")

    if item.point_rate is not None and item.point_rate > 1:
        rate = format_point_rate(item.point_rate)
        facts.add(f"{bullet}ポイント{rate}")
        facts.allow(item.point_rate)

    return facts


def sentence_facts(item: RakutenItem, style: int = 0) -> tuple[str, set[str]]:
    """事実を一言で置く。

    **数値は1つだけ。** 価格・レビュー件数・平均を並べると表になってしまい、
    人が書いた文章に見えなくなる。驚きや温度感は文章パーツ側（開き・締め）が
    担当し、ここは数字を一つ置くだけにする。

    送料無料は数値ではないので、密度を上げずに添えられる。
    """
    allowed: set[str] = {
        normalize_number(str(item.item_price)),
        normalize_number(str(price_band_value(item.item_price))),
    }

    price = format_price(item.item_price)
    free = bool(item.is_postage_free)
    count = item.review_count if (item.review_count or 0) > 0 else None
    average = item.review_average if count is not None else None

    if count is not None:
        allowed.add(normalize_number(str(count)))
    if average is not None:
        allowed.add(normalize_number(format_review_average(average)))

    # 語尾に感情を乗せる。数値は増やさない。
    variants: list[str] = [
        f"{price}。送料もかからないの、うれしい🤍" if free else f"{price}。",
        f"{price}だった…！",
    ]
    if count is not None:
        variants.append(f"レビュー{format_review_count(count)}ついてる👀")
    if average is not None:
        variants.append(f"レビューの平均{format_review_average(average)}だって☺️")
    variants.append(f"{price}で送料無料〜" if free else f"{price}みたい💭")

    return variants[style % len(variants)], allowed


def inline_facts(item: RakutenItem) -> tuple[str, set[str]]:
    """短文型で使う、1〜2文にまとめた事実表現。"""
    allowed: set[str] = {
        normalize_number(str(item.item_price)),
        normalize_number(str(price_band_value(item.item_price))),
    }
    chunks = [format_price(item.item_price)]

    if item.review_count and item.review_count > 0:
        chunks.append(f"レビュー{format_review_count(item.review_count)}")
        allowed.add(normalize_number(str(item.review_count)))
        if item.review_average is not None:
            avg = format_review_average(item.review_average)
            chunks.append(f"平均{avg}")
            allowed.add(normalize_number(avg))

    if item.is_postage_free:
        chunks.append("送料無料")

    if item.point_rate is not None and item.point_rate > 1:
        rate = format_point_rate(item.point_rate)
        chunks.append(f"ポイント{rate}")
        allowed.add(normalize_number(str(item.point_rate)))

    return "／".join(chunks), allowed


def category_label(item: RakutenItem) -> str:
    """収集時に付与したジャンルラベル。無ければ汎用語にフォールバックする。"""
    label = (item.raw or {}).get("_genre_label")
    return str(label) if label else "コスメ"


# 投稿に付けるトピックタグ。
# フォロワーが少ないうちは、タグ経由の流入がほぼ唯一の発見導線になる。
# 広く使われている語を選ぶ（狭すぎると誰も見ていない）。
TOPIC_TAGS: dict[str, tuple[str, ...]] = {
    "スキンケア": ("スキンケア", "コスメ", "プチプラコスメ"),
    "メイク": ("コスメ", "メイク", "プチプラコスメ"),
    "ヘアケア": ("ヘアケア", "コスメ"),
    "ボディケア": ("ボディケア", "コスメ"),
    "美容小物": ("コスメ", "メイク"),
    "コスメ": ("コスメ", "スキンケア", "美容"),
}


def topic_tag_for(category: str, cursor: int = 0, post_type: str = "product") -> str | None:
    """トピックタグを1つ選ぶ。付けないこともある。

    全投稿に付けると宣伝アカウントの見た目になる。人は毎回タグを付けない。

      * つぶやき    … 付けない。「今日の湿気」にコスメタグは明らかに不自然
      * リンクなし  … 3回に1回くらい
      * 商品投稿    … 2回に1回くらい（発見導線が要るので少し高め）

    Threads はタグを1投稿に1つしか付けられないので、
    同じタグに偏らないよう cursor で回す。
    """
    if post_type == "casual":
        return None

    every = 3 if post_type == "no_link" else 2
    if cursor % every != 0:
        return None

    tags = TOPIC_TAGS.get(category) or TOPIC_TAGS["コスメ"]
    return tags[(cursor // every) % len(tags)]
