"""クリックを生む訴求の型。

## なぜ型を分けるのか

書き出しが「悩みを名指しする」一種類に寄っていた。
悩み型は強いが、それだけだと結局また同じ形になる。

人がクリックする理由は一つではない。

    自分に関係がある / 知らないと損する / 答えを知りたい
    予想と違う / 比較の手間が減る / 得しそう / 続きが気になる

投稿ごとに違う理由を出す。そのための型を16置く。

## 何を材料にするか

型ごとに必要な材料が違う。材料が無ければその型は使わない。

    pain    剤形から引く悩み（benefits.py）
    tips    選ぶとき見るところ（benefits.py）
    price   実際の価格
    reviews 実際のレビュー件数・平均

**手元のデータで言えることしか書かない。**
確かめられない事実や、使ってもいない体験は作らない。

## 守ること

  * 効能の断定をしない（薬機法）。悩みは56項目の範囲内
  * 使用体験を書かない。書けるのは「気になった」「良さそう」まで
  * 煽らない。「今すぐ買え」「損する」の直接表現は景表法で弾かれる
    （「見落としやすい」までにとどめる）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..rakuten.models import RakutenItem
from . import facts as F
from .benefits import Benefit


@dataclass(frozen=True)
class Appeal:
    """訴求の型ひとつ。"""

    id: str
    label: str
    # 本文を作る。材料が足りなければ None を返す。
    build: Callable[["AppealContext"], str | None]


@dataclass
class AppealContext:
    """訴求文を作るための材料。"""

    item: RakutenItem
    benefit: Benefit | None
    category: str
    cursor: int
    # 数値を本文に出したときは、ここに入れてデータ整合性チェックを通す
    allowed_numbers: set[str]
    # 実際に使った人の声（使用感だけ）。無ければ空。
    voices: tuple[str, ...] = ()

    def pick(self, options: tuple[str, ...]) -> str:
        return options[self.cursor % len(options)]

    @property
    def pain(self) -> str:
        return self.benefit.pain if self.benefit else ""

    @property
    def voice(self) -> str:
        from .voices import voice_phrase
        return voice_phrase(self.voices, cursor=self.cursor)

    @property
    def future(self) -> str:
        return self.benefit.future if self.benefit else ""

    @property
    def tips(self) -> tuple[str, ...]:
        return self.benefit.tips if self.benefit else ()

    def tip(self, offset: int = 0) -> str:
        if not self.tips:
            return ""
        return self.tips[(self.cursor + offset) % len(self.tips)]


# ======================================================================
# 1. 自分ごと化
# ======================================================================
def _self_relevant(c: AppealContext) -> str | None:
    if not c.pain:
        return None
    if c.future:
        return c.pick((
            f"{c.pain}人、これ見てみてほしい",
            f"{c.pain}人へ。{c.future}",
            f"{c.pain}。同じ人、いる？",
            f"{c.pain}人。求めてるの、{c.future}じゃない？",
        ))
    return c.pick((
        f"{c.pain}人、これ見てみてほしい",
        f"{c.pain}人向け。条件だけ置いておきます",
        f"{c.pain}。同じ人、いる？",
        f"{c.pain}人にこそ見てほしいやつ",
    ))


# ======================================================================
# 2. 損失回避
# ======================================================================
def _loss_aversion(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    return c.pick((
        f"{c.category}買う前に、ここだけ確認してほしい。\n\n「{tip}」",
        f"{c.category}選びで見落としやすいのが「{tip}」",
        f"値段だけで{c.category}決めると、たぶんここでつまずく。\n\n「{tip}」",
        f"{c.category}、知らずに選ぶと「{tip}」で後悔する",
    ))


# ======================================================================
# 3. 答え・結論欲求
# ======================================================================
def _answer(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    if c.future:
        return c.pick((
            f"結局{c.category}ってどれ選べばいいの、の答え。\n\n「{tip}」で決めていい",
            f"{c.future}のが目的なら、見るのは「{tip}」",
            f"{c.category}で迷ってる人へ。見るのは「{tip}」だけでいい",
        ))
    return c.pick((
        f"結局{c.category}ってどれ選べばいいの、の答え。\n\n「{tip}」で決めていい",
        f"{c.category}、条件から絞るとこうなる。\n\nまず「{tip}」",
        f"{c.category}で迷ってる人へ。見るのは「{tip}」だけでいい",
    ))


# ======================================================================
# 4. 意外性・常識破壊
# ======================================================================
def _surprise(c: AppealContext) -> str | None:
    tip = c.tip()
    return c.pick((
        f"高い{c.category}が正解とは限らない",
        f"{c.category}、実は見るべきなのは値段じゃない",
        f"{c.category}は新作じゃなくていい。条件が合うかどうか",
        f"{c.category}で本当に効いてくるのは「{tip}」のほう" if tip else
        f"{c.category}、定番だけで選ぶ必要はないと思ってる",
    ))


# ======================================================================
# 5. 省力化・時短
# ======================================================================
def _effort_saving(c: AppealContext) -> str | None:
    if len(c.tips) < 3:
        return None
    three = "\n".join(f"・{c.tips[(c.cursor + i) % len(c.tips)]}" for i in range(3))
    return c.pick((
        f"{c.category}、これだけ見ればOK。\n\n{three}",
        f"比べる時間がない人向けに、{c.category}の条件を三つまで絞った。\n\n{three}",
        f"{c.category}を調べるのが面倒な人へ。見るのはここだけ。\n\n{three}",
    ))


# ======================================================================
# 6. 得・コスパ
# ======================================================================
def _value(c: AppealContext) -> str | None:
    price = c.item.item_price
    if not price:
        return None
    c.allowed_numbers |= F.build_facts(c.item).allowed_numbers
    shown = F.format_price(price)
    return c.pick((
        f"{shown}の{c.category}。この条件でこの値段は、見ておいていいと思う",
        f"{c.category}を{shown}で。値段だけじゃなく容量あたりで比べてほしい",
        f"コスパ重視で{c.category}探してるなら、{shown}のこれは候補",
    ))


# ======================================================================
# 7. 好奇心・続き
# ======================================================================
def _curiosity(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    return c.pick((
        f"{c.category}で意外と知られてないのが「{tip}」",
        f"{c.category}、ここで差が出る。\n\n「{tip}」",
        f"{c.category}選びで見落とされがちな違いがある。\n\n「{tip}」",
    ))


# ======================================================================
# 8. 比較・対立
# ======================================================================
def _contrast(c: AppealContext) -> str | None:
    if len(c.tips) < 2:
        return None
    a, b = c.tip(), c.tip(1)
    return c.pick((
        f"{c.category}、「{a}」派と「{b}」派で選ぶものが変わる",
        f"{c.category}で迷うなら、違いはここ。\n\n「{a}」か「{b}」か",
        f"安さ重視か、{a}重視か。{c.category}はそこで分かれる",
    ))


# ======================================================================
# 9. 条件付き最適解
# ======================================================================
def _conditional(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    if c.future:
        return c.pick((
            f"全員向けではないけど、「{tip}」を重視するなら候補",
            f"{c.future}ことを求めてるなら向く。そうじゃないなら別のがいい",
            f"{c.category}、「{tip}」で選ぶ人向け",
        ))
    return c.pick((
        f"全員向けではないけど、「{tip}」を重視するなら候補",
        f"「{tip}」を求める人には向くと思う。そうじゃないなら別のがいい",
        f"{c.category}、「{tip}」で選ぶ人向け",
    ))


# ======================================================================
# 10. 問題の言語化
# ======================================================================
def _articulate(c: AppealContext) -> str | None:
    if not c.pain:
        return None
    return c.pick((
        f"{c.pain}。うまく言えないけど、これが地味にストレス",
        f"なんとなくで{c.category}選んで、毎回同じところで迷ってる。\n\n{c.pain}",
        f"{c.pain}のを、なんとかしたいだけなんだよね",
    ))


# ======================================================================
# 11. チェック・診断
# ======================================================================
def _checklist(c: AppealContext) -> str | None:
    if len(c.tips) < 3:
        return None
    three = "\n".join(f"・{c.tips[(c.cursor + i) % len(c.tips)]}" for i in range(3))
    return c.pick((
        f"{c.category}買う前の三つ、当てはまるか見てみて。\n\n{three}",
        f"その{c.category}の選び方、合ってる？\n\n{three}",
        f"{c.category}を買う前に、ここだけ確認。\n\n{three}",
    ))


# ======================================================================
# 12. ランキング・序列
# ======================================================================
def _ranking(c: AppealContext) -> str | None:
    if len(c.tips) < 3:
        return None
    ordered = "\n".join(
        f"{n}. {c.tips[(c.cursor + i) % len(c.tips)]}" for n, i in ((1, 0), (2, 1), (3, 2))
    )
    # 順番の数字は商品データ由来ではないので、ここで許可する
    c.allowed_numbers |= {"1", "2", "3"}
    return c.pick((
        f"{c.category}、迷ったらこの順で見るといい。\n\n{ordered}",
        f"{c.category}を選ぶときの優先順位。\n\n{ordered}",
    ))


# ======================================================================
# 13. 発見・穴場
# ======================================================================
def _discovery(c: AppealContext) -> str | None:
    return c.pick((
        f"{c.category}、定番以外にもちゃんとある",
        f"埋もれてるけど条件は良い{c.category}、けっこうある",
        f"{c.category}の穴場候補。名前は知らなかった",
    ))


# ======================================================================
# 14. タイミング・今見る理由
# ======================================================================
def _timing(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    return c.pick((
        f"{c.category}、買い替える前に一回見てほしい。\n\n「{tip}」",
        f"切らしてから探すと雑になる。{c.category}は先に決めておきたい",
        f"いま{c.category}選ぶなら、見るのは「{tip}」",
    ))


# ======================================================================
# 15. ネガティブ訴求
# ======================================================================
def _negative(c: AppealContext) -> str | None:
    tip = c.tip()
    if not tip:
        return None
    return c.pick((
        f"これは万人向けではないと思う。「{tip}」が合う人向け",
        f"{c.category}にこだわりが無い人には、たぶん要らない。\n\n「{tip}」を気にする人向け",
        f"弱点もある。でも「{tip}」で選ぶならこれ",
    ))


# ======================================================================
# 16. 未来イメージ
# ======================================================================
def _future(c: AppealContext) -> str | None:
    """使ったあとどうなるかを出す。

    56項目は「化粧品で言ってよい効能のリスト」であり、
    つまり**唯一許された「いい未来」**でもある。
    それを辞書の文のまま使っていたので、何も伝わっていなかった。
    """
    if not c.future:
        return None
    if c.pain:
        return c.pick((
            f"{c.pain}人へ。\n\n{c.future}",
            f"{c.pain}。\n\nそれが、{c.future}",
            f"{c.future}。\n\n{c.pain}人は、これでいいと思う",
        ))
    return c.pick((
        f"{c.future}。それだけでいい人向け",
        f"求めてるのは、たぶんこれ。\n\n{c.future}",
    ))


# ======================================================================
# 17. 未来 × 手間
# ======================================================================
def _future_effortless(c: AppealContext) -> str | None:
    """未来と、そこに至る手間の少なさを一緒に出す。"""
    if not c.future:
        return None
    tip = c.tip()
    if not tip:
        return None
    return c.pick((
        f"{c.future}。\n\n見るのは「{tip}」だけでいい",
        f"やることは一つ。それで{c.future}",
        f"{c.future}。\n\nそのために見るのは「{tip}」",
    ))


# ======================================================================
# 18. 使った人の声
# ======================================================================
def _voices(c: AppealContext) -> str | None:
    """使用感を出す。

    ## 出典は書かない

    「レビューで多かったのは」と毎回言うと、投稿が調査報告に見える。
    出典は投稿の中身ではないので出さない
    （根拠は data/voices.json に残っている）。

    ## ただし自分の体験にはしない

    出典を消したうえで「さっぱりした」と書くと、
    **こちらが使った話に読める**。このアカウントは使っていない。

    そこで、体験ではなく **商品の性質** として書く。

      ×  さっぱりして良かった        使った話になる
      ×  レビューで多かったのは…     出典が前に出る
      ○  さっぱりめのタイプ          商品の性質。誰の体験でもない
      ○  さっぱりするらしい          伝聞。自分の話ではない

    **使用感だけ。** 効能に触れたレビューは voices.py の時点で
    集計から外してある。薬機法で、体験談を効能効果の証明に
    使うことはできないため。
    """
    if not c.voice:
        return None
    return c.pick((
        f"{c.voice}のタイプ",
        f"{c.voice}らしい。そこが合うかどうかだと思う",
        f"{c.category}としては{c.voice}寄り",
        f"{c.voice}って言われてるやつ",
    ))


# ======================================================================
# 19. 悩み × 使った人の声
# ======================================================================
def _pain_with_voices(c: AppealContext) -> str | None:
    if not (c.pain and c.voice):
        return None
    return c.pick((
        f"{c.pain}人へ。\n\n{c.voice}のタイプらしい",
        f"{c.pain}。\n\nこれは{c.voice}寄りみたい",
        f"{c.voice}らしいので、{c.pain}人には合うかも",
    ))


APPEALS: tuple[Appeal, ...] = (
    Appeal("self_relevant", "自分ごと化", _self_relevant),
    Appeal("loss_aversion", "損失回避", _loss_aversion),
    Appeal("answer", "答え・結論", _answer),
    Appeal("surprise", "意外性", _surprise),
    Appeal("effort_saving", "省力化", _effort_saving),
    Appeal("value", "得・コスパ", _value),
    Appeal("curiosity", "好奇心", _curiosity),
    Appeal("contrast", "比較・対立", _contrast),
    Appeal("conditional", "条件付き最適解", _conditional),
    Appeal("articulate", "問題の言語化", _articulate),
    Appeal("checklist", "チェック・診断", _checklist),
    Appeal("ranking", "ランキング", _ranking),
    Appeal("discovery", "発見・穴場", _discovery),
    Appeal("timing", "タイミング", _timing),
    Appeal("negative", "ネガティブ訴求", _negative),
    Appeal("future", "未来イメージ", _future),
    Appeal("future_effortless", "未来×手間", _future_effortless),
    Appeal("voices", "使った人の声", _voices),
    Appeal("pain_voices", "悩み×声", _pain_with_voices),
)


def build_appeal(
    item: RakutenItem,
    benefit: Benefit | None,
    category: str,
    *,
    cursor: int,
    avoid: set[str] | None = None,
    voices: tuple[str, ...] = (),
) -> tuple[str, str, set[str]] | None:
    """訴求文をひとつ作る。(本文, 訴求ID, 許可する数値) を返す。

    直近使った軸は avoid で外す。材料が足りない軸は自動で飛ばす。
    どれも作れなければ None（呼び出し側は従来の書き出しに戻す）。
    """
    avoid = avoid or set()
    order = [APPEALS[(cursor + i) % len(APPEALS)] for i in range(len(APPEALS))]

    # 実際に使った人の声があるなら、それを先に見る。
    #
    # 声は全商品では取れない（レビューが少ない・使用感に触れていない）。
    # 取れた商品は少数なので、後回しにすると一度も出ない。
    # 実測で、声の軸は19軸中17番目にあり一度も選ばれていなかった。
    #
    # ただし毎回ではなく3回に2回。声だけになると、それも型になる。
    if voices and cursor % 3 != 2:
        voiced = [a for a in order if a.id in ("pain_voices", "voices")]
        order = voiced + [a for a in order if a not in voiced]
    for appeal in order:
        if appeal.id in avoid:
            continue
        ctx = AppealContext(item=item, benefit=benefit, category=category,
                            cursor=cursor, allowed_numbers=set(), voices=voices)
        text = appeal.build(ctx)
        if text:
            return text, appeal.id, ctx.allowed_numbers
    return None
