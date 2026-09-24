"""年収相場チェッカーへの誘導（投稿型 site_link）。

2026-09-14 追加。サイト: https://business-3hy.pages.dev/
（厚労省の賃金構造基本統計調査をもとに、職種と都道府県で年収の目安を出す。広告なし）

見張ること:

1. **毎回は出さない。** 出るのは site_link 型だけで、ローテーションで週3回前後
2. **作者を名乗らない。他人のサイトを見つけた体にもしない。**
   作者を伏せるのと、他人のものを装うのは別。後者は広告を載せた時点でステマになる
3. **許すのは config の own_site_urls と完全一致するURLだけ。**
   パス違い・別ドメイン・http は落ちる
4. サイトに広告を載せたら own_site_needs_pr = true にすれば #PR が要るようになる
"""

from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime, timedelta

import pytest

from src.compliance.checker import ComplianceChecker
from src.compliance.rules import scan
from src.content import parts as P
from src.content.builder import ContentBuilder
from src.content.persona import contradicts
from src.content.templates import PR_TAG, TEMPLATES
from src.pipeline import Pipeline
from src.storage.history import JST, History
from src.storage.state import State

# 作者を名乗る語
AUTHOR_WORDS = ("作った", "作りました", "自作", "手作り", "開発した", "つくった")
# 他人のサイトを見つけた・薦められた体の語
DISCOVERY_WORDS = (
    "見つけた", "見つけました", "教えてもらった", "教えてもらいました",
    "おすすめされた", "オススメされた", "勧められた", "紹介してもらった", "知人の", "友達の",
)
# サイトが出すのは平均からの目安。個人の適正額が分かるとは言わない
OVERCLAIM_WORDS = ("適正年収", "本当の年収", "正確な年収", "市場価値が分かる", "市場価値がわかる")


def _checker(config, **overrides):
    compliance = {**config.compliance, **overrides}
    return ComplianceChecker(compliance, config.dedup, max_length=500)


def _site_draft(tmp_path):
    builder = ContentBuilder(State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json")
    return builder.build("site_link", [], template_id="site_link", with_affiliate_link=False,
                         today=date(2026, 9, 15))


# ======================================================================
# 1. 設定とテンプレートの URL が食い違わないこと
# ======================================================================
def test_the_template_url_is_the_one_allowed_in_config(config):
    assert config.compliance["own_site_urls"] == [P.SITE_URL]
    assert "business-3hy.pages.dev" in config.compliance["allowed_url_hosts"]


def test_the_site_is_not_marked_as_an_ad_while_it_has_no_ads(config):
    """いまのサイトは広告もアフィリエイトも無い。広告を載せたらここと config を見直す。"""
    assert config.compliance["own_site_needs_pr"] is False


# ======================================================================
# 2. 文面
# ======================================================================
def test_the_pool_is_large_enough():
    """週3回前後なので、2週間の類似度の窓に6本前後が入る。その倍はほしい。"""
    assert len(P.SITE_LINK_POSTS) >= 12
    assert len({p.id for p in P.SITE_LINK_POSTS}) == len(P.SITE_LINK_POSTS)
    assert len({p.text for p in P.SITE_LINK_POSTS}) == len(P.SITE_LINK_POSTS)


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_never_claims_or_disguises_who_made_the_site(part):
    hits = [w for w in (*AUTHOR_WORDS, *DISCOVERY_WORDS) if w in part.text]
    assert not hits, f"{part.id}: {hits}\n  {part.text}"


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_does_not_overclaim_what_the_site_shows(part):
    hits = [w for w in OVERCLAIM_WORDS if w in part.text]
    assert not hits, f"{part.id}: {hits}\n  {part.text}"


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_writes_no_numbers(part):
    """統計の数字は投稿に書かない。数字はサイトが出典つきで出す。"""
    assert not re.search(r"[0-9０-９]", part.text), f"{part.id}: {part.text}"


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_text_leaves_the_url_to_the_template(part):
    assert "http" not in part.text and ".dev" not in part.text


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_passes_every_expression_rule(part):
    """リンクの付く投稿なので、景表法系（リンク投稿用）のルールにも当てておく。"""
    assert not scan(part.text, has_link=False), f"{part.id}: {scan(part.text, has_link=False)}"
    assert not scan(part.text, has_link=True), f"{part.id}: {scan(part.text, has_link=True)}"


@pytest.mark.parametrize("part", P.SITE_LINK_POSTS, ids=lambda p: p.id)
def test_the_post_fits_the_persona(part):
    assert not contradicts(part.text), f"{part.id}: {contradicts(part.text)}"


# ======================================================================
# 3. 組み上がった投稿
# ======================================================================
def test_the_site_link_post_carries_the_site_and_nothing_else(tmp_path):
    draft = _site_draft(tmp_path)
    assert draft.text.rstrip().endswith(P.SITE_URL)
    assert re.findall(r"https?://\S+", draft.text) == [P.SITE_URL]
    assert not draft.items
    assert not draft.link_attachment
    # 商品を持たないので、楽天のリンク枠にも「アフィリエイトリンクあり」にも数えない
    assert not draft.has_affiliate_link
    assert PR_TAG not in draft.text


def test_the_url_is_not_in_the_first_post(tmp_path):
    """URLは2本目（自分への返信）に置く。

    本文にURLを入れた3本の表示は 1 / 7 / 0 だった（2026-09-24 の実測）。
    同じ日の他の投稿は100〜600出ている。タイムラインに乗る1本目にURLを置かない。
    """
    draft = _site_draft(tmp_path)
    assert len(draft.segments) == 2, draft.segments
    assert "http" not in draft.segments[0], draft.segments[0]
    assert draft.segments[1] == P.SITE_URL


def test_the_site_link_post_passes_compliance(config, tmp_path):
    builder = ContentBuilder(State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json")
    checker = _checker(config)
    for _ in range(len(P.SITE_LINK_POSTS)):
        draft = builder.build("site_link", [], template_id="site_link",
                              with_affiliate_link=False, today=date(2026, 9, 15))
        result = checker.check(draft, recent_texts=[])
        assert result.passed, f"{result.summary()}\n{draft.text}"
        builder.commit(draft)


def test_no_other_post_type_ever_carries_a_url(tmp_path):
    """サイトが出るのは site_link 型だけ。ほかの投稿には一切出ない。"""
    builder = ContentBuilder(State(tmp_path / "state.json"), voices_path=tmp_path / "voices.json")
    others = sorted({t.post_types[0] for t in TEMPLATES
                     if t.item_count == 0 and t.id != "site_link"})
    start = date(2026, 9, 14)
    for day in range(7):
        today = start + timedelta(days=day)
        for hour in (7, 12, 19, 22):
            now = datetime(today.year, today.month, today.day, hour, tzinfo=JST)
            for post_type in others:
                draft = builder.build(post_type, [], with_affiliate_link=False, today=today, now=now)
                builder.commit(draft)
                assert "http" not in draft.text, f"{post_type}:\n{draft.text}"
                assert "pages.dev" not in draft.text, f"{post_type}:\n{draft.text}"


# ======================================================================
# 4. 頻度 — 毎回は出さない
# ======================================================================
def test_the_site_appears_about_three_times_a_week(config):
    """各枠は1日1回、ローテーションを1つ進める。週あたりの出現回数を設定から計算する。"""
    per_week = sum(
        options.count("site_link") / len(options) * 7
        for options in config.rotation.values()
    )
    assert 2 <= per_week <= 4, f"週 {per_week:.1f} 回"


def test_no_slot_repeats_the_site_within_its_rotation(config):
    for slot, options in config.rotation.items():
        assert options.count("site_link") <= 1, f"{slot}: {options}"


def test_the_site_is_not_a_fallback_for_other_types():
    """在庫が尽きた型の逃がし先に site_link を入れない。入れると頻度が読めなくなる。"""
    for post_type in ("casual", "question", "no_link", "thread_topic", "howto", "essay"):
        assert "site_link" not in Pipeline._no_link_fallbacks(post_type)


def test_the_pipeline_posts_the_site_when_the_rotation_reaches_it(config, tmp_path):
    slot = next(name for name, options in config.rotation.items() if "site_link" in options)
    options = config.rotation[slot]
    state = State(tmp_path / "state.json")
    state._data.setdefault("rotation_cursor", {})[slot] = options.index("site_link")
    pipeline = Pipeline(config, history=History(tmp_path / "history.jsonl"), state=state,
                        rakuten=object())

    result = pipeline.run(slot)

    assert result.check.passed, result.check.summary()
    assert result.draft.template_id == "site_link"
    assert P.SITE_URL in result.draft.text


# ======================================================================
# 5. 判定 — 許すのは完全一致だけ
# ======================================================================
@pytest.mark.parametrize("url", [
    "https://business-3hy.pages.dev/other",
    "https://business-3hy.pages.dev/?ref=threads",
    "http://business-3hy.pages.dev/",
    "https://evil.pages.dev/",
    "https://example.com/",
])
def test_only_the_exact_site_url_is_allowed(config, tmp_path, url):
    draft = _site_draft(tmp_path)
    text = draft.text.replace(P.SITE_URL, url)
    draft = dataclasses.replace(draft, text=text, segments=[text])
    result = _checker(config).check(draft, recent_texts=[])
    assert not result.passed, f"{url} が通ってしまう"
    assert any(v.category == "url" for v in result.violations), result.summary()


def test_marking_the_site_as_an_ad_requires_the_pr_tag(config, tmp_path):
    """サイトに広告を載せて own_site_needs_pr = true にしたら、#PR 無しは落ちること。"""
    draft = _site_draft(tmp_path)
    result = _checker(config, own_site_needs_pr=True).check(draft, recent_texts=[])
    assert not result.passed
    assert any(v.category == "pr" for v in result.violations), result.summary()


def test_without_the_setting_the_site_is_treated_as_an_ad(config, tmp_path):
    """設定を書いていない config では #PR を要求する側に倒す（判定がゆるむ方向に変えない）。"""
    compliance = {k: v for k, v in config.compliance.items() if k != "own_site_needs_pr"}
    draft = _site_draft(tmp_path)
    result = ComplianceChecker(compliance, config.dedup, max_length=500).check(draft, recent_texts=[])
    assert not result.passed


def test_the_site_does_not_excuse_a_missing_pr_tag_on_product_posts(config):
    """自分のサイトの例外は、自分のサイト**だけ**にリンクしている投稿に限ること。

    商品のリンクと並べたら、#PR 無しは落ちる（本文に並べても、リンクカードで添付しても）。
    URL の本数上限とは別に見るため、ここでは上限を2本に広げて確かめる。
    """
    from conftest import make_item

    from src.content.builder import Draft

    item = make_item()
    checker = _checker(config, max_urls=2)
    body = f"年収の目安はここ👇\n\n{P.SITE_URL}"
    drafts = [
        Draft(text=f"{body}\n\n{item.affiliate_url}", template_id="short",
              post_type="product", items=[item]),
        Draft(text=body, template_id="short", post_type="product", items=[item],
              link_attachment=item.affiliate_url),
    ]
    for draft in drafts:
        result = checker.check(draft, recent_texts=[])
        assert any(v.category == "pr" for v in result.violations), (
            f"#PR 無しで通ってしまう: {result.summary()}\n{draft.text}"
        )
