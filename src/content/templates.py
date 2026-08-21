"""投稿型のスケルトン。

各テンプレートは「ブロックの並び」を返す。ブロックには drop_priority があり、
文字数上限（Threads は 500 文字）を超えたときは priority の大きいものから落とす。
楽天のアフィリエイトURLは長いので、この仕組みが無いと本文が入らない。

**数値リテラルはここに書かない**。数値はすべて facts.py 経由で入る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..rakuten.models import RakutenItem
from . import facts as F
from .benefits import benefit_by_cursor, benefit_for, tips_block
from .parts import (
    CTA_PARTS,
    DISCLAIMERS,
    FACT_INTROS,
    CASUAL_MURMURS,
    HOWTO_POSTS,
    NO_LINK_TOPICS,
    PRODUCT_CLOSINGS,
    PRODUCT_OPENINGS,
    RECOMMEND_CLOSINGS,
    RECOMMEND_OPENINGS,
    ROUNDUP_CLOSINGS,
    ROUNDUP_OPENINGS,
    THREAD_BRIDGES,
    THREAD_HOOKS,
    Part,
)


@dataclass
class Block:
    """本文の1ブロック。drop_priority が大きいほど先に削られる。"""

    text: str
    drop_priority: int = 0  # 0 = 必須


@dataclass
class Rendered:
    blocks: list[Block]
    # スレッドに分ける場合の各本文。空なら単発投稿。
    segments: list[str] = field(default_factory=list)
    part_ids: dict[str, str] = field(default_factory=dict)
    allowed_numbers: set[str] = field(default_factory=set)


class Picker(Protocol):
    """直近使用を避けつつパーツを1つ選ぶ。builder が実装を渡す。"""

    def __call__(
        self,
        group: str,
        pool: tuple[Part, ...],
        *,
        postage_free: bool = False,
        many_reviews: bool = False,
        cheap: bool = False,
    ) -> Part: ...


@dataclass
class RenderContext:
    pick: Picker
    items: list[RakutenItem]
    category: str
    postage_free: bool
    many_reviews: bool
    cheap: bool
    affiliate_url: str | None
    # 事実の言い回しを回すためのカウンタ。同じ形が続かないようにする。
    fact_style: int = 0

    @property
    def item(self) -> RakutenItem:
        return self.items[0]

    def flags(self) -> dict[str, bool]:
        return {
            "postage_free": self.postage_free,
            "many_reviews": self.many_reviews,
            "cheap": self.cheap,
        }



# 広告であることの表示。
# 景表法（ステマ規制）が求めるのは「一般消費者が広告と判別できること」で、
# 表記の形は自由。【PR】は硬いので #PR を冒頭の独立行に置く。
# アフィリエイトリンクが無い投稿には付けない。
PR_TAG = "#PR"


def _pr(ctx: "RenderContext", body: str) -> str:
    return f"{PR_TAG}\n\n{body}" if ctx.affiliate_url else body


# ----------------------------------------------------------------------
def _link_blocks(ctx: RenderContext, rendered: Rendered) -> list[Block]:
    """CTA + URL + 注記。URLは必須ブロック、注記は削れるブロック。"""
    if not ctx.affiliate_url:
        return []
    cta = ctx.pick("cta", CTA_PARTS, **ctx.flags())
    disclaimer = ctx.pick("disclaimer", DISCLAIMERS, **ctx.flags())
    rendered.part_ids["cta"] = cta.id
    rendered.part_ids["disclaimer"] = disclaimer.id
    # URLは本文に書かない。link_attachment としてカード添付するので
    # 500文字を消費しない（楽天のURLは280文字近くある）。
    return [
        Block(cta.text.rstrip(" →"), 1),
        Block(disclaimer.text, 3),
    ]


def _tips_stage(ctx: RenderContext, heading: str | None = None) -> str:
    """箇条書きのノウハウ段。

    商品の剤形に対応した「選ぶとき見るところ」を並べ、
    末尾に56項目の範囲内の役割を置く。

    剤形が判定できない商品（ブラシ・ポーチ）では、
    カテゴリー横断のノウハウにフォールバックする。空にはしない。
    """
    benefit = None
    if ctx.items:
        # 楽天の商品名の末尾には検索用キーワードが羅列されていることが多く
        # （「…[ ヘアパック ヘアマスク 美髪 …]」）、そのまま判定すると
        # 別の剤形として誤判定する。販促文を落とした表示名で判定する。
        benefit = benefit_for(ctx.item.display_name(60))
    if benefit is None:
        benefit = benefit_by_cursor(ctx.fact_style)
    return tips_block(benefit, heading)


def _concern_stage(ctx: RenderContext) -> str:
    """所感・悩みの段。煽らない形にとどめる。"""
    benefit = None
    if ctx.items:
        benefit = benefit_for(ctx.item.display_name(60))
    if benefit is None:
        benefit = benefit_by_cursor(ctx.fact_style + 1)
    return benefit.concern


def _lead_opening(ctx: RenderContext) -> str:
    """前振りの導入。

    リンクがあるときは薦める側の温度感にする。
    「どう思う？」と問いかけると、薦めたいのか意見がほしいのか
    分からなくなるため。
    """
    pool = RECOMMEND_OPENINGS if ctx.affiliate_url else PRODUCT_OPENINGS
    group = "recommend_opening" if ctx.affiliate_url else "opening"
    part = ctx.pick(group, pool, **ctx.flags())
    return part.id, _opening_text(part, ctx)


def _lead_closing(ctx: RenderContext) -> str:
    """締め。リンクがあるときは薦める温度感を保つ。"""
    pool = RECOMMEND_CLOSINGS if ctx.affiliate_url else PRODUCT_CLOSINGS
    group = "recommend_closing" if ctx.affiliate_url else "closing"
    part = ctx.pick(group, pool, **ctx.flags())
    return part.id, part.text


def _opening_text(part: Part, ctx: RenderContext) -> str:
    return part.text.format(
        category=ctx.category,
        band=F.format_price_band(ctx.item.item_price),
        band_range="",
    )



def _split_for_thread(
    ctx: RenderContext,
    rendered: Rendered,
    stages: list[str],
) -> None:
    """前振り（可変本数）＋ 最後の1本（広告表示・商品名・リンク）に分ける。

    決まっているのは「最後がPRとリンク」だけで、前振りの本数は
    テンプレートによって1〜3本と変わる。見た目に幅が出る。

    最後の1本には**数値を一切入れない**（値段・レビュー・送料）。
    スペック羅列をやめ、前振りで作った興味のまま押してもらう形にする。

    そのぶん「この商品が良い」とは書けない。使っていないうえ、
    客観的な裏付け（値段・レビュー）も本文から外すので、
    良し悪しの主張は根拠を失う。書けるのは
    「このカテゴリーはこう選ぶ → これがその一つ」まで。

    Threads は外部リンクのある投稿の表示回数を落とすので、
    タイムラインに出る1本目にはリンクも商品名も置かない。
    """
    body = [text.strip() for text in stages if text and text.strip()]

    # ノウハウの箇条書きには「1本でどのくらいもつか」のような数値が入る。
    # 商品データ由来ではないが、事実の主張でもないので許可する。
    for text in body:
        rendered.allowed_numbers |= set(F.extract_numbers(text))

    if not ctx.affiliate_url:
        rendered.blocks = [Block(text, 0) for text in body]
        return

    cta = ctx.pick("cta", CTA_PARTS, **ctx.flags())
    rendered.part_ids["cta"] = cta.id

    # 最後の1本。押す理由はCTAだけなので、注記で埋もれさせない。
    # 容量・入数の数値も出さない方針なので、名前から落とす
    final = f"{PR_TAG}\n\n{ctx.item.display_name_without_volume(38)}\n\n{cta.text}"

    rendered.segments = [*body, final]
    rendered.blocks = [Block(seg, 0) for seg in rendered.segments]


# ======================================================================
# 単一商品テンプレート
# ======================================================================
def render_objective(ctx: RenderContext) -> Rendered:
    """悩み → 箇条書きノウハウ → 商品（3本）。"""
    r = Rendered(blocks=[])
    opening_id, opening_text = _lead_opening(ctx)
    r.part_ids["opening"] = opening_id

    _split_for_thread(ctx, r, [opening_text, _tips_stage(ctx)])
    return r

def render_short(ctx: RenderContext) -> Rendered:
    """悩みひとつ → 商品（2本）。いちばん短い形。"""
    r = Rendered(blocks=[])
    opening_id, opening_text = _lead_opening(ctx)
    r.part_ids["opening"] = opening_id

    _split_for_thread(ctx, r, [opening_text])
    return r

def render_checklist(ctx: RenderContext) -> Rendered:
    """悩み → 箇条書きノウハウ → 所感 → 商品（4本）。いちばん長い形。"""
    r = Rendered(blocks=[])
    closing_id, closing_text = _lead_closing(ctx)
    r.part_ids["closing"] = closing_id

    _split_for_thread(ctx, r, [
        f"{ctx.category}選ぶとき、だいたいこのへん見てる👀",
        # 1本目で見出しを言っているので、ここは繰り返さない
        _tips_stage(ctx, heading="こんな感じ📝"),
        f"{_concern_stage(ctx)}\n\n{closing_text}",
    ])
    return r

def render_band_focus(ctx: RenderContext) -> Rendered:
    """価格帯の話 → 箇条書きノウハウ → 商品（3本）。

    価格帯は「探していた文脈」として1本目に置く。
    商品そのものの値段は最後の1本でも出さない。
    """
    r = Rendered(blocks=[])
    band = F.format_price_band(ctx.item.item_price)
    r.allowed_numbers.add(F.normalize_number(str(F.price_band_value(ctx.item.item_price))))

    _split_for_thread(ctx, r, [
        f"{band}の{ctx.category}探してたときのメモ📝",
        _tips_stage(ctx),
    ])
    return r


# ======================================================================
# 複数商品テンプレート
# ======================================================================
def _roundup(ctx: RenderContext, kind: str) -> Rendered:
    r = Rendered(blocks=[])
    pool = ROUNDUP_OPENINGS[kind]
    opening = ctx.pick(f"roundup_{kind}", pool, **ctx.flags())
    closing = ctx.pick("roundup_closing", ROUNDUP_CLOSINGS, **ctx.flags())
    r.part_ids.update({f"roundup_{kind}": opening.id, "roundup_closing": closing.id})

    prices = [i.item_price for i in ctx.items]
    lo = F.price_band_value(min(prices))
    hi = F.price_band_value(max(prices))
    band_range = f"{lo:,}〜{hi:,}円" if lo != hi else f"{lo:,}円前後"
    r.allowed_numbers |= {F.normalize_number(str(lo)), F.normalize_number(str(hi))}

    headline = opening.text.format(
        category=ctx.category, band=band_range, band_range=band_range
    )
    # アフィリエイトリンクがある場合は【PR】を見出しの先頭に付ける（冒頭付近に置く）
    r.blocks.append(Block(headline, 0))

    lines: list[str] = []
    for item in ctx.items:
        inline, allowed = F.sentence_facts(item, ctx.fact_style + len(lines))
        r.allowed_numbers |= allowed
        lines.append(f"{item.display_name(26)}\n{inline}")
    r.blocks.append(Block("\n\n".join(lines), 0))

    r.blocks.append(Block(closing.text, 2))

    if ctx.affiliate_url:
        # 複数商品を並べているのにリンクは1つなので、どれのリンクかを明示する。
        r.allowed_numbers.add("1")
        cta = ctx.pick("cta", CTA_PARTS, **ctx.flags())
        disclaimer = ctx.pick("disclaimer", DISCLAIMERS, **ctx.flags())
        r.part_ids.update({"cta": cta.id, "disclaimer": disclaimer.id})
        # 1本目は見出しだけ。商品と数値は2本目へ。
        rest = "\n\n".join(b.text.strip() for b in r.blocks[1:] if b.text.strip())
        r.segments = [
            r.blocks[0].text.strip(),
            _pr(ctx, rest),
            f"※リンクは1つ目のものです\n{cta.text}\n{disclaimer.text}",
        ]
        r.blocks = [Block(seg, 0) for seg in r.segments]
    return r


def render_price_band(ctx: RenderContext) -> Rendered:
    return _roundup(ctx, "price_band")


def render_review_heavy(ctx: RenderContext) -> Rendered:
    return _roundup(ctx, "review_heavy")


def render_postage_free(ctx: RenderContext) -> Rendered:
    return _roundup(ctx, "postage_free")


def render_comparison(ctx: RenderContext) -> Rendered:
    return _roundup(ctx, "comparison")


# ======================================================================
# リンクなし
# ======================================================================
def render_thread(ctx: RenderContext) -> Rendered:
    """フック → 箇条書きノウハウ → 商品（3本）。

    参考にした運用アカウントが、共感できる話から入って
    最後にリンクを置く形にしていた。
    """
    r = Rendered(blocks=[])
    hooks = THREAD_HOOKS.get(ctx.category) or THREAD_HOOKS["コスメ"]
    hook = ctx.pick("thread_hook", hooks, **ctx.flags())
    r.part_ids["thread_hook"] = hook.id

    _split_for_thread(ctx, r, [
        hook.text,
        _tips_stage(ctx),
    ])
    return r

def render_howto(ctx: RenderContext) -> Rendered:
    """ノウハウ投稿。保存したくなるチェックリスト型。

    参考にした伸びている投稿がこの形だった。商品もリンクも出さず、
    それ単体で価値があるので保存・リポストされやすい。

    効能には踏み込まない（薬機法）。扱うのは選び方・買い方・
    コスパの考え方に限る。
    """
    r = Rendered(blocks=[])
    post = ctx.pick("howto", HOWTO_POSTS)
    r.part_ids["howto"] = post.id
    r.blocks.append(Block(post.text, 0))
    # 手順番号など、本文に含まれる数値は商品データ由来ではないので許可する
    r.allowed_numbers |= set(F.extract_numbers(post.text))
    return r


def render_casual(ctx: RenderContext) -> Rendered:
    """ただのつぶやき。

    役に立つ話しかしないアカウントは、人がやっている感じが出ない。
    商品も買い物ノウハウも出さず、生活の断片だけを置く。
    """
    r = Rendered(blocks=[])
    murmur = ctx.pick("casual", CASUAL_MURMURS)
    r.part_ids["casual"] = murmur.id
    r.blocks.append(Block(murmur.text, 0))
    r.allowed_numbers |= set(F.extract_numbers(murmur.text))
    return r


def render_topic(ctx: RenderContext) -> Rendered:
    r = Rendered(blocks=[])
    topic = ctx.pick("topic", NO_LINK_TOPICS)
    r.part_ids["topic"] = topic.id
    r.blocks.append(Block(topic.text, 0))
    # トピック文に数値が含まれる場合に備えて許可リストへ入れる
    r.allowed_numbers |= set(F.extract_numbers(topic.text))
    return r


# ======================================================================
@dataclass(frozen=True)
class Template:
    id: str
    render: Callable[[RenderContext], Rendered]
    post_types: tuple[str, ...]
    item_count: int = 1
    requires_affiliate: bool = True


TEMPLATES: tuple[Template, ...] = (
    Template("thread", render_thread, ("product",)),
    Template("objective", render_objective, ("product",)),
    Template("short", render_short, ("product",)),
    Template("checklist", render_checklist, ("product",)),
    Template("band_focus", render_band_focus, ("product",)),
    Template("price_band", render_price_band, ("price_band",), item_count=3),
    Template("review_heavy", render_review_heavy, ("review_heavy",), item_count=3),
    Template("postage_free", render_postage_free, ("postage_free",), item_count=3),
    Template("comparison", render_comparison, ("comparison",), item_count=2),
    Template("topic", render_topic, ("no_link",), item_count=0, requires_affiliate=False),
    Template("casual", render_casual, ("casual",), item_count=0, requires_affiliate=False),
    Template("howto", render_howto, ("howto",), item_count=0, requires_affiliate=False),
)

TEMPLATES_BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}


def templates_for(post_type: str) -> list[Template]:
    return [t for t in TEMPLATES if post_type in t.post_types]
