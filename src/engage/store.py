"""自動返信の記録。sqlite（標準ライブラリ）。

## なぜ JSONL ではないのか

投稿履歴（`data/history.jsonl`）と運用状態（`data/state.json`）は
これまでどおり JSONL/JSON のまま。ここだけ sqlite にする理由は3つある。

1. **見た投稿の量が違う。** タイムラインをスクロールした投稿すべてが
   「もう見たか」の判定対象になる。1回数十〜数百件、毎日実行すれば年に数万行。
   JSONL は所属判定のたびに全読みが要る。

2. **古い行を消す必要がある。** 60日前に見た投稿は忘れてよい。JSONL の剪定は
   ファイル全書き換えで、途中で落ちるとデータを失う。既存データを壊さない
   という原則に反する操作を毎日やることになる。

3. **返信の成果は後から書き戻す。** 24時間後にいいね数・返信数を埋めるのは
   既存行の更新。**これが決め手。** 追記専用のログには向かない形。

外部DBではない。標準ライブラリなのでコストも依存も増えない。

## 公開しない

`data/engage/` は .gitignore 済み。第三者のユーザー名・投稿本文・返信本文が
入るので、`git add data/` するワークフローに拾わせない。

## 既存 EngagementLog との関係

あちらは残す。返信できたときは**両方**に書く。

    threads_replies（ここ）     判定スコアと成果を持つ。自動返信の正
    data/engagements.jsonl      手動返信との共有台帳

1日の上限と同じ相手へのクールダウンを、手で返した分と自動で返した分の
**両方**で数えるための意図的な二重書き。片方に寄せない。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

SCHEMA_VERSION = 1

# 候補をどう処理したか。あとで «なぜ返さなかったのか» を辿るために残す。
DECISION_OBSERVED = "observed"              # 見ただけ。判断はまだ
DECISION_SKIPPED_RULE = "skipped_rule"      # ルールベースで除外（センシティブ語・古い等）
DECISION_SKIPPED_JUDGE = "skipped_judge"    # LLM が返信対象外と判断
DECISION_NO_DRAFT = "no_draft"              # 返信案を作れなかった
DECISION_VERIFY_FAILED = "verify_failed"    # 投稿直前の再確認で不一致
DECISION_REPLIED = "replied"
DECISION_FAILED = "failed"

# 判断が確定したもの。二度と候補に戻さない。
#
# ここに入らない決定（observed / skipped_rule / no_draft）は**わざと**
# 再検討の対象にする。投稿は時間とともに伸びるので、
# 「いいねが足りない」で落ちた投稿が1時間後に条件を満たすことはよくある。
TERMINAL_DECISIONS: frozenset[str] = frozenset({
    DECISION_REPLIED,        # もう返信した
    DECISION_VERIFY_FAILED,  # 削除・編集された。同じ投稿を追いかけない
})

# **DECISION_SKIPPED_JUDGE は終了状態にしない。**
#
# 2026-09-06 実測: 同じ投稿を3回判定させたら 0.35 / 0.60 / 0.70 とばらついた。
# 1回の判定で永久に除外すると、返信できたはずの候補を取りこぼす。
# 投稿はどのみち max_age_hours で候補から外れるので、再判定の回数は
# 自然に頭打ちになる（LLM の呼び出しは max_llm_calls_per_run も縛る）。

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads_seen_posts (
  shortcode     TEXT PRIMARY KEY,
  username      TEXT NOT NULL,
  source        TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  likes         INTEGER NOT NULL DEFAULT 0,
  replies       INTEGER NOT NULL DEFAULT 0,
  age_hours     REAL,
  buzz_score    REAL,
  decision      TEXT NOT NULL DEFAULT '',
  reason        TEXT NOT NULL DEFAULT '',
  text_head     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_seen_last ON threads_seen_posts(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_seen_user ON threads_seen_posts(username);

CREATE TABLE IF NOT EXISTS threads_replies (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  shortcode           TEXT NOT NULL UNIQUE,
  username            TEXT NOT NULL,
  permalink           TEXT NOT NULL DEFAULT '',
  reply_text          TEXT NOT NULL,
  reply_shape         TEXT NOT NULL DEFAULT '',
  replied_at          TEXT NOT NULL,
  source              TEXT NOT NULL DEFAULT '',
  buzz_score          REAL,
  target_score        REAL,
  target_reason       TEXT NOT NULL DEFAULT '',
  reply_score         REAL,
  reply_reason        TEXT NOT NULL DEFAULT '',
  generation_attempts INTEGER NOT NULL DEFAULT 1,
  our_reply_url       TEXT NOT NULL DEFAULT '',
  confirmed           INTEGER NOT NULL DEFAULT 0,
  outcome_fetched_at  TEXT,
  outcome_likes       INTEGER,
  outcome_replies     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_replies_at ON threads_replies(replied_at);
"""


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SeenPost:
    shortcode: str
    username: str
    first_seen_at: str
    last_seen_at: str
    likes: int = 0
    replies: int = 0
    age_hours: float | None = None
    buzz_score: float | None = None
    decision: str = ""
    source: str = ""

    @property
    def last_seen(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.last_seen_at)
        except ValueError:
            return None


@dataclass(frozen=True)
class ReplyRow:
    id: int
    shortcode: str
    username: str
    permalink: str
    reply_text: str
    reply_shape: str
    replied_at: str
    source: str = ""
    our_reply_url: str = ""
    confirmed: bool = False
    # なぜ選ばれ、なぜ通ったか。--explain で人が読む。
    buzz_score: float | None = None
    target_score: float | None = None
    reply_score: float | None = None
    generation_attempts: int = 1
    outcome_likes: int | None = None
    outcome_replies: int | None = None


class EngageStore:
    """自動返信の記録。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EngageStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        """スキーマを作る／上げる。

        列を足すときは version を上げて ALTER TABLE をここに積む。
        **既存行を読めなくする変更はしない。**
        """
        with self._conn:
            self._conn.executescript(_SCHEMA)
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # ==================================================================
    # 見た投稿
    # ==================================================================
    def mark_seen(
        self,
        candidate: Any,
        *,
        decision: str,
        reason: str = "",
        buzz_score: float | None = None,
    ) -> None:
        """候補を見たことと、どう処理したかを記録する。

        初めて見た時刻は保たれる（`first_seen_at` は更新しない）。
        伸びの速度は前回観測との差分で出すので、ここが上書きされると
        速度が計算できなくなる。
        """
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO threads_seen_posts
                  (shortcode, username, source, first_seen_at, last_seen_at,
                   likes, replies, age_hours, buzz_score, decision, reason, text_head)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shortcode) DO UPDATE SET
                  last_seen_at = excluded.last_seen_at,
                  likes        = excluded.likes,
                  replies      = excluded.replies,
                  age_hours    = excluded.age_hours,
                  buzz_score   = excluded.buzz_score,
                  decision     = excluded.decision,
                  reason       = excluded.reason
                """,
                (
                    candidate.shortcode,
                    candidate.username,
                    getattr(candidate, "source", ""),
                    now,
                    now,
                    int(getattr(candidate, "likes", 0) or 0),
                    int(getattr(candidate, "replies", 0) or 0),
                    getattr(candidate, "age_hours", None),
                    buzz_score,
                    decision,
                    reason[:300],
                    (getattr(candidate, "text", "") or "")[:200],
                ),
            )

    def previous_sighting(self, shortcode: str) -> SeenPost | None:
        """前回この投稿を見たときの数字。伸びの速度の分母になる。"""
        row = self._conn.execute(
            "SELECT * FROM threads_seen_posts WHERE shortcode = ?", (shortcode,)
        ).fetchone()
        return self._to_seen(row) if row else None

    def seen_shortcodes(self, *, within_days: int) -> set[str]:
        cutoff = (datetime.now(JST) - timedelta(days=within_days)).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT shortcode FROM threads_seen_posts WHERE last_seen_at >= ?", (cutoff,)
        )
        return {row["shortcode"] for row in rows}

    def settled_shortcodes(self, *, within_days: int) -> set[str]:
        """判断が確定している投稿。二度と候補に戻さない。"""
        cutoff = (datetime.now(JST) - timedelta(days=within_days)).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(TERMINAL_DECISIONS))
        rows = self._conn.execute(
            f"SELECT shortcode FROM threads_seen_posts"
            f" WHERE last_seen_at >= ? AND decision IN ({placeholders})",
            (cutoff, *sorted(TERMINAL_DECISIONS)),
        )
        return {row["shortcode"] for row in rows}

    def filter_actionable(self, candidates: Iterable[Any], *, within_days: int) -> list[Any]:
        """まだ判断が確定していない候補だけ返す。

        **「見たことがある」だけでは外さない。**

        伸びの速度は前回観測との差分で出す。観測済みの投稿を候補から
        外してしまうと `previous_sighting()` が使われる場面が無くなり、
        velocity が常に 0 になる。つまり、このモジュールの中心である
        「どれくらいの速度で伸びているか」がまるごと効かなくなる。

        外すのは `TERMINAL_DECISIONS` に入ったものだけ。
        """
        settled = self.settled_shortcodes(within_days=within_days)
        return [c for c in candidates if c.shortcode not in settled]

    def prune(self, *, days: int) -> int:
        """古い観測を捨てる。返信の記録は消さない。"""
        cutoff = (datetime.now(JST) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM threads_seen_posts WHERE last_seen_at < ?", (cutoff,)
            )
        return cursor.rowcount

    @staticmethod
    def _to_seen(row: sqlite3.Row) -> SeenPost:
        return SeenPost(
            shortcode=row["shortcode"],
            username=row["username"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            likes=row["likes"],
            replies=row["replies"],
            age_hours=row["age_hours"],
            buzz_score=row["buzz_score"],
            decision=row["decision"],
            source=row["source"],
        )

    # ==================================================================
    # 返信
    # ==================================================================
    def record_reply(
        self,
        *,
        shortcode: str,
        username: str,
        reply_text: str,
        permalink: str = "",
        reply_shape: str = "",
        source: str = "",
        buzz_score: float | None = None,
        target_score: float | None = None,
        target_reason: str = "",
        reply_score: float | None = None,
        reply_reason: str = "",
        generation_attempts: int = 1,
        our_reply_url: str = "",
        confirmed: bool = False,
    ) -> int:
        """返信を記録する。**shortcode が UNIQUE なので二度目は無視される。**

        同じ投稿へ二度返さないことをアプリ側の判断だけに任せない。
        取りこぼしたら日次の枠を二重に食い、相手の投稿に2つ並ぶ。
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO threads_replies
                  (shortcode, username, permalink, reply_text, reply_shape, replied_at,
                   source, buzz_score, target_score, target_reason, reply_score,
                   reply_reason, generation_attempts, our_reply_url, confirmed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shortcode) DO NOTHING
                """,
                (
                    shortcode, username, permalink, reply_text, reply_shape, _now(),
                    source, buzz_score, target_score, target_reason[:300],
                    reply_score, reply_reason[:300], generation_attempts,
                    our_reply_url, int(confirmed),
                ),
            )
        if cursor.rowcount == 0:
            logger.warning("すでに返信済みの投稿です: %s", shortcode)
            row = self._conn.execute(
                "SELECT id FROM threads_replies WHERE shortcode = ?", (shortcode,)
            ).fetchone()
            return int(row["id"]) if row else 0
        return int(cursor.lastrowid or 0)

    def replied_shortcodes(self) -> set[str]:
        rows = self._conn.execute("SELECT shortcode FROM threads_replies")
        return {row["shortcode"] for row in rows}

    def recent_reply_texts(self, limit: int = 100) -> list[str]:
        """直近の返信本文。同じことを繰り返さないための比較材料。"""
        rows = self._conn.execute(
            "SELECT reply_text FROM threads_replies ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [row["reply_text"] for row in rows]

    def replied_since(self, *, hours: int) -> list[ReplyRow]:
        cutoff = (datetime.now(JST) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT * FROM threads_replies WHERE replied_at >= ? ORDER BY id DESC", (cutoff,)
        )
        return [self._to_reply(row) for row in rows]

    def recent_replies(self, limit: int = 20) -> list[ReplyRow]:
        rows = self._conn.execute(
            "SELECT * FROM threads_replies ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [self._to_reply(row) for row in rows]

    # ------------------------------------------------------------------
    # 成果（24時間後に埋める）
    # ------------------------------------------------------------------
    def pending_outcomes(self, *, older_than_hours: int = 24) -> list[ReplyRow]:
        """まだ成果を取っていない返信。"""
        cutoff = (
            datetime.now(JST) - timedelta(hours=older_than_hours)
        ).isoformat(timespec="seconds")
        rows = self._conn.execute(
            """
            SELECT * FROM threads_replies
            WHERE outcome_fetched_at IS NULL AND replied_at < ? AND our_reply_url != ''
            ORDER BY id
            """,
            (cutoff,),
        )
        return [self._to_reply(row) for row in rows]

    def update_outcome(self, reply_id: int, *, likes: int, replies: int) -> None:
        """返信がどれだけ読まれたかを書き戻す。

        反応の良かった返信を、あとで生成の手本に回すための土台。
        """
        with self._conn:
            self._conn.execute(
                """
                UPDATE threads_replies
                SET outcome_fetched_at = ?, outcome_likes = ?, outcome_replies = ?
                WHERE id = ?
                """,
                (_now(), likes, replies, reply_id),
            )

    def best_replies(self, limit: int = 5) -> list[ReplyRow]:
        """反応の良かった返信。生成の手本に使う。"""
        rows = self._conn.execute(
            """
            SELECT * FROM threads_replies
            WHERE outcome_fetched_at IS NOT NULL
            ORDER BY (COALESCE(outcome_likes, 0) + COALESCE(outcome_replies, 0) * 3) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._to_reply(row) for row in rows]

    @staticmethod
    def _to_reply(row: sqlite3.Row) -> ReplyRow:
        return ReplyRow(
            id=row["id"],
            shortcode=row["shortcode"],
            username=row["username"],
            permalink=row["permalink"],
            reply_text=row["reply_text"],
            reply_shape=row["reply_shape"],
            replied_at=row["replied_at"],
            source=row["source"],
            our_reply_url=row["our_reply_url"],
            confirmed=bool(row["confirmed"]),
            buzz_score=row["buzz_score"],
            target_score=row["target_score"],
            reply_score=row["reply_score"],
            generation_attempts=row["generation_attempts"],
            outcome_likes=row["outcome_likes"],
            outcome_replies=row["outcome_replies"],
        )
