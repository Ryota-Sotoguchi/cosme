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
from .parts import (
    CTA_PARTS,
    DISCLAIMERS,
    FACT_INTROS,
    NO_LINK_TOPICS,
    PRODUCT_CLOSINGS,
    PRODUCT_OPENINGS,
    ROUNDUP_CLOSINGS,
    ROUNDUP_OPENINGS,
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

    @property
    def item(self) -> RakutenItem:
        return self.items[0]

    def flags(self) -> dict[str, bool]:
        return {
            "postage_free": self.postage_free,
            "many_reviews": self.many_reviews,
            "cheap": self.cheap,
        }


# ----------------------------------------------------------------------
def _link_blocks(ctx: RenderContext, rendered: Rendered) -> list[Block]:
    """CTA + URL + 注記。URLは必須ブロック、注記は削れるブロック。"""
    if not ctx.affiliate_url:
        return []
    cta = ctx.pick("cta", CTA_PARTS, **ctx.flags())
    disclaimer = ctx.pick("disclaimer", DISCLAIMERS, **ctx.flags())
    rendered.part_ids["cta"] = cta.id
    rendered.part_ids["disclaimer"] = disclaimer.id
    return [
        Block(f"{cta.text} {ctx.affiliate_url}", 0),
        Block(disclaimer.text, 3),
    ]


def _opening_text(part: Part, ctx: RenderContext) -> str:
    return part.text.format(
        category=ctx.category,
        band=F.format_price_band(ctx.item.item_price),
        band_range="",
    )


# ======================================================================
# 単一商品テンプレート
# ======================================================================
def render_objective(ctx: RenderContext) -> Rendered:
    """客観情報型: 導入 → 商品名 → 箇条書きの事実 → 締め → リンク。"""
    r = Rendered(blocks=[])
    item = ctx.item

    opening = ctx.pick("opening", PRODUCT_OPENINGS, **ctx.flags())
    intro = ctx.pick("fact_intro", FACT_INTROS, **ctx.flags())
    closing = ctx.pick("closing", PRODUCT_CLOSINGS, **ctx.flags())
    r.part_ids.update({"opening": opening.id, "fact_intro": intro.id, "closing": closing.id})

    fs = F.build_facts(item)
    r.allowed_numbers |= fs.allowed_numbers

    r.blocks.append(Block(f"【PR】{_opening_text(opening, ctx)}", 0))
    r.blocks.append(Block(item.display_name(), 1))
    if intro.text:
        r.blocks.append(Block(intro.text, 4))
    r.blocks.append(Block("\n".join(fs.lines), 0))
    r.blocks.append(Block(closing.text, 2))
    r.blocks.extend(_link_blocks(ctx, r))
    return r


def render_short(ctx: RenderContext) -> Rendered:
    """短文型: 導入 → 1〜2文の事実 → リンク。"""
    r = Rendered(blocks=[])
    item = ctx.item

    opening = ctx.pick("opening", PRODUCT_OPENINGS, **ctx.flags())
    r.part_ids["opening"] = opening.id

    inline, allowed = F.inline_facts(item)
    r.allowed_numbers |= allowed

    r.blocks.append(Block(f"【PR】{_opening_text(opening, ctx)}", 0))
    r.blocks.append(Block(f"{item.display_name(34)}\n{inline}", 0))
    r.blocks.extend(_link_blocks(ctx, r))
    return r


def render_checklist(ctx: RenderContext) -> Rendered:
    """チェック項目型: 「見た項目」を先に提示してから値を並べる。"""
    r = Rendered(blocks=[])
    item = ctx.item

    closing = ctx.pick("closing", PRODUCT_CLOSINGS, **ctx.flags())
    r.part_ids["closing"] = closing.id

    fs = F.build_facts(item, bullet="□ ")
    r.allowed_numbers |= fs.allowed_numbers

    r.blocks.append(Block(f"【PR】{ctx.category}を選ぶときに見る項目で、この商品を確認したもの。", 0))
    r.blocks.append(Block(item.display_name(38), 1))
    r.blocks.append(Block("\n".join(fs.lines), 0))
    r.blocks.append(Block(closing.text, 2))
    r.blocks.extend(_link_blocks(ctx, r))
    return r


def render_band_focus(ctx: RenderContext) -> Rendered:
    """価格帯起点型: 価格帯を主語にして条件を並べる。"""
    r = Rendered(blocks=[])
    item = ctx.item

    intro = ctx.pick("fact_intro", FACT_INTROS, **ctx.flags())
    closing = ctx.pick("closing", PRODUCT_CLOSINGS, **ctx.flags())
    r.part_ids.update({"fact_intro": intro.id, "closing": closing.id})

    fs = F.build_facts(item)
    r.allowed_numbers |= fs.allowed_numbers
    band = F.format_price_band(item.item_price)

    r.blocks.append(Block(f"【PR】{band}の{ctx.category}を探しているとき用のメモ。", 0))
    r.blocks.append(Block(item.display_name(38), 1))
    if intro.text:
        r.blocks.append(Block(intro.text, 4))
    r.blocks.append(Block("\n".join(fs.lines), 0))
    r.blocks.append(Block(closing.text, 2))
    r.blocks.extend(_link_blocks(ctx, r))
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
    r.blocks.append(Block(f"【PR】{headline}" if ctx.affiliate_url else headline, 0))

    lines: list[str] = []
    for item in ctx.items:
        inline, allowed = F.inline_facts(item)
        r.allowed_numbers |= allowed
        lines.append(f"◽{item.display_name(26)}\n　{inline}")
    r.blocks.append(Block("\n\n".join(lines), 0))

    r.blocks.append(Block(closing.text, 2))

    if ctx.affiliate_url:
        # 複数商品を並べているのにリンクは1つなので、どれのリンクかを明示する。
        # 誤解を招く表示にしないための必須ブロック。
        r.blocks.append(Block("※リンクは1つ目の商品のものです", 0))
        r.allowed_numbers.add("1")
        r.blocks.extend(_link_blocks(ctx, r))
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
    Template("objective", render_objective, ("product",)),
    Template("short", render_short, ("product",)),
    Template("checklist", render_checklist, ("product",)),
    Template("band_focus", render_band_focus, ("product",)),
    Template("price_band", render_price_band, ("price_band",), item_count=3),
    Template("review_heavy", render_review_heavy, ("review_heavy",), item_count=3),
    Template("postage_free", render_postage_free, ("postage_free",), item_count=3),
    Template("comparison", render_comparison, ("comparison",), item_count=2),
    Template("topic", render_topic, ("no_link",), item_count=0, requires_affiliate=False),
)

TEMPLATES_BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}


def templates_for(post_type: str) -> list[Template]:
    return [t for t in TEMPLATES if post_type in t.post_types]
