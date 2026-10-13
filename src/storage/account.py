"""アカウント全体の指標を日ごとに残す。

## なぜ要るのか

`insights` はアカウント全体の表示・いいね・返信・**フォロワー数**を取得しているが、
画面に出すだけで捨てていた。そのため「フォロワーが増えているのか止まっているのか」を
**一度も確認できない**状態だった（2026-09-24 の見直しで判明）。

投稿ごとの成績（history.jsonl）は伸びの «結果» で、フォロワー数は積み上がる «資産» にあたる。
どちらが動いているかが分からないと、投稿を変えた効果も判断できない。

## 形

1日1行の JSONL。同じ日に何度実行しても最後の値で置き換える
（`insights` は毎日1回だが、手で動かすこともあるため）。

    {"date": "2026-09-24", "followers_count": 123, "views": 4560, "likes": 78, "replies": 9}

**2026-10-13 までの行（9/25〜10/13）は views / likes / replies が
«測った時点までの当日ぶん» になっている。** insights が窓を渡していなかったため、
何時までの値かが日によって違う（9/28 が 5294、翌日が 67 はこの差が大きい）。
`followers_count` は絶対値なので、その期間も正しい。
いまは前日1日ぶんを取るので、以降の行は日ごとに比べられる。
窓付きで取れなかった日は `"partial": true` が付く。

`data/` はワークフローがコミットバックするので、履歴として残る。
**個人を特定する情報は入らない**（自分のアカウントの合計値だけ）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 壊れた行は飛ばす。**読めない1行で全部を失わない**
        if isinstance(row, dict):
            out.append(row)
    return out


def record(path: Path, metrics: dict[str, int], *, when: datetime | None = None,
           day: str | None = None, partial: bool = False) -> dict[str, Any]:
    """その日の指標を1行残す。同じ日の行は置き換える。

    `day`（YYYY-MM-DD）を渡せばその日付で書く。**どの日の数字かは取得側が知っている**
    （`ThreadsInsights.for_account` は前日1日ぶんを取るので、今日の行に書くと1日ずれる）。

    `partial=True` の行は、その日の途中までの値。**日ごとの比較に使えない**ので
    印を付けて残す（捨てるとフォロワー数まで失う）。
    """
    label = day or (when or datetime.now(JST)).astimezone(JST).strftime("%Y-%m-%d")
    row: dict[str, Any] = {
        "date": label,
        **{k: int(v) for k, v in metrics.items() if isinstance(v, (int, float))},
    }
    if partial:
        row["partial"] = True

    rows = [r for r in _rows(path) if r.get("date") != label]
    rows.append(row)
    rows.sort(key=lambda r: str(r.get("date")))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return row


def growth(path: Path, *, days: int = 7) -> str:
    """直近の増減を一言で返す。表示するだけなので、無ければ空文字。"""
    rows = [r for r in _rows(path) if "followers_count" in r]
    if len(rows) < 2:
        return ""
    latest = rows[-1]
    base = rows[max(0, len(rows) - 1 - days)]
    delta = int(latest["followers_count"]) - int(base["followers_count"])
    span = f"{base['date']} → {latest['date']}"
    sign = "+" if delta >= 0 else ""
    return f"フォロワー {latest['followers_count']:,}人（{span} で {sign}{delta}）"
