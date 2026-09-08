"""LLM 呼び出し口の検査。

実際に claude CLI を起動しない。subprocess.run を差し替えて、
**出力の揺れをどこまで吸収できるか**と
**認証情報を子プロセスへ漏らしていないか**を見る。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from src.engage.llm import (
    MAX_PROMPT_CHARS,
    ClaudeCliClient,
    extract_json,
)
from src.errors import LlmUnavailableError, TransientError


# ======================================================================
# JSON の取り出し
# ======================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"score": 0.9}', {"score": 0.9}),
        ('```json\n{"score": 0.9}\n```', {"score": 0.9}),
        ('```\n{"score": 0.9}\n```', {"score": 0.9}),
        ('はい、判定しました。\n{"score": 0.9}', {"score": 0.9}),
        ('{"score": 0.9}\n以上です。', {"score": 0.9}),
        ('  \n {"a": {"b": 1}} \n ', {"a": {"b": 1}}),
    ],
)
def test_extracts_json_from_common_shapes(text, expected):
    assert extract_json(text) == expected


def test_extracts_json_when_prose_after_it_contains_a_brace():
    """素朴な rfind("}") はここで壊れる。

    JSON の後ろに `}` を含む散文が続くと、そこまで飲み込んでしまう。
    """
    text = '{"score": 0.9}\n補足: 閉じ括弧 } は文中にも出ます。'
    assert extract_json(text) == {"score": 0.9}


def test_extracts_json_when_a_string_value_contains_braces():
    text = '{"reason": "本文に { } が入っている場合"}'
    assert extract_json(text) == {"reason": "本文に { } が入っている場合"}


def test_extracts_json_when_a_string_value_contains_escaped_quote():
    text = r'{"reason": "彼は\"だめ\"と言った"}'
    assert extract_json(text) == {"reason": '彼は"だめ"と言った'}


@pytest.mark.parametrize("text", ["", "JSONを出せませんでした", "[1, 2, 3]", "{壊れている"])
def test_returns_empty_when_no_object_is_found(text):
    """**空 = 判断できなかった。** 呼び出し側が安全側に倒す。"""
    assert extract_json(text) == {}


# ======================================================================
# 起動
# ======================================================================
class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _envelope(result: str, **extra) -> str:
    return json.dumps({"result": result, **extra})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("src.engage.llm.shutil.which", lambda _: "/usr/bin/claude")
    return ClaudeCliClient({"min_interval_seconds": 0})


def test_reports_unavailable_without_starting_a_process(monkeypatch):
    monkeypatch.setattr("src.engage.llm.shutil.which", lambda _: None)

    def explode(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        raise AssertionError("available は subprocess を起こしてはいけない")

    monkeypatch.setattr(subprocess, "run", explode)
    assert ClaudeCliClient().available is False


def test_missing_binary_raises_with_install_instructions(monkeypatch):
    monkeypatch.setattr("src.engage.llm.shutil.which", lambda _: None)
    with pytest.raises(LlmUnavailableError) as excinfo:
        ClaudeCliClient().ask("なんでもいい")
    message = str(excinfo.value)
    assert "npm install -g @anthropic-ai/claude-code" in message
    assert "claude.cmd" in message  # Windows 側を使わない理由も出す


def test_passes_the_prompt_as_its_own_argv_element(client, monkeypatch):
    """shell=True にすると投稿本文がシェル補間される。argv 固定であること。"""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeCompleted(_envelope('{"ok": true}'))

    monkeypatch.setattr(subprocess, "run", fake_run)
    dangerous = '`rm -rf /` $(whoami) "; echo pwned"'
    client.ask(dangerous)

    argv = seen["argv"]
    assert argv[argv.index("-p") + 1] == dangerous
    assert seen["kwargs"].get("shell") in (None, False)
    assert seen["kwargs"]["encoding"] == "utf-8"


def test_does_not_run_in_the_repository(client, monkeypatch):
    """リポジトリで起動すると CLAUDE.md が判定者のコンテキストに入る。"""
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: (seen.update(kw), FakeCompleted(_envelope("{}")))[1],
    )
    client.ask("x")
    assert "cosme" not in str(seen["cwd"])


def test_does_not_leak_credentials_to_the_child_process(client, monkeypatch):
    """.env の中身は os.environ に載っている。子プロセスへ渡さない。"""
    for name in ("THREADS_ACCESS_TOKEN", "RAKUTEN_ACCESS_KEY", "GH_PAT", "THREADS_APP_SECRET"):
        monkeypatch.setenv(name, "should-not-be-passed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "allowed")

    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: (seen.update(kw), FakeCompleted(_envelope("{}")))[1],
    )
    client.ask("x")

    env = seen["env"]
    assert "THREADS_ACCESS_TOKEN" not in env
    assert "RAKUTEN_ACCESS_KEY" not in env
    assert "GH_PAT" not in env
    assert "THREADS_APP_SECRET" not in env
    assert env["ANTHROPIC_API_KEY"] == "allowed"  # CLI の認証に要るものは通す
    assert "PATH" in env


def test_truncates_an_overlong_prompt(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: (seen.update({"argv": argv}), FakeCompleted(_envelope("{}")))[1],
    )
    client.ask("あ" * (MAX_PROMPT_CHARS + 5000))
    argv = seen["argv"]
    assert len(argv[argv.index("-p") + 1]) == MAX_PROMPT_CHARS


# ======================================================================
# 失敗の扱い
# ======================================================================
def test_timeout_becomes_transient(client, monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TransientError):
        client.ask("x")


def test_nonzero_exit_becomes_transient(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: FakeCompleted("", "認証が切れています", returncode=1),
    )
    with pytest.raises(TransientError) as excinfo:
        client.ask("x")
    assert "認証が切れています" in str(excinfo.value)


def test_unparsable_stdout_becomes_transient(client, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: FakeCompleted("これはJSONではない"))
    with pytest.raises(TransientError):
        client.ask("x")


def test_is_error_envelope_becomes_transient(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: FakeCompleted(_envelope("上限に達しました", is_error=True)),
    )
    with pytest.raises(TransientError):
        client.ask("x")


def test_unextractable_json_returns_empty_instead_of_raising(client, monkeypatch):
    """ここで例外にしない。判定側が「判断できなかった」として却下に倒す。"""
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: FakeCompleted(_envelope("すみません、JSONを出せません")),
    )
    response = client.ask("x")
    assert response.data == {}
    assert response.ok is False


def test_returns_parsed_data_on_success(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: FakeCompleted(_envelope('```json\n{"score": 0.82}\n```')),
    )
    response = client.ask("x")
    assert response.data == {"score": 0.82}
    assert response.ok is True
