"""A8 のリンクを投稿に直接置く試験（型 guide_a8）を見張る。2026-11-04 追加。

自サイト経由の導線（投稿 → サイト → A8 のバナー）は、11/1〜4 で広告表示6・クリック2だった。
本人の判断で、年収の記事の最後の1本に A8 のリンクを直接置く。

見張ること:

1. **リンクは Secrets（A8_LINKS）から読む。** 未設定・壊れているときは自サイトの紹介に戻る
2. **通すのは登録したURLと完全一致するものだけ。** ホストが px.a8.net でも別のURLは落ちる
3. **広告の投稿として扱う。** #PR が要り、体験談・誇張のルール（リンク投稿用）も掛かる
4. **URL を公開の場所に残さない。** history（public リポジトリ）・ログ・Actions の表示で伏せる
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import tempfile
from datetime import date
from pathlib import Path

import pytest

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content import direct_ads
from src.content.builder import ContentBuilder
from src.content.guides import A8_INVITES, SITE_GUIDES
from src.content.parts import SITE_URL
from src.content.persona import contradicts
from src.content.templates import PR_TAG
from src.storage.history import History, PostRecord
from src.storage.state import State
from tests.test_genre import _cosme_hits
from tests.test_site_link import AUTHOR_WORDS, DISCOVERY_WORDS, OVERCLAIM_WORDS

A8_URL = "https://px.a8.net/svt/ejp?a8mat=TEST0000+AAAAAA+1111+2222"
LINKS = {"agent": {"name": "テスト転職", "url": A8_URL}}


@pytest.fixture
def a8(monkeypatch):
    monkeypatch.setenv(direct_ads.ENV_NAME, json.dumps(LINKS, ensure_ascii=False))


@pytest.fixture
def no_a8(monkeypatch):
    monkeypatch.delenv(direct_ads.ENV_NAME, raising=False)


def _builder() -> ContentBuilder:
    tmp = Path(tempfile.mkdtemp())
    return ContentBuilder(State(tmp / "state.json"), voices_path=tmp / "voices.json")


def _draft():
    return _builder().build("guide_a8", [], with_affiliate_link=False, today=date(2026, 11, 4))


def _checker(config) -> ComplianceChecker:
    # checker は作った時点で A8_LINKS を読む。環境を整えてから作ること
    return ComplianceChecker(config.compliance, config.dedup)


# ======================================================================
# 1. 読み込み
# ======================================================================
def test_nothing_is_loaded_without_the_secret(no_a8):
    assert direct_ads.load() == ()


def test_the_secret_is_read(a8):
    (ad,) = direct_ads.load()
    assert (ad.id, ad.name, ad.url) == ("agent", "テスト転職", A8_URL)


@pytest.mark.parametrize("url", [
    "http://px.a8.net/svt/ejp?a8mat=X",          # https でない
    "https://example.com/svt/ejp?a8mat=X",       # A8 でない
    "https://px.a8.net/",                        # 媒体IDが無い
    "https://px.a8.net/svt/ejp?a8mat=X Y",       # 空白で途切れる
])
def test_only_a8_links_are_accepted(monkeypatch, url):
    monkeypatch.setenv(direct_ads.ENV_NAME, json.dumps({"x": {"name": "n", "url": url}}))
    assert direct_ads.load() == ()


def test_a_broken_secret_is_ignored_without_leaking_the_url(monkeypatch, caplog):
    monkeypatch.setenv(direct_ads.ENV_NAME, json.dumps({"x": {"url": A8_URL}}))  # name が無い
    with caplog.at_level(logging.ERROR):
        assert direct_ads.load() == ()
    assert "x" in caplog.text
    assert A8_URL not in caplog.text

    monkeypatch.setenv(direct_ads.ENV_NAME, "{not json")
    assert direct_ads.load() == ()


# ======================================================================
# 2. 組み上がった投稿
# ======================================================================
def test_without_the_secret_it_falls_back_to_the_site(no_a8):
    draft = _draft()
    assert draft.part_ids["a8"] == "none"
    assert re.findall(r"https?://\S+", draft.text) == [SITE_URL]
    assert draft.segments[-1].rstrip().endswith(f"\n{PR_TAG}")


def test_the_a8_link_is_only_on_the_last_post(a8):
    draft = _draft()
    *body, last = draft.segments
    assert draft.part_ids["a8"] == "agent"
    assert re.findall(r"https?://\S+", draft.text) == [A8_URL]
    assert A8_URL in last and "テスト転職" in last
    assert last.rstrip().endswith(f"\n{PR_TAG}"), last
    assert draft.text.count(PR_TAG) == 1
    for segment in body:
        assert "http" not in segment and PR_TAG not in segment
    assert SITE_URL not in draft.text


def test_the_article_rotation_is_shared_with_guide_site(a8):
    """同じ記事を2つの型で近い日に出さない（出すと類似度で弾かれる）。"""
    draft = _draft()
    assert draft.part_ids["guide_site"] in {p.id for p in SITE_GUIDES}


def test_every_article_passes_compliance_with_the_a8_link(config, a8):
    checker = _checker(config)
    builder = _builder()
    for part in SITE_GUIDES:
        for invite in A8_INVITES:
            def pick(group, pool, _part=part, _invite=invite, **flags):
                return {"guide_site": _part, "a8_invite": _invite}.get(group, pool[0])
            builder._pick = pick
            draft = builder.build("guide_a8", [], with_affiliate_link=False, today=date(2026, 11, 4))
            result = checker.check(draft, recent_texts=[])
            assert result.passed, f"{part.id}/{invite.id}: {result.summary()}\n{draft.segments[-1]}"


# ======================================================================
# 3. 検査 — 広告の投稿として扱う
# ======================================================================
def test_an_unregistered_a8_url_is_stopped(config, a8):
    draft = _draft()
    other = "https://px.a8.net/svt/ejp?a8mat=OTHER+BBBBBB+3333+4444"
    segments = [seg.replace(A8_URL, other) for seg in draft.segments]
    swapped = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = _checker(config).check(swapped, recent_texts=[])
    assert any(v.category == "url" for v in result.violations), result.summary()


def test_the_ad_mark_is_required(config, a8):
    draft = _draft()
    segments = [seg.replace(f"\n\n{PR_TAG}", "") for seg in draft.segments]
    stripped = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = _checker(config).check(stripped, recent_texts=[])
    assert any(v.category == "pr" for v in result.violations), result.summary()


def test_link_post_rules_apply_to_the_a8_post(config, a8):
    """使ってもいないサービスの成功談は、広告の投稿では止める（景表法・ASP規約）。"""
    draft = _draft()
    *body, last = draft.segments
    claim = "このエージェント経由で年収が上がった。\n"
    segments = [*body, claim + last]
    claimed = dataclasses.replace(draft, text="\n\n".join(segments), segments=segments)
    result = _checker(config).check(claimed, recent_texts=[])
    assert any(v.category == "experience" for v in result.violations), result.summary()


# ======================================================================
# 4. 公開の場所に URL を残さない
# ======================================================================
def test_history_does_not_keep_the_a8_url(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.append(PostRecord(
        posted_at="2026-11-04T23:10:00+09:00", slot="late", post_type="guide_a8",
        template_id="guide_a8", status="success", text=f"相談先はここ👇\n{A8_URL}\n\n#PR",
        has_affiliate_link=False,
    ))
    saved = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "px.a8.net" not in saved
    assert "a8mat" not in saved


def test_logs_do_not_show_the_a8_url(a8, caplog):
    from src.logging_setup import SecretMaskingFilter

    logger = logging.getLogger("test_direct_ads")
    logger.addFilter(SecretMaskingFilter())
    try:
        with caplog.at_level(logging.INFO, logger="test_direct_ads"):
            logger.info("投稿: %s", A8_URL)
    finally:
        logger.filters.clear()
    assert A8_URL not in caplog.text


def test_the_printed_draft_hides_the_a8_url(a8, capsys):
    """GitHub Actions のログは public。投稿予定の本文を表示するところでも伏せる。"""
    from src.main import _print_draft

    _print_draft(_draft())
    out = capsys.readouterr().out
    assert "テスト転職" in out
    assert A8_URL not in out and "px.a8.net" not in out


# ======================================================================
# 5. 文面
# ======================================================================
@pytest.mark.parametrize("invite", A8_INVITES, ids=[p.id for p in A8_INVITES])
def test_the_invite_text(invite):
    text = invite.text.format(name="テスト転職")
    assert "{name}" in invite.text
    assert not scan(text, has_link=True), [h.label for h in scan(text, has_link=True)]
    assert not re.search(r"[0-9０-９]", text)
    assert not [w for w in (*AUTHOR_WORDS, *DISCOVERY_WORDS, *OVERCLAIM_WORDS) if w in text]
    assert not contradicts(text) and not _cosme_hits(text)
    assert "http" not in text and PR_TAG not in text
    assert text.rstrip().endswith("👇")
    # URL と #PR を足しても1本に収まること（記事の中でいちばん長い本文と合わせても）
    assert len(f"{text}\n{A8_URL}\n\n{PR_TAG}") <= 500
