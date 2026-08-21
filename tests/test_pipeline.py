"""パイプラインと DRY_RUN のテスト。

楽天クライアントだけを差し替えて、取得〜生成〜検証〜投稿の経路を通す。
"""

from __future__ import annotations

import pytest
from datetime import datetime

from src.errors import ComplianceSkip
from src.main import cmd_post
from src.pipeline import Pipeline
from src.storage.history import JST, History, PostRecord
from src.storage.state import State
from tests.conftest import make_item


class FakeRakuten:
    """collect_candidates だけを持つスタブ。"""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def collect_candidates(self, genres, **kwargs):
        self.calls.append(kwargs)
        return list(self.items)


def pool(n=12):
    items = []
    for i in range(n):
        items.append(
            make_item(
                item_code=f"shop{i}:{i}",
                item_name=f"テストコスメ{'A' * (i % 5 + 1)} {(i + 1) * 10}g",
                item_price=800 + i * 300,
                review_count=50 + i * 137,
                review_average=3.8 + (i % 10) * 0.1,
                postage_flag=i % 2,
                shop_code=f"shop{i}",
            )
        )
    return items


def make_pipeline(config, tmp_path, items=None):
    history = History(tmp_path / "history.jsonl")
    state = State(tmp_path / "state.json")
    return Pipeline(config, history=history, state=state, rakuten=FakeRakuten(items or pool()))


# ======================================================================
@pytest.mark.parametrize("slot", ["morning", "noon", "evening", "night", "late"])
def test_every_slot_produces_a_compliant_post(config, tmp_path, slot):
    pipeline = make_pipeline(config, tmp_path)
    result = pipeline.run(slot)
    assert result.check.passed
    assert result.draft.text.strip()


def test_no_link_slots_never_contain_urls(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    for slot in ("morning", "evening"):
        draft = pipeline.run(slot).draft
        assert "http" not in draft.text
        assert not draft.has_affiliate_link


def test_product_slot_includes_pr_marker_and_link(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    draft = pipeline.run("noon").draft
    # 広告表示・商品名・リンクは、すべて最後の1本に集約する
    assert draft.segments[-1].startswith("#PR")
    # URLは本文ではなくリンクカードとして添付する（500文字を消費しないため）
    assert draft.link_attachment
    assert "hb.afl.rakuten.co.jp" in draft.link_attachment
    assert "http" not in draft.text


def test_late_slot_rotates_post_types(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    expected = config.rotation["late"]
    seen = [pipeline.resolve_post_type("late") for _ in range(len(expected))]
    assert seen == expected


def test_excluded_products_are_never_selected(config, tmp_path):
    """除外対象は絶対に投稿しない（枠はリンクなし投稿で埋める）。"""
    banned = [
        make_item(item_code=f"s{i}:{i}", item_name=f"【第2類医薬品】テスト薬{i}", shop_code=f"s{i}")
        for i in range(5)
    ]
    pipeline = make_pipeline(config, tmp_path, items=banned)
    draft = pipeline.run("noon").draft

    assert draft.post_type == "no_link"
    assert not draft.items          # 除外商品は一切出てこない


def test_recently_posted_item_is_skipped(config, tmp_path):
    # ここで見たいのは商品選定なので、ランプアップのリンク枠制限は外す
    # （枠を使い切るとリンクなし投稿へ切り替わり、商品を選ばなくなる）
    config.raw["ramp_up"]["enabled"] = False
    items = pool(3)
    pipeline = make_pipeline(config, tmp_path, items=items)

    # スコア1位の商品を「投稿済み」にする
    scored = pipeline.gather_candidates("product")
    top = scored[0].item
    pipeline.history.append(
        PostRecord(
            posted_at=datetime.now(JST).isoformat(timespec="seconds"),
            slot="noon", post_type="product", template_id="objective",
            status="success", text="過去の投稿", has_affiliate_link=True,
            item_code=top.item_code, item_url_hash=top.item_url_hash,
            affiliate_url_hash=top.affiliate_url_hash,
        )
    )
    pipeline.history._records = None  # 再読込

    draft = pipeline.run("noon").draft
    assert draft.primary_item.item_code != top.item_code


def test_ramp_up_limits_affiliate_posts_on_early_days(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    pipeline.state.operation_start_date()  # 1日目

    # ランプアップ1段目は「1日1本まで」
    assert pipeline.affiliate_allowed("noon") is True

    pipeline.history.append(
        PostRecord(
            posted_at=datetime.now(JST).isoformat(timespec="seconds"),
            slot="noon", post_type="product", template_id="objective",
            status="success", text="#PR投稿済み", has_affiliate_link=True,
        )
    )
    pipeline.history._records = None
    assert pipeline.affiliate_allowed("night") is False


def test_ramp_up_can_be_disabled(config, tmp_path):
    config.raw["ramp_up"]["enabled"] = False
    pipeline = make_pipeline(config, tmp_path)
    assert pipeline.affiliate_allowed("noon") is True


def test_consecutive_posts_use_different_templates(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    used = []
    for _ in range(4):
        result = pipeline.run("noon")
        pipeline.builder.commit(result.draft)
        used.append(result.draft.template_id)
    assert len(set(used)) > 1, f"同じテンプレートが連続しています: {used}"


def test_similar_text_is_rejected_and_regenerated(config, tmp_path):
    """直近と似た本文になったら、別テンプレートで作り直される。"""
    pipeline = make_pipeline(config, tmp_path)
    first = pipeline.run("noon")
    pipeline.builder.commit(first.draft)
    pipeline.history.append(
        PostRecord(
            posted_at=datetime.now(JST).isoformat(timespec="seconds"),
            slot="noon", post_type="product", template_id=first.draft.template_id,
            status="success", text=first.draft.text, has_affiliate_link=True,
            item_code=first.draft.primary_item.item_code,
        )
    )
    pipeline.history._records = None

    second = pipeline.run("noon")
    assert second.draft.text != first.draft.text


def test_generated_numbers_always_match_source_data(config, tmp_path):
    """全スロットで、本文の数値が商品データと一致していること。"""
    from src.content.facts import extract_numbers

    pipeline = make_pipeline(config, tmp_path)
    for slot in ("noon", "night", "late"):
        draft = pipeline.run(slot).draft
        allowed = set(draft.allowed_numbers)
        for item in draft.items:
            allowed |= pipeline.checker._allowed_numbers_for(item)
        urls = " ".join(
            u for u in draft.text.split() if u.startswith("http")
        )
        for token in extract_numbers(draft.text):
            assert token in allowed or token in extract_numbers(urls), (
                f"{slot}: 出所不明の数値 {token}\n{draft.text}"
            )


# ======================================================================
# DRY_RUN / cmd_post
# ======================================================================
class _Args:
    def __init__(self, slot):
        self.slot = slot
        self.dry_run = False
        self.live = False


def test_dry_run_does_not_post_and_records_dry_run(config, tmp_path, monkeypatch):
    config.dry_run = True

    posted = []

    class _NoPost:
        def __init__(self, *a, **k):
            pass

        def post_text(self, text, **kwargs):
            posted.append(text)
            raise AssertionError("DRY_RUN では投稿してはいけない")

        def post_thread(self, segments, **kwargs):
            posted.extend(segments)
            raise AssertionError("DRY_RUN では投稿してはいけない")

    monkeypatch.setattr("src.main.ThreadsClient", _NoPost)
    monkeypatch.setattr(
        "src.pipeline.RakutenClient", lambda config: FakeRakuten(pool())
    )

    assert cmd_post(config, _Args("noon")) == 0
    assert posted == []

    records = History(config.history_path).load()
    assert len(records) == 1
    assert records[0].status == "dry_run"
    assert "#PR" in records[0].text


def test_live_run_posts_and_records_post_id(config, tmp_path, monkeypatch):
    config.dry_run = False

    class _Published:
        post_id = "POST_ABC"
        permalink = "https://www.threads.net/@u/post/abc"
        verified = True

    class _Client:
        def __init__(self, *a, **k):
            pass

        def post_text(self, text, **kwargs):
            return _Published()

        def post_thread(self, segments, **kwargs):
            return [_Published() for _ in segments]

    monkeypatch.setattr("src.main.ThreadsClient", _Client)
    monkeypatch.setattr("src.pipeline.RakutenClient", lambda config: FakeRakuten(pool()))

    assert cmd_post(config, _Args("noon")) == 0

    records = History(config.history_path).load()
    assert records[0].status == "success"
    assert records[0].thread_post_id == "POST_ABC"
    assert records[0].permalink.startswith("https://www.threads.net/")
    # Secret が履歴に入っていないこと
    raw = config.history_path.read_text(encoding="utf-8")
    assert "hb.afl.rakuten.co.jp" not in raw


def test_compliance_skip_does_not_break_future_runs(config, tmp_path, monkeypatch):
    """1回スキップしても終了コード0（次回の定期実行を止めない）。"""
    class _AlwaysSkip:
        def __init__(self, *a, **k):
            pass

        def run(self, slot):
            raise ComplianceSkip("テスト用スキップ")

        def daily_cap_reached(self):
            return False

        builder = None

    monkeypatch.setattr("src.main.Pipeline", _AlwaysSkip)
    assert cmd_post(config, _Args("noon")) == 0

    records = History(config.history_path).load()
    assert records[0].status == "skipped"


def test_missing_threads_secret_returns_config_exit_code(config, tmp_path, monkeypatch):
    from src.errors import MissingSecretError

    config.dry_run = False

    class _NoToken:
        def __init__(self, *a, **k):
            pass

        def post_text(self, text, **kwargs):
            raise MissingSecretError(["THREADS_ACCESS_TOKEN"])

        def post_thread(self, segments, **kwargs):
            raise MissingSecretError(["THREADS_ACCESS_TOKEN"])

    monkeypatch.setattr("src.main.ThreadsClient", _NoToken)
    monkeypatch.setattr("src.pipeline.RakutenClient", lambda config: FakeRakuten(pool()))

    assert cmd_post(config, _Args("noon")) == 1


def test_transient_failure_returns_transient_exit_code(config, tmp_path, monkeypatch):
    from src.errors import TransientError

    config.dry_run = False

    class _Flaky:
        def __init__(self, *a, **k):
            pass

        def post_text(self, text, **kwargs):
            raise TransientError("一時障害")

        def post_thread(self, segments, **kwargs):
            raise TransientError("一時障害")

    monkeypatch.setattr("src.main.ThreadsClient", _Flaky)
    monkeypatch.setattr("src.pipeline.RakutenClient", lambda config: FakeRakuten(pool()))

    assert cmd_post(config, _Args("noon")) == 2
    assert History(config.history_path).load()[0].status == "failed"


def test_roundup_posts_use_one_category(config, tmp_path):
    """複数商品を並べる投稿は、見出しのカテゴリーと中身を一致させる。

    混ざっていると「ヘアケア、並べてみた」と言いながらメイクブラシが
    出てくることになり、読み手を誤解させる。
    """
    from tests.conftest import make_item

    mixed = []
    for i in range(4):
        mixed.append(make_item(item_code=f"h{i}:{i}", shop_code=f"h{i}",
                               genre_label="ヘアケア", review_count=500 + i))
    for i in range(4):
        mixed.append(make_item(item_code=f"m{i}:{i}", shop_code=f"m{i}",
                               genre_label="メイク", review_count=900 + i))

    pipeline = make_pipeline(config, tmp_path, items=mixed)
    for post_type in ("price_band", "review_heavy", "comparison"):
        scored = pipeline.gather_candidates(post_type)
        needed = 3 if post_type != "comparison" else 2
        ordered = pipeline._same_category_first(scored, needed)
        labels = {
            (s.item.raw or {}).get("_genre_label") for s in ordered[:needed]
        }
        assert len(labels) == 1, f"{post_type}: カテゴリーが混在 {labels}"


def test_same_category_first_falls_back_when_no_group_is_large_enough(config, tmp_path):
    """どのカテゴリーも件数が足りないときは、元の順序を壊さない。"""
    from tests.conftest import make_item

    items = [make_item(item_code=f"x{i}:{i}", shop_code=f"x{i}",
                       genre_label=f"cat{i}") for i in range(3)]
    pipeline = make_pipeline(config, tmp_path, items=items)
    scored = pipeline.gather_candidates("product")
    assert pipeline._same_category_first(scored, 3) == scored


# ======================================================================
# 商品が用意できないときのフォールバック
# ======================================================================
def test_falls_back_to_topic_post_when_no_products(config, tmp_path):
    """商品が1件も取れなくても、投稿枠は埋める。

    30日クールダウンで候補が尽きたり除外で全滅したりしたときに
    投稿が丸ごと消えると、アカウントの更新が止まって見える。
    """
    # make_pipeline は `items or pool()` なので、空リストだと既定プールに
    # すり替わってしまう。ここは本当に0件の状態を作りたいので直接組む。
    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=FakeRakuten([]),
    )
    result = pipeline.run("noon")

    assert result.check.passed
    assert result.draft.post_type == "no_link"
    assert not result.draft.has_affiliate_link
    assert result.draft.text.strip()


def test_falls_back_when_every_product_is_excluded(config, tmp_path):
    banned = [
        make_item(item_code=f"s{i}:{i}", item_name=f"【第2類医薬品】テスト薬{i}", shop_code=f"s{i}")
        for i in range(5)
    ]
    pipeline = make_pipeline(config, tmp_path, items=banned)
    result = pipeline.run("night")

    assert result.draft.post_type == "no_link"
    assert result.check.passed


def test_falls_back_when_all_products_are_in_cooldown(config, tmp_path):
    """全候補がクールダウン中でも投稿は出る。"""
    items = pool(3)
    pipeline = make_pipeline(config, tmp_path, items=items)
    for item in items:
        pipeline.history.append(
            PostRecord(
                posted_at=datetime.now(JST).isoformat(timespec="seconds"),
                slot="noon", post_type="product", template_id="objective",
                status="success", text="過去の投稿", has_affiliate_link=True,
                item_code=item.item_code, item_url_hash=item.item_url_hash,
                affiliate_url_hash=item.affiliate_url_hash,
            )
        )
    pipeline.history._records = None

    result = pipeline.run("noon")
    assert result.draft.post_type == "no_link"


# ======================================================================
# 候補が薄くなったときの自動補充
# ======================================================================
class CountingRakuten:
    """呼ばれるたびに別の商品を返すスタブ。補充が効いているかを見る。"""

    def __init__(self, per_call=8):
        self.per_call = per_call
        self.seeds = []

    def collect_candidates(self, genres, **kwargs):
        seed = kwargs.get("rotation_seed")
        self.seeds.append(seed)
        offset = len(self.seeds) * 100
        return [
            make_item(item_code=f"shop{offset + i}:{i}", shop_code=f"shop{offset + i}")
            for i in range(self.per_call)
        ]


def test_pool_is_refilled_when_candidates_are_thin(config, tmp_path):
    """使える候補が少ないと、別の層を取りに行って積み増す。"""
    rakuten = CountingRakuten(per_call=8)   # 1回では min_usable_candidates に届かない
    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=rakuten,
    )

    scored = pipeline.gather_candidates("product")

    assert len(rakuten.seeds) > 1, "補充が行われていない"
    assert rakuten.seeds[0] is None            # 1回目は日替わりの既定値
    assert all(s is not None for s in rakuten.seeds[1:])
    assert len(set(rakuten.seeds[1:])) == len(rakuten.seeds[1:]), "同じ層を引き直している"
    assert len(scored) > 8, "積み増した分が反映されていない"


def test_pool_is_not_refilled_when_enough(config, tmp_path):
    """十分あるときは余計なリクエストを投げない（1秒1リクエスト制限のため）。"""
    rakuten = CountingRakuten(per_call=40)
    pipeline = Pipeline(
        config,
        history=History(tmp_path / "history.jsonl"),
        state=State(tmp_path / "state.json"),
        rakuten=rakuten,
    )

    pipeline.gather_candidates("product")
    assert len(rakuten.seeds) == 1


def test_product_info_is_confined_to_the_last_post(config, tmp_path):
    """商品名・広告表示・リンクは最後の1本だけ。

    タイムラインに出るのは1本目なので、そこは単体で読み物として
    成立させる。前振りには商品を出さない。
    """
    pipeline = make_pipeline(config, tmp_path)
    for template_id in ("thread", "objective", "short", "checklist", "band_focus"):
        draft = pipeline.builder.build("product", pool(3)[:1], template_id=template_id)
        # リンク投稿では容量を落とした名前を使う
        name = draft.items[0].display_name_without_volume(38)

        assert draft.segments[-1].startswith("#PR"), template_id
        assert name in draft.segments[-1], template_id

        for index, segment in enumerate(draft.segments[:-1]):
            assert "#PR" not in segment, f"{template_id}: {index + 1}本目にPR表記"
            assert name not in segment, f"{template_id}: {index + 1}本目に商品名"
            assert "http" not in segment, f"{template_id}: {index + 1}本目にURL"


def test_link_posts_do_not_show_price_or_reviews(config, tmp_path):
    """値段・レビュー・送料を本文に出さない。

    スペック表に見えるので外した。商品名に含まれる容量（「10g」）は
    商品名の一部なので残る。ここで見たいのは、こちらが数値を
    「主張として」置いていないこと。
    """
    pipeline = make_pipeline(config, tmp_path)
    for template_id in ("thread", "objective", "short", "checklist", "band_focus"):
        draft = pipeline.builder.build("product", pool(3)[:1], template_id=template_id)
        item = draft.items[0]
        last = draft.segments[-1]

        # 商品名の部分を取り除いてから検査する
        rest = last.replace(item.display_name_without_volume(38), "")
        assert f"{item.item_price:,}" not in rest, f"{template_id}: 値段が出ている"
        assert "円" not in rest, f"{template_id}: 値段が出ている — {rest}"
        assert "レビュー" not in rest, f"{template_id}: レビューが出ている"
        assert "送料" not in rest, f"{template_id}: 送料が出ている"


def test_segment_count_varies_by_template(config, tmp_path):
    """本数は固定しない。テンプレートによって2〜4本と変わる。"""
    pipeline = make_pipeline(config, tmp_path)
    counts = {
        template_id: len(
            pipeline.builder.build("product", pool(3)[:1], template_id=template_id).segments
        )
        for template_id in ("short", "thread", "objective", "checklist", "band_focus")
    }
    assert counts["short"] == 2
    assert counts["checklist"] == 4
    assert len(set(counts.values())) >= 3, f"本数に幅がない: {counts}"


def test_tips_stage_is_never_empty(config, tmp_path):
    """剤形が判定できない商品でも、ノウハウ段を空にしない。"""
    pipeline = make_pipeline(config, tmp_path)
    brush = make_item(item_code="b:1", item_name="メイクブラシ 5本セット", shop_code="b")

    draft = pipeline.builder.build("product", [brush], template_id="thread")
    assert len(draft.segments) == 3
    assert "・" in draft.segments[1], f"箇条書きが無い: {draft.segments[1]}"


def test_every_thread_segment_fits_the_platform_limit(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    draft = pipeline.builder.build("product", pool(3)[:1], template_id="thread")
    for segment in draft.segments:
        assert 0 < len(segment) <= 500


def test_link_less_slot_does_not_consume_a_product(config, tmp_path):
    """リンクを付けられないときに商品を焼かないこと。

    リンクの無い商品投稿は読者が買えず収益にもならないのに、
    「投稿済み」として記録されて30日クールダウンに入ってしまう。
    ランプアップ中はリンク枠が絞られるので、放置すると初期に
    20件前後の商品を無駄に消費する。
    """
    pipeline = make_pipeline(config, tmp_path)
    pipeline.state.operation_start_date()

    # ランプアップ1段目（1日1本）の枠を使い切らせる
    pipeline.history.append(
        PostRecord(
            posted_at=datetime.now(JST).isoformat(timespec="seconds"),
            slot="noon", post_type="product", template_id="thread",
            status="success", text="#PR 済み", has_affiliate_link=True,
            item_code="used:1",
        )
    )
    pipeline.history._records = None

    draft = pipeline.run("night").draft

    assert draft.post_type == "no_link"
    assert draft.items == [], "リンクなしなのに商品を消費している"
    assert "円" not in draft.text, "リンクなしなのに商品情報が出ている"


def test_link_allowed_slot_still_posts_products(config, tmp_path):
    """リンクが付けられるときは、これまで通り商品投稿を出す。"""
    pipeline = make_pipeline(config, tmp_path)
    pipeline.state.operation_start_date()

    draft = pipeline.run("noon").draft
    assert draft.post_type == "product"
    assert draft.items
    assert draft.link_attachment
    assert "#PR" in draft.text


# ======================================================================
# つぶやき投稿
# ======================================================================
def test_casual_posts_appear_in_rotation(config, tmp_path):
    """役に立つ話だけにならないよう、つぶやきが混ざること。"""
    pipeline = make_pipeline(config, tmp_path)
    seen = [pipeline.resolve_post_type("morning") for _ in range(7)]
    assert "casual" in seen
    assert "no_link" in seen


def test_casual_post_has_no_product_and_no_link(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    draft = pipeline.builder.build("casual", [], with_affiliate_link=False)

    assert draft.items == []
    assert draft.link_attachment is None
    assert "http" not in draft.text
    assert draft.text.strip()


def test_casual_murmurs_are_short_and_clean():
    """つぶやきは短く、NG表現も数値も含まない。"""
    from src.compliance.rules import scan
    from src.content.parts import CASUAL_MURMURS

    assert len(CASUAL_MURMURS) >= 30
    for part in CASUAL_MURMURS:
        assert not scan(part.text), f"{part.id}: {[h.label for h in scan(part.text)]}"
        assert not any(c.isdigit() for c in part.text), f"{part.id} に数値"
        assert len(part.text) <= 120, f"{part.id} が長すぎる（つぶやきは短く）"


def test_link_never_appears_on_the_first_post(config, tmp_path):
    """タイムラインに出る1本目にリンクを載せない。

    Threads は外部リンクのある投稿の表示回数を落とす。
    実測でも、リンク付き投稿だけ同時間帯の他より2桁少なかった。
    """
    pipeline = make_pipeline(config, tmp_path)
    for template_id in ("thread", "objective", "short", "checklist", "band_focus"):
        draft = pipeline.builder.build("product", pool(3)[:1], template_id=template_id)
        assert draft.link_attachment, template_id
        assert len(draft.segments) >= 2, f"{template_id}: リンクが分離されていない"
        assert "http" not in draft.segments[0], template_id


def test_topic_tags_are_occasional_not_every_post(config, tmp_path):
    """全投稿にタグを付けると宣伝アカウントの見た目になる。

    人は毎回タグを付けない。付ける投稿と付けない投稿が混ざるのが自然。
    """
    from src.content.facts import topic_tag_for

    product = [topic_tag_for("スキンケア", i, "product") for i in range(8)]
    no_link = [topic_tag_for("スキンケア", i, "no_link") for i in range(8)]

    assert any(product) and not all(product), "商品投稿のタグが全付け/全無し"
    assert any(no_link) and not all(no_link), "リンクなし投稿のタグが全付け/全無し"
    # 商品投稿のほうが発見導線が要るので、付ける頻度は高め
    assert sum(1 for t in product if t) > sum(1 for t in no_link if t)


def test_casual_posts_never_get_a_topic_tag():
    """「今日の湿気」にコスメタグが付くのは明らかに不自然。"""
    from src.content.facts import topic_tag_for

    assert all(topic_tag_for("スキンケア", i, "casual") is None for i in range(10))


def test_topic_tags_rotate(config, tmp_path):
    """同じタグに偏らないこと。"""
    from src.content.facts import topic_tag_for

    seen = {topic_tag_for("スキンケア", i) for i in range(3)}
    assert len(seen) == 3


# ======================================================================
# 1日の総投稿数の上限
# ======================================================================
def test_daily_cap_stops_posting(config, tmp_path):
    """種別もトリガーも問わず、1日の総投稿数で頭打ちにする。

    ランプアップはリンク投稿しか見ていなかったため、動作確認の手動実行が
    積み上がって1日18件投稿し、Meta に API アクセスをブロックされた。
    """
    pipeline = make_pipeline(config, tmp_path)
    limit = int(config.ramp_up["max_posts_per_day"])

    assert pipeline.daily_cap_reached() is False

    for i in range(limit):
        pipeline.history.append(
            PostRecord(
                posted_at=datetime.now(JST).isoformat(timespec="seconds"),
                slot="noon", post_type="no_link", template_id="topic",
                status="success", text=f"投稿{i}", has_affiliate_link=False,
                extra={"trigger": "workflow_dispatch"},   # 手動でも数える
            )
        )
    pipeline.history._records = None

    assert pipeline.daily_cap_reached() is True


def test_daily_cap_counts_manual_runs_too(config, tmp_path):
    """リンク枠と違い、こちらは手動実行も数える（ブロック対策のため）。"""
    pipeline = make_pipeline(config, tmp_path)
    for i in range(int(config.ramp_up["max_posts_per_day"])):
        pipeline.history.append(
            PostRecord(
                posted_at=datetime.now(JST).isoformat(timespec="seconds"),
                slot="noon", post_type="no_link", template_id="topic",
                status="success", text=f"手動{i}", has_affiliate_link=False,
                extra={"trigger": "manual"},
            )
        )
    pipeline.history._records = None
    assert pipeline.daily_cap_reached() is True


def test_link_posts_use_recommending_tone(config, tmp_path):
    """リンク投稿で「どう思う？」と問いかけない。

    薦めたいのか意見がほしいのか分からなくなるため、
    リンクがあるときは薦める側の温度感にする。
    """
    pipeline = make_pipeline(config, tmp_path)
    for template_id in ("thread", "objective", "short", "checklist", "band_focus"):
        draft = pipeline.builder.build("product", pool(3)[:1], template_id=template_id)
        assert draft.link_attachment, template_id
        for index, segment in enumerate(draft.segments):
            assert "どう思う" not in segment, f"{template_id}: {index + 1}本目に問いかけ"
            assert "みんなならどうする" not in segment, template_id


def test_link_posts_hide_volume_numbers(config, tmp_path):
    """容量・入数の数値も出さない。"""
    pipeline = make_pipeline(config, tmp_path)
    item = make_item(
        item_code="v:1", item_name="サロンプレミアム トリートメント 300mL", shop_code="v"
    )
    draft = pipeline.builder.build("product", [item], template_id="short")

    assert "300" not in draft.segments[-1]
    assert "mL" not in draft.segments[-1]
    assert "サロンプレミアム" in draft.segments[-1]
