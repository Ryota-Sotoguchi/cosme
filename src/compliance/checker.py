"""Compliance Checker。

投稿生成と投稿実行の間に置く独立した検証工程。
不合格なら投稿しない。1商品の問題で全体運用は止めない（呼び出し側が次候補へ進む）。

検査項目:
  1. NG表現（薬機法/効果保証/架空体験/架空口コミ/誇張/禁止カテゴリー）
  2. PR表記（アフィリエイトリンクを含むなら冒頭付近に【PR】）
  3. URL（allowlist のホストのみ・本数上限）
  4. データ整合性（本文の数値が商品データ由来か）
  5. 重複（itemCode / URLハッシュ / 再投稿クールダウン）
  6. 文章類似度（直近投稿との類似）
  7. 文字数上限
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from ..content.builder import Draft
from ..content.facts import extract_numbers, normalize_number
from ..rakuten.models import RakutenItem
from . import rules as R

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s　]+")


@dataclass(frozen=True)
class Violation:
    category: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.category}] {self.detail}"


@dataclass
class CheckResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed

    def summary(self) -> str:
        if self.passed:
            return "OK"
        return " / ".join(str(v) for v in self.violations)


def _normalize_for_similarity(text: str) -> str:
    """類似度比較用の正規化: URL・空白・数値を落とす。

    URLと数値は商品ごとに必ず違うので、残すと「構造は同じなのに別物」と
    判定されてしまう。文章の骨格だけを比べたい。
    """
    without_url = URL_PATTERN.sub("", text)
    without_digits = re.sub(r"[\d,\.]+", "", without_url)
    return "".join(without_digits.split())


def _ngrams(text: str, n: int = 3) -> set[str]:
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def similarity(a: str, b: str) -> float:
    """n-gram Jaccard と difflib の大きいほうを採用する（見逃しを減らす）。"""
    na, nb = _normalize_for_similarity(a), _normalize_for_similarity(b)
    if not na or not nb:
        return 0.0
    ga, gb = _ngrams(na), _ngrams(nb)
    jaccard = len(ga & gb) / len(ga | gb) if (ga | gb) else 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return max(jaccard, ratio)


class ComplianceChecker:
    def __init__(self, compliance: dict[str, Any], dedup: dict[str, Any], max_length: int = 500) -> None:
        self.pr_marker: str = compliance.get("pr_marker", "【PR】")
        self.pr_max_offset: int = int(compliance.get("pr_marker_max_offset", 20))
        self.allowed_hosts: set[str] = set(compliance.get("allowed_url_hosts", []))
        self.max_urls: int = int(compliance.get("max_urls", 1))
        self.max_length = max_length
        self.max_similarity: float = float(dedup.get("max_similarity", 0.72))

    # ------------------------------------------------------------------
    def check(
        self,
        draft: Draft,
        *,
        recent_texts: Sequence[str] = (),
        blocked_item_codes: Iterable[str] = (),
        blocked_url_hashes: Iterable[str] = (),
    ) -> CheckResult:
        violations: list[Violation] = []
        text = draft.text

        violations += self._check_expressions(text)
        violations += self._check_length(text)
        violations += self._check_urls(text, draft)
        violations += self._check_pr_marker(text, draft)
        violations += self._check_data_integrity(text, draft)
        violations += self._check_duplication(draft, blocked_item_codes, blocked_url_hashes)
        violations += self._check_similarity(text, recent_texts)

        result = CheckResult(passed=not violations, violations=violations)
        if result.passed:
            logger.info("コンプライアンスチェック: 合格 (template=%s)", draft.template_id)
        else:
            logger.warning(
                "コンプライアンスチェック: 不合格 (template=%s) %s",
                draft.template_id,
                result.summary(),
            )
        return result

    # ------------------------------------------------------------------
    def _check_expressions(self, text: str) -> list[Violation]:
        return [
            Violation(rule.category, f"NG表現に該当: {rule.label}")
            for rule in R.scan(text)
        ]

    def _check_length(self, text: str) -> list[Violation]:
        if not text.strip():
            return [Violation("format", "本文が空です")]
        if len(text) > self.max_length:
            return [
                Violation("format", f"文字数超過: {len(text)} > {self.max_length}")
            ]
        return []

    def _check_urls(self, text: str, draft: Draft) -> list[Violation]:
        # 本文中のURLと、リンクカードとして添付するURLの両方を検証する。
        # 添付は本文に現れないが、読者がたどる先は同じなので同じ基準で見る。
        urls = URL_PATTERN.findall(text)
        if draft.link_attachment:
            urls = [*urls, draft.link_attachment]
        violations: list[Violation] = []

        if len(urls) > self.max_urls:
            violations.append(Violation("url", f"URLが多すぎます: {len(urls)} > {self.max_urls}"))

        for url in urls:
            host = urlparse(url).netloc.lower()
            if host not in self.allowed_hosts:
                violations.append(Violation("url", f"許可されていないURLホスト: {host}"))

        # 本文中のURLが、実際に選んだ商品の affiliate URL と一致しているか
        if urls:
            known = {item.affiliate_url for item in draft.items if item.affiliate_url}
            for url in urls:
                if url not in known:
                    violations.append(
                        Violation("url", "本文のURLが商品データのaffiliate URLと一致しません")
                    )
        return violations

    def _check_pr_marker(self, text: str, draft: Draft) -> list[Violation]:
        """広告表示があるか、そして読者が見る位置にあるか。

        スレッド投稿では、1本目に商品を出さず2本目から紹介を始める。
        その場合、広告表示は「商品の紹介が始まる投稿の冒頭」にあればよい。
        連結した全文の先頭で判定すると、この構成を弾いてしまう。
        """
        has_url = bool(URL_PATTERN.search(text)) or bool(draft.link_attachment)
        if not has_url:
            # リンクなし投稿にPR表記は不要（付いていても害はないので許容）
            return []

        segments = draft.segments or [text]

        # 広告表示が冒頭にある投稿を探す
        marked = [
            index
            for index, segment in enumerate(segments)
            if 0 <= segment.find(self.pr_marker) <= self.pr_max_offset
        ]
        if not marked:
            if any(self.pr_marker in segment for segment in segments):
                return [
                    Violation(
                        "pr",
                        f"{self.pr_marker} はありますが、投稿の冒頭にありません",
                    )
                ]
            return [
                Violation("pr", f"アフィリエイトリンクがあるのに {self.pr_marker} がありません")
            ]

        # 商品名が最初に出てくる投稿より後ろに表示があってはいけない
        first_product = next(
            (
                index
                for index, segment in enumerate(segments)
                if any(
                    item.display_name(38) and item.display_name(38) in segment
                    for item in draft.items
                )
            ),
            None,
        )
        if first_product is not None and min(marked) > first_product:
            return [
                Violation(
                    "pr",
                    f"商品の紹介が始まる投稿より後ろに {self.pr_marker} があります",
                )
            ]
        return []

    def _check_data_integrity(self, text: str, draft: Draft) -> list[Violation]:
        """本文に現れる数値がすべて商品データ由来かを検証する。

        テンプレートやフォーマッタが数値を書き換えていないことを
        コードで確認するのがこのチェックの目的。
        """
        allowed = set(draft.allowed_numbers)
        for item in draft.items:
            allowed |= self._allowed_numbers_for(item)

        violations: list[Violation] = []
        for token in extract_numbers(text):
            if token in allowed:
                continue
            # URL に含まれる数値は本文の主張ではないので除外する
            urls = [*URL_PATTERN.findall(text)]
            if draft.link_attachment:
                urls.append(draft.link_attachment)
            if any(token in normalize_number(url) for url in urls):
                continue
            violations.append(
                Violation("data", f"商品データに存在しない数値が本文にあります: {token}")
            )
        return violations

    @staticmethod
    def _allowed_numbers_for(item: RakutenItem) -> set[str]:
        """1商品から本文に書いてよい数値の集合。"""
        from ..content.facts import format_review_average, price_band_value

        allowed = {
            normalize_number(str(item.item_price)),
            normalize_number(str(price_band_value(item.item_price))),
        }
        if item.review_count is not None:
            allowed.add(normalize_number(str(item.review_count)))
        if item.review_average is not None:
            allowed.add(normalize_number(format_review_average(item.review_average)))
            allowed.add(normalize_number(str(item.review_average)))
        if item.point_rate is not None:
            allowed.add(normalize_number(str(item.point_rate)))
        # 商品名に含まれる数値（容量・型番など）は事実なので許可する
        allowed |= set(extract_numbers(item.item_name))
        return allowed

    def _check_duplication(
        self,
        draft: Draft,
        blocked_item_codes: Iterable[str],
        blocked_url_hashes: Iterable[str],
    ) -> list[Violation]:
        codes = set(blocked_item_codes)
        hashes = set(blocked_url_hashes)
        violations: list[Violation] = []

        seen_in_draft: set[str] = set()
        for item in draft.items:
            if item.item_code in seen_in_draft:
                violations.append(Violation("dedup", f"同一投稿内に商品が重複: {item.item_code}"))
            seen_in_draft.add(item.item_code)

            if item.item_code in codes:
                violations.append(
                    Violation("dedup", f"再投稿クールダウン中の商品: {item.item_code}")
                )
            if item.item_url_hash in hashes:
                violations.append(Violation("dedup", "同一商品URLが最近投稿済み"))
            if item.affiliate_url_hash and item.affiliate_url_hash in hashes:
                violations.append(Violation("dedup", "同一affiliate URLが最近投稿済み"))
        return violations

    def _check_similarity(self, text: str, recent_texts: Sequence[str]) -> list[Violation]:
        for past in recent_texts:
            score = similarity(text, past)
            if score > self.max_similarity:
                return [
                    Violation(
                        "similarity",
                        f"直近投稿と類似しすぎています (類似度 {score:.2f} > {self.max_similarity})",
                    )
                ]
        return []
