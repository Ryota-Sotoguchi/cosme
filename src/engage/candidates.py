"""返信候補の解析と評価。

## 探し方

Threads の検索ページを描画して、投稿ごとに次を取る。

    /@sana_oniku/post/DTVoI4xlSTZ
    sana_oniku | 2026/01/11 | 本文… | 216 | 9 | 1 | 5
                                       ↑いいね ↑返信 ↑リポスト ↑シェア

公式APIのキーワード検索は **いいね数・返信数を返さない**ので、
「伸びている投稿」を判定するにはこの経路しかない。

## 何を上に置くか

  * 美容との関連度 … このアカウントの軸
  * 反応の勢い     … いいね＋返信。返信は重く見る
  * 新しさ         … 伸び切った投稿より伸び始めを狙う

返信が重いのは、コメント欄が動いている投稿のほうが自分の返信も
読まれるため。いいねだけ多くて無言の投稿に返しても、誰も見ない。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 返信をいいねの何倍に見るか。コメント欄が動いていることを重視する。
REPLY_WEIGHT = 8

# 美容・コスメの語。ここに当たるほど優先度が上がる。
BEAUTY_WORDS: tuple[str, ...] = (
    "コスメ", "メイク", "スキンケア", "美容", "ヘアケア", "香水", "美容液",
    "化粧水", "乳液", "クリーム", "下地", "ファンデ", "リップ", "口紅",
    "アイシャドウ", "マスカラ", "アイライナー", "チーク", "眉", "アイブロウ",
    "クレンジング", "洗顔", "日焼け止め", "パック", "シートマスク",
    "シャンプー", "トリートメント", "ヘアオイル", "髪", "肌",
    "プチプラ", "デパコス", "韓国コスメ", "新作", "購入品", "buy",
    "レビュー", "垢抜け", "自分磨き", "毛穴", "乾燥", "保湿", "くすみ",
    "ネイル", "まつげ", "まつ毛", "美容家電", "ドライヤー", "スチーマー",
    "ファッション", "コーデ", "美白", "トレンド", "ツヤ肌",
)

# 触らない投稿。荒れる・失礼になる・炎上する可能性があるもの。
SENSITIVE_WORDS: tuple[str, ...] = (
    "整形", "美容外科", "ダウンタイム", "施術", "注入", "ヒアルロン酸注射",
    "痩せ", "ダイエット", "体重", "拒食", "過食",
    "うつ", "メンタル", "病気", "手術", "入院", "アトピー", "アレルギー",
    "訃報", "他界", "亡く", "事故", "災害", "地震",
    "政治", "選挙", "宗教", "炎上", "誹謗", "訴訟", "晒し",
    "妊娠", "出産", "不妊", "生理",
    "副業", "稼げ", "投資", "FX", "仮想通貨", "情報商材",
)

# 宣伝アカウントの投稿。返しても意味がない。
SPAM_WORDS: tuple[str, ...] = (
    "DM下さい", "DMください", "プロフから", "公式LINE", "LINE@",
    "相互フォロー", "フォロバ", "拡散希望", "PR案件募集", "案件受付",
)

_NUM = re.compile(r"^([\d,]+(?:\.\d+)?)(万|k|K)?$")
_PERMALINK = re.compile(r"^/@([A-Za-z0-9_.]+)/post/([A-Za-z0-9_-]+)")
# 「4日」「2時間」「2026/08/14」
_RELATIVE = re.compile(r"^(\d+)\s*(分|時間|日|週)$")
_ABSOLUTE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


def _to_int(token: str) -> int | None:
    """「3.4万」「1,691」を数値にする。"""
    m = _NUM.match(token.strip())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit == "万":
        value *= 10_000
    elif unit in ("k", "K"):
        value *= 1_000
    return int(value)


def _age_hours(token: str, *, now: datetime) -> float | None:
    """投稿からの経過時間。分からなければ None。"""
    m = _RELATIVE.match(token.strip())
    if m:
        n = int(m.group(1))
        return {"分": n / 60, "時間": float(n), "日": n * 24.0, "週": n * 168.0}[m.group(2)]
    m = _ABSOLUTE.match(token.strip())
    if m:
        try:
            posted = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=JST)
        except ValueError:
            return None
        return max((now - posted).total_seconds() / 3600, 0.0)
    return None


@dataclass
class Candidate:
    """返信するかもしれない投稿ひとつ。"""

    username: str
    shortcode: str
    text: str
    likes: int = 0
    replies: int = 0
    age_hours: float | None = None
    keyword: str = ""
    # 公式検索で引けた数値ID。API から返信するのに要る。
    # スクレイプだけでは取れない（短縮IDとは別のID空間）。
    post_id: str = ""
    # 評価の内訳。なぜ選ばれたかを人が読めるように残す。
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def is_postable(self) -> bool:
        """API から返信できるか。post_id が無ければ手で返すしかない。"""
        return bool(self.post_id)

    @property
    def permalink(self) -> str:
        return f"https://www.threads.com/@{self.username}/post/{self.shortcode}"

    @property
    def matched_beauty_words(self) -> list[str]:
        return [w for w in BEAUTY_WORDS if w in self.text]

    @property
    def is_beauty(self) -> bool:
        return bool(self.matched_beauty_words)

    @property
    def sensitive_hits(self) -> list[str]:
        return [w for w in SENSITIVE_WORDS if w in self.text]

    @property
    def spam_hits(self) -> list[str]:
        return [w for w in SPAM_WORDS if w in self.text]

    @property
    def is_repliable(self) -> bool:
        """返していい投稿か。

        本文が短すぎるものは、何に触れて返せばいいか分からないので外す。
        """
        if self.sensitive_hits or self.spam_hits:
            return False
        if len(self.text.strip()) < 20:
            return False
        return True


def parse_search_block(href: str, block: str, *, keyword: str = "",
                       now: datetime | None = None) -> Candidate | None:
    """検索結果1件ぶんの塊を Candidate にする。

    block は投稿要素の innerText。

        sana_oniku
        2026/01/11
        まつげ下がる人向けの…
        216
        9
        1
        5

    末尾に並ぶ数字が反応数（いいね・返信・リポスト・シェア）。
    数がいくつ並ぶかは投稿によって変わるので、後ろから拾う。
    """
    m = _PERMALINK.match(href or "")
    if not m:
        return None
    username, shortcode = m.group(1), m.group(2)
    now = now or datetime.now(JST)

    lines = [ln.strip() for ln in (block or "").split("\n") if ln.strip()]
    if not lines:
        return None

    # 先頭のユーザー名行を落とす
    if lines and lines[0] == username:
        lines = lines[1:]

    # 2行目までに時刻表記があれば拾う
    age = None
    for i in range(min(2, len(lines))):
        age = _age_hours(lines[i], now=now)
        if age is not None:
            lines = lines[:i] + lines[i + 1:]
            break

    # 末尾に続く数字を反応数として取る
    trailing: list[int] = []
    while lines:
        value = _to_int(lines[-1])
        if value is None:
            break
        trailing.insert(0, value)
        lines.pop()

    body = "\n".join(lines).strip()
    if not body:
        return None

    likes = trailing[0] if len(trailing) >= 1 else 0
    replies = trailing[1] if len(trailing) >= 2 else 0
    return Candidate(
        username=username, shortcode=shortcode, text=body,
        likes=likes, replies=replies, age_hours=age, keyword=keyword,
    )


def score(candidate: Candidate, *, beauty_weight: float = 3.0) -> float:
    """返す価値の点数。内訳を candidate.scores に残す。"""
    beauty = min(len(candidate.matched_beauty_words), 4) / 4.0

    # 反応の勢い。**返信をいいねより大きく重み付けする。**
    #
    # 自分の返信が読まれるのは、コメント欄を開く人がいる投稿に限る。
    # いいねが300でも返信ゼロの投稿は、誰もコメント欄を見ていないので
    # そこに返しても意味がない。返信40の投稿のほうが価値が高い。
    reaction = candidate.likes + candidate.replies * REPLY_WEIGHT
    # 経過時間で割る。伸び切った投稿より伸び始めを上に置く。
    hours = max(candidate.age_hours or 24.0, 1.0)
    momentum = reaction / hours
    # 上限で切ると、そこを超えた投稿の順序が消えてしまう
    # （いいね300と3万が同じ点になる）。対数で潰して順序を保つ。
    momentum_score = min(math.log1p(momentum) / math.log1p(300.0), 1.0)

    # 新しさ。返信が読まれるのは投稿から間もないうち。
    if candidate.age_hours is None:
        freshness = 0.3
    elif candidate.age_hours <= 6:
        freshness = 1.0
    elif candidate.age_hours <= 24:
        freshness = 0.7
    elif candidate.age_hours <= 72:
        freshness = 0.4
    else:
        freshness = 0.1

    candidate.scores = {
        "beauty": round(beauty, 3),
        "momentum": round(momentum_score, 3),
        "freshness": round(freshness, 3),
    }
    return beauty * beauty_weight + momentum_score * 2.0 + freshness * 1.5


def rank_candidates(
    candidates: list[Candidate],
    *,
    limit: int = 12,
    beauty_ratio: float = 0.8,
    exclude_usernames: set[str] | None = None,
    exclude_shortcodes: set[str] | None = None,
) -> list[Candidate]:
    """返す価値の高い順に並べ、美容とそれ以外の比率を保って返す。

    美容だけに固定すると露出先が狭くなるので、`beauty_ratio` の割合で
    美容以外も混ぜる。軸は崩さず、間口は狭めすぎない。

    同じ相手・同じ投稿を二度出さないよう、除外リストを受け取る。
    """
    exclude_usernames = exclude_usernames or set()
    exclude_shortcodes = exclude_shortcodes or set()

    usable = [
        c for c in candidates
        if c.is_repliable
        and c.username not in exclude_usernames
        and c.shortcode not in exclude_shortcodes
    ]
    # 同じ投稿が複数キーワードで拾えることがある
    seen: set[str] = set()
    deduped = []
    for c in usable:
        if c.shortcode in seen:
            continue
        seen.add(c.shortcode)
        deduped.append(c)

    deduped.sort(key=score, reverse=True)

    beauty = [c for c in deduped if c.is_beauty]
    other = [c for c in deduped if not c.is_beauty]

    want_beauty = round(limit * beauty_ratio)
    picked = beauty[:want_beauty]
    picked += other[: limit - len(picked)]
    # 美容以外が足りなければ美容で埋める（逆は埋めない。軸を守る）
    if len(picked) < limit:
        picked += [c for c in beauty[want_beauty:] if c not in picked][: limit - len(picked)]

    picked.sort(key=score, reverse=True)
    return picked


# ======================================================================
# 公式検索とスクレイプの突き合わせ
# ======================================================================
def merge_with_search(
    scraped: list[Candidate],
    hits: list["object"],
) -> list[Candidate]:
    """スクレイプした候補に、公式検索の post_id を付ける。

    ## なぜ突き合わせるのか

    それぞれ片方しか持っていない。

        公式検索    post_id（返信に必要）        反応数を返さない
        スクレイプ  いいね数・返信数（選別に必要） post_id が取れない

    permalink の短縮ID が両方に入っているので、そこで繋ぐ。

    **post_id が付かなかった候補は返信できない。** 落とさずに残すが、
    `is_postable` が False になる。人が手で返す分には permalink があれば足りる。
    """
    by_shortcode = {}
    for hit in hits:
        code = getattr(hit, "shortcode", "")
        if code:
            by_shortcode[code] = hit

    for candidate in scraped:
        hit = by_shortcode.get(candidate.shortcode)
        if hit is not None:
            candidate.post_id = getattr(hit, "post_id", "")
    return scraped


def from_search_hit(hit: "object", *, keyword: str = "") -> Candidate:
    """公式検索の結果だけから候補を作る。

    反応数が取れないので、スクレイプで補えなかったときの形。
    勢いでは選べないが、返信は可能。
    """
    return Candidate(
        username=getattr(hit, "username", ""),
        shortcode=getattr(hit, "shortcode", ""),
        text=getattr(hit, "text", ""),
        keyword=keyword,
        post_id=getattr(hit, "post_id", ""),
        # has_replies しか分からないので、最低限の目安を入れる
        replies=1 if getattr(hit, "has_replies", False) else 0,
    )
