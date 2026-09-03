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


# ----------------------------------------------------------------------
# おおよその言い方
#
# 人は「1,980円」「レビュー1,284件、平均4.4」とは書かない。
# メモに残すときも「2,000円くらい」「レビューめっちゃ多い」と書く。
# 細かい桁まで出すと、値札を転記した機械の文になる。
#
# ## 安全側の丸め方
#
# 景表法（有利誤認）を避けるため、**実際より安く見せない**。
#   「◯円台」   … 切り捨て。3,520円 → 3,500円台。必ず正しい
#   「◯円くらい」… 切り上げ。3,520円 → 3,600円くらい。実際はより安い
#   「◯円ちょっと」… 切り捨て＋超過の明示。3,520円 → 3,500円ちょっと
#
# レビュー件数も同じで、「◯件超え」は切り捨てた値にしか使わない。
# ----------------------------------------------------------------------
def _grain(price: int) -> int:
    """価格の丸め幅。桁が上がるほど粗くする。"""
    if price < 1000:
        return 100
    if price < 10000:
        return 100
    return 1000


def approx_price(price: int, style: int = 0) -> tuple[str, set[str]]:
    """おおよその価格。(本文, 本文に出る数値) を返す。

    style を回して同じ言い方が続かないようにする。
    """
    grain = _grain(price)
    floor = (price // grain) * grain
    ceil = floor + grain if price % grain else price

    allowed = {normalize_number(str(floor)), normalize_number(str(ceil))}
    forms = [
        f"{ceil:,}円くらい",
        f"{floor:,}円台",
        f"{ceil:,}円くらい",
        # 「ちょっと」で済むのは、丸め幅の3割まではみ出した場合まで。
        # 12,800円を「12,000円ちょっと」と書くと 800円ぶん安く見える。
        f"{floor:,}円ちょっと" if (price - floor) <= grain * 0.3 else f"{floor:,}円台",
    ]
    # ぴったりの値段なら「ちょっと」「台」は嘘になる
    if price % grain == 0:
        forms = [f"{price:,}円", f"{price:,}円ぴったり", f"{price:,}円"]
        allowed = {normalize_number(str(price))}
    return forms[style % len(forms)], allowed


def approx_review_count(count: int, style: int = 0) -> tuple[str, set[str]]:
    """おおよそのレビュー件数。少ないときは数を出さない。

    「1,284件」と書くのは人の書き方ではない。
    数えた人にしか意味の無い桁は落とす。
    """
    if count < 50:
        return "", set()
    if count < 300:
        forms = ["レビューもそこそこある", "レビューはそれなりに付いてる"]
        return forms[style % len(forms)], set()

    grain = 1000 if count >= 1000 else 100
    floor = (count // grain) * grain
    if floor >= count:
        text = f"レビュー{floor:,}件"
    else:
        forms = [
            f"レビュー{floor:,}件超え",
            f"レビューが{floor:,}件以上ついてる",
            f"レビュー{floor:,}件超え",
        ]
        text = forms[style % len(forms)]
    return text, {normalize_number(str(floor))}


def approx_review_average(average: float, count: int, style: int = 0) -> str:
    """レビュー平均は数字にしない。

    「4.4」と書いても読み手は判断できず、桁を写しただけになる。
    件数が少ないうちは平均そのものに意味が無いので黙る。
    """
    if count < 30:
        return ""
    if average >= 4.5:
        forms = ["評価もかなり高い", "評価も高い", "評価はだいぶ良さそう"]
    elif average >= 4.2:
        forms = ["評価も高め", "評価は良いほう", "評価も悪くない"]
    else:
        return ""
    return forms[style % len(forms)]


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


def build_facts(item: RakutenItem, *, bullet: str = "・", style: int = 0) -> FactSet:
    """商品から事実行を作る。取得できた項目だけを並べる。

    箇条書き記号は控えめにする。記号を並べると「Botの表」に見えて、
    人が書いたメモらしさが消えるため。

    **数値はおおよそにする。** 人は値札の桁をそのまま書き写さない。
    「1,980円」「レビュー1,284件、平均4.4」は、読み手の判断を助けないうえ、
    機械が転記した文にしか見えない。
    """
    facts = FactSet(item=item)

    price_text, price_allowed = approx_price(item.item_price, style)
    facts.add(f"{bullet}{price_text}")
    for token in price_allowed:
        facts.allowed_numbers.add(token)
    # 価格帯の言い回し（別のパーツが使う）も許可しておく
    facts.allow(item.item_price, price_band_value(item.item_price))

    count = item.review_count or 0
    if count > 0:
        review_text, review_allowed = approx_review_count(count, style)
        if review_text:
            average_text = (
                approx_review_average(item.review_average, count, style)
                if item.review_average is not None
                else ""
            )
            # 「〜あるで評価も高め」のような接続にならないよう読点で繋ぐ
            line = f"{review_text}、{average_text}" if average_text else review_text
            facts.add(f"{bullet}{line}")
            for token in review_allowed:
                facts.allowed_numbers.add(token)
        # 正確な件数・平均も許可はしておく（他のパーツが使う可能性がある）
        facts.allow(count)
        if item.review_average is not None:
            facts.allow(format_review_average(item.review_average))

    # 送料とポイントは1行にまとめる。
    #
    # 行を分けると4行の箇条書きになり、人のメモではなく仕様表に見える。
    # どちらも短いので、並べても読みにくくならない。
    extras: list[str] = []
    if item.is_postage_free:
        extras.append("送料無料")
    if item.point_rate is not None and item.point_rate > 1:
        # ポイント倍率はもともと粗い整数で、人もそのまま口にする数字。
        # ここは丸めない（「20倍」を「20倍くらい」と書く人はいない）。
        extras.append(f"ポイント{format_point_rate(item.point_rate)}")
        facts.allow(item.point_rate)
    if extras:
        facts.add(f"{bullet}{'で'.join(extras)}")

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

    price, price_allowed = approx_price(item.item_price, style)
    allowed |= price_allowed
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
        review_text, review_allowed = approx_review_count(count, style)
        if review_text:
            allowed |= review_allowed
            variants.append(f"{review_text}👀")
    if average is not None and count is not None:
        average_text = approx_review_average(average, count, style)
        if average_text:
            variants.append(f"{average_text}みたい☺️")
    variants.append(f"{price}で送料無料〜" if free else f"{price}みたい💭")

    return variants[style % len(variants)], allowed


def inline_facts(item: RakutenItem, style: int = 0) -> tuple[str, set[str]]:
    """短文型で使う、1〜2文にまとめた事実表現。

    ここもおおよそにする。桁をそのまま並べると値札の転記になる。
    """
    allowed: set[str] = {
        normalize_number(str(item.item_price)),
        normalize_number(str(price_band_value(item.item_price))),
    }
    price_text, price_allowed = approx_price(item.item_price, style)
    allowed |= price_allowed
    chunks = [price_text]

    count = item.review_count or 0
    if count > 0:
        review_text, review_allowed = approx_review_count(count, style)
        if review_text:
            chunks.append(review_text)
            allowed |= review_allowed
        allowed.add(normalize_number(str(count)))
        if item.review_average is not None:
            average_text = approx_review_average(item.review_average, count, style)
            if average_text:
                chunks.append(average_text)
            allowed.add(normalize_number(format_review_average(item.review_average)))

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
