"""伸びている投稿の見分け方。

## いいねの絶対数では選ばない

「いいね1000超え」で切ると、伸び切った投稿ばかりが残る。そこに返信しても
コメント欄はもう動いていないので誰にも読まれない。見たいのは
**単位時間あたりどれだけ伸びているか**。

## 何を足すか

    reaction  反応の絶対数（いいね + 返信×8）  ← 2026-09-06 から主軸
    base      candidates.score() … 美容との関連度・勢い・新しさ
    velocity  前回観測からの実測の伸び
    reply_fit 返信数の逆U字（**いまは重み0**。下記）

`base` は既存のものをそのまま使う。あれは1回の観測だけで出せる推定値で、
初めて見た投稿にも点が付く。`velocity` は2回目以降にしか出せないが、
**推定ではなく実測**なので取れるときは重く見る。

## 逆U字をやめて、上限を足した

もとは `reply_fit` で「返信が多すぎる投稿」を減点していた。返信500件の
投稿では自分の返信が一番下に沈むから、という理屈自体は正しい。

ただし運用の指示が「反応数の多い投稿に返す」に変わったので、
**減点ではなく足切り**にした（`passes_filter` の `max_replies`）。
順位は反応数で決め、埋もれる帯は候補から外す。

`reply_fit()` は関数もテストも残してある。`reply_fit_weight` を上げれば
元の挙動に戻る。**消さないのは、戻す判断を設定1行で済ませるため。**

## 上限を「返信数」で見る理由

埋もれるかどうかは「自分より上に何件コメントがあるか」で決まる。
いいねでは埋もれない。`max_likes` は別の目的（コメント欄が濁流の投稿）。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .candidates import REPLY_WEIGHT, Candidate, rank_candidates
from .candidates import score as base_score

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ひらがな・カタカナ。**日本語の投稿かどうかの判定に使う。**
#
# 漢字だけだと中国語と区別が付かないが、かなは日本語にしか出ない。
# 2026-09-07: ベトナム語の写真投稿に日本語で返信してしまった。
# 相手にも読めないし、日本のコスメを見てほしい人には届かない。
_KANA = re.compile(r"[ぁ-んァ-ヴー]")

# 反応の勢いを 0-1 に潰すときの基準。既存 score() と同じ値を使い、
# base と velocity のスケールを揃える。
_MOMENTUM_SATURATION = 300.0

# base_score() が返しうる最大値。beauty*3 + momentum*2 + freshness*1.5
_BASE_MAX = 3.0 + 2.0 + 1.5

# 反応数を潰すときの既定の基準
_REACTION_SATURATION = 5000.0

# 表示上の最大。0〜10 に収める。
BUZZ_MAX = 10.0


@dataclass(frozen=True)
class BuzzScore:
    """なぜその点になったかを人が読めるように、内訳ごと持ち歩く。"""

    total: float
    base: float
    reaction: float
    velocity: float
    reply_fit: float
    parts: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        bits = " / ".join(f"{k}={v:.2f}" for k, v in self.parts.items())
        return f"{self.total:.2f} ({bits})"


def _compress(value: float, saturation: float = _MOMENTUM_SATURATION) -> float:
    """対数で 0-1 に潰す。

    上限で切ると、そこを超えた投稿の順序が消える（いいね300と3万が同じ点に
    なる）。対数なら順序を保ったまま頭を抑えられる。
    """
    if value <= 0:
        return 0.0
    return min(math.log1p(value) / math.log1p(saturation), 1.0)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def observed_velocity(
    candidate: Candidate,
    previous: Any | None,
    *,
    min_hours: float = 0.5,
    now: datetime | None = None,
) -> float:
    """前回観測からの実測の伸びを 0-1 で返す。

    前回が無ければ 0。**推測で埋めない。** 初見の投稿は base だけで判断する。
    """
    if previous is None:
        return 0.0

    last_seen = _parse_time(getattr(previous, "last_seen_at", "") or "")
    if last_seen is None:
        return 0.0

    now = now or datetime.now(JST)
    elapsed_hours = (now - last_seen).total_seconds() / 3600
    if elapsed_hours < min_hours:
        # 間隔が短すぎる。分母が小さいと少しの差で点が暴れる。
        return 0.0

    before = int(getattr(previous, "likes", 0) or 0) + int(
        getattr(previous, "replies", 0) or 0) * REPLY_WEIGHT
    after = (candidate.likes or 0) + (candidate.replies or 0) * REPLY_WEIGHT
    gained = after - before
    if gained <= 0:
        # 減ることもある（いいね取り消し・削除）。マイナスは 0 に丸める。
        return 0.0

    return _compress(gained / elapsed_hours)


def reaction(candidate: Candidate, *, saturation: float = _REACTION_SATURATION) -> float:
    """反応の絶対数を 0-1 で返す。

    返信をいいねの `REPLY_WEIGHT` 倍に見るのは既存 `score()` と同じ理屈で、
    コメント欄が動いている投稿のほうが自分の返信も読まれるため。

    対数で潰すのも既存と同じ。上限で切ると、そこを超えた投稿の順序が
    消えてしまう（いいね5千と5万が同じ点になる）。
    """
    total = (candidate.likes or 0) + (candidate.replies or 0) * REPLY_WEIGHT
    return _compress(total, saturation)


def reply_fit(replies: int, *, low: int = 3, high: int = 60) -> float:
    """返信数が「自分の返信が読まれる帯」に入っているか（0-1）。

    帯の中は 1.0。下に外れればコメント欄が動いていない。
    上に外れれば自分の返信が埋もれる。どちらも対数で緩やかに落とす。
    """
    replies = max(int(replies or 0), 0)
    if low <= replies <= high:
        return 1.0
    if replies < low:
        # 0件でも 0点にはしない。これから伸びる可能性はある。
        return 0.3 + 0.7 * (replies / low) if low > 0 else 1.0
    # 帯を超えた分だけ緩やかに落とす。high の10倍で 0.2 あたり。
    excess = math.log1p(replies - high) / math.log1p(high * 9 or 1)
    return max(1.0 - excess * 0.8, 0.15)


def buzz(
    candidate: Candidate,
    *,
    previous: Any | None = None,
    weights: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> BuzzScore:
    """返す価値の点数（0〜10）。"""
    opts = weights or {}
    w_base = float(opts.get("base_weight", 1.0))
    w_reaction = float(opts.get("reaction_weight", 3.0))
    w_velocity = float(opts.get("velocity_weight", 1.5))
    w_fit = float(opts.get("reply_fit_weight", 0.0))
    min_hours = float(opts.get("min_observation_hours", 0.5))

    # base_score() は candidate.scores に内訳を書き込む副作用を持つ
    raw_base = base_score(candidate)
    base = min(raw_base / _BASE_MAX, 1.0)
    reacted = reaction(candidate, saturation=float(
        opts.get("reaction_saturation", _REACTION_SATURATION)))
    velocity = observed_velocity(candidate, previous, min_hours=min_hours, now=now)
    fit = reply_fit(
        candidate.replies,
        low=int(opts.get("reply_band_low", 3)),
        high=int(opts.get("reply_band_high", 60)),
    )

    total_weight = w_base + w_reaction + w_velocity + w_fit
    if total_weight <= 0:
        total_weight = 1.0
    weighted = (base * w_base + reacted * w_reaction
                + velocity * w_velocity + fit * w_fit) / total_weight

    parts = dict(candidate.scores)
    parts.update({
        "base": round(base, 3),
        "reaction": round(reacted, 3),
        "velocity": round(velocity, 3),
        "reply_fit": round(fit, 3),
    })
    score = BuzzScore(
        total=round(weighted * BUZZ_MAX, 3),
        base=base,
        reaction=reacted,
        velocity=velocity,
        reply_fit=fit,
        parts=parts,
    )
    candidate.scores = parts
    return score


# ======================================================================
# ルールベースの足切り。**LLM を呼ぶ前に効かせる。**
# ======================================================================
def resolve_filter(section: dict[str, Any] | None, source: str) -> dict[str, Any]:
    """収集元ごとの足切り条件を解決する。

    入れ子の dict（[autoreply.filter.accounts]）を、その収集元のときだけ
    上に重ねる。Config.autoreply_filter() と同じ規則。
    """
    section = section or {}
    base = {k: v for k, v in section.items() if not isinstance(v, dict)}
    override = section.get(source)
    return {**base, **override} if isinstance(override, dict) else base


def passes_filter(
    candidate: Candidate,
    filt: dict[str, Any] | None = None,
    *,
    last_reply_at: dict[str, datetime] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """返す価値のある投稿か。(可否, 理由) を返す。

    確定的で無料な判定を先に置く。ここで落ちる投稿に LLM を使わない。

    同一アカウントのクールダウンもここで見る。**`rank_candidates()` の
    `exclude_usernames` ではなくここに置くのが重要**で、理由は2つ:

      1. `resolve_filter()` が収集元ごとに条件を解決するので、
         タイムラインは7日、監視対象は3時間、と分けられる
      2. `exclude_usernames` は `rejected` を組み立てる前に効くため、
         落ちた候補が記録にも画面にも残らなかった
    """
    opts = filt or {}

    if candidate.sensitive_hits:
        return False, f"センシティブな話題: {candidate.sensitive_hits[:3]}"
    if candidate.spam_hits:
        return False, f"宣伝アカウント: {candidate.spam_hits[:3]}"
    if candidate.is_reply:
        return False, "リプライへのリプライ"
    if not candidate.is_browser_repliable:
        return False, "返信先を特定できない（username / shortcode が無い）"

    body = (candidate.text or "").strip()
    min_length = int(opts.get("min_text_length", 30))
    if len(body) < min_length:
        return False, f"本文が短すぎる（{len(body)}字 < {min_length}）"

    if opts.get("require_japanese", True):
        kana = len(_KANA.findall(body))
        # 数文字の «かな» は絵文字混じりの他言語投稿にも紛れるので、
        # 割合で見る。日本語の文なら1割は超える。
        if kana < 3 or kana / max(len(body), 1) < 0.05:
            return False, "日本語の投稿ではない"

    max_age = opts.get("max_age_hours")
    if max_age is not None:
        if candidate.age_hours is None:
            return False, "投稿時刻が読めない"
        if candidate.age_hours > float(max_age):
            return False, f"古すぎる（{candidate.age_hours:.0f}時間 > {max_age}）"

    likes = candidate.likes or 0
    min_likes = int(opts.get("min_likes", 0))
    if likes < min_likes:
        return False, f"反応が少ない（いいね {likes} < {min_likes}）"
    max_likes = opts.get("max_likes")
    if max_likes is not None and likes > int(max_likes):
        return False, f"伸びすぎ（いいね {likes} > {max_likes}）コメント欄が濁流"

    # **埋もれるかどうかは返信数で決まる。** いいねでは埋もれない。
    replies = candidate.replies or 0
    max_replies = opts.get("max_replies")
    if max_replies is not None and replies > int(max_replies):
        return False, f"返信が多すぎる（返信 {replies} > {max_replies}）自分の返信が沈む"

    ok, reason = _within_cooldown(candidate, opts,
                                  last_reply_at=last_reply_at, now=now)
    if not ok:
        return False, reason

    return True, ""


def _within_cooldown(
    candidate: Candidate,
    opts: dict[str, Any],
    *,
    last_reply_at: dict[str, datetime] | None,
    now: datetime | None,
) -> tuple[bool, str]:
    """同じ相手に返したばかりでないか。"""
    if not last_reply_at:
        return True, ""
    previous = last_reply_at.get(candidate.username)
    if previous is None:
        return True, ""

    now = now or datetime.now(JST)
    elapsed = now - previous

    days = float(opts.get("same_account_cooldown_days", 0) or 0)
    if days > 0 and elapsed < timedelta(days=days):
        return False, (f"@{candidate.username} に返したばかり"
                       f"（{elapsed.days}日前 / 最短{days:.0f}日）")

    hours = float(opts.get("same_account_cooldown_hours", 0) or 0)
    if hours > 0 and elapsed < timedelta(hours=hours):
        return False, (f"@{candidate.username} に返したばかり"
                       f"（{elapsed.total_seconds() / 3600:.1f}時間前 / 最短{hours:.0f}時間）")

    return True, ""


def rank(
    candidates: Iterable[Candidate],
    *,
    store: Any | None = None,
    filt: dict[str, Any] | None = None,
    weights: dict[str, Any] | None = None,
    exclude_usernames: set[str] | None = None,
    exclude_shortcodes: set[str] | None = None,
    last_reply_at: dict[str, datetime] | None = None,
    beauty_ratio: float = 0.8,
    limit: int = 20,
    now: datetime | None = None,
) -> tuple[list[tuple[Candidate, BuzzScore]], list[tuple[Candidate, str]]]:
    """候補を並べる。(採用, 落ちたもの) を返す。

    `filt` は [autoreply.filter] そのものを渡す。収集元ごとの上書き
    （[autoreply.filter.accounts]）は候補ごとに解決する。

    落ちたものも返すのは、あとで「なぜ返さなかったのか」を記録するため。
    """
    rejected: list[tuple[Candidate, str]] = []
    survivors: list[Candidate] = []
    for candidate in candidates:
        ok, reason = passes_filter(
            candidate, resolve_filter(filt, candidate.source),
            last_reply_at=last_reply_at, now=now)
        if ok:
            survivors.append(candidate)
        else:
            rejected.append((candidate, reason))

    # 既存の並べ替えを通す。重複排除・除外・美容比率の維持は
    # tests/test_engage_candidates.py が固定している挙動なので作り直さない。
    picked = rank_candidates(
        survivors,
        limit=max(limit, 1),
        beauty_ratio=beauty_ratio,
        exclude_usernames=exclude_usernames,
        exclude_shortcodes=exclude_shortcodes,
    )

    scored = []
    for candidate in picked:
        previous = store.previous_sighting(candidate.shortcode) if store else None
        scored.append((candidate, buzz(candidate, previous=previous,
                                       weights=weights, now=now)))
    scored.sort(key=lambda pair: pair[1].total, reverse=True)
    return scored, rejected
