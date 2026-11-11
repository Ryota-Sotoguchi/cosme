"""転職の話をしているアカウントをフォローする。

## なぜ要るのか

2026-11-09 時点でフォロワーは9人。**投稿が届く先がほぼ無い。**
フォロワーがいないアカウントの投稿は、おすすめに拾われるかどうかが全部で、
実際そこが絞られた（10/22 以降、表示が1日で1/20になって戻っていない）。

フォローは、こちらから相手の画面に一度だけ出る数少ない手段。
自動返信と同じで、**相手は必ず人間が見る**。

**2026-11-09 に本人がルールを変えて解禁した**（それまで Claude はフォローを触らなかった）。
プロフィール（表示名・自己紹介）の変更は**いまも禁止**。

## 誰をフォローするか

`data/engage/engage.sqlite3` の `threads_seen_posts` から引く。
ここには**転職の語で検索して見つけた投稿**が溜まっていて、
2026-11-09 時点で検索由来のアカウントが1,228。発信ジャンルが合っていることは
収集の時点で担保されている（`[autoreply.filter]` を通っている）。

* `source='search'` のものだけ。タイムライン由来はコスメ時代のフォロー先が混ざる
* ルールで落ちた投稿（`skipped_rule`）の相手は見ない。センシティブ・宣伝が含まれる
* **検索に2回以上出てきた相手だけ**（`[follow] min_posts`）。下の «一発屋を取らない»
* **一度フォローしたら二度と触らない。** 外す操作もしない
  （フォローと解除を繰り返すのは、プラットフォームから見ていちばん悪い形）

## 一発屋を取らない（2026-11-11 に直した）

**いいねの多い順に並べると、このジャンルの本職のアカウントが構造的に落ちる。**
最初の15件を実測したら、4〜6件が発信ジャンルの外だった
（「ちいかわ × ANA」♥932／「議員ボーナスカットしてくれ」♥332／
「大学受験 うちの子は第一志望に落ち」♥306）。

原因は語ではなく**順位付け**。転職の語を含むいいね900の投稿は、
ほぼ必ず一般向けのバズ投稿で、転職の話をしている人ではない。
**語を増やしても直らない**ことも確かめた（「議員ボーナス」は `ボーナス` で通り、
逆に転職の投稿が0語で落ちる）。

**«検索に何回出てきたか» で並べ替えた。** 実測では上位18件が18件とも
転職アカウントになり（`minami.tensyoku` / `tenshoku_caritaro` / `sho_career_up` …）、
**そのいいねは3〜7**しかなかった。繰り返し出てくる＝そのジャンルの話を
続けている人、という判定で、`trends` の監視対象（«何度も伸びている
アカウントだけ。一発屋は入れない»）と同じ考え方。

* いいねは**足切りとしてだけ**使う（`min_likes`）。並べ替えには使わない
* 2回以上で77人・3回以上で20人（2026-11-11 実測）。検索は1日9回回るので候補は増える
* **候補が尽きたら何もしない。** 取りこぼすほうがましなので

## やりすぎない

**自動フォローは «業者» の典型的な signal。** この口座は原因不明の表示低下の最中で、
ここで数を出すと、戻る見込みを自分で潰すことになる。

* 1日の上限は小さく（`[follow] max_per_day`、既定8）
* 1件ずつ60〜180秒あける
* 絞られている最中は動かない（`autoreply_throttled_until` を共有して見る）
* キルスイッチは自動返信と同じ（`data/engage/STOP` / `AUTOREPLY_OFF`）
* 既定は Dry Run。実行するには `--live` が要る
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

# 1件ずつあける秒数。まとめて押さない
INTERVAL_SECONDS = (60, 180)

# フォロー済みの記録（data/engage/ は gitignore 済み）
FOLLOWED_LOG = "followed.jsonl"

# ボタンの文言。押せるのは «まだフォローしていない» ときだけ
FOLLOW_LABEL = "フォローする"
ALREADY_LABELS = ("フォロー中", "リクエスト済み", "フォローバック")

# **プロフィール本体のボタンだけを押す。**
# サイドバーの «おすすめのアカウント» にも同じ文言が並ぶので（実測で10個）、
# 文言だけで引くと見てもいない人をフォローする。
# 本体のボタンは「メッセージ」と横並びにあるので、それを目印にする。
FIND_FOLLOW_JS = """
([followLabel, alreadyLabels]) => {
  const buttons = Array.from(document.querySelectorAll('[role="button"]'));
  const text = (el) => (el.innerText || '').trim();
  const anchor = buttons.find(b => text(b) === 'メッセージ');
  if (!anchor) return {state: 'no_anchor'};
  const base = anchor.getBoundingClientRect();
  const sameRow = buttons.filter(b => {
    const r = b.getBoundingClientRect();
    return Math.abs(r.y - base.y) < 8 && r.width > 150 && b !== anchor;
  });
  for (const b of sameRow) {
    const t = text(b);
    if (alreadyLabels.includes(t)) return {state: 'already', label: t};
    if (t === followLabel) {
      b.setAttribute('data-cosme-follow', '1');
      return {state: 'found', label: t};
    }
  }
  return {state: 'not_found', labels: sameRow.map(text)};
}
"""


@dataclass(frozen=True)
class Candidate:
    username: str
    posts: int
    best_likes: int
    last_seen: str


def _log_path(data_dir: Path) -> Path:
    return data_dir / "engage" / FOLLOWED_LOG


def already_handled(data_dir: Path) -> set[str]:
    """一度でも触ったアカウント。**二度と触らない。**"""
    path = _log_path(data_dir)
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.add(str(json.loads(line).get("username") or ""))
        except json.JSONDecodeError:
            continue   # 壊れた1行で全部を失わない
    out.discard("")
    return out


def record(data_dir: Path, username: str, outcome: str) -> None:
    path = _log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"username": username, "outcome": outcome,
           "at": datetime.now(_JST).isoformat(timespec="seconds")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _followed_rows(data_dir: Path) -> list[dict[str, Any]]:
    path = _log_path(data_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue   # 壊れた1行で全部を失わない
        if row.get("outcome") == "followed":
            rows.append(row)
    return rows


def followed_today(data_dir: Path, *, now: datetime | None = None) -> int:
    """今日フォローした数。**押せなかったぶんは数えない。**"""
    today = (now or datetime.now(_JST)).strftime("%Y-%m-%d")
    return sum(1 for row in _followed_rows(data_dir)
               if str(row.get("at", "")).startswith(today))


def followed_total(data_dir: Path) -> int:
    """これまでにフォローできた数。**`[follow] max_total` の判定はここ。**

    2026-11-10 に本人が「一旦100人までゆっくり増やす」と決めた。
    日の上限だけだと止まらないので、**通算でも頭打ちにする**。
    100に達したら、表示とフォロワーの動きを見てから次を決める。
    """
    return len(_followed_rows(data_dir))


def candidates(db_path: Path, *, limit: int = 20, exclude: set[str] | None = None,
               min_likes: int = 0, min_posts: int = 1) -> list[Candidate]:
    """検索で見つけた、転職の話をしているアカウント。

    **«検索に何回出てきたか» で並べる。** 繰り返し出てくる相手は、
    そのジャンルの話を続けている人。いいねの多い順に並べると、転職の語を
    含むだけの一般向けバズ投稿が上に来て、本職のアカウントが落ちる
    （2026-11-11 の実測。モジュールの «一発屋を取らない» を参照）。

    いいねは足切りとしてだけ使う（`min_likes`）。
    """
    if not db_path.exists():
        return []
    exclude = exclude or set()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT username,
                   COUNT(*)              AS posts,
                   MAX(COALESCE(likes, 0)) AS best_likes,
                   MAX(first_seen_at)    AS last_seen
            FROM threads_seen_posts
            WHERE source = 'search'
              AND decision != 'skipped_rule'
              AND username IS NOT NULL AND username != ''
            GROUP BY username
            HAVING best_likes >= ? AND posts >= ?
            ORDER BY posts DESC, best_likes DESC, last_seen DESC
            """,
            (min_likes, max(min_posts, 1)),
        ).fetchall()
    finally:
        con.close()

    out: list[Candidate] = []
    for row in rows:
        name = str(row["username"])
        if name in exclude:
            continue
        out.append(Candidate(username=name, posts=int(row["posts"]),
                             best_likes=int(row["best_likes"]),
                             last_seen=str(row["last_seen"] or "")))
        if len(out) >= limit:
            break
    return out


def follow_one(session: Any, username: str) -> str:
    """1人フォローする。戻り値は followed / already / not_found / no_anchor。

    **押せる確信があるときだけ押す。** 見分けがつかないときは何もしない
    （知らない人を勝手にフォローするほうが、取りこぼすより悪い）。
    """
    page = session.goto(f"https://www.threads.com/@{username}")
    page.wait_for_timeout(5000)

    found = page.evaluate(FIND_FOLLOW_JS, [FOLLOW_LABEL, list(ALREADY_LABELS)])
    state = str(found.get("state"))
    if state != "found":
        logger.info("フォローしません（@%s）: %s %s", username, state, found.get("labels") or "")
        return state

    page.click('[data-cosme-follow="1"]')
    page.wait_for_timeout(3000)

    # **押したあと、本当に変わったかを見る。** 変わっていなければ «できた» と記録しない
    after = page.evaluate(FIND_FOLLOW_JS, [FOLLOW_LABEL, list(ALREADY_LABELS)])
    if str(after.get("state")) == "already":
        return "followed"
    logger.info("フォローを確認できません（@%s）: %s", username, after.get("state"))
    return "unconfirmed"


def run(session: Any, *, db_path: Path, data_dir: Path, limit: int,
        dry_run: bool = True, min_likes: int = 0, min_posts: int = 1,
        sleep: Any = time.sleep) -> list[dict[str, Any]]:
    """上限まで順にフォローする。dry_run のときは押さない。"""
    handled = already_handled(data_dir)
    picked = candidates(db_path, limit=limit, exclude=handled,
                        min_likes=min_likes, min_posts=min_posts)
    done: list[dict[str, Any]] = []

    for index, candidate in enumerate(picked):
        entry = {"username": candidate.username, "best_likes": candidate.best_likes,
                 "posts": candidate.posts, "outcome": "（下書き）"}
        if dry_run:
            done.append(entry)
            continue

        if index:
            wait = random.randint(*INTERVAL_SECONDS)
            logger.info("… %d秒あけます", wait)
            sleep(wait)
        try:
            outcome = follow_one(session, candidate.username)
        except Exception as exc:  # noqa: BLE001 — 1人で全部を止めない
            logger.warning("フォローできませんでした（@%s）: %s", candidate.username, exc)
            outcome = "error"
        entry["outcome"] = outcome
        # **触ったことは、できなくても記録する。** 同じ相手を何度も開かない
        record(data_dir, candidate.username, outcome)
        done.append(entry)

    return done
