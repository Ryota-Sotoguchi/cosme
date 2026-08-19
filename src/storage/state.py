"""運用状態 (data/state.json)。

保持するもの:
  * 運用開始日（ランプアップ判定に使う）
  * ローテーションカーソル（夜スロットの投稿タイプ順送り）
  * 直近に使ったテンプレート/文章パーツID（連続使用を避ける）
  * Threads トークンの発行/更新日時と期限（Secret本体は保存しない）

Secret は一切書き込まない。public リポジトリにコミットされる前提。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("state.json を読めませんでした（初期状態で続行）: %s", exc)
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # --- 運用開始日 ---------------------------------------------------
    def operation_start_date(self) -> date:
        """初回呼び出し時に今日の日付を記録し、以降はそれを返す。"""
        raw = self._data.get("operation_started_on")
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        today = datetime.now(JST).date()
        self._data["operation_started_on"] = today.isoformat()
        return today

    def days_since_start(self) -> int:
        """運用開始からの経過日数（開始当日を 1 日目とする）。"""
        return (datetime.now(JST).date() - self.operation_start_date()).days + 1

    # --- ローテーション -----------------------------------------------
    def next_rotation(self, slot: str, options: list[str]) -> str:
        """スロットのローテーションを1つ進めて返す。"""
        if not options:
            raise ValueError(f"スロット '{slot}' のローテーション候補が空です")
        cursors: dict[str, int] = self._data.setdefault("rotation_cursor", {})
        index = int(cursors.get(slot, 0)) % len(options)
        cursors[slot] = (index + 1) % len(options)
        return options[index]

    def peek_rotation(self, slot: str, options: list[str]) -> str:
        """カーソルを進めずに次の値を見る。"""
        if not options:
            raise ValueError(f"スロット '{slot}' のローテーション候補が空です")
        cursors: dict[str, int] = self._data.get("rotation_cursor", {})
        return options[int(cursors.get(slot, 0)) % len(options)]

    # --- 文章パーツの使用履歴 -------------------------------------------
    def recent_part_ids(self, group: str, limit: int = 6) -> list[str]:
        history: dict[str, list[str]] = self._data.get("part_history", {})
        return list(history.get(group, []))[-limit:]

    def record_part_ids(self, used: dict[str, str], keep: int = 12) -> None:
        """使用した文章パーツを記録する。連続使用回避に使う。"""
        history: dict[str, list[str]] = self._data.setdefault("part_history", {})
        for group, part_id in used.items():
            entries = history.setdefault(group, [])
            entries.append(part_id)
            del entries[:-keep]

    def recent_template_ids(self, limit: int = 8) -> list[str]:
        return list(self._data.get("template_history", []))[-limit:]

    def record_template_id(self, template_id: str, keep: int = 20) -> None:
        entries: list[str] = self._data.setdefault("template_history", [])
        entries.append(template_id)
        del entries[:-keep]

    # --- Threads トークン期限 -------------------------------------------
    def record_token_refresh(self, expires_in_seconds: int) -> datetime:
        """トークンの更新日時と期限を記録する。トークン本体は保存しない。"""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=int(expires_in_seconds))
        self._data["threads_token"] = {
            "refreshed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        return expires_at

    def token_expires_at(self) -> datetime | None:
        info = self._data.get("threads_token") or {}
        raw = info.get("expires_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def token_days_remaining(self) -> int | None:
        expires_at = self.token_expires_at()
        if expires_at is None:
            return None
        return (expires_at - datetime.now(timezone.utc)).days
