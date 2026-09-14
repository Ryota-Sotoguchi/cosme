"""発信ジャンル「転職・年収・キャリア」の見張り。

2026-09-14 に、発信ジャンルをコスメ・美容から転職・年収・キャリアへ切り替えた。
**変えたのは「何を言うか」のデータと判定語だけで、自動運用の仕組みは変えていない。**

このファイルは2つを見張る:

1. 仕組みのファイルが切り替え前と同じであること
   （投稿・返信・スケジュール・DB・LLM 呼び出し・ワークフロー）
2. 投稿・返信・タグ・検索語にコスメ・美容が戻ってこないこと
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# ======================================================================
# 1. 仕組みを触っていないこと
#
# 切り替え前のコミット 6656465 時点の git blob ハッシュ。
# **ここを書き換えるのは、仕組みを変えると決めたときだけ。**
# ジャンルの切り替えのついでに直したくなっても、別の変更として扱う。
#
# 例外として決めたもの（ここに入れていない）:
#   compliance/checker.py … 自作サイトのURLを許可する2か所
#   engage/review.py      … 「レビュー」「口コミ」の禁止を撤回
#   engage/browser/selectors.py … 過去投稿の削除に使うセレクタの追加
# ======================================================================
UNCHANGED_MECHANISM = {
    "src/pipeline.py": "b4f0bbd8405aadd7704e0e4d07e86ded640575dd",
    "src/content/builder.py": "a0c500ab449d6c6392b416b343e6adf519cf3bb0",
    "src/threads/client.py": "fd7751fb637ee1aa6e1ce4162e1588656c79fa64",
    "src/threads/insights.py": "7baf13b2c12f6ff03baf25796c5f0c20a196fd69",
    "src/threads/replies.py": "269f43e75ba921196b986dbc7f59460e2a3dc2f7",
    "src/threads/token.py": "80973d116b5aaaca06b2a3783b841add0a6cd65a",
    "src/storage/history.py": "ec367dd090d26b0e2c6922a7ee9543a96661d9c1",
    "src/storage/state.py": "f3236916c3f92eaa1bfb2871bf2fcb77c7abc0f1",
    "src/storage/revenue.py": "d31c43d11822d4dbc8a9825ab8f07009d1167ed2",
    "src/engage/runner.py": "c8fb5d95696e44fbe19a8b2e8fc6466bb78dc723",
    "src/engage/executor.py": "bf1b9ee3184bd8648f71e56fdad1b7b0a9d98ac0",
    "src/engage/verify.py": "89e53846389ec637efc14f3d89510fba38fe7a45",
    "src/engage/budget.py": "de178e77975cc4797019a16f0296b56973a09f88",
    "src/engage/store.py": "76d45d04d5578339fbbe6663f6ffa526c61199d0",
    "src/engage/llm.py": "e82c4ee9906770160cc6b146c7b5d44149d54e69",
    "src/engage/writer.py": "4e9bc5f1ffda406fb82a0a1ab95f141765993ae2",
    "src/engage/judge.py": "c15f85f84968199030203d7d609838cf0cb58d45",
    "src/engage/browser/session.py": "b44a56ad0dacc9d5faf1ec90805d74ebb5d0bebe",
    "src/engage/browser/actions.py": "79e2e3f2cc2deb55a8c47a9bcf202cb43b9868fd",
    ".github/workflows/insights.yml": "afac8780162c20da0dfc7f57c7614d45ea6937e2",
    ".github/workflows/post.yml": "eaa8a29e57d57bc2c8fec04b588663a7646b9a3e",
    ".github/workflows/research.yml": "3c9422f1e4e8d1d55636307d1d546b78059c630c",
    ".github/workflows/review.yml": "300160f40e3791e706ac6275294fa248f820aa5d",
    ".github/workflows/test.yml": "983ba9ef090f5f73db0197b60dae7625f080261e",
    ".github/workflows/token-refresh.yml": "4883e910e89e91bcd7217369dcecd380624d61fe",
}


def _blob_hash(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.mark.parametrize("relative", sorted(UNCHANGED_MECHANISM))
def test_the_automation_mechanism_is_unchanged(relative):
    path = ROOT / relative
    assert path.exists(), f"{relative} が消えている"
    assert _blob_hash(path) == UNCHANGED_MECHANISM[relative], (
        f"{relative} が切り替え前から変わっている。ジャンルの切り替えでは仕組みを触らない。"
        " 仕組みを変えると決めたなら、別の変更としてハッシュを更新すること"
    )


def test_every_workflow_is_guarded():
    """ワークフローを足したら、ここにも入ること（見張りの抜けを作らない）。"""
    workflows = {f".github/workflows/{p.name}" for p in (ROOT / ".github" / "workflows").glob("*.yml")}
    assert workflows <= set(UNCHANGED_MECHANISM)


def test_the_schedule_timing_is_unchanged(config):
    """時刻・cron・本数は変えない。変えたのは noon のリンクの有無だけ。"""
    expected = {
        "morning": ("07:30", "30 22 * * *"), "midmorning": ("09:30", "30 0 * * *"),
        "latemorning": ("11:00", "0 2 * * *"), "noon": ("12:15", "15 3 * * *"),
        "afternoon": ("14:30", "30 5 * * *"), "predinner": ("16:30", "30 7 * * *"),
        "evening": ("18:00", "0 9 * * *"), "earlynight": ("19:00", "0 10 * * *"),
        "night": ("20:30", "30 11 * * *"), "late": ("22:30", "30 13 * * *"),
    }
    actual = {s.slot: (s.time_jst, s.cron_utc) for s in config.schedule}
    assert actual == expected
