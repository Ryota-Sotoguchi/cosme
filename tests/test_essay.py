"""リンクなしで書ききる型。

1日10枠のうちリンク投稿は1枠だけで、残り9枠はリンクなし。
書ききる型をリンク投稿にしか入れないと、9割の投稿に効かない。

実測（101投稿）の文字数は中央値60字・最大214字で、上限500字の
半分も使っていなかった。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content.benefits import (
    BENEFITS,
    PITFALLS,
    TIP_NOTES,
    pitfall_line,
    tips_block,
)
from src.content.builder import ContentBuilder
from src.content.facts import extract_numbers
from src.storage.state import State

MIN_LENGTH = 300
MAX_LENGTH = 500


def essays(tmp_path: Path, count: int = 15):
    builder = ContentBuilder(
        State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json"
    )
    out = []
    for _ in range(count):
        draft = builder.build("essay", [], with_affiliate_link=False)
        builder.commit(draft)
        out.append(draft)
    return out


# ======================================================================
def test_essay_actually_uses_the_limit(tmp_path):
    """既存の最長（214字）を大きく超え、上限は超えないこと。"""
    for draft in essays(tmp_path):
        assert MIN_LENGTH <= len(draft.text) <= MAX_LENGTH, (
            f"{len(draft.text)}字:\n{draft.text}"
        )


def test_essay_has_no_link_and_no_ad_mark(tmp_path):
    """リンクなしの型なので、広告表示も商品も出さないこと。"""
    for draft in essays(tmp_path, 5):
        assert not draft.has_affiliate_link
        assert "#PR" not in draft.text
        assert draft.items == []


def test_essay_passes_compliance(tmp_path, config):
    checker = ComplianceChecker(config.compliance, config.dedup, max_length=MAX_LENGTH)
    for draft in essays(tmp_path):
        result = checker.check(draft, recent_texts=[])
        assert result.passed, f"{result.summary()}\n{draft.text}"


def test_essay_rotates_subjects(tmp_path):
    """同じ剤形が続けて出ないこと。

    カーソル任せにしていたときは、連続4本とも同じ剤形になった。
    """
    subjects = [d.part_ids.get("essay") for d in essays(tmp_path)]
    assert len(set(subjects)) >= len(BENEFITS) - 2, f"剤形が偏っている: {subjects}"
    for a, b in zip(subjects, subjects[1:]):
        assert a != b, f"同じ剤形が連続した: {subjects}"


def test_essay_explains_every_criterion(tmp_path):
    """観点に理由が付いていること。

    観点の名前だけ並べても読み手には何も残らない。実測でも、
    箇条書きだけの howto 型は表示中央値62・反応ゼロで最下位だった。
    """
    for draft in essays(tmp_path, 5):
        bullets = [ln for ln in draft.text.split("\n") if ln.startswith("・")]
        assert len(bullets) >= 5, f"観点が少ない:\n{draft.text}"
        # 各観点の次の行に理由が入っていること
        lines = draft.text.split("\n")
        for index, line in enumerate(lines):
            if line.startswith("・"):
                assert index + 1 < len(lines), f"理由が無い: {line}"
                assert lines[index + 1].startswith("　"), (
                    f"「{line}」に理由が付いていない:\n{draft.text}"
                )


def test_essay_ends_with_a_question(tmp_path):
    """読み切ったあとに返信の入口があること。

    人からの返信が付いたのは問いかけ型だけだった（実測7件中6件）。
    """
    for draft in essays(tmp_path, 5):
        assert draft.text.rstrip().endswith(("？", "〜", "ね〜")), draft.text[-30:]
        assert "essay_question" in draft.part_ids


def test_essay_declares_its_numbers(tmp_path):
    for draft in essays(tmp_path, 5):
        for token in extract_numbers(draft.text):
            assert token in draft.allowed_numbers, f"{token} が許可されていない"


# ======================================================================
# 素材そのもの
# ======================================================================
def test_every_criterion_has_a_reason():
    """全部の観点に理由が付いていること。

    一部だけだと、理由のある行と無い行が混ざって不揃いに見える。
    """
    missing = [
        (b.id, tip) for b in BENEFITS for tip in b.tips
        if tip not in TIP_NOTES.get(b.id, {})
    ]
    assert not missing, f"理由が無い観点: {missing}"


def test_no_stale_reasons():
    """観点から消した語の理由が残っていないこと。"""
    known = {b.id: set(b.tips) for b in BENEFITS}
    stale = [
        (bid, tip) for bid, notes in TIP_NOTES.items()
        for tip in notes if tip not in known.get(bid, set())
    ]
    assert not stale, f"観点に無い理由: {stale}"


def test_every_benefit_has_a_pitfall():
    missing = [b.id for b in BENEFITS if b.id not in PITFALLS]
    assert not missing, f"よくある外し方が無い剤形: {missing}"


def test_reasons_carry_no_digits():
    """理由に半角数字を入れないこと。

    templates.py と同じ理由。compliance のデータ整合性チェックが、
    商品データに無い数値として弾く。
    """
    for bid, notes in TIP_NOTES.items():
        for tip, why in notes.items():
            assert not extract_numbers(why), f"{bid}/{tip}: {why}"
    for bid, body in PITFALLS.items():
        assert not extract_numbers(body), f"{bid}: {body}"


def test_reasons_pass_the_ng_dictionary():
    for bid, notes in TIP_NOTES.items():
        for tip, why in notes.items():
            assert not scan(why, has_link=True), f"{bid}/{tip}: {why}"
    for bid, body in PITFALLS.items():
        assert not scan(body, has_link=True), f"{bid}: {body}"
    for benefit in BENEFITS:
        for cursor in range(5):
            line = pitfall_line(benefit, cursor)
            assert not scan(line, has_link=True), line


def test_pitfall_lead_rotates():
    """「よくあるのが、」だけだと、そこが型になる。"""
    benefit = BENEFITS[0]
    seen = {pitfall_line(benefit, c) for c in range(5)}
    assert len(seen) >= 4, seen


def test_tips_block_without_reasons_is_unchanged():
    """短い型は従来どおり、観点だけを並べること。"""
    benefit = BENEFITS[0]
    plain = tips_block(benefit, cursor=0, count=3)
    assert "　" not in plain, plain


# ======================================================================
def test_essay_is_in_the_link_free_rotation(config):
    """リンクなしの枠に入っていること。9割の投稿はこちら側。"""
    slots = [name for name, options in config.rotation.items() if "essay" in options]
    assert len(slots) >= 3, f"書ききる型が {slots} にしか入っていない"

    affiliate = {s.slot for s in config.schedule if s.allow_affiliate}
    assert not (set(slots) & affiliate), "リンクなしの型がリンク枠に入っている"


def test_essay_needs_no_items():
    from src.pipeline import ITEMS_NEEDED

    assert ITEMS_NEEDED["essay"] == 0
