"""ブラウザ依存が漏れていないことを機械で確かめる。

## なぜこれをテストにするのか

`.github/workflows/research.yml` は playwright を requirements.txt に
入れない理由をこう書いている:

    本体の requirements には入れない。
    投稿パイプラインはブラウザを必要としないので、依存を混ぜたくない。

この約束は、うっかり `from playwright...` をモジュール先頭に書いた瞬間に
壊れる。しかも壊れたことに気づくのは、playwright の入っていない
GitHub Actions で投稿が落ちたときになる。**人の注意力ではなく import で縛る。**
"""

from __future__ import annotations

import importlib
import sys

import pytest

# session.py だけが playwright を知っていてよい。
BROWSER_FREE_MODULES = [
    "src.engage.buzz",
    "src.engage.candidates",
    "src.engage.judge",
    "src.engage.llm",
    "src.engage.prompts",
    "src.engage.responder",
    "src.engage.review",
    "src.engage.store",
    "src.engage.writer",
    "src.engage.browser",
    "src.engage.browser.selectors",
]

# 投稿パイプラインの経路。ここにブラウザが混ざったら本番が落ちる。
POSTING_PIPELINE_MODULES = [
    "src.main",
    "src.pipeline",
    "src.config",
    "src.content.builder",
    "src.compliance.checker",
]


@pytest.mark.parametrize("name", BROWSER_FREE_MODULES + POSTING_PIPELINE_MODULES)
def test_importing_does_not_pull_in_playwright(name):
    for module in list(sys.modules):
        if module == "playwright" or module.startswith("playwright."):
            del sys.modules[module]

    importlib.import_module(name)

    leaked = [m for m in sys.modules if m == "playwright" or m.startswith("playwright.")]
    assert not leaked, (
        f"{name} が playwright を引き込んでいます: {leaked}\n"
        "ブラウザの import は src/engage/browser/session.py の関数本体でだけ行うこと。"
    )


def test_playwright_is_not_in_the_runtime_requirements():
    """投稿パイプラインにブラウザ依存を持ち込まない。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    body = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "playwright" not in body.lower()


def test_playwright_is_declared_in_its_own_requirements_file():
    """入れ方は書いてある、という状態にする。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    path = root / "requirements-browser.txt"
    assert path.is_file(), "requirements-browser.txt がありません"
    assert "playwright" in path.read_text(encoding="utf-8").lower()


def test_the_session_module_defers_its_playwright_import():
    """session.py も、モジュール先頭では import しない。

    先頭に書くと `src.engage.browser.session` を import しただけで
    playwright が要る。selfcheck やヘルプ表示で困る。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    body = (root / "src/engage/browser/session.py").read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.startswith(("import playwright", "from playwright")):
            raise AssertionError(f"モジュール先頭で playwright を import しています: {line}")
