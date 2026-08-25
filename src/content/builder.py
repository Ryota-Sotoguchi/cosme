"""投稿文の組み立て。

* パーツ選択は「直近使用を避ける決定的な選択」。純粋ランダムにはしない。
* 文字数超過時はブロックを drop_priority 順に落として収める
  （楽天のアフィリエイトURLが長いため、この処理が実用上必須）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from ..rakuten.models import RakutenItem
from ..storage.state import State
from . import facts as F
from .parts import Part
from .templates import Block, RenderContext, Template, templates_for

logger = logging.getLogger(__name__)

# 「レビューが多い」と表現してよい閾値
MANY_REVIEWS_THRESHOLD = 300
# 「この価格帯としては安い」と表現してよい閾値
CHEAP_THRESHOLD = 1500


@dataclass
class Draft:
    """生成された投稿文の草案。compliance に渡される。"""

    text: str
    template_id: str
    post_type: str
    items: list[RakutenItem]
    part_ids: dict[str, str] = field(default_factory=dict)
    allowed_numbers: set[str] = field(default_factory=set)
    # スレッド投稿の各本文。1要素なら通常の単発投稿。
    segments: list[str] = field(default_factory=list)
    # リンクカードとして添付するURL。本文の文字数を消費しない。
    link_attachment: str | None = None
    # トピックタグ。フォロワーがいなくても一覧経由で見てもらえる導線。
    topic_tag: str | None = None

    def __post_init__(self) -> None:
        if not self.segments:
            self.segments = [self.text]

    @property
    def has_affiliate_link(self) -> bool:
        """本文中のURL、またはリンクカード添付のどちらでも「リンクあり」とする。"""
        if self.link_attachment:
            return True
        return any(
            item.affiliate_url and item.affiliate_url in self.text for item in self.items
        )

    @property
    def primary_item(self) -> RakutenItem | None:
        return self.items[0] if self.items else None


class ContentBuilder:
    def __init__(self, state: State, *, max_length: int = 500) -> None:
        self.state = state
        self.max_length = max_length
        self._used_this_draft: dict[str, str] = {}

    # ------------------------------------------------------------------
    def _pick(
        self,
        group: str,
        pool: tuple[Part, ...],
        *,
        postage_free: bool = False,
        many_reviews: bool = False,
        cheap: bool = False,
    ) -> Part:
        """条件を満たすパーツから、直近使っていないものを決定的に選ぶ。"""
        eligible = [
            part
            for part in pool
            if (not part.requires_postage_free or postage_free)
            and (not part.requires_many_reviews or many_reviews)
            and (not part.requires_cheap or cheap)
        ]
        if not eligible:
            eligible = list(pool)
        if not eligible:
            raise ValueError(f"パーツプール '{group}' が空です")

        # プールの3/4を避ける。半分だと、6件のプールなら4投稿で一巡して
        # 同じ言い回しが戻ってくる。1日1〜5投稿なので、それだと
        # 見ている側には「同じことを繰り返している」ように映る。
        # 候補が尽きたら下で eligible に戻すので、行き詰まりはしない。
        recent = self.state.recent_part_ids(group, limit=max(3, len(eligible) * 3 // 4))
        fresh = [p for p in eligible if p.id not in recent]
        candidates = fresh or eligible

        # 決定的だが偏らない選択: 直近履歴の長さでカーソルを進める
        cursor = len(self.state.recent_part_ids(group, limit=10_000))
        chosen = candidates[cursor % len(candidates)]
        self._used_this_draft[group] = chosen.id
        return chosen

    # ------------------------------------------------------------------
    def _assemble(self, blocks: list[Block], affiliate_url: str | None) -> str:
        """ブロックを連結し、上限を超えたら drop_priority の大きい順に落とす。"""
        droppable = sorted(
            {b.drop_priority for b in blocks if b.drop_priority > 0}, reverse=True
        )
        dropped: set[int] = set()

        while True:
            kept = [b for b in blocks if b.drop_priority not in dropped and b.text.strip()]
            text = "\n\n".join(b.text.strip() for b in kept).strip()
            if len(text) <= self.max_length:
                return text
            remaining = [p for p in droppable if p not in dropped]
            if not remaining:
                # 必須ブロックだけで超過。URLを残して機械的に切り詰める。
                return self._hard_trim(text, affiliate_url)
            dropped.add(remaining[0])

    def _hard_trim(self, text: str, affiliate_url: str | None) -> str:
        """最終手段。URLだけは絶対に壊さずに末尾から詰める。"""
        if not affiliate_url or affiliate_url not in text:
            return text[: self.max_length].rstrip()

        head, _, tail = text.partition(affiliate_url)
        budget = self.max_length - len(affiliate_url) - len(tail)
        if budget <= 0:
            # URLだけで上限を超えるケース。呼び出し側が compliance で弾く。
            logger.error("アフィリエイトURLが長すぎて本文に収まりません (%d文字)", len(affiliate_url))
            return (affiliate_url + tail)[: self.max_length]
        return (head[:budget].rstrip() + affiliate_url + tail).strip()

    # ------------------------------------------------------------------
    def available_templates(self, post_type: str, *, exclude: set[str] | None = None) -> list[Template]:
        """そのポストタイプで使えるテンプレートを、直近使用が後ろに来る順で返す。"""
        recent = self.state.recent_template_ids(limit=8)
        pool = [t for t in templates_for(post_type) if not exclude or t.id not in exclude]
        if not pool:
            return []

        def freshness(template: Template) -> tuple[int, str]:
            # 直近で使ったものほど後ろへ。未使用は最優先。
            try:
                # recent の末尾が最新なので、末尾からの距離が小さいほど「新しい」
                last = len(recent) - 1 - recent[::-1].index(template.id)
            except ValueError:
                last = -1
            return (last, template.id)

        return sorted(pool, key=freshness)

    # ------------------------------------------------------------------
    def build(
        self,
        post_type: str,
        items: list[RakutenItem],
        *,
        template_id: str | None = None,
        with_affiliate_link: bool = True,
        exclude_templates: set[str] | None = None,
    ) -> Draft:
        """投稿文を1つ生成する。"""
        candidates = self.available_templates(post_type, exclude=exclude_templates)
        if not candidates:
            raise ValueError(f"post_type '{post_type}' に使えるテンプレートがありません")

        if template_id:
            template = next((t for t in candidates if t.id == template_id), candidates[0])
        else:
            template = candidates[0]

        needed = template.item_count
        selected = items[:needed] if needed else []
        if needed and len(selected) < needed:
            raise ValueError(
                f"テンプレート '{template.id}' は商品{needed}件必要ですが {len(selected)}件しかありません"
            )

        primary = selected[0] if selected else None
        affiliate_url = (
            primary.affiliate_url if (with_affiliate_link and primary and template.requires_affiliate) else None
        )

        self._used_this_draft = {}
        ctx = RenderContext(
            pick=self._pick,
            items=selected,
            category=F.category_label(primary) if primary else "コスメ",
            postage_free=bool(primary and primary.is_postage_free),
            many_reviews=bool(primary and (primary.review_count or 0) >= MANY_REVIEWS_THRESHOLD),
            cheap=bool(primary and primary.item_price <= CHEAP_THRESHOLD),
            affiliate_url=affiliate_url,
            # 直近の使用履歴の長さでずらす。乱数を使わず決定的に回す。
            fact_style=len(self.state.recent_part_ids("closing", limit=10_000)),
        )

        rendered = template.render(ctx)
        if rendered.segments:
            # スレッドは1本ずつ文字数を守る。連結してから切ると
            # 分割位置がずれるので、ブロック結合は使わない。
            segments = [
                seg.strip()[: self.max_length] for seg in rendered.segments if seg.strip()
            ]
            text = "\n\n".join(segments)
        else:
            text = self._assemble(rendered.blocks, affiliate_url)
            segments = [text]

        draft = Draft(
            text=text,
            template_id=template.id,
            post_type=post_type,
            items=selected,
            part_ids=dict(rendered.part_ids),
            allowed_numbers=set(rendered.allowed_numbers),
            link_attachment=affiliate_url,
            segments=segments,
            # フォロワーが少ないうちは、タグ経由がほぼ唯一の発見導線になる。
            # リンクなし投稿にも付ける（むしろそちらのほうが伸びる）。
            topic_tag=F.topic_tag_for(
                F.category_label(primary) if primary else "コスメ",
                len(self.state.recent_part_ids("closing", limit=10_000)),
                post_type,
            ),
        )
        logger.info(
            "生成: template=%s type=%s len=%d link=%s parts=%s",
            draft.template_id,
            post_type,
            len(text),
            draft.has_affiliate_link,
            ",".join(f"{k}:{v}" for k, v in sorted(draft.part_ids.items())),
        )
        return draft

    # ------------------------------------------------------------------
    def commit(self, draft: Draft) -> None:
        """投稿が確定したときに、使用パーツ/テンプレートを state に記録する。"""
        self.state.record_part_ids(draft.part_ids)
        self.state.record_template_id(draft.template_id)

    @staticmethod
    def fingerprint(text: str) -> str:
        """本文の同一性判定用ハッシュ（空白と改行を無視）。"""
        normalized = "".join(text.split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
