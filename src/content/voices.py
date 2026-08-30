"""実際に使った人の声から、使用感だけを取り出す。

## なぜ「そのまま載せない」のか

レビュー本文は書いた人の著作物なので、まるごと転載はできない。
**語の出現回数という事実**にすれば、著作物ではなくなる。

    ×  「伸びが良くてベタつかないので毎日使ってます」（本文の転載）
    ○  レビューで多かったのは「伸びがいい」「べたつかない」（事実の要約）

## なぜ「使用感だけ」なのか

薬機法。化粧品の広告で、**体験談を効能効果の証明に使うことはできない**。

    ×  「シミが消えたという声が多い」   効能の保証。違反
    ○  「伸びがいいという声が多い」     使用感。効能ではないので可

使用感（テクスチャ・香り・使い勝手）は効能ではないため、
56項目の制約の外にある。ここだけを拾う。

## 何を数えるか

あらかじめ決めた語だけを数える。レビューに出てくる言葉を
自由に拾うと、効能の語まで入ってしまう。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# 拾う使用感。**効能の語をここに入れないこと。**
#
# key   … 投稿に出す言い方
# value … レビュー本文で探す表記ゆれ
# ラベルは **連体形**（「〜な」「〜の」で名詞に繋がる形）にする。
# 「さっぱりする」だと「さっぱりするで伸びがいい」のように繋がらない。
TEXTURE_WORDS: dict[str, tuple[str, ...]] = {
    "伸びがいい": ("伸びが良", "伸びがい", "のびが良", "のびがい", "伸びやすい"),
    "べたつかない": ("べたつかな", "ベタつかな", "べたべたしな", "ベタベタしな",
                  "さらっと", "サラッと", "さらさら"),
    "しっとり": ("しっとり",),
    "さっぱり": ("さっぱり", "サッパリ"),
    "とろみがある": ("とろみ", "とろっと"),
    "軽いつけ心地": ("軽いつけ", "軽い付け", "重くない", "軽くて"),
    "香りがいい": ("香りが良", "香りがい", "いい香り", "良い香り"),
    "香りが控えめ": ("香りが控え", "無香", "香りが弱", "きつくない"),
    "泡立ちがいい": ("泡立ちが良", "泡立ちがい", "よく泡立"),
    "少量で足りる": ("少量で", "ちょっとで", "少しで足"),
    "落としやすい": ("落としやす", "落ちやす", "するっと落"),
    "容器が使いやすい": ("使いやす", "出しやす", "ポンプが", "詰め替えやす"),
    "刺激が少ない": ("刺激が少な", "ひりひりしな", "痛くな"),
}

# レビューに出てきても**絶対に拾わない**語。
# 効能の主張なので、体験談として引くと薬機法違反になる。
# 拾う語は「使用感」に限る。
#
# 「リピートしてる」「コスパがいい」は入れない。
#   「リピートしてる」はこちらが使った体験に読める（既存の検査でも弾かれた）
#   「また買ってる」も主語が曖昧で同じ問題がある
#   「コスパがいい」は価格の主観評価で、こちらでは裏を取れない
#
# 残すのは **テクスチャ・香り・使い勝手** だけ。
# 誰が言っても同じ意味になり、こちらの立場と混ざらない語。
FORBIDDEN_IN_VOICES = (
    "シミ", "しみ", "シワ", "しわ", "たるみ", "ニキビ", "にきび", "毛穴",
    "くすみ", "美白", "治", "改善", "消え", "効果", "効いた", "変わった",
    "肌質", "アトピー", "アレルギー", "赤み", "炎症",
)


@dataclass(frozen=True)
class VoiceSummary:
    """あるひとつの商品について、レビューから数えた使用感。"""

    item_code: str
    total_reviews: int
    # 出現回数の多い順。(投稿に出す言い方, 件数)
    counts: tuple[tuple[str, int], ...]

    @property
    def top(self) -> tuple[str, ...]:
        """よく言われている使用感。多い順。"""
        return tuple(word for word, _ in self.counts)

    def phrase(self, limit: int = 2) -> str:
        """投稿に出す一文。材料が足りなければ空。

        **件数は出さない。** レビューの総数は書いてよいが、
        「10人が伸びがいいと言った」は数え方の説明が要るうえ、
        こちらの集計方法に依存する数字なので出さない。
        """
        words = self.top[:limit]
        if not words:
            return ""
        joined = "」「".join(words)
        return f"「{joined}」"


def extract_voices(item_code: str, reviews: list[str]) -> VoiceSummary:
    """レビュー本文の一覧から、使用感の出現回数を数える。

    本文は保存しない。数えた結果だけを返す。
    """
    counter: Counter[str] = Counter()
    for body in reviews:
        text = body or ""
        # 効能に触れているレビューは丸ごと使わない。
        # 使用感の語が入っていても、そのレビューを根拠にはしない。
        if any(word in text for word in FORBIDDEN_IN_VOICES):
            continue
        for label, variants in TEXTURE_WORDS.items():
            if any(v in text for v in variants):
                counter[label] += 1

    # 1件しか言っていないものは「多かった」と書けない
    counts = tuple((w, n) for w, n in counter.most_common() if n >= 2)
    return VoiceSummary(item_code=item_code, total_reviews=len(reviews), counts=counts)


# ======================================================================
# 保存済みの声を読む
# ======================================================================
def load_voices(path: "Path") -> dict[str, tuple[str, ...]]:
    """collect_reviews.py がためた結果を読む。

    無ければ空。**投稿は声が無くても成立する**ので、
    読めないことを失敗にしない。
    """
    import json

    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        code: tuple(entry.get("voices", []))
        for code, entry in raw.items()
        if entry.get("voices")
    }


def voice_phrase(voices: tuple[str, ...], *, cursor: int = 0, limit: int = 1) -> str:
    """投稿に出す言い方。

    **1語だけにする。** 2語並べると「さっぱり、伸びがいいのタイプ」の
    ように繋ぎが不自然になる。地の文に溶かす前提なので、
    列挙ではなく一つの性質として置く。

    件数は出さない。こちらの集計方法に依存する数字なので、
    「何人が言った」とは書けない。
    """
    if not voices:
        return ""
    start = cursor % len(voices)
    picked = list(dict.fromkeys(
        voices[(start + i) % len(voices)] for i in range(min(limit, len(voices)))
    ))
    # 鉤括弧で括ると「引用してます」という顔になる。
    # 出典を出さない方針なので、地の文に混ぜる。
    return "、".join(picked)
