"""投稿履歴 (data/history.jsonl)。

public リポジトリに置くため、以下を守る:
  * Secret（トークン・アクセスキー）は一切保存しない
  * affiliate URL はアフィリエイトIDを含むので SHA-256 ハッシュのみ保存
  * 商品URLは公開情報なのでそのまま保存してよい
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 投稿本文をそのまま保存すると、アフィリエイトIDを含むURLが public リポジトリの
# git 履歴に残り続ける。投稿自体は公開されるので秘密ではないが、リポジトリへ複製する
# 理由も無いので伏せる。商品の同定は affiliate_url_hash / item_url_hash が担う。
AFFILIATE_URL_PATTERN = re.compile(r"https?://(?:hb\.afl\.rakuten\.co\.jp|a\.r10\.to)/\S+")
AFFILIATE_URL_PLACEHOLDER = "[affiliate-url]"


def redact_affiliate_urls(text: str) -> str:
    """本文中のアフィリエイトURLをプレースホルダに置き換える。

    類似度判定は checker._normalize_for_similarity() が元々URLを除去しているため、
    置換しても重複検出の精度は落ちない。
    """
    return AFFILIATE_URL_PATTERN.sub(AFFILIATE_URL_PLACEHOLDER, text)


@dataclass
class PostRecord:
    """投稿1件の記録。"""

    posted_at: str                      # ISO8601 (JST)
    slot: str
    post_type: str
    template_id: str
    status: str                         # "success" | "failed" | "skipped" | "dry_run"
    text: str
    has_affiliate_link: bool

    thread_post_id: str | None = None
    permalink: str | None = None

    item_code: str | None = None
    item_name: str | None = None
    item_price: int | None = None
    category: str | None = None
    review_average: float | None = None
    review_count: int | None = None
    affiliate_rate: float | None = None
    postage_free: bool | None = None
    shop_code: str | None = None
    brand_key: str | None = None
    genre_id: str | None = None

    # URL識別用（生のaffiliate URLは保存しない）
    affiliate_url_hash: str | None = None
    item_url_hash: str | None = None
    item_url: str | None = None

    error_type: str | None = None
    error_message: str | None = None

    # 投稿後に取得する成績。どの投稿が効いたかを判断する材料。
    insights: dict[str, Any] = field(default_factory=dict)

    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PostRecord":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in payload.items() if k in known}
        filtered.setdefault("posted_at", "")
        filtered.setdefault("slot", "")
        filtered.setdefault("post_type", "")
        filtered.setdefault("template_id", "")
        filtered.setdefault("status", "unknown")
        filtered.setdefault("text", "")
        filtered.setdefault("has_affiliate_link", False)
        return cls(**filtered)

    @property
    def posted_datetime(self) -> datetime | None:
        if not self.posted_at:
            return None
        try:
            dt = datetime.fromisoformat(self.posted_at)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=JST)


class History:
    """JSONL 形式の投稿履歴。読み込みは全件メモリ展開で十分な規模。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: list[PostRecord] | None = None

    # ------------------------------------------------------------------
    def load(self) -> list[PostRecord]:
        if self._records is not None:
            return self._records

        records: list[PostRecord] = []
        if self.path.is_file():
            for line_no, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    records.append(PostRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError) as exc:
                    # 壊れた行があっても運用は止めない
                    logger.warning("履歴 %s:%d を読み飛ばしました: %s", self.path.name, line_no, exc)
        self._records = records
        return records

    def append(self, record: PostRecord) -> None:
        # 保存の境界で必ず伏せる。呼び出し側が忘れても漏れないようにするため。
        #
        # text だけでは足りない。affiliateId を指定すると楽天APIは itemUrl も
        # アフィリエイトURLで返すので、URLを持つフィールドはすべて通す。
        record.text = redact_affiliate_urls(record.text)
        if record.item_url:
            record.item_url = redact_affiliate_urls(record.item_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_json() + "\n")
        if self._records is not None:
            self._records.append(record)
        logger.info(
            "履歴に記録: slot=%s status=%s item=%s post_id=%s",
            record.slot,
            record.status,
            record.item_code,
            record.thread_post_id,
        )

    # ------------------------------------------------------------------
    def successful(self) -> list[PostRecord]:
        return [r for r in self.load() if r.status == "success"]

    def recent(self, limit: int, *, only_success: bool = True) -> list[PostRecord]:
        source = self.successful() if only_success else self.load()
        return source[-limit:] if limit > 0 else []

    def since(self, days: int, *, only_success: bool = True) -> list[PostRecord]:
        cutoff = datetime.now(JST) - timedelta(days=days)
        source = self.successful() if only_success else self.load()
        out = []
        for record in source:
            dt = record.posted_datetime
            if dt is None or dt >= cutoff:
                out.append(record)
        return out

    def posts_today(self, *, only_success: bool = True) -> list[PostRecord]:
        today = datetime.now(JST).date()
        source = self.successful() if only_success else self.load()
        return [
            r
            for r in source
            if (dt := r.posted_datetime) is not None and dt.astimezone(JST).date() == today
        ]

    def affiliate_posts_today(self) -> int:
        return sum(1 for r in self.posts_today() if r.has_affiliate_link)

    # ------------------------------------------------------------------
    def recent_item_codes(self, days: int) -> set[str]:
        return {r.item_code for r in self.since(days) if r.item_code}

    def recent_url_hashes(self, days: int) -> set[str]:
        hashes: set[str] = set()
        for record in self.since(days):
            if record.affiliate_url_hash:
                hashes.add(record.affiliate_url_hash)
            if record.item_url_hash:
                hashes.add(record.item_url_hash)
        return hashes

    def recent_shop_codes(self, days: int) -> list[str]:
        return [r.shop_code for r in self.since(days) if r.shop_code]

    def recent_texts(self, limit: int) -> list[str]:
        return [r.text for r in self.recent(limit) if r.text]

    def recent_template_ids(self, limit: int) -> list[str]:
        return [r.template_id for r in self.recent(limit) if r.template_id]

    def update_insights(self, post_id: str, values: dict[str, Any]) -> bool:
        """投稿IDに対応する行の成績を更新して書き戻す。

        追記型のJSONLだが、成績は後から確定するので該当行だけ差し替える。
        """
        records = self.load()
        target = next((r for r in records if r.thread_post_id == post_id), None)
        if target is None:
            return False
        target.insights = {**target.insights, **values}
        self.path.write_text(
            "\n".join(r.to_json() for r in records) + "\n", encoding="utf-8"
        )
        return True

    def counter(self, attribute: str, limit: int) -> dict[str, int]:
        """直近 limit 件における属性値の出現回数。偏り抑制のペナルティ計算に使う。"""
        counts: dict[str, int] = {}
        for record in self.recent(limit):
            value = getattr(record, attribute, None)
            if value:
                counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    def __iter__(self) -> Iterator[PostRecord]:
        return iter(self.load())

    def __len__(self) -> int:
        return len(self.load())
