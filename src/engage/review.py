"""返信文の検査と、返信した相手の記録。

## なぜ検査するのか

返信は他人の投稿に残る。こちらの投稿と違って、後から直せない。
出す前に機械で止められるものは止める。

いちばん重いのは **URL** で、楽天アフィリエイト規約はコメント欄への
アフィリエイトリンクを禁じている。ただのURLでも宣伝に見えるので入れない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..compliance.rules import scan

JST = timezone(timedelta(hours=9))

URL_PATTERN = re.compile(r"https?://|www\.|[a-z0-9-]+\.(com|jp|net|co|io)\b", re.I)

# 誰にでも使える返信。これだけで終わっているものは弾く。
TEMPLATE_ONLY = (
    "わかります", "分かります", "いいですね", "素敵です", "気になります",
    "参考になります", "ありがとうございます", "すごいです", "かわいい",
)

# 自分のアカウントへ誘導する言い方
SELF_PROMO = (
    "プロフ", "私のアカウント", "わたしのアカウント", "うちのアカウント",
    "投稿してます", "まとめてます", "フォローしてね", "見てください",
)

MAX_LENGTH = 120


@dataclass
class ReviewResult:
    ok: bool
    problems: list[str]

    def summary(self) -> str:
        return "OK" if self.ok else " / ".join(self.problems)


def review(text: str) -> ReviewResult:
    """返信文を検査する。"""
    problems: list[str] = []
    body = (text or "").strip()

    if not body:
        return ReviewResult(False, ["空です"])

    if URL_PATTERN.search(body):
        problems.append("URLが入っています（コメント欄へのリンクは禁止）")

    if len(body) > MAX_LENGTH:
        problems.append(f"長すぎます（{len(body)}字 > {MAX_LENGTH}）")

    # リンクの無い投稿なので薬機法だけが効く。効能を語らせない。
    hits = scan(body, has_link=False)
    if hits:
        problems.append(f"NG表現: {[h.label for h in hits]}")

    for word in SELF_PROMO:
        if word in body:
            problems.append(f"自分への誘導: {word}")
            break

    # テンプレ判定。定型句を抜いて、中身が残らなければ弾く。
    stripped = body
    for word in TEMPLATE_ONLY:
        stripped = stripped.replace(word, "")
    stripped = re.sub(r"[!！?？。、〜\s…]|[\U0001F300-\U0001FAFF]", "", stripped)
    if len(stripped) < 10:
        problems.append("定型句だけで、投稿に触れていません")

    return ReviewResult(not problems, problems)


# ======================================================================
# 記録
# ======================================================================
@dataclass
class Engagement:
    username: str
    shortcode: str
    replied_at: str
    text: str


class EngagementLog:
    """返信した相手の記録。同じ人に張り付かないために使う。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Engagement]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            out.append(Engagement(d["username"], d["shortcode"],
                                  d["replied_at"], d.get("text", "")))
        return out

    def append(self, username: str, shortcode: str, text: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "username": username,
            "shortcode": shortcode,
            "replied_at": datetime.now(JST).isoformat(timespec="seconds"),
            # 本文にURLが混ざっていても記録に残さない。data/ は公開される。
            "text": URL_PATTERN.sub("[url]", text)[:200],
        }
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    def replied_today(self) -> int:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        return sum(1 for e in self.all() if e.replied_at.startswith(today))

    def recent_usernames(self, *, days: int = 7) -> set[str]:
        """最近返信した相手。ここに入っている人には返さない。"""
        cutoff = datetime.now(JST) - timedelta(days=days)
        out = set()
        for e in self.all():
            try:
                when = datetime.fromisoformat(e.replied_at)
            except ValueError:
                continue
            if when >= cutoff:
                out.add(e.username)
        return out

    def replied_shortcodes(self) -> set[str]:
        return {e.shortcode for e in self.all()}
