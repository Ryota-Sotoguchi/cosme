"""投稿文が言い切りになっていることのテスト。

「〜な気がする」「〜かも」で締めると、読む側に「で、どっち？」が残る。
参考にしている伸びているアカウントは、例外なく言い切ってから理由を足す。

薬機法が縛るのは **商品の効能** であって **買い物の考え方** ではない。
「この化粧水でシミが消える」は違反だが、
「安い店を探し続けるのは時間の無駄」は何の問題もない。
ここは言い切ってよい領域なので、使い切る。
"""

from __future__ import annotations

from src.compliance.rules import scan
from src.content import parts as P

# 主張をぼかす語。casual な語尾（〜だよね、〜けど）は含めない。
# ここで狙うのは「結論を濁すこと」であって、口調を硬くすることではない。
HEDGE_WORDS = ("かも", "気がする", "かもしれ", "じゃないかな", "ような気")


def _hedge_ratio(pool) -> float:
    n = sum(1 for p in pool if any(h in p.text for h in HEDGE_WORDS))
    return n / len(pool)


def test_link_post_openings_do_not_hedge():
    """リンク投稿の書き出しは濁さない。

    濁すと、薦めたいのか迷ってるのか分からなくなって押す理由が消える。
    ここは収益に直結するので、例外を認めない。
    """
    for part in P.RECOMMEND_OPENINGS:
        assert not any(h in part.text for h in HEDGE_WORDS), (
            f"{part.id} がぼかしている: {part.text}"
        )


def test_link_post_closings_do_not_hedge():
    for part in P.RECOMMEND_CLOSINGS:
        assert not any(h in part.text for h in HEDGE_WORDS), (
            f"{part.id} がぼかしている: {part.text}"
        )


def test_no_link_topics_mostly_assert():
    """選び方の投稿は、大半が言い切りであること。

    全部を言い切りにはしない。問いかけの投稿もあるし、
    本当に人によって違うことまで断定すると嘘になる。
    上限を決めて、そこを超えたら見直す。
    """
    ratio = _hedge_ratio(P.NO_LINK_TOPICS)
    assert ratio <= 0.20, (
        f"ぼかし表現が {ratio:.0%} ある。言い切れるところは言い切ること"
    )


def test_howto_posts_mostly_assert():
    ratio = _hedge_ratio(P.HOWTO_POSTS)
    assert ratio <= 0.20, f"ぼかし表現が {ratio:.0%} ある"


# ======================================================================
# 言い切っても規制に触れないことの確認
# ======================================================================
def test_assertive_shopping_claims_are_legal():
    """買い方についての言い切りは、リンクの有無に関係なく通ること。

    ここが通らなくなったら、ルールが効能以外まで広がっている。
    """
    claims = [
        "コスメをいちばん安い店で探し続けるのは、時間の無駄です。",
        "はっきり言うけど、レビューの点数だけ見て買うのはやめたほうがいい。",
        "断言するけど、詰め替えがある商品を選んだほうが後で得します。",
        "はじめてのコスメで本命を狙うのは、まず失敗します。",
        "30代でスキンケア見直す人、これだけ先に決めてほしい。",
    ]
    for text in claims:
        assert not scan(text, has_link=True), f"買い方の言い切りが止まった: {text}"
        assert not scan(text, has_link=False)


def test_all_recommend_parts_still_pass_compliance():
    """書き出し・締めはリンク投稿で使うので、全ルールを通ること。"""
    for pool in (P.RECOMMEND_OPENINGS, P.RECOMMEND_CLOSINGS):
        for part in pool:
            text = part.text.replace("{category}", "スキンケア")
            hits = scan(text, has_link=True)
            assert not hits, f"{part.id}: {[h.label for h in hits]}"
