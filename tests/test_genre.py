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
#   pipeline.py           … 型ごとの必要商品数の表（ITEMS_NEEDED）に "site_link": 0 を1行
#                           （新しい投稿型をローテーションに入れるのに必要。処理は変えていない。
#                             下のハッシュはこの1行を足した後のもの）
#   engage/review.py      … 「レビュー」「口コミ」の禁止を撤回
#   engage/browser/selectors.py … 過去投稿の削除に使うセレクタの追加
#   .github/workflows/post.yml … 発火時刻の前倒し（2026-09-24）。定期実行の遅れ（中央値268分）で
#                           狙った時間帯に出ていなかったため。本数・処理は変えていない
#   engage/store.py       … 成果待ちの返信を our_reply_url 無しでも返す + permalink の後埋め
#                           （2026-09-24）。着弾確認に失敗した返信が成果測定から漏れていた
#   engage/executor.py    … 着弾確認で下までスクロールする（2026-09-24）。返信は投稿の下に
#                           描かれるので、再読み込みだけでは見つからず、実際に付いた返信3件中2件を
#                           «確認できなかった» と記録していた。送信・再送しない方針は変えていない
#   engage/llm.py         … CLI が exit!=0 のとき stdout も添えて理由を残す（2026-09-24）。
#                           stderr だけだと «exit=1: » と空になり、自動返信が1件も返せない
#                           原因が追えなかった。呼び出し方・判断の仕組みは変えていない
# ======================================================================
UNCHANGED_MECHANISM = {
    "src/pipeline.py": "cb553ce7c70b8d4e53f4b786479005916fbdf0e1",
    "src/content/builder.py": "a0c500ab449d6c6392b416b343e6adf519cf3bb0",
    "src/threads/client.py": "fd7751fb637ee1aa6e1ce4162e1588656c79fa64",
    "src/threads/insights.py": "7baf13b2c12f6ff03baf25796c5f0c20a196fd69",
    "src/threads/replies.py": "269f43e75ba921196b986dbc7f59460e2a3dc2f7",
    "src/threads/token.py": "80973d116b5aaaca06b2a3783b841add0a6cd65a",
    "src/storage/history.py": "ec367dd090d26b0e2c6922a7ee9543a96661d9c1",
    "src/storage/state.py": "f3236916c3f92eaa1bfb2871bf2fcb77c7abc0f1",
    "src/storage/revenue.py": "d31c43d11822d4dbc8a9825ab8f07009d1167ed2",
    "src/engage/runner.py": "c8fb5d95696e44fbe19a8b2e8fc6466bb78dc723",
    "src/engage/executor.py": "96bc6834d7ef98ac8d3309650ce921f0e64a9ea6",
    "src/engage/verify.py": "89e53846389ec637efc14f3d89510fba38fe7a45",
    "src/engage/budget.py": "de178e77975cc4797019a16f0296b56973a09f88",
    "src/engage/store.py": "279cdb1d1d0331810342eee523d4f1da693d1841",
    "src/engage/llm.py": "d07cffb7a73173297b5d8bd8c6373ae2d6a5c843",
    "src/engage/writer.py": "4e9bc5f1ffda406fb82a0a1ab95f141765993ae2",
    "src/engage/judge.py": "c15f85f84968199030203d7d609838cf0cb58d45",
    "src/engage/browser/session.py": "b44a56ad0dacc9d5faf1ec90805d74ebb5d0bebe",
    "src/engage/browser/actions.py": "79e2e3f2cc2deb55a8c47a9bcf202cb43b9868fd",
    ".github/workflows/insights.yml": "afac8780162c20da0dfc7f57c7614d45ea6937e2",
    ".github/workflows/post.yml": "a3664614977ce39cea9134855927807fc6defbc8",
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
    """時刻と本数の見張り。

    **2026-09-24 に発火時刻を前倒しした**（ジャンルの切り替えとは別の変更）。
    GitHub Actions の遅れが実測で中央値268分あり、07:30〜22:30 に発火していた投稿が
    実際には 09:26〜02:14 に出ていた。出したい時刻から遅れぶんを引いた時刻へ組み直した。
    1日10本と枠の名前は変えていない。
    """
    expected = {
        "morning": ("05:30", "30 20 * * *"), "midmorning": ("06:30", "30 21 * * *"),
        "latemorning": ("08:00", "0 23 * * *"), "noon": ("09:30", "30 0 * * *"),
        "afternoon": ("11:00", "0 2 * * *"), "predinner": ("12:30", "30 3 * * *"),
        "evening": ("15:00", "0 6 * * *"), "earlynight": ("16:00", "0 7 * * *"),
        "night": ("17:00", "0 8 * * *"), "late": ("18:30", "30 9 * * *"),
    }
    actual = {s.slot: (s.time_jst, s.cron_utc) for s in config.schedule}
    assert actual == expected


# ======================================================================
# 2. 投稿にコスメ・美容が戻ってこないこと
# ======================================================================
# 投稿に出てはいけない語（コスメ・美容の発信の名残）
COSME_WORDS = (
    "コスメ", "美容", "スキンケア", "化粧", "メイク", "ヘアケア", "ボディケア",
    "シャンプー", "トリートメント", "日焼け止め", "リップ", "クレンジング", "美容液",
    "乳液", "ファンデ", "アイシャドウ", "マスカラ", "ポーチ", "詰め替え", "プチプラ",
    "デパコス", "肌", "楽天", "送料",
)
PRODUCT_TYPES = {"product", "price_band", "postage_free", "comparison", "longform"}


def _cosme_hits(text: str) -> list[str]:
    return [w for w in COSME_WORDS if w in text]


def test_no_slot_posts_affiliate_links(config):
    """**リンク投稿（楽天・コスメ）は休止中。**

    発信ジャンルと合わないアフィリエイトを混ぜない（2026-09-14 決定）。
    再開するときは、このテストごと見直すこと。
    """
    assert [s.slot for s in config.schedule if s.allow_affiliate] == []


def test_no_rotation_contains_a_product_type(config):
    for slot, options in config.rotation.items():
        assert not (set(options) & PRODUCT_TYPES), f"{slot} に商品投稿の型が残っている: {options}"


def test_every_slot_still_has_seven_options(config):
    """構成は変えていない（どの枠も7要素、1日10本）。"""
    assert len(config.schedule) == 10
    for slot, options in config.rotation.items():
        assert len(options) == 7, f"{slot}: {options}"


def test_brand_murmurs_from_product_names_are_off():
    """直近の投稿履歴の商品名からコスメのつぶやきを作らないこと。"""
    from src.content.brand_murmurs import BRAND_MURMURS

    assert BRAND_MURMURS == ()


@pytest.mark.parametrize("pool_name", [
    "CASUAL_MURMURS", "QUESTION_POSTS", "NO_LINK_TOPICS", "HOWTO_POSTS",
    "THREAD_TOPICS", "ESSAY_QUESTIONS", "SITE_LINK_POSTS",
])
def test_link_free_pools_are_career_content(pool_name):
    from src.content import parts as P

    for part in getattr(P, pool_name):
        hits = _cosme_hits(part.text)
        assert not hits, f"{pool_name}.{part.id} にコスメの語: {hits}\n  {part.text}"


def test_essay_subjects_are_career_content():
    from src.content.benefits import CAREER_SUBJECTS, PITFALLS, TIP_NOTES

    for subject in CAREER_SUBJECTS:
        texts = [subject.concern, subject.pain, subject.future, *subject.tips,
                 *TIP_NOTES[subject.id].values(), PITFALLS[subject.id]]
        for text in texts:
            assert not _cosme_hits(text), f"{subject.id}: {text}"


def test_generated_posts_carry_no_cosme_words(config, tmp_path):
    """**実際に組み上がった投稿**を、全リンクなし型 × 複数日で見る。

    プールだけ見ていると、テンプレート側の見出しや既定値（トピックタグ、
    書ききる型の見出し）から混ざる分を取りこぼす。
    """
    from datetime import date, datetime, timedelta

    from src.content.builder import ContentBuilder
    from src.content.templates import TEMPLATES
    from src.storage.state import State
    from src.storage.history import JST

    link_free = sorted({t.post_types[0] for t in TEMPLATES if t.item_count == 0})
    assert {"casual", "question", "no_link", "thread_topic", "howto", "essay"} <= set(link_free)

    builder = ContentBuilder(State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json")
    start = date(2026, 9, 14)
    for day in range(14):
        today = start + timedelta(days=day)
        for hour in (7, 12, 18, 22):
            now = datetime(today.year, today.month, today.day, hour, tzinfo=JST)
            for post_type in link_free:
                draft = builder.build(post_type, [], with_affiliate_link=False,
                                      today=today, now=now)
                builder.commit(draft)
                for text in (draft.text, *draft.segments):
                    assert not _cosme_hits(text), f"{post_type}: {_cosme_hits(text)}\n{text}"
                if draft.topic_tag:
                    assert not _cosme_hits(draft.topic_tag), f"{post_type} のタグ: {draft.topic_tag}"


# ======================================================================
# 3. 返信にコスメ・美容が戻ってこないこと
# ======================================================================
def test_the_reply_prompts_carry_no_cosme_words():
    """中の人の説明・例文・禁止事項・話題の前提に、コスメの語が残っていないこと。"""
    from dataclasses import dataclass

    from src.engage import prompts

    @dataclass
    class Post:
        username: str = "someone"
        text: str = "転職活動三か月目。面接で年収の希望を聞かれて固まった"
        likes: int = 40
        replies: int = 6
        age_hours: float = 3.0

    texts = [
        prompts.target_prompt(Post()),
        prompts.judge_prompt(Post(), "希望を聞かれると身構えますよね"),
        *(prompts.reply_prompt(Post(), shape=shape) for shape in prompts.REPLY_SHAPES),
    ]
    for text in texts:
        assert not _cosme_hits(text), _cosme_hits(text)


def test_the_persona_is_the_career_one():
    from src.content.persona import TRAITS

    joined = " ".join(f"{k}: {v}" for k, v in TRAITS.items())
    assert "転職" in joined and "面接官" in joined
    assert not _cosme_hits(joined)


def test_the_reply_search_words_are_career_words(config):
    import importlib.util

    from src.engage.candidates import BEAUTY_WORDS
    from src.engage.sources.search import DEFAULT_KEYWORDS

    spec = importlib.util.spec_from_file_location("cc", ROOT / "scripts" / "collect_candidates.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    for name, words in (
        ("config search_keywords", config.autoreply_section("sources")["search_keywords"]),
        ("DEFAULT_KEYWORDS", DEFAULT_KEYWORDS),
        ("BEAUTY_WORDS（名前は当初のジャンルの名残）", BEAUTY_WORDS),
        ("collect_candidates", (*script.BEAUTY_KEYWORDS, *script.OTHER_KEYWORDS)),
    ):
        hits = [w for w in words if _cosme_hits(w)]
        assert not hits, f"{name} にコスメの語: {hits}"
        assert any("転職" in w or "年収" in w for w in words), f"{name} に転職の語が無い"


def test_side_jobs_are_a_topic_but_solicitation_is_still_avoided():
    """副業は発信ジャンルの話題。稼げる系の勧誘は触らない。"""
    from src.engage.candidates import SENSITIVE_WORDS

    assert "副業" not in SENSITIVE_WORDS
    assert {"稼げ", "情報商材"} <= set(SENSITIVE_WORDS)
