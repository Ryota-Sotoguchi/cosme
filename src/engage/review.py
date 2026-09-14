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

from ..compliance.rules import EXPERIENCE_RULES, FAKE_REVIEW_RULES, scan

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

# **返信はすべて敬語（です・ます）で書く。**（2026-09-14）
#
# 投稿（src/content/）はやわらかい話し言葉のまま。返信は知らない人の
# コメント欄に書くので、タメ口は馴れ馴れしく見える。
# プロンプトにも書いてあるが LLM は守り切らないので、機械側でも止める。
#
# 文ごとに、文末が次の形で終わっているかを見る:
#     です / ます / でした / ました / ません / ましょう / でしょう / ください / ございます
#     + 終助詞（か・ね・よ・よね…）は付いてよい
_POLITE_END = re.compile(
    r"(です|ます|でした|ました|ません|ませんでした|ましょう|でしょう"
    r"|ください|下さい|ございます|ございました)"
    # 「ますかね」「ですもんね」「ありましたっけ」のように重なってよい
    r"(か|ね|よ|な|わ|っけ|もの|もん|けど|けれど|が|し|から|ので|のに|って)*$"
)
# 文末に付く飾り。これを落としてから判定する。
_TRAILING_NOISE = re.compile(
    r"[\s〜～ー…・。、，．！!？?（）()「」『』\"'wｗ笑"
    r"\U0001F300-\U0001FAFF☀-➿️‍]+$"
)
# 「…」も文の切れ目として扱う。「見てる…眠くなりますね」の前半のように、
# タメ口の節を「…」でつないで最後だけ敬語にする書き方を通さない。
_SENTENCE_BREAK = re.compile(r"[。！!？?\n…‥]+")


def impolite_fragment(text: str) -> str:
    """敬語で終わっていない文を1つ返す。全部敬語なら空文字。

    「わ！」「え？」のような2文字以下の感嘆は文として数えない。
    """
    for raw in _SENTENCE_BREAK.split(text or ""):
        sentence = _TRAILING_NOISE.sub("", raw.strip())
        if len(sentence) <= 2:
            continue
        if not _POLITE_END.search(sentence):
            return sentence
    return ""

# 「レビュー」「口コミ」の語を止める検査はここにあった（REVIEW_WORDS）。
#
# 2026-09-14 に発信ジャンルを転職・年収・キャリアへ変えたとき解禁した。
# 「会社の口コミサイト」は転職の定番の話題なので、語そのものは使ってよい。
# 架空の口コミ（「口コミでは○○という声が多い」）は FAKE_REVIEW_RULES が引き続き止める。


@dataclass
class ReviewResult:
    ok: bool
    problems: list[str]

    def summary(self) -> str:
        return "OK" if self.ok else " / ".join(self.problems)


def review(text: str, *, include_experience: bool = False) -> ReviewResult:
    """返信文を検査する。

    `include_experience=True` で、使用体験と口コミの創作も弾く。
    **機械が書いた文には必ず付けること。**

    既定が False なのは人が書く場合のため。`.claude/commands/reply.md` は
    「経験」型（同じ状況の話）を返信の型のひとつとして認めており、
    人は「使ってもいない商品の体験は書かない」を自分で判断できる。
    """
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
    if include_experience:
        # scan(has_link=False) は ALWAYS_RULES しか見ないので、
        # EXPERIENCE_RULES / FAKE_REVIEW_RULES が素通りする。
        # 景表法の観点ではリンクの無い返信に体験談を禁じる理由は無いが、
        # **このアカウントは商品を使っていない**（CLAUDE.md「架空の体験・
        # 口コミを書かない」）。人は文脈で線を引けるが、機械には引けない。
        hits = hits + [
            rule for rule in (*EXPERIENCE_RULES, *FAKE_REVIEW_RULES)
            if rule.pattern.search(body)
        ]
    if hits:
        problems.append(f"NG表現: {[h.label for h in hits]}")

    for word in SELF_PROMO:
        if word in body:
            problems.append(f"自分への誘導: {word}")
            break

    # 人が書く返信（/reply → engage --text）も機械が書く返信も同じ。
    # 返信の文体は1つに揃える。
    fragment = impolite_fragment(body)
    if fragment:
        problems.append(f"敬語（です・ます）で終わっていません: 「{fragment[-20:]}」")

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
def _parse(value: str) -> datetime | None:
    """記録の時刻を読む。壊れていれば None（recent_usernames と同じ扱い）。"""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


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

    def last_replied_at(self) -> datetime | None:
        """最後に返信した時刻。まだ無ければ None。

        **手動返信も含む。** Meta から見れば、手で返したのも機械が返したのも
        同じアカウントの返信なので、間隔の計算では区別しない。
        """
        latest: datetime | None = None
        for e in self.all():
            when = _parse(e.replied_at)
            if when is not None and (latest is None or when > latest):
                latest = when
        return latest

    def last_replied_at_by_username(self, *, days: int = 30) -> dict[str, datetime]:
        """相手ごとの最終返信時刻。同一アカウントのクールダウン判定に使う。

        recent_usernames() が「返したかどうか」しか返さないのに対し、
        こちらは「いつ返したか」を返す。収集元ごとにクールダウンの長さを
        変えたいので、集合ではなく時刻が要る。
        """
        cutoff = datetime.now(JST) - timedelta(days=days)
        out: dict[str, datetime] = {}
        for e in self.all():
            when = _parse(e.replied_at)
            if when is None or when < cutoff:
                continue
            if e.username not in out or when > out[e.username]:
                out[e.username] = when
        return out
