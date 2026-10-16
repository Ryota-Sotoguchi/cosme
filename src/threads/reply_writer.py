"""自分の投稿に付いたコメントへの返信文を書く。

## なぜ定型文をやめるのか

`replies.py` はもともと「ありがとうございます🤍」のような定型文を下書きとして
出すだけで、**実際に返すのは人**という前提だった。その結果、
**他人から付いた返信38件のうち、答えたと記録があるのは1件**という状態になった
（2026-10-16 に判明）。9/26 の投稿には14件のコメントが付いていて、それも残っている。

わざわざ書き込んだ人に返さないのは、いちばん安くていちばん効く機会を捨てている。
定型文で埋めても会話にはならないので、**相手が書いたことに答える文**を作る。

## 費用

CLAUDE.md の「例外1」と同じ。**サブスクリプションの `claude` CLI を
サブプロセスで呼ぶ**（`src/engage/llm.py`）。Anthropic の HTTP API は使わない。
返信文は相手のコメントに依存するので、テンプレート合成では書けない。

## 安全側

* CLI が無い・答えが読めない・検査に落ちた → **None を返す**。
  呼び出し側は定型文に落とすか、その回は返さない
* `review(include_experience=True)` を必ず通す。機械が書いた文なので、
  使用体験と口コミの創作、URL、敬語崩れ、薬機法・転職ジャンルの禁止表現を機械的に弾く
* 書き直しは1回だけ。2回落ちたら諦める（粘るほど不自然になる）
"""

from __future__ import annotations

import logging
from typing import Any

from ..engage.llm import extract_json
from ..engage.review import review

logger = logging.getLogger(__name__)

# 返信の長さ。コメント欄なので短くする。
MAX_CHARS = 100
# 書き直しの回数（最初の1回を含めて2回まで）
MAX_ATTEMPTS = 2


def _prompt(comment: str, post_text: str, *, problems: list[str] | None = None) -> str:
    retry = ""
    if problems:
        retry = (
            "\n# 前回の文は次の理由で使えませんでした。直してください\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n"
        )
    return f"""あなたは Threads アカウントの中の人です。
転職を二回している三十代の会社員で、面接官として採用にも入っています。
発信ジャンルは転職・年収・キャリア。

自分の投稿に付いたコメントへ、返信を1つ書いてください。

# 自分の投稿
{post_text.strip() or "(本文を取得できませんでした)"}

# 付いたコメント
{comment.strip()}
{retry}
# 書き方
- **敬語（です・ます）**。知らない人のコメント欄なので、タメ口は馴れ馴れしく見えます
- **相手が書いたことに答える。** 挨拶やお礼だけで終わらせない
- 質問なら、自分の経験の範囲で具体的に答える（転職2回・面接官の経験は書いてよい）
- 1〜2文、{MAX_CHARS}字以内
- 絵文字は使っても0〜1個
- 相手の言い分を否定しない。上から教えない

# 禁止
- URL、サービス名の宣伝、DMや公式LINEへの誘導
- 使っていないサービスの体験談（「このエージェントで年収が上がった」等）
- 統計の断定（「平均年収は○万円」「○割の人が」）
- 「必ず」「絶対」「誰でも」のような保証の言い方
- 年齢や性別で可能性を断定する言い方
- 断定的な法的助言
- 実在する会社の名指しの批判

# 出力
JSON だけを返してください。説明は不要です。
{{"reply": "返信の本文"}}"""


def write_reply(
    client: Any,
    *,
    comment: str,
    post_text: str = "",
) -> str | None:
    """コメント1件への返信文を作る。作れなければ None。

    `client` は `src/engage/llm.py` の `LlmClient`（`ask()` を持つもの）。
    """
    if not (comment or "").strip():
        return None
    if client is None or not getattr(client, "available", False):
        logger.info("claude CLI が無いので、返信文は定型文に任せます")
        return None

    problems: list[str] = []
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.ask(_prompt(comment, post_text, problems=problems))
        except Exception as exc:  # noqa: BLE001 — 1件で運用を止めない
            logger.warning("返信文を書けませんでした: %s", exc)
            return None

        text = ""
        try:
            data = extract_json(response.text)
            text = str(data.get("reply") or "").strip()
        except Exception:  # noqa: BLE001 — 読めない答えは却下に倒す
            logger.info("返信文の答えを読めませんでした（%d回目）", attempt + 1)
            return None

        if not text:
            return None
        if len(text) > MAX_CHARS:
            problems = [f"長すぎます（{len(text)}字 > {MAX_CHARS}）"]
            continue

        # **機械が書いた文なので include_experience=True。**
        # 使用体験と口コミの創作まで弾く（CLAUDE.md §4）。
        result = review(text, include_experience=True)
        if result.ok:
            return text
        problems = result.problems
        logger.info("返信文が検査に落ちました（%d回目）: %s", attempt + 1, result.summary())

    return None


def build_writer(client: Any):
    """`ReplyResponder` に渡す形（コメントと投稿本文を受け取って文を返す）にする。"""

    def _write(reply: Any, post_text: str) -> str | None:
        return write_reply(client, comment=getattr(reply, "text", ""), post_text=post_text)

    return _write
