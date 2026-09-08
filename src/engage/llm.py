"""LLM の呼び出し口。Claude Code CLI をサブプロセスとして叩く。

## なぜ HTTP API ではなく CLI なのか

CLAUDE.md は「コストは0円に保つ」を最優先ルールに置いている。
Anthropic の HTTP API を使うと従量課金が発生するが、既に持っている
Claude Code のサブスクリプションから `claude -p` を呼べば追加課金は無い。

投稿文（`src/content/`）はこれまでどおりルールベースのままで、
LLM を使うのは他人の投稿への**返信**だけ。返信は相手の投稿に依存するので、
テンプレート合成では書けない。

## 副産物

`claude -p` は毎回まっさらなコンテキストで起動する。そのため
「返信を書いた者が自分の返信を採点する」構図に構造的にならない。
Writer と Judge は別プロセスで、互いの内部状態を共有しない。

## ここではリトライしない

失敗は呼び出し側へ返す。返信案の書き直しは `writer.py` が
`max_generation_attempts` の範囲で回す（`ComplianceChecker` の
`max_regenerations` と同じ考え方）。二重にリトライすると、
1件の返信のために CLI が何度も起動して手に負えなくなる。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import LlmUnavailableError, TransientError
from ..http import RateLimiter

logger = logging.getLogger(__name__)

# プロンプトに埋める他人の投稿本文の上限。
# ARG_MAX は 2MB あるので実害は無いが、暴走した長文が判断を汚すのを防ぐ。
MAX_PROMPT_CHARS = 20000

# 子プロセスへ通す環境変数。**allowlist であることが重要。**
#
# config.load_config() は .env を読んで RAKUTEN_* / THREADS_* / GH_PAT を
# os.environ に載せる。制御下にないプロセスへ認証情報を渡す理由が無い
# （CLAUDE.md「Secret を絶対にコミットしない」の実行時版）。
_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "SHELL", "TMPDIR", "XDG_CONFIG_HOME")
_ENV_ALLOWED_PREFIXES = ("ANTHROPIC_", "CLAUDE_")

_INSTALL_HINT = """claude CLI が見つかりません（探した名前: {binary}）。

WSL 内に Node.js と Claude Code を入れてください:

    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
    npm install -g @anthropic-ai/claude-code
    claude          # 初回だけ対話で認証する

Windows 側の claude.cmd は WSL から呼べません
（.bat なので cmd.exe /c が要るが、UNC パスで失敗し日本語も化ける）。"""


@dataclass(frozen=True)
class LlmResponse:
    """CLI の応答ひとつ。"""

    text: str
    """CLI が返した result 文字列。"""

    data: dict[str, Any] = field(default_factory=dict)
    """text から取り出した JSON。取れなければ空。**空は「判断できなかった」。**"""

    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.data)


class LlmClient(Protocol):
    """テストが差し替えられるようにするための継ぎ目。"""

    @property
    def available(self) -> bool: ...

    def ask(self, prompt: str, *, timeout_s: float | None = None) -> LlmResponse: ...


# ======================================================================
# JSON の取り出し
# ======================================================================
def _strip_code_fence(text: str) -> str:
    """```json ... ``` を剥がす。"""
    body = text.strip()
    if not body.startswith("```"):
        return body
    # 最初の改行までがフェンス（```json など）
    _, _, rest = body.partition("\n")
    closing = rest.rfind("```")
    return (rest[:closing] if closing != -1 else rest).strip()


def _balanced_object(text: str) -> str | None:
    """最初の { から、釣り合う } までを切り出す。

    `text[find("{"):rfind("}")+1]` は使わない。JSON の後ろに `}` を含む
    散文が続くと、そこまで飲み込んで壊れるため。文字列リテラルと
    バックスラッシュエスケープを見ながら深さを数える。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str) -> dict[str, Any]:
    """モデルの返答から JSON を取り出す。取れなければ空 dict。

    **eval / ast.literal_eval は使わない。**
    """
    if not text:
        return {}

    for candidate in (_strip_code_fence(text), text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            return parsed if isinstance(parsed, dict) else {}

    chunk = _balanced_object(_strip_code_fence(text)) or _balanced_object(text)
    if chunk:
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _result_text(envelope: dict[str, Any]) -> str:
    """CLI のエンベロープから本文を取り出す。

    スキーマが変わっても致命傷にならないよう、順に落とす。
    """
    for key in ("result", "text", "content"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# ======================================================================
# Claude Code CLI
# ======================================================================
class ClaudeCliClient:
    """`claude -p` をサブプロセスで呼ぶ。"""

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        rate_limiter: RateLimiter | None = None,
        workdir: str | None = None,
    ) -> None:
        opts = options or {}
        self.binary = str(opts.get("binary", "claude"))
        self.model = str(opts.get("model", "sonnet"))
        self.timeout_s = float(opts.get("timeout_seconds", 120))
        interval = float(opts.get("min_interval_seconds", 2.0))
        self.rate_limiter = rate_limiter or (RateLimiter(interval) if interval > 0 else None)
        # **リポジトリを cwd にしない。**
        # ここで起動すると CLAUDE.md が子プロセスのコンテキストに読み込まれ、
        # 判定が偏るうえトークンも無駄に食う。
        self._workdir = workdir or tempfile.gettempdir()

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """CLI が PATH にあるか。**サブプロセスは起こさない。**"""
        return shutil.which(self.binary) is not None

    def resolved_binary(self) -> str | None:
        return shutil.which(self.binary)

    def _require_binary(self) -> str:
        path = shutil.which(self.binary)
        if path is None:
            raise LlmUnavailableError(_INSTALL_HINT.format(binary=self.binary))
        return path

    def _child_env(self) -> dict[str, str]:
        """子プロセスへ渡す環境変数。allowlist で絞る。"""
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _ENV_ALLOWLIST or key.startswith(_ENV_ALLOWED_PREFIXES)
        }
        # 日本語がロケール依存で化けないようにする
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        return env

    # ------------------------------------------------------------------
    def ask(self, prompt: str, *, timeout_s: float | None = None) -> LlmResponse:
        """プロンプトを投げて応答を返す。**リトライしない。**"""
        binary = self._require_binary()
        body = (prompt or "")[:MAX_PROMPT_CHARS]

        if self.rate_limiter is not None:
            self.rate_limiter.wait()

        argv = [
            binary,
            # プロンプトは argv の独立要素として渡す。
            # **shell=True にしないこと。** 本文には第三者の投稿が入るので、
            # バッククォートや $ がシェル補間されればそのまま injection になる。
            "-p",
            body,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--max-turns",
            "1",
            # ツールを使わせない。承認待ちで固まるのを防ぐ。
            "--allowed-tools",
            "",
        ]

        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 — argv 固定・shell=False
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s or self.timeout_s,
                cwd=self._workdir,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError(
                f"claude CLI が {timeout_s or self.timeout_s:.0f} 秒で応答しませんでした"
            ) from exc
        except OSError as exc:
            raise TransientError(f"claude CLI を起動できません: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = proc.stdout or ""
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            raise TransientError(f"claude CLI exit={proc.returncode}: {stderr[:300]}")

        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TransientError(
                f"claude CLI の出力を解釈できません: {(stderr or stdout)[:300]}"
            ) from exc

        if not isinstance(envelope, dict):
            raise TransientError(f"claude CLI の出力が想定外の形です: {stdout[:200]}")

        if envelope.get("is_error"):
            raise TransientError(f"claude CLI がエラーを返しました: {_result_text(envelope)[:300]}")

        text = _result_text(envelope)
        data = extract_json(text)
        if not data:
            # 例外にしない。**呼び出し側が「判断できなかった」として
            # 安全側（返信しない）に倒す。**
            logger.warning("LLM の応答から JSON を取り出せませんでした: %s", text[:200])
        return LlmResponse(text=text, data=data, duration_ms=duration_ms)


def build_client(config: Any, **kwargs: Any) -> ClaudeCliClient:
    """Config から CLI クライアントを組む。"""
    return ClaudeCliClient(config.autoreply_section("llm"), **kwargs)
