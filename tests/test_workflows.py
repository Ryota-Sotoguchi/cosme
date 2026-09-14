"""GitHub Actions のワークフローが、動かすコードの依存を入れていること。

## なぜ要るのか

research.yml は playwright だけを入れて scripts/collect_reviews.py を
動かしていた。スクリプトは src.content.voices を import し、パッケージの
__init__ 経由で src.http → requests まで読み込む。

2026-09-06 から**毎日** ModuleNotFoundError で落ち、失敗通知が届き続けた。
その間 data/voices.json は更新されず、投稿の主役である「使った人の感想」の
素材が1週間止まっていた。

ローカルでは requests が入っているのでテストもスクリプトも通る。
**ワークフローの定義を読まないと気づけない種類の壊れ方**なので、ここで見る。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# src/ のコードを読み込む実行。scripts/ も sys.path に src を足して import する。
RUNS_PROJECT_CODE = re.compile(r"python3?\s+(-m\s+src\.|scripts/)")
INSTALLS_REQUIREMENTS = re.compile(r"pip\s+install\b[^\n]*-r\s+requirements\.txt")


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def test_the_workflows_are_found():
    """見る対象が空だと、下のテストは何も保証しない。"""
    names = {p.name for p in workflow_files()}
    assert {"post.yml", "research.yml", "insights.yml"} <= names


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflows_that_run_project_code_install_its_requirements(path):
    text = path.read_text(encoding="utf-8")
    if not RUNS_PROJECT_CODE.search(text):
        return
    assert INSTALLS_REQUIREMENTS.search(text), (
        f"{path.name} は src/ か scripts/ を実行するのに requirements.txt を入れていない。"
        " ローカルでは通っても、Actions では ModuleNotFoundError で落ちる"
    )


def test_every_script_run_by_research_imports_cleanly_without_a_browser():
    """research.yml が動かすスクリプトは、import の段階で落ちないこと。

    requirements.txt が揃っていれば読み込めること（playwright はスクリプトの
    実行時に初めて要る）を、ワークフローと同じ呼び方で確かめる。
    """
    import subprocess
    import sys

    text = (WORKFLOWS / "research.yml").read_text(encoding="utf-8")
    scripts = re.findall(r"python3?\s+(scripts/[\w./]+\.py)", text)
    assert scripts, "research.yml からスクリプトを拾えていない"

    root = WORKFLOWS.parent.parent
    for script in scripts:
        # ファイルを実行せず、import 部分だけを評価する（__main__ に入らない）
        code = (
            "import runpy, sys; sys.argv=['x','--help'];"
            f"runpy.run_path({str(root / script)!r}, run_name='not_main')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root,
            capture_output=True, text=True, timeout=60,
        )
        assert "ModuleNotFoundError" not in result.stderr, (
            f"{script} の import で落ちる:\n{result.stderr[-600:]}"
        )


def test_the_research_commit_survives_a_failed_collector():
    """1つの収集が落ちても、ほかの収集結果はコミットすること。

    以前は collect_reviews だけが落ちても Commit ごと飛ばされ、
    成功していた収集まで捨てていた。
    """
    text = (WORKFLOWS / "research.yml").read_text(encoding="utf-8")
    commit_step = text.split("- name: Commit", 1)[1].split("run:", 1)[0]
    assert "if: always()" in commit_step
