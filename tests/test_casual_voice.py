"""リンクなし投稿が「人が書いている」形を保っていることのテスト。

もとは、リンクなし投稿149件のうち101件が助言・箇条書き型だった。

    NO_LINK_TOPICS  76件 … 71件（93%）が「主張＋理由」の2段落。1行の投稿はゼロ
    HOWTO_POSTS     15件 … 14件が箇条書き
    THREAD_TOPICS   10件 … 全件が4段落

役に立つことを整った形で言う投稿しか出ていなくて、
それがAIアカウントらしさになっていた。

人がふだんThreadsに書くのは、結論のない一行・答えのない疑問・
ただのぼやきのほうが多い。その比率を保つための縛りを置く。

**全部を有益な内容にしない**、というのがここでの方針。
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from src.compliance.rules import scan
from src.content import parts as P

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️❤♥]+")

# リンクなしで出るプール全部
NO_LINK_POOLS = {
    "NO_LINK_TOPICS": P.NO_LINK_TOPICS,
    "CASUAL_MURMURS": P.CASUAL_MURMURS,
    "HOWTO_POSTS": P.HOWTO_POSTS,
    "THREAD_TOPICS": P.THREAD_TOPICS,
    "QUESTION_POSTS": P.QUESTION_POSTS,
}


# ======================================================================
# つぶやきが「つぶやき」の形をしていること
# ======================================================================
def test_many_murmurs_have_no_emoji():
    """絵文字を毎回付けないこと。

    人は毎回は付けない。全部に付いていると、それだけで機械に見える。
    """
    without = [p for p in P.CASUAL_MURMURS if not EMOJI.search(p.text)]
    ratio = len(without) / len(P.CASUAL_MURMURS)
    assert ratio >= 0.30, (
        f"絵文字なしの投稿が {ratio:.0%} しかない（{len(without)}/{len(P.CASUAL_MURMURS)}件）"
    )


def test_most_murmurs_are_a_single_line():
    """つぶやきは一行で終わるものが大半であること。

    改行して理由を足しはじめると、つぶやきではなく助言になる。
    """
    one_line = [p for p in P.CASUAL_MURMURS if "\n" not in p.text]
    ratio = one_line and len(one_line) / len(P.CASUAL_MURMURS) or 0
    assert ratio >= 0.60, f"1行の投稿が {ratio:.0%} しかない"


def test_murmurs_do_not_give_advice():
    """つぶやきで助言しないこと。

    「〜したほうがいい」が出てきた時点で、それはつぶやきではない。
    """
    advice = ("したほうがいい", "しておくと", "おすすめです", "しましょう",
              "してください", "が大事", "を決めておく")
    for part in P.CASUAL_MURMURS:
        hits = [w for w in advice if w in part.text]
        assert not hits, f"{part.id} が助言になっている（{'/'.join(hits)}）:\n{part.text}"


def test_murmurs_have_grown_enough_to_carry_the_extra_slots():
    """枠を増やしたぶん、母数があること。

    1日5〜6本のリンクなし投稿を回すので、少ないとすぐ一巡する。
    """
    assert len(P.CASUAL_MURMURS) >= 80


# ======================================================================
# 問いかけ
# ======================================================================
def test_questions_actually_ask_something():
    for part in P.QUESTION_POSTS:
        assert "？" in part.text, f"{part.id} に問いかけが無い"


def test_questions_do_not_answer_themselves():
    """自分で答えを書かないこと。

    答えを書くと、問いかけではなく助言になって返信が来なくなる。
    ひとこと添えるのは可（「わたしはワンテンポある」程度）。
    """
    for part in P.QUESTION_POSTS:
        assert len(part.text) <= 80, f"{part.id} が長い（{len(part.text)}字）"
        assert "・" not in part.text, f"{part.id} が箇条書きになっている"


def test_questions_are_easy_to_answer():
    """1件目の行が問いかけで終わること。

    前置きが長いと、何を聞かれているのか分からないまま流される。
    """
    for part in P.QUESTION_POSTS:
        first = part.text.split("\n")[0]
        assert first.rstrip().endswith(("？", "〜？")), (
            f"{part.id} の1行目が問いかけで終わっていない:\n{first}"
        )


def test_there_are_enough_questions_to_rotate():
    assert len(P.QUESTION_POSTS) >= 20


# ======================================================================
# 全体の比率
# ======================================================================
def test_bullet_posts_are_a_minority():
    """箇条書きが全体の3割を超えないこと。

    箇条書きは保存されやすい反面、続くと資料に見える。
    """
    total = sum(len(pool) for pool in NO_LINK_POOLS.values())
    bulleted = sum(
        1
        for pool in NO_LINK_POOLS.values()
        for part in pool
        if "・" in part.text or "①" in part.text
    )
    ratio = bulleted / total
    assert ratio <= 0.30, f"箇条書きが {ratio:.0%}（{bulleted}/{total}件）"


def test_short_posts_are_a_meaningful_share():
    """短い投稿がきちんとした割合を占めること。

    長い投稿ばかりだと、タイムラインで読み飛ばされる。
    """
    total = sum(len(pool) for pool in NO_LINK_POOLS.values())
    short = sum(
        1 for pool in NO_LINK_POOLS.values() for part in pool if len(part.text) <= 40
    )
    ratio = short / total
    assert ratio >= 0.35, f"40字以内の投稿が {ratio:.0%} しかない"


def test_endings_are_not_concentrated():
    """語尾が一種類に偏っていないこと。"""
    tails = Counter()
    for pool in NO_LINK_POOLS.values():
        for part in pool:
            stripped = EMOJI.sub("", part.text.strip())
            if len(stripped) >= 4:
                tails[stripped[-4:]] += 1
    total = sum(tails.values())
    top, count = tails.most_common(1)[0]
    assert count / total <= 0.15, f"語尾「…{top}」が {count}/{total} 件に偏っている"


# ======================================================================
# 中身の安全性（リンクなしでも薬機法は効く）
# ======================================================================
@pytest.mark.parametrize("pool_name", sorted(NO_LINK_POOLS))
def test_no_link_pools_pass_compliance(pool_name):
    for part in NO_LINK_POOLS[pool_name]:
        hits = scan(part.text, has_link=False)
        assert not hits, f"{pool_name}.{part.id}: {[h.label for h in hits]}"


@pytest.mark.parametrize("pool_name", ["CASUAL_MURMURS", "QUESTION_POSTS"])
def test_no_digits_in_short_pools(pool_name):
    """商品データを持たないので数値の裏取りができない。"""
    for part in NO_LINK_POOLS[pool_name]:
        assert not any(c.isdigit() for c in part.text), f"{part.id} に数値"


def test_part_ids_are_unique_across_the_new_pools():
    ids = [p.id for p in P.CASUAL_MURMURS] + [p.id for p in P.QUESTION_POSTS]
    dupes = [i for i, c in Counter(ids).items() if c > 1]
    assert not dupes, f"IDが重複: {dupes}"


# ======================================================================
# 話し言葉であること
# ======================================================================
# 「日焼け止めの塗り直し、タイミングをいつも逃す」は観察メモの形で、
# 人がSNSに書く文ではない。人はこう書く。
#
#   逃しちゃう…
#   のがしちゃった😢
#   忘れないようにしたい😭
#
# 違いは終止形かどうか。動作動詞の終止形で言い切ると、
# 出来事を記録している文になって、喋っている感じが消える。
#
# 「ある」「思ってる」「〜ない」「〜たい」は話し言葉として自然なので
# ここでは対象にしない。狙うのは事実を報告する形の動詞だけ。
STIFF_ENDING = re.compile(
    r"(?<![てでっんなよだかもわ])"
    r"(逃す|なる|減る|変わる|増える|溶ける|終わる|消える|出てくる|かかる"
    r"|見える|違う|分かる|できる|残る|効く|決まる|上がる|下がる|落ちる)$"
)


def _final_line(text: str) -> str:
    """最後の行から絵文字と句点を落としたもの。"""
    last = text.strip().split("\n")[-1]
    return EMOJI.sub("", last).rstrip().rstrip("。")


@pytest.mark.parametrize("pool_name", sorted(NO_LINK_POOLS))
def test_posts_end_in_spoken_form(pool_name):
    for part in NO_LINK_POOLS[pool_name]:
        tail = _final_line(part.text)
        assert not STIFF_ENDING.search(tail), (
            f"{pool_name}.{part.id} が終止形で言い切っている。"
            f"「〜ちゃう」「〜んだよね」のような話し言葉にすること:\n  {part.text}"
        )


def test_spoken_endings_are_varied():
    """話し言葉の語尾も散らすこと。

    全部「〜ちゃう」にすると、それはそれで型になる。
    """
    chau = [p for p in P.CASUAL_MURMURS if "ちゃう" in p.text or "ちゃった" in p.text]
    ratio = len(chau) / len(P.CASUAL_MURMURS)
    assert ratio <= 0.40, f"「〜ちゃう」が {ratio:.0%} を占めている"


def test_some_posts_carry_feeling():
    """感情を出す投稿があること。

    整った観察ばかりだと、書いている人の気持ちが見えない。
    """
    feeling = re.compile("[😢😭🥲😮‍💨🫠🙃]|…$")
    have = [p for p in P.CASUAL_MURMURS if feeling.search(p.text.strip())]
    assert len(have) >= 12, f"感情の出ている投稿が {len(have)}件しかない"
