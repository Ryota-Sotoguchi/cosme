"""投稿1件を作るまでのパイプライン。

  楽天API → 除外フィルタ → スコアリング → 商品選択
       → 投稿文生成 → Compliance Check →（合格するまで再生成/次候補）

1商品の問題で全体を止めない。不合格なら別テンプレートで再生成し、
それでもだめならその商品をスキップして次の候補へ進む。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .compliance.checker import CheckResult, ComplianceChecker
from .config import Config
from .content.builder import ContentBuilder, Draft
from .errors import ComplianceSkip, NoDataError
from .rakuten.client import DEFAULT_SORTS, RakutenClient
from .rakuten.models import RakutenItem
from .selector.filters import filter_items
from .selector.scoring import ScoredItem, score_items
from .storage.history import History
from .storage.state import State

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ポストタイプごとの必要商品数（templates.py と一致させる）
ITEMS_NEEDED = {
    "product": 1,
    "price_band": 3,
    "review_heavy": 3,
    "postage_free": 3,
    "comparison": 2,
    "no_link": 0,
}


@dataclass
class PipelineResult:
    draft: Draft
    check: CheckResult
    attempts: int
    skipped_items: int


class Pipeline:
    def __init__(
        self,
        config: Config,
        *,
        history: History,
        state: State,
        rakuten: RakutenClient | None = None,
    ) -> None:
        self.config = config
        self.history = history
        self.state = state
        self.rakuten = rakuten or RakutenClient(config)
        self.builder = ContentBuilder(
            state, max_length=int(config.threads.get("max_text_length", 500))
        )
        self.checker = ComplianceChecker(
            config.compliance,
            config.dedup,
            max_length=int(config.threads.get("max_text_length", 500)),
        )

    # ------------------------------------------------------------------
    def resolve_post_type(self, slot_name: str) -> str:
        """スロットから実際の投稿タイプを決める。ローテーションはここで進める。"""
        slot = self.config.slot(slot_name)
        if slot.post_type != "rotation":
            return slot.post_type

        options = list(self.config.rotation.get(slot_name, []))
        if not options:
            logger.warning("スロット '%s' のローテーション設定が無いので product を使います", slot_name)
            return "product"
        return self.state.next_rotation(slot_name, options)

    def affiliate_allowed(self, slot_name: str) -> bool:
        """このスロットでアフィリエイトリンクを付けてよいか。

        ランプアップ設定により、運用開始直後はリンク投稿本数を抑える。
        """
        slot = self.config.slot(slot_name)
        if not slot.allow_affiliate:
            return False

        ramp = self.config.ramp_up
        if not ramp.get("enabled", False):
            return True

        day = self.state.days_since_start()
        limit = ramp.get("default_max_link_posts", 3)
        for stage in ramp.get("stages", []):
            if day <= int(stage.get("until_day", 0)):
                limit = int(stage.get("max_link_posts", limit))
                break

        already = self.history.affiliate_posts_today()
        if already >= limit:
            logger.info(
                "ランプアップ制限: 運用%d日目、本日のリンク投稿 %d/%d 件のためリンクなしにします",
                day,
                already,
                limit,
            )
            return False

        logger.info("ランプアップ: 運用%d日目、本日のリンク投稿 %d/%d 件", day, already, limit)
        return True

    # ------------------------------------------------------------------
    def _blocked(self) -> tuple[set[str], set[str]]:
        days = int(self.config.dedup.get("item_cooldown_days", 30))
        return self.history.recent_item_codes(days), self.history.recent_url_hashes(days)

    def gather_candidates(self, post_type: str) -> list[ScoredItem]:
        """候補商品をスコア順で取得する。"""
        selection = self.config.selection
        genres = self.config.genres

        # 投稿タイプに応じて取得条件を変える
        postage_flag = 1 if post_type == "postage_free" else None
        # ソートは投稿タイプによらず -reviewCount に統一する。
        # standard や価格ソートはレビュー0件の商品ばかり返し、除外フィルタで
        # 全滅して取得枠を無駄にする（実測: standard は30件中0件しか通らない）。
        # 「価格帯別」「送料無料」といった切り口は、ソートではなく
        # 取得条件（price 範囲・postageFlag）とスコアリングで表現する。
        sorts = DEFAULT_SORTS

        # 価格帯別のまとめは、価格の幅を狭めて取り直すと粒が揃う
        price_window: tuple[int | None, int | None] = (None, None)
        if post_type == "price_band":
            lo = selection["min_price"]
            hi = selection["max_price"]
            span = (hi - lo) // 3
            band = self.state.next_rotation("price_band_window", ["low", "mid", "high"])
            price_window = {
                "low": (lo, lo + span),
                "mid": (lo + span, lo + span * 2),
                "high": (lo + span * 2, hi),
            }[band]
            logger.info("価格帯まとめ: %s帯 %s円〜%s円", band, *price_window)

        pool = self.rakuten.collect_candidates(
            genres,
            sorts=sorts,
            postage_flag=postage_flag,
            min_price=price_window[0],
            max_price=price_window[1],
        )

        blocked_codes, blocked_hashes = self._blocked()
        kept = filter_items(
            pool,
            self.config.exclusion,
            selection,
            excluded_item_codes=blocked_codes,
            excluded_url_hashes=blocked_hashes,
        )
        if not kept:
            raise NoDataError("除外フィルタ後に候補商品が残りませんでした")

        window = int(self.config.scoring.get("recency_window", 20))
        recent = self.history.recent(window)
        scored = score_items(
            kept,
            self.config.scoring,
            selection,
            recent_shop_codes=[r.shop_code for r in recent if r.shop_code],
            recent_brand_keys=[r.brand_key for r in recent if r.brand_key],
            recent_genre_ids=[r.genre_id for r in recent if r.genre_id],
        )
        return scored

    # ------------------------------------------------------------------
    def run(self, slot_name: str) -> PipelineResult:
        """スロットに対する投稿草案を1つ作り、コンプライアンス合格まで持っていく。"""
        post_type = self.resolve_post_type(slot_name)
        with_link = self.affiliate_allowed(slot_name)
        needed = ITEMS_NEEDED.get(post_type, 1)

        logger.info(
            "パイプライン開始: slot=%s post_type=%s items_needed=%d affiliate=%s",
            slot_name,
            post_type,
            needed,
            with_link,
        )

        recent_texts = self.history.recent_texts(
            int(self.config.dedup.get("similarity_window", 60))
        )
        blocked_codes, blocked_hashes = self._blocked()

        # --- リンクなし投稿は商品取得が不要 ---
        if needed == 0:
            return self._run_no_link(recent_texts)

        scored = self.gather_candidates(post_type)
        max_skips = int(self.config.compliance.get("max_item_skips", 12))
        max_regen = int(self.config.compliance.get("max_regenerations", 4))

        attempts = 0
        last_check: CheckResult | None = None

        for skip_index in range(min(max_skips, max(1, len(scored) - needed + 1))):
            selection = [s.item for s in scored[skip_index : skip_index + needed]]
            if len(selection) < needed:
                break

            tried_templates: set[str] = set()
            for _ in range(max_regen):
                attempts += 1
                try:
                    draft = self.builder.build(
                        post_type,
                        selection,
                        with_affiliate_link=with_link,
                        exclude_templates=tried_templates,
                    )
                except ValueError as exc:
                    logger.warning("生成できませんでした: %s", exc)
                    break

                tried_templates.add(draft.template_id)

                check = self.checker.check(
                    draft,
                    recent_texts=recent_texts,
                    blocked_item_codes=blocked_codes,
                    blocked_url_hashes=blocked_hashes,
                )
                last_check = check
                if check.passed:
                    return PipelineResult(
                        draft=draft, check=check, attempts=attempts, skipped_items=skip_index
                    )

                # 商品自体に問題がある違反なら、再生成しても無駄なので次の商品へ
                fatal = {"category", "dedup", "url"}
                if any(v.category in fatal for v in check.violations):
                    logger.info("商品側の問題のため次の候補へ: %s", check.summary())
                    break

            logger.info("候補 %d 番目をスキップします", skip_index + 1)

        detail = last_check.summary() if last_check else "候補なし"
        raise ComplianceSkip(
            f"コンプライアンスに合格する投稿を生成できませんでした（最後の不合格: {detail}）",
            violations=[str(v) for v in (last_check.violations if last_check else [])],
        )

    # ------------------------------------------------------------------
    def _run_no_link(self, recent_texts: list[str]) -> PipelineResult:
        max_regen = int(self.config.compliance.get("max_regenerations", 4))
        last_check: CheckResult | None = None

        for attempt in range(1, max_regen + 1):
            draft = self.builder.build("no_link", [], with_affiliate_link=False)
            check = self.checker.check(draft, recent_texts=recent_texts)
            last_check = check
            if check.passed:
                return PipelineResult(draft=draft, check=check, attempts=attempt, skipped_items=0)
            # トピックを次のものに進めるため、パーツ履歴を記録してから再試行する
            self.builder.state.record_part_ids(draft.part_ids)

        detail = last_check.summary() if last_check else "候補なし"
        raise ComplianceSkip(f"リンクなし投稿を生成できませんでした（{detail}）")
