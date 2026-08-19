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
    assert draft.text.startswith("#PR")
    assert "hb.afl.rakuten.co.jp" in draft.text


def test_late_slot_rotates_post_types(config, tmp_path):
    pipeline = make_pipeline(config, tmp_path)
    expected = config.rotation["late"]
    seen = [pipeline.resolve_post_type("late") for _ in range(len(expected))]
    assert seen == expected


def test_excluded_products_are_never_selected(config, tmp_path):
    """除外対象しか無ければ、投稿せずスキップする。"""
    banned = [
        make_item(item_code=f"s{i}:{i}", item_name=f"【第2類医薬品】テスト薬{i}", shop_code=f"s{i}")
        for i in range(5)
    ]
    pipeline = make_pipeline(config, tmp_path, items=banned)
    with pytest.raises(Exception) as exc:
        pipeline.run("noon")
    assert exc.type.__name__ in {"NoDataError", "ComplianceSkip"}


def test_recently_posted_item_is_skipped(config, tmp_path):
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

    monkeypatch.setattr("src.main.ThreadsClient", _NoPost)
    monkeypatch.setattr(
        "src.pipeline.RakutenClient", lambda config: FakeRakuten(pool())
    )

    assert cmd_post(config, _Args("noon")) == 0
    assert posted == []

    records = History(config.history_path).load()
    assert len(records) == 1
    assert records[0].status == "dry_run"
    assert records[0].text.startswith("#PR")


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

    monkeypatch.setattr("src.main.ThreadsClient", _Flaky)
    monkeypatch.setattr("src.pipeline.RakutenClient", lambda config: FakeRakuten(pool()))

    assert cmd_post(config, _Args("noon")) == 2
    assert History(config.history_path).load()[0].status == "failed"
