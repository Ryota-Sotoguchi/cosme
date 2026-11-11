"""おすすめ（ホームタイムライン）に流れてくる投稿を分析する。

## なぜ要るのか

2026-11-11 の本人の指示: 「おすすめに乗ってる投稿を常に分析してほしい。
特に伸びているものは監視対象にしてもいいし、そこから取り入れるのはありかと」。

**集める仕組みは前からあった**（自動返信の `sources/timeline.py` がホームを開く。
Threads のホームは既定がおすすめ）。無かったのは分析のほうで、
おすすめに **いいね29,000** の投稿が流れてきていたのに、どこにも集計されていなかった。

## 取り入れていいのは «形» だけ

* ○ **形**: 長さ・1行目の字数・改行の数・箇条書き・問いかけ・絵文字・鍵括弧・数字
* × **中身**: 本文・言い回し・体験談。他人の投稿を写すのは CLAUDE.md §4 の
  «誰の感想かを偽らない» に触れる。参考にするのは «どう置くか» まで
* × **他人の本文を公開される場所に出さない。** `data/engage/` は gitignore 済みで、
  `research/`（コミットされる）には書かない。表示も端末に出すだけ

## なぜ別のテーブルなのか

`threads_seen_posts` に入れると、**自動返信が «もう見た» として候補から外す**
（`seen_shortcodes`）。分析のために深くスクロールした結果で返信先を潰すことになる。
分析用は `threads_trend_posts` に分け、返信の経路には触らせない。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

WATCHLIST = "watchlist.jsonl"

TREND_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads_trend_posts (
  shortcode     TEXT PRIMARY KEY,
  username      TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  likes         INTEGER NOT NULL DEFAULT 0,
  replies       INTEGER NOT NULL DEFAULT 0,
  age_hours     REAL,
  text_len      INTEGER NOT NULL DEFAULT 0,
  text_head     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trend_likes ON threads_trend_posts(likes);
CREATE INDEX IF NOT EXISTS idx_trend_seen ON threads_trend_posts(first_seen_at);
"""

# 絵文字（記号・絵文字・補助記号の面）。顔文字や「！」は数えない
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF⬀-⯿]"
)
_DIGIT = re.compile(r"[0-9０-９一二三四五六七八九十百千万]")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    with con:
        con.executescript(TREND_SCHEMA)
    return con


# ======================================================================
# 集める
# ======================================================================
def record(con: sqlite3.Connection, candidates: Iterable[Any]) -> int:
    """見た投稿を入れる。**いいねは上書きする**（伸びを後から見るため）。"""
    now = datetime.now(_JST).isoformat(timespec="seconds")
    rows = []
    for c in candidates:
        text = str(getattr(c, "text", "") or "")
        shortcode = str(getattr(c, "shortcode", "") or "")
        if not shortcode:
            continue
        rows.append((
            shortcode,
            str(getattr(c, "username", "") or ""),
            now, now,
            int(getattr(c, "likes", 0) or 0),
            int(getattr(c, "replies", 0) or 0),
            getattr(c, "age_hours", None),
            len(text),
            text[:400],
        ))
    if not rows:
        return 0
    with con:
        con.executemany(
            """
            INSERT INTO threads_trend_posts
              (shortcode, username, first_seen_at, last_seen_at,
               likes, replies, age_hours, text_len, text_head)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shortcode) DO UPDATE SET
              last_seen_at = excluded.last_seen_at,
              likes        = MAX(excluded.likes, threads_trend_posts.likes),
              replies      = MAX(excluded.replies, threads_trend_posts.replies)
            """,
            rows,
        )
    return len(rows)


def seed_from_seen(con: sqlite3.Connection) -> int:
    """自動返信が貯めていたホームTLの行を取り込む（空のときの1回だけ）。

    **本文は200字で切れている**ので `text_len = 0`（不明）で入れる。
    字数の比較には使わない（`shape_of` が0を混ぜない）。
    """
    try:
        rows = con.execute(
            """
            SELECT shortcode, username, first_seen_at, last_seen_at,
                   likes, replies, age_hours, text_head
            FROM threads_seen_posts WHERE source = 'timeline'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return 0   # 自動返信を一度も動かしていない環境
    if not rows:
        return 0
    with con:
        con.executemany(
            """
            INSERT OR IGNORE INTO threads_trend_posts
              (shortcode, username, first_seen_at, last_seen_at,
               likes, replies, age_hours, text_len, text_head)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [tuple(r) for r in rows],
        )
    return len(rows)


def prune(con: sqlite3.Connection, *, keep_days: int = 120,
          keep_likes: int = 500) -> int:
    """古い行を落とす。**よく伸びた投稿は残す**（手本として後から見たい）。"""
    limit = (datetime.now(_JST) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with con:
        cur = con.execute(
            "DELETE FROM threads_trend_posts WHERE first_seen_at < ? AND likes < ?",
            (limit, keep_likes),
        )
    return cur.rowcount or 0


# ======================================================================
# 形（取り入れていいのはここだけ）
# ======================================================================
# **1行目はトピックタグのことがある。**
# 2026-11-11 の実測で、伸びている投稿の1行目が7字だった。中を見ると
# 「仕事」「お弁当」「Travel Threads」「Cats of Threads」で、本文ではなく
# Threads が投稿の上に出すタグだった（extract が本文の1行目として拾う）。
# これを «1行目を短くしろ» と読むと間違える。タグとして分けて数える。
_TAG_MAX = 14


def split_tag(text: str) -> tuple[str, str]:
    """先頭のトピックタグ行を本文から切り離す。（タグ, 本文）を返す。

    タグと見なすのは、**短くて句点も疑問符も無い1行目**。
    判定できないときはタグ無しとして本文をそのまま返す（取りこぼす側に倒す）。
    """
    head, _, rest = (text or "").partition("\n")
    head = head.strip()
    if not rest.strip() or not head or len(head) > _TAG_MAX:
        return "", text or ""
    if any(ch in head for ch in "。？！?!、…"):
        return "", text or ""
    return head, rest.lstrip("\n")


@dataclass(frozen=True)
class Shape:
    """投稿の «形» を数えたもの。**中身は入らない。**"""

    n: int
    chars: float          # 字数の中央値
    first_line: float     # 1行目の字数の中央値（タグ行を除いた本文の1行目）
    breaks: float         # 空行の数の中央値
    bullets: float        # 箇条書きがある割合
    question: float       # 問いかけ（？）がある割合
    emoji: float          # 絵文字がある割合
    numbers: float        # 数字がある割合
    quotes: float         # 「」がある割合
    tagged: float         # トピックタグが付いている割合

    def row(self, label: str) -> str:
        chars = f"{self.chars:.0f}" if self.chars else "—"
        return (f"  {label:<16}{self.n:>5}{chars:>8}"
                f"{self.first_line:>8.0f}{self.breaks:>7.1f}"
                f"{self.bullets:>8.0%}{self.question:>8.0%}"
                f"{self.emoji:>8.0%}{self.numbers:>8.0%}{self.quotes:>8.0%}"
                f"{self.tagged:>8.0%}")

    @staticmethod
    def header() -> str:
        return (f"  {'':<16}{'件数':>4}{'字数':>7}{'1行目':>7}{'空行':>6}"
                f"{'箇条書':>7}{'問い':>7}{'絵文字':>7}{'数字':>7}{'「」':>7}"
                f"{'タグ':>7}")


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _share(flags: Sequence[bool]) -> float:
    return (sum(1 for f in flags if f) / len(flags)) if flags else 0.0


def shape_of(texts: Sequence[str], *, lengths: Sequence[int] | None = None,
             tags: Sequence[str] | None = None) -> Shape:
    """本文の並びから «形» を出す。

    `lengths` を渡すと字数はそちらを使う（保存した本文は頭だけなので、
    真の字数は別に持っている）。

    `tags` を渡すとタグの有無はそちらを使う。**自分の投稿のタグは本文に入らない**
    （API の `topic_tag` に渡すメタ情報）。本文から探すと «自分はタグ0%» という
    嘘が出る（2026-11-11 に実際に出た）。
    """
    texts = [t for t in texts if (t or "").strip()]
    if not texts:
        return Shape(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    # タグ行を分けてから形を見る（分けないと «1行目が短い» の嘘が出る）
    split = [split_tag(t) for t in texts]
    detected = [tag for tag, _ in split]
    bodies = [body for _, body in split]
    # **字数が分からない行は混ぜない。** 取り込んだ古い行は本文が200字で
    # 切れていて、そのまま数えると «みんな短い» という嘘になる
    known = [int(v) for v in (lengths or []) if int(v or 0) > 0]
    chars = known if known else ([len(t) for t in texts] if not lengths else [])
    return Shape(
        n=len(texts),
        chars=_median(chars),
        first_line=_median([len(b.split("\n", 1)[0]) for b in bodies]),
        breaks=_median([b.count("\n\n") for b in bodies]),
        bullets=_share([bool(re.search(r"(^|\n)[・\-−‐•]", b)) for b in bodies]),
        question=_share(["？" in b or "?" in b for b in bodies]),
        emoji=_share([bool(_EMOJI.search(b)) for b in bodies]),
        numbers=_share([bool(_DIGIT.search(b)) for b in bodies]),
        quotes=_share(["「" in b for b in bodies]),
        tagged=_share([bool(t) for t in (tags if tags is not None else detected)]),
    )


# ======================================================================
# 読む
# ======================================================================
def _since(days: int) -> str:
    return (datetime.now(_JST) - timedelta(days=days)).isoformat(timespec="seconds")


def top_posts(con: sqlite3.Connection, *, days: int = 14,
              limit: int = 15) -> list[sqlite3.Row]:
    """いいねの多い順。**伸びているもの。**"""
    return con.execute(
        """
        SELECT * FROM threads_trend_posts
        WHERE first_seen_at >= ?
        ORDER BY likes DESC, replies DESC
        LIMIT ?
        """,
        (_since(days), limit),
    ).fetchall()


def rows_since(con: sqlite3.Connection, *, days: int = 14) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM threads_trend_posts WHERE first_seen_at >= ?",
        (_since(days),),
    ).fetchall()


def genre_top(con: sqlite3.Connection, *, days: int = 14,
              limit: int = 10) -> list[sqlite3.Row]:
    """**発信ジャンルの中で伸びている投稿。**

    おすすめ（ホーム）は興味とフォロー先で決まるので、うちの場合は
    お弁当・猫・アイドルで埋まる（2026-11-11 の実測で上位15のうち
    仕事の話は1本だけ）。**転職の語で検索して見つけた投稿**のほうが、
    «このジャンルで何が伸びるか» の材料としては正しい。

    こちらは自動返信が貯めた `threads_seen_posts`（source='search'）から読む。
    本文は200字で切れているので、字数の比較には使わない。
    """
    try:
        return con.execute(
            """
            SELECT username, likes, replies, text_head, first_seen_at
            FROM threads_seen_posts
            WHERE source = 'search' AND first_seen_at >= ?
            ORDER BY likes DESC, replies DESC
            LIMIT ?
            """,
            (_since(days), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def risers(con: sqlite3.Connection, *, days: int = 30, min_likes: int = 300,
           min_posts: int = 2) -> list[dict[str, Any]]:
    """**何度も伸びているアカウント。** 一発だけのものは入れない。"""
    rows = con.execute(
        """
        SELECT username,
               COUNT(*) AS hits,
               MAX(likes) AS best,
               MAX(first_seen_at) AS last_seen
        FROM threads_trend_posts
        WHERE first_seen_at >= ? AND likes >= ? AND username != ''
        GROUP BY username
        HAVING hits >= ?
        ORDER BY best DESC
        """,
        (_since(days), min_likes, min_posts),
    ).fetchall()
    return [dict(r) for r in rows]


# ======================================================================
# 監視対象（watchlist）
# ======================================================================
def _watch_path(data_dir: Path) -> Path:
    return data_dir / "engage" / WATCHLIST


def load_watchlist(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = _watch_path(data_dir)
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue   # 壊れた1行で全部を失わない
        name = str(row.get("username") or "")
        if name:
            out[name] = row
    return out


def update_watchlist(data_dir: Path, found: Sequence[dict[str, Any]]) -> list[str]:
    """監視対象を足す。戻り値は**新しく入った名前**。

    **一覧は追記で持つ**（履歴が残る）。読むときは後の行が勝つ。
    """
    known = load_watchlist(data_dir)
    path = _watch_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    with path.open("a", encoding="utf-8") as fh:
        for row in found:
            name = str(row.get("username") or "")
            if not name:
                continue
            if name not in known:
                added.append(name)
            fh.write(json.dumps({
                "username": name,
                "hits": int(row.get("hits", 0)),
                "best": int(row.get("best", 0)),
                "last_seen": str(row.get("last_seen") or ""),
                "at": datetime.now(_JST).isoformat(timespec="seconds"),
            }, ensure_ascii=False) + "\n")
    return added


# ======================================================================
# 自分の投稿（比べる相手）
# ======================================================================
def own_posts(history_path: Path, *, days: int = 30) -> list[tuple[str, str]]:
    """自分の投稿の（本文, トピックタグ）。**連投は1本目だけ**（おすすめに出るのは1本目）。

    タグは `extra.topic_tag`。**本文には入らない**ので、ここで一緒に返す。
    2026-11-09 より前の行にはタグが記録されていない（空になる）。
    """
    if not history_path.exists():
        return []
    limit = (datetime.now(_JST) - timedelta(days=days)).isoformat(timespec="seconds")
    out: list[tuple[str, str]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "success":
            continue
        if str(row.get("posted_at") or "") < limit:
            continue
        text = str(row.get("text") or "")
        if text:
            tag = str((row.get("extra") or {}).get("topic_tag") or "")
            out.append((text.split("\n\n\n")[0], tag))
    return out


# ======================================================================
# 差を名指しする
# ======================================================================
# 見る項目と、差が «大きい» と言える幅
_FIELDS: tuple[tuple[str, str, float], ...] = (
    ("chars", "字数", 80),
    ("first_line", "1行目の字数", 15),
    ("breaks", "空行の数", 1.5),
    ("bullets", "箇条書き", 0.30),
    ("question", "問いかけ", 0.30),
    ("emoji", "絵文字", 0.30),
    ("numbers", "数字", 0.30),
    ("quotes", "「」", 0.30),
    ("tagged", "タグ", 0.30),
)


def differences(top: Shape, own: Shape) -> list[str]:
    """伸びている投稿と自分の形で、差が大きい項目を並べる。

    **直し方は書かない。** «取り入れるかどうか» は人が決める
    （標本が薄いうちに形を合わせにいって失敗した前例がある）。
    """
    if not top.n or not own.n:
        return []
    out: list[str] = []
    for key, label, width in _FIELDS:
        a = float(getattr(top, key))
        b = float(getattr(own, key))
        if abs(a - b) < width:
            continue
        fmt = (lambda v: f"{v:.0%}") if width <= 1 else (lambda v: f"{v:.0f}")
        direction = "多い" if a > b else "少ない"
        out.append(f"{label}: 伸びてる投稿 {fmt(a)} / 自分 {fmt(b)}（向こうのほうが{direction}）")
    return out
