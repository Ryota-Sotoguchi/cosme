"""絵文字が文の中身と合っていることのテスト。

絵文字には役割がある。

  🤍 好意・愛着・うれしさ      「見てて楽しい🤍」
  ☺️ 微笑ましい・和やか        「レビュー多いと安心して見られる☺️」
  😌 落ち着き・あきらめ・苦笑  「だいたい途中で予算上がってる😌」
  🥺 困り・お願い・かわいさ    「ちょうどいい数がいちばん難しい🥺」
  💭 考えている・独り言
  👀 見る・気づく
  📝 メモ・記録

好意の絵文字（🤍☺️）を、困りごとや素っ気ない断定に付けると、
書いている人の感情と文が食い違って見える。

  ×  全部よくしようとすると、だいたい決まらない🤍
  ○  全部よくしようとすると、だいたい決まらない😌

パーツを足すときに崩れやすいので、ここで止める。
"""

from __future__ import annotations

import re

import pytest

from src.content import parts as P

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️❤♥]+")

# 好意・やわらかさを表す絵文字。困りごとの文には付けない。
WARM_EMOJI = ("🤍", "☺️")

# 困りごと・否定・うまくいかなさを示す語
TROUBLE_WORDS = (
    "決まらない", "分からなくなる", "逃す", "ずれる", "ゆるむ",
    "限らない", "できない", "足りない", "外す", "失敗する",
    "無駄", "疲れる", "困る", "止まってる", "慌て",
)

CHECKED_POOLS = {
    "NO_LINK_TOPICS": P.NO_LINK_TOPICS,
    "CASUAL_MURMURS": P.CASUAL_MURMURS,
    "HOWTO_POSTS": P.HOWTO_POSTS,
    "RECOMMEND_OPENINGS": P.RECOMMEND_OPENINGS,
    "RECOMMEND_CLOSINGS": P.RECOMMEND_CLOSINGS,
    "PRODUCT_OPENINGS": P.PRODUCT_OPENINGS,
    "PRODUCT_CLOSINGS": P.PRODUCT_CLOSINGS,
    "CTA_PARTS": P.CTA_PARTS,
}


def _lines_with_trailing_emoji(text: str):
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        found = EMOJI.findall(line)
        if found:
            yield line, found[-1]


@pytest.mark.parametrize("pool_name", sorted(CHECKED_POOLS))
def test_warm_emoji_are_not_used_on_trouble(pool_name):
    for part in CHECKED_POOLS[pool_name]:
        for line, emoji in _lines_with_trailing_emoji(part.text):
            if emoji not in WARM_EMOJI:
                continue
            hits = [w for w in TROUBLE_WORDS if w in line]
            assert not hits, (
                f"{pool_name}.{part.id}: 困りごとの文に {emoji} が付いている"
                f"（{'/'.join(hits)}）\n  {line}"
            )


def test_emoji_are_not_piled_up():
    """同じ行に絵文字を並べすぎないこと。

    「〜だと思う🥺💭🤍」のように積むと、感情が読み取れなくなる。
    CTA の🔖👇のような、役割の違う2つまでは許す。
    """
    for pool_name, pool in CHECKED_POOLS.items():
        for part in pool:
            for line, emoji in _lines_with_trailing_emoji(part.text):
                assert len(emoji) <= 2, (
                    f"{pool_name}.{part.id}: 絵文字が多い（{emoji}）\n  {line}"
                )
