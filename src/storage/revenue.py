"""楽天アフィリエイトの実績 (data/revenue.jsonl)。

## なぜ手で入れるのか

楽天アフィリエイトにはレポートの公開APIが無い。クリック数・成果件数・
報酬額は管理画面のレポート（CSV）でしか取れないので、取り込み口だけ作る。

## なぜ日次でよいのか

レポートは日次の合計しか出せず、投稿単位に割り当てられない。
そこで **リンク投稿を1日1本に絞っている**（config の [[schedule]] で
allow_affiliate を1枠だけにしてある）。1日1本なら、その日のクリック数が
そのままその投稿のクリック数になる。追加のインフラも費用も要らない。

リンク投稿が1日に2本以上あった日は、投稿単位には割り当てられない。
`attributable` が False になり、レポート側で除外される。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# CSVの列名は楽天側の都合で変わりうる。見出しを部分一致で拾う。
_DATE_KEYS = ("日付", "date", "発生日", "対象日")
_CLICK_KEYS = ("クリック", "click")
_ORDER_KEYS = ("件数", "注文", "成果", "order", "件")
_REWARD_KEYS = ("報酬", "成果報酬", "reward", "金額")


@dataclass
class RevenueDay:
    """ある1日の実績。"""

    date: str          # YYYY-MM-DD (JST)
    clicks: int = 0
    orders: int = 0
    reward: int = 0    # 円
    source: str = "manual"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _to_int(value: str | int | float | None) -> int:
    """「1,200円」「¥48」「3件」から数字だけ取る。取れなければ 0。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^0-9\-]", "", str(value))
    return int(digits) if digits and digits != "-" else 0


def _match(header: str, keys: tuple[str, ...]) -> bool:
    lowered = header.strip().lower()
    return any(key.lower() in lowered for key in keys)


class Revenue:
    """日次実績の追記型ストア。同じ日付は後から入れたもので置き換える。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, RevenueDay] | None = None

    # ------------------------------------------------------------------
    def load(self) -> dict[str, RevenueDay]:
        if self._rows is not None:
            return self._rows

        rows: dict[str, RevenueDay] = {}
        if self.path.is_file():
            for line_no, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    entry = RevenueDay(
                        date=str(payload["date"]),
                        clicks=int(payload.get("clicks", 0)),
                        orders=int(payload.get("orders", 0)),
                        reward=int(payload.get("reward", 0)),
                        source=str(payload.get("source", "manual")),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    # 壊れた行があっても運用は止めない
                    logger.warning("実績 %s:%d を読み飛ばしました: %s", self.path.name, line_no, exc)
                    continue
                rows[entry.date] = entry
        self._rows = rows
        return rows

    def put(self, entry: RevenueDay) -> None:
        rows = self.load()
        rows[entry.date] = entry
        self._flush()

    def put_many(self, entries: list[RevenueDay]) -> int:
        rows = self.load()
        for entry in entries:
            rows[entry.date] = entry
        self._flush()
        return len(entries)

    def _flush(self) -> None:
        rows = self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(rows[key].to_json() for key in sorted(rows))
        self.path.write_text(body + "\n" if body else "", encoding="utf-8")

    # ------------------------------------------------------------------
    def get(self, day: str | date) -> RevenueDay | None:
        key = day.isoformat() if isinstance(day, date) else str(day)
        return self.load().get(key)

    def totals(self) -> RevenueDay:
        rows = self.load().values()
        return RevenueDay(
            date="合計",
            clicks=sum(r.clicks for r in rows),
            orders=sum(r.orders for r in rows),
            reward=sum(r.reward for r in rows),
            source=f"{len(list(rows))}日分",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def parse_csv(path: Path) -> list[RevenueDay]:
        """楽天アフィリエイトのレポートCSVを読む。

        列名は変わりうるので、見出しを部分一致で拾う。日付列が見つからない
        CSVは扱えないので空を返す（**推測で埋めない**）。
        """
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(text.splitlines())
        if not reader.fieldnames:
            logger.warning("%s に見出し行がありません", path.name)
            return []

        def column(keys: tuple[str, ...]) -> str | None:
            for name in reader.fieldnames or []:
                if name and _match(name, keys):
                    return name
            return None

        date_col = column(_DATE_KEYS)
        if not date_col:
            logger.warning(
                "%s に日付の列が見つかりません（見出し: %s）",
                path.name, ", ".join(reader.fieldnames or []),
            )
            return []

        click_col = column(_CLICK_KEYS)
        order_col = column(_ORDER_KEYS)
        reward_col = column(_REWARD_KEYS)

        out: list[RevenueDay] = []
        for row in reader:
            day = _normalize_date(str(row.get(date_col) or ""))
            if not day:
                continue
            out.append(
                RevenueDay(
                    date=day,
                    clicks=_to_int(row.get(click_col)) if click_col else 0,
                    orders=_to_int(row.get(order_col)) if order_col else 0,
                    reward=_to_int(row.get(reward_col)) if reward_col else 0,
                    source=f"csv:{path.name}",
                )
            )
        return out


def _normalize_date(raw: str) -> str:
    """「2026/09/03」「2026-09-03」「20260903」を YYYY-MM-DD に均す。"""
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) != 8:
        return ""
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return ""
