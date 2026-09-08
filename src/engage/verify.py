"""投稿直前の再確認。**誤爆防止。**

## なぜ必要か

候補を選んだ時点と、実際に返信する時点にはずれがある。その間に

  * 投稿が消される・非公開になる
  * 本文が編集される（Threads は投稿後5分の編集を許す。
    伸び始めの投稿はまさにその時間帯にいる）
  * URL がログイン画面や404へ飛ぶ
  * 省略表示で見えていなかった部分に、触れてはいけない話題があった

返信は他人の投稿に残り、後から直せない。**1つでも合わなければ返さない。**
「直して続行」はしない。

## ナビゲーションを挟まない

パーマリンクを開いた直後、コンポーザを開く前に、同じページ上で全部やる。
検証と実行の間にページ遷移を挟むと、そこが誤爆の窓になる。
だから runner はこれを単体で呼ばず、executor に渡す。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .browser import actions
from .browser.extract import age_hours_from_datetime
from .candidates import SENSITIVE_WORDS, SPAM_WORDS, parse_search_block

logger = logging.getLogger(__name__)

# 本文の突き合わせに使う先頭の文字数
COMPARE_HEAD = 60

_NOISE = re.compile(r"[\s　]|[\U0001F300-\U0001FAFF]|[!！?？。、〜…・]")


def normalize(text: str) -> str:
    """比較用に均す。空白・絵文字・記号を落とす。

    改行の入り方や絵文字の有無だけで «別の投稿» と判定したくない。
    """
    return _NOISE.sub("", text or "")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""
    fresh_text: str = ""
    fresh_age_hours: float | None = None


class PrePostVerifier:
    def __init__(self, config: Config) -> None:
        self.config = config
        filt = config.autoreply_section("filter")
        self.max_age_hours = float(filt.get("max_age_hours", 12))

    # ------------------------------------------------------------------
    def verify(self, page: Any, candidate: Any, *, own_username: str = "") -> VerifyResult:
        """返していい状態か。**1つでも落ちたらその候補は捨てる。**"""
        # 1. 開いた先が本当にその投稿か
        url = (getattr(page, "url", "") or "").split("?")[0]
        expected = f"/@{candidate.username}/post/{candidate.shortcode}"
        if expected.lower() not in url.lower():
            return VerifyResult(False, f"別のページに飛ばされました: {url[:120]}")

        # 2. 消えていないか
        if actions.exists(page, "post_deleted_marker"):
            return VerifyResult(False, "投稿が削除・非公開になっています")

        # 3. 返信できる状態か
        if actions.exists(page, "replies_disabled_marker"):
            return VerifyResult(False, "この投稿は返信を受け付けていません")

        # 4. 自分の投稿ではないか
        if own_username and candidate.username.lower() == own_username.lower():
            return VerifyResult(False, "自分の投稿です")

        body = self._read_body(page, candidate)
        if not body:
            return VerifyResult(False, "本文を読めませんでした")

        # 5. 選定時に読んだ内容がまだ残っているか
        #
        # 見たいのは「返信の根拠にした部分が今もあるか」。末尾への追記
        # （「追記: コメントありがとうございます」）まで弾くと、よくある
        # 投稿の大半を捨てることになる。**冒頭が保たれているか**で見て、
        # 追記された部分の危険は次のセンシティブ語の再走査で拾う。
        before = normalize(candidate.text)
        after = normalize(body)
        head = min(COMPARE_HEAD, len(before))
        if head and before[:head] != after[:head]:
            return VerifyResult(
                False,
                f"本文が変わっています（選定時「{candidate.text[:24]}…」/"
                f" 現在「{body[:24]}…」）",
                fresh_text=body)

        # 6. 改めて読んだ本文に触れてはいけない話題が無いか
        #    順位付けに使ったのは省略表示だった可能性がある。
        sensitive = [w for w in SENSITIVE_WORDS if w in body]
        if sensitive:
            return VerifyResult(False, f"センシティブな話題: {sensitive[:3]}",
                                fresh_text=body)
        spam = [w for w in SPAM_WORDS if w in body]
        if spam:
            return VerifyResult(False, f"宣伝アカウント: {spam[:3]}", fresh_text=body)

        # 7. 返信ボタンに手が届くか
        try:
            if actions.resolve(page, "reply_button", required=False) is None:
                return VerifyResult(False, "返信ボタンが見つかりません", fresh_text=body)
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(False, f"返信ボタンを確認できません: {exc}", fresh_text=body)

        # 8. まだ新しいか（候補にしてから時間が経っていることがある）
        age = self._read_age(page)
        if age is not None and age > self.max_age_hours:
            return VerifyResult(
                False, f"古くなりました（{age:.1f}時間 > {self.max_age_hours}）",
                fresh_text=body, fresh_age_hours=age)

        return VerifyResult(True, fresh_text=body, fresh_age_hours=age)

    # ------------------------------------------------------------------
    def _read_body(self, page: Any, candidate: Any) -> str:
        """投稿カードから本文だけを取り出す。

        **候補と同じパーサを通すこと。**

        投稿カードの innerText は「著者名 / 時刻 / 本文 / 反応数」という形で、
        候補の `text` はそこから著者名と時刻と数字を剥がした後の文字列。
        生の innerText とそのまま突き合わせると、

            選定時 「Threads伸びないって…」
            現在   「ai.tem_studio 6時間 Thread…」

        となって**構造上いつまでも一致しない**。実際、審査を通った返信が
        投稿直前に止められた（2026-09-07）。
        """
        element = actions.resolve(page, "post_body", required=False)
        if element is None:
            return ""
        try:
            raw = (element.inner_text() or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("本文を読めません: %s", exc)
            return ""
        if not raw:
            return ""

        href = getattr(candidate, "href", "") or (
            f"/@{candidate.username}/post/{candidate.shortcode}")
        parsed = parse_search_block(href, raw)
        return parsed.text if parsed is not None else raw

    def _read_age(self, page: Any) -> float | None:
        element = actions.resolve(page, "post_timestamp", required=False)
        if element is None:
            return None
        try:
            return age_hours_from_datetime(element.get_attribute("datetime"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("投稿時刻を読めません: %s", exc)
            return None
