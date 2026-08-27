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
    "thread_topic": 0,
    "question": 0,
    "casual": 0,
    "howto": 0,
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

    def daily_cap_reached(self) -> bool:
        """本日の投稿数が上限に達しているか。

        種別も実行トリガーも問わず数える。手動での動作確認が積み上がって
        1日18件投稿し、Meta に API アクセスをブロックされたことがあるため、
        ここは無条件の頭打ちにしておく。
        """
        limit = int(self.config.ramp_up.get("max_posts_per_day", 7))
        today = len(self.history.posts_today())
        if today >= limit:
            logger.error(
                "本日の投稿数が上限に達しています（%d/%d件）。投稿しません。", today, limit
            )
            return True
        logger.info("本日の投稿数 %d/%d件", today, limit)
        return False

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

        # 動作確認のための手動実行が、本番のリンク枠を食わないようにする。
        # ランプアップは1日1〜2本しか許さないので、テストで1本使うと
        # その日の定期実行がリンクなしになってしまう（実際に起きた）。
        already = self.history.affiliate_posts_today(scheduled_only=True)
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

    def _brand_hint(self, post_type: str) -> str:
        """過去に投稿した商品から、つぶやきを1本作る。作れなければ空。

        ## なぜ過去の商品を使うのか

        新しく候補を取ると、その商品が30日クールダウンに入る。
        つぶやき1本のために在庫を焼くのは割に合わない。
        すでに投稿済みの商品なら、もう焼けているので追加の損が無い。

        「この前見たやつ、詰め替えあったんだ」という距離感にもなる。

        ## 書けるのは商品名から確かめられる事実だけ

        使っていないので、評価も感想も書けない。
        ブランド名が取れないときは黙る（「メール便」を出さない）。
        """
        if post_type != "casual":
            return ""

        from .content.brand_murmurs import murmur_for

        recent = self.history.recent(30)
        # 直近ほど新しいので、後ろから見る
        cursor = len(self.history.recent_texts(10_000))
        for record in reversed(recent):
            if not record.item_name:
                continue
            item = RakutenItem.from_history_name(record.item_name)
            hint = murmur_for(item, cursor=cursor)
            if hint:
                logger.info("実在商品からつぶやきを作りました: %s", hint)
                return hint
        return ""

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

        blocked_codes, blocked_hashes = self._blocked()

        # 使える候補が薄いときは、取得を深掘りして自動で積み増す。
        #
        # 30日クールダウンで既出商品が増えると、同じ層を引き続けている限り
        # 使える候補が減っていく。放っておくと商品投稿が出せなくなるので、
        # 別ページ（rotation_seed をずらす）を追加で取りに行って補充する。
        min_pool = int(selection.get("min_usable_candidates", 20))
        max_attempts = int(selection.get("pool_refill_attempts", 3))

        seen: set[str] = set()
        pool: list[RakutenItem] = []
        kept: list[RakutenItem] = []

        for attempt in range(max_attempts):
            batch = self.rakuten.collect_candidates(
                genres,
                sorts=sorts,
                postage_flag=postage_flag,
                min_price=price_window[0],
                max_price=price_window[1],
                # 1回目は日替わりの既定値、2回目以降は別の層を見る
                rotation_seed=None if attempt == 0 else self._refill_seed(attempt),
            )
            for item in batch:
                if item.item_code not in seen:
                    seen.add(item.item_code)
                    pool.append(item)

            kept = filter_items(
                pool,
                self.config.exclusion,
                selection,
                excluded_item_codes=blocked_codes,
                excluded_url_hashes=blocked_hashes,
            )
            if len(kept) >= min_pool:
                break
            logger.warning(
                "使える候補が %d件しかありません（目標 %d件）。深掘りして補充します（%d/%d回目）",
                len(kept),
                min_pool,
                attempt + 1,
                max_attempts,
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


    @staticmethod
    def _same_category_first(scored: list[ScoredItem], needed: int) -> list[ScoredItem]:
        """複数商品を並べる投稿では、同じカテゴリーの商品が先に来るようにする。

        見出しのカテゴリー名は1件目から取るので、混ざっていると
        「ヘアケア、迷ってるので並べてみた」と言いながらメイクブラシが
        並ぶことになる。読み手を誤解させるので揃える。
        """
        groups: dict[str, list[ScoredItem]] = {}
        for entry in scored:
            label = (entry.item.raw or {}).get("_genre_label", "")
            groups.setdefault(label, []).append(entry)

        # needed 件そろうグループをスコア順に並べ、足りないものは後ろへ回す
        usable = [g for g in groups.values() if len(g) >= needed]
        if not usable:
            return scored
        usable.sort(key=lambda g: -g[0].score)
        rest = [e for g in groups.values() if len(g) < needed for e in g]
        return [e for g in usable for e in g] + rest

    def _refill_seed(self, attempt: int) -> int:
        """補充時に使う rotation_seed。

        日ごとの既定値から大きくずらして、まだ見ていない層を狙う。
        素数をかけてページ・ジャンル順の周期と重ならないようにする。
        """
        base = datetime.now(JST).timetuple().tm_yday
        return base + attempt * 37

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

        # --- リンクを付けられないなら、商品は出さない ---
        #
        # リンクの無い商品投稿は誰の得にもならない。
        # 読者はその商品ページへ行けないし、こちらは収益にならない。
        # そのうえ「投稿済み」として記録されるので、その商品が30日間
        # クールダウンに入ってしまう（在庫だけ減って見返りがない）。
        #
        # ランプアップ中はリンク枠が1日1〜2本に絞られるため、
        # 対策しないと初期の2週間で20件前後の商品を無駄に焼くことになる。
        if needed > 0 and not with_link:
            logger.info(
                "リンクを付けられないスロットなので、商品を消費せずリンクなし投稿にします"
            )
            return self._run_no_link(recent_texts, slot=slot_name)

        # --- リンクなし投稿は商品取得が不要 ---
        if needed == 0:
            return self._run_no_link(recent_texts, post_type=post_type)

        try:
            scored = self.gather_candidates(post_type)
        except NoDataError as exc:
            # 楽天側が一時的に返さない・全部除外された、等。枠は埋める。
            logger.warning("候補商品が取れないため、リンクなし投稿に切り替えます（%s）", exc)
            return self._run_no_link(recent_texts, slot=slot_name)
        if needed > 1:
            scored = self._same_category_first(scored, needed)
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
                        slot=slot_name,
                        today=datetime.now(JST).date(),
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

        # 商品側で作れなかった場合は、リンクなし投稿に切り替えて枠を埋める。
        #
        # 30日クールダウンで候補が尽きたり、除外フィルタで全滅したりしても、
        # 投稿が丸ごと無くなるとアカウントの更新が止まって見える。
        # 収益は出ないが、話題投稿なら商品データが無くても作れる。
        detail = last_check.summary() if last_check else "候補なし"
        logger.warning(
            "商品投稿を作れなかったため、リンクなし投稿に切り替えます（%s）", detail
        )
        return self._run_no_link(recent_texts, slot=slot_name)

    # ------------------------------------------------------------------
    def _run_no_link(
        self, recent_texts: list[str], post_type: str = "no_link", slot: str = ""
    ) -> PipelineResult:
        max_regen = int(self.config.compliance.get("max_regenerations", 4))
        last_check: CheckResult | None = None

        for attempt in range(1, max_regen + 1):
            draft = self.builder.build(
                post_type, [], with_affiliate_link=False, slot=slot,
                brand_hint=self._brand_hint(post_type),
                today=datetime.now(JST).date(),
            )
            check = self.checker.check(draft, recent_texts=recent_texts)
            last_check = check
            if check.passed:
                return PipelineResult(draft=draft, check=check, attempts=attempt, skipped_items=0)
            # トピックを次のものに進めるため、パーツ履歴を記録してから再試行する
            self.builder.state.record_part_ids(draft.part_ids)

        detail = last_check.summary() if last_check else "候補なし"
        raise ComplianceSkip(f"リンクなし投稿を生成できませんでした（{detail}）")
