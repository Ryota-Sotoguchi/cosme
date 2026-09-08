"""全体の流れの検査。**ブラウザも LLM も起動しない。**

収集 → 既読を除く → 順位付け → 対象判定 → 生成 → 審査 → 再確認 →
投稿 → 記録 を、フェイクを差し込んで端から端まで通す。

いちばん見たいのは:
  * DRY_RUN で投稿も記録もしないこと
  * 投稿するには鍵が2つ要ること
  * 1件が落ちても次の候補へ進むこと
  * 日次上限を手動返信と共有していること
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from datetime import datetime, timedelta

from src.config import load_config
from src.engage.budget import JST
from src.engage.candidates import Candidate
from src.engage.executor import ExecuteResult
from src.engage.llm import LlmResponse
from src.engage.review import EngagementLog
from src.engage.runner import EngageRunner
from src.engage.store import EngageStore

BODY = "無印の化粧水しか使ってないけど、そろそろ何か足したほうがいいのかな。プチプラで探してる"
GOOD_REPLY = "無印だけでも十分だと思います〜。足すなら日焼け止めからがいいって聞きました"
ANOTHER_REPLY = "朝はいつもぎりぎりなので、増やすほど続かなくなっちゃうんですよね〜"

GOOD_TARGET = {"relevance": 0.9, "opportunity": 0.9, "reach": 0.8, "risk": 0.05,
               "score": 0.9, "recommended": True, "reason": "コスメの相談"}
GOOD_JUDGE = {"naturalness": 0.9, "context_match": 0.9, "engagement": 0.85,
              "spam_risk": 0.05, "ai_smell": 0.1, "specific": True,
              "score": 0.9, "publish": True, "problems": [], "rewrite": ""}


def candidate(shortcode="ABC123", username="someone", **kwargs) -> Candidate:
    base = dict(username=username, shortcode=shortcode, text=BODY,
                likes=200, replies=12, age_hours=2.0, source="timeline")
    base.update(kwargs)
    return Candidate(**base)


class ScriptedLlm:
    """呼ばれた順に応答を返す。プロンプトの中身で分岐する。"""

    def __init__(self, *, target=None, reply=GOOD_REPLY, judge=None) -> None:
        self.target = target if target is not None else GOOD_TARGET
        self.reply = reply
        self.judge = judge if judge is not None else GOOD_JUDGE
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def ask(self, prompt: str, *, timeout_s: float | None = None) -> LlmResponse:
        if "返信して問題がないか" in prompt:
            self.calls.append("target")
            return LlmResponse(text="", data=self.target)
        if "落とす理由を探して" in prompt:
            self.calls.append("judge")
            return LlmResponse(text="", data=self.judge)
        self.calls.append("write")
        return LlmResponse(text="", data={"reply": self.reply, "touches": "無印"})


@dataclass
class FakeSession:
    """ブラウザの代わり。runner が触る面だけ持つ。"""

    closed: bool = False

    def goto(self, url: str, **kwargs) -> Any:
        return object()

    def dwell(self, page: Any) -> None:
        pass

    def type_delay(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSource:
    """収集元の代わり。"""

    name: str = "timeline"
    found: list[Candidate] = field(default_factory=list)
    error: Exception | None = None

    def collect(self, session, *, limit: int) -> list[Candidate]:
        if self.error:
            raise self.error
        return list(self.found)


@dataclass
class FakeExecutor:
    """投稿の代わり。何を渡されたか記録する。"""

    results: dict = field(default_factory=dict)
    default: ExecuteResult = ExecuteResult(True, our_reply_url="https://example.test/r",
                                           confirmed=True)
    calls: list[tuple[str, str, bool]] = field(default_factory=list)

    def post(self, session, cand, text, *, dry_run=True, own_username="") -> ExecuteResult:
        self.calls.append((cand.shortcode, text, dry_run))
        return self.results.get(cand.shortcode, self.default)


@pytest.fixture
def config(tmp_path):
    return load_config(data_dir=tmp_path)


@pytest.fixture
def store(tmp_path):
    with EngageStore(tmp_path / "engage.sqlite3") as s:
        yield s


@pytest.fixture
def log(tmp_path):
    return EngagementLog(tmp_path / "engagements.jsonl")


# 時刻を固定する。暦日の境目やランダムな間隔でテストがぶれないように。
# **日付は今日に合わせる。** 固定日付だと、翌日に走らせたとき
# store.replied_since()（実時刻で窓を切る）と噛み合わなくなる。
NOW = datetime.now(JST).replace(hour=12, minute=0, second=0, microsecond=0)


def build(config, store, log, *, llm=None, sources=None, executor=None,
          enabled=True, state=None, now=NOW) -> EngageRunner:
    config.raw.setdefault("autoreply", {})["enabled"] = enabled
    runner = EngageRunner(
        config, store=store, engagement_log=log,
        llm=llm or ScriptedLlm(), state=state,
        session_factory=FakeSession, now=now)
    runner.executor = executor or FakeExecutor()
    found = sources if sources is not None else [candidate()]
    runner._collect = lambda session, only, report: (  # type: ignore[method-assign]
        report.__setattr__("collected", len(found))
        or store.filter_actionable(found, within_days=runner.seen_ttl_days))
    return runner


# ======================================================================
# 通しで動くか
# ======================================================================
def test_dry_run_plans_a_reply_without_posting(config, store, log):
    executor = FakeExecutor()
    report = build(config, store, log, executor=executor).run(dry_run=True)

    assert len(report.planned) == 1
    assert report.planned[0].draft.text == GOOD_REPLY
    assert executor.calls[0][2] is True  # dry_run で呼ばれている


def test_dry_run_records_nothing_in_the_reply_table(config, store, log):
    """**下見では記録しない。** 上限を無駄に食わないため。"""
    build(config, store, log).run(dry_run=True)
    assert store.replied_shortcodes() == set()
    assert log.replied_today() == 0


def test_live_posts_and_records_to_both_stores(config, store, log):
    report = build(config, store, log).run(dry_run=False)

    assert len(report.posted) == 1
    assert store.replied_shortcodes() == {"ABC123"}
    # JSONL は手動返信との共有台帳。両方に書く。
    assert log.replied_today() == 1

    row = store.recent_replies()[0]
    assert row.reply_text == GOOD_REPLY
    assert row.confirmed is True
    assert row.our_reply_url == "https://example.test/r"
    assert row.buzz_score is not None
    assert row.reply_score == pytest.approx(0.9)


def test_dry_run_and_live_plan_the_same_reply(config, store, log, tmp_path):
    dry = build(config, store, log).run(dry_run=True)

    with EngageStore(tmp_path / "second.sqlite3") as fresh_store:
        fresh_log = EngagementLog(tmp_path / "second.jsonl")
        live = build(config, fresh_store, fresh_log).run(dry_run=False)

    assert [p.candidate.shortcode for p in dry.planned] == \
           [p.candidate.shortcode for p in live.planned]


# ======================================================================
# 投稿するには鍵が2つ
# ======================================================================
def test_live_is_refused_when_the_config_switch_is_off(config, store, log):
    report = build(config, store, log, enabled=False).run(dry_run=False)
    assert report.posted == []
    assert "enabled" in report.stopped


def test_dry_run_still_works_with_the_switch_off(config, store, log):
    """設定を触らずに下見できること。"""
    report = build(config, store, log, enabled=False).run(dry_run=True)
    assert len(report.planned) == 1


def test_the_stop_file_halts_everything(config, store, log):
    """設定を編集せずに止められる。"""
    config.autoreply_stop_path.parent.mkdir(parents=True, exist_ok=True)
    config.autoreply_stop_path.write_text("止める", encoding="utf-8")

    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []
    assert "停止ファイル" in report.stopped


def test_the_environment_kill_switch_halts_everything(config, store, log, monkeypatch):
    """ファイルを置けない場面のためのもう1つの手。"""
    monkeypatch.setenv("AUTOREPLY_OFF", "1")
    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []
    assert "AUTOREPLY_OFF" in report.stopped


def test_an_unset_kill_switch_does_not_halt(config, store, log, monkeypatch):
    monkeypatch.setenv("AUTOREPLY_OFF", "")
    assert len(build(config, store, log).run(dry_run=True).planned) == 1


# ======================================================================
# 上限
# ======================================================================
def test_stops_at_the_daily_cap_shared_with_manual_replies(config, store, log):
    """**手で3件返した日は自動返信は動かない。**

    Meta のスパム規定が見るのは «そのアカウントが1日に何件返したか» で、
    どの経路から返したかではない。
    """
    cap = int(config.engagement["max_per_day"])
    for i in range(cap):
        log.append("manual_person", f"MANUAL{i}", "手で書いた返信")

    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []
    assert "上限" in report.stopped


def test_posts_at_most_max_per_run(config, store, log):
    """1回の実行で連投しない。数分内に複数返信は自動化の signal。"""
    many = [candidate(shortcode=f"S{i}", username=f"user{i}") for i in range(5)]
    report = build(config, store, log, sources=many).run(dry_run=False)
    assert len(report.posted) == int(config.autoreply["max_per_run"])


def test_an_explicit_limit_narrows_further(config, store, log):
    many = [candidate(shortcode=f"S{i}", username=f"user{i}") for i in range(5)]
    report = build(config, store, log, sources=many).run(dry_run=False, limit=1)
    assert len(report.posted) == 1


# ======================================================================
# 1件の問題で全体を止めない
# ======================================================================
def test_moves_to_the_next_candidate_when_verification_fails(config, store, log):
    """`Pipeline` と同じ方針。1商品の問題で運用全体を止めない。"""
    executor = FakeExecutor(results={
        "FIRST": ExecuteResult(False, "本文が変わっています"),
    })
    candidates = [
        candidate(shortcode="FIRST", username="a", likes=9999, replies=40),
        candidate(shortcode="SECOND", username="b"),
    ]
    report = build(config, store, log, sources=candidates,
                   executor=executor).run(dry_run=False)

    assert [c for c, _, _ in executor.calls] == ["FIRST", "SECOND"]
    assert len(report.posted) == 1
    assert report.posted[0][0].candidate.shortcode == "SECOND"
    assert any("本文が変わって" in r for _, r in report.skips("verify"))


def test_a_failed_verification_is_recorded_so_it_is_not_retried(config, store, log):
    executor = FakeExecutor(results={"ABC123": ExecuteResult(False, "削除されています")})
    build(config, store, log, executor=executor).run(dry_run=False)

    seen = store.previous_sighting("ABC123")
    assert seen.decision == "verify_failed"


# ======================================================================
# 除外
# ======================================================================
def test_skips_a_post_already_replied_to(config, store, log):
    store.record_reply(shortcode="ABC123", username="someone", reply_text="前回の返信")
    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []


def test_skips_someone_replied_to_recently(config, store, log):
    """同じ人に張り付かない。"""
    log.append("someone", "OTHER_POST", "先週の返信")
    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []


def test_skips_a_post_already_replied_to_by_decision(config, store, log):
    store.mark_seen(candidate(), decision="replied")
    report = build(config, store, log).run(dry_run=True)
    assert report.after_seen == 0
    assert report.planned == []


def test_an_llm_rejection_gets_another_look_on_a_later_run(config, store, log):
    """**LLM の判断はばらつく。** 1回で永久に捨てない。

    2026-09-06 実測: 同じ投稿を3回判定させたら 0.35 / 0.60 / 0.70 になった。
    """
    store.mark_seen(candidate(), decision="skipped_judge", reason="対象外")
    report = build(config, store, log).run(dry_run=True)
    assert len(report.planned) == 1, "却下された候補が二度と戻ってこない"


def test_a_post_rejected_by_a_rule_comes_back_when_it_grows(config, store, log):
    """**投稿は時間とともに伸びる。**

    「いいねが足りない」で1度落ちた投稿を45日間封印すると、
    伸び始めを狙うという目的そのものに反する。
    """
    quiet = candidate(shortcode="GROWING", likes=5)
    build(config, store, log, sources=[quiet]).run(dry_run=True)
    assert store.previous_sighting("GROWING").decision == "skipped_rule"

    grown = candidate(shortcode="GROWING", likes=400, replies=25)
    report = build(config, store, log, sources=[grown]).run(dry_run=True)
    assert len(report.planned) == 1


def test_rule_rejections_are_recorded_with_their_reason(config, store, log):
    """live でも同じ結果になるので記録して安全。次回の下見が速くなる。"""
    build(config, store, log,
          sources=[candidate(shortcode="SENSITIVE", text="整形のダウンタイムがつらい" + "あ" * 40)],
          ).run(dry_run=True)

    seen = store.previous_sighting("SENSITIVE")
    assert seen.decision == "skipped_rule"


# ======================================================================
# 伸びの速度が実際に効いているか
# ======================================================================
def test_observations_are_recorded_so_velocity_can_be_computed(config, store, log):
    """**このモジュールの中心。**

    観測を残さないと previous_sighting() が常に None になり、
    velocity（重み 2.0）が永久に 0 のままになる。
    """
    build(config, store, log).run(dry_run=True)

    seen = store.previous_sighting("ABC123")
    assert seen is not None
    assert seen.likes == 200
    assert seen.replies == 12


def test_the_second_run_can_see_the_growth_since_the_first(config, store, log):
    build(config, store, log, sources=[candidate(likes=200)]).run(dry_run=True)
    first = store.previous_sighting("ABC123")

    build(config, store, log, sources=[candidate(likes=900)]).run(dry_run=True)
    second = store.previous_sighting("ABC123")

    assert first.likes == 200
    assert second.likes == 900
    assert second.first_seen_at == first.first_seen_at  # 起点は動かさない


def test_a_failed_verification_is_not_recorded_during_a_preview(config, store, log):
    """**下見では記録しない。**

    verify_failed は終了状態なので、下見で書くとその候補が本番から
    永久に外れる。
    """
    executor = FakeExecutor(results={"ABC123": ExecuteResult(False, "一時的に開けません")})
    build(config, store, log, executor=executor).run(dry_run=True)

    seen = store.previous_sighting("ABC123")
    assert seen.decision != "verify_failed"


# ======================================================================
# 予算
# ======================================================================
def test_llm_calls_count_actual_invocations(config, store, log):
    """上限を実際より早く使い切らないこと。

    1回で通ったのに «上限ぶん使った» と数えると、実行が4割ほど早く止まる。
    """
    llm = ScriptedLlm()
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert report.llm_calls == len(llm.calls)


def test_llm_calls_include_the_retries(config, store, log):
    llm = ScriptedLlm()
    original = llm.ask
    seen = {"n": 0}

    def ask(prompt, *, timeout_s=None):
        if "**共感**" in prompt or "型:" in prompt:
            seen["n"] += 1
            if seen["n"] == 1:
                llm.calls.append("write")
                return LlmResponse(text="", data={"reply": "わかります！"})
        return original(prompt, timeout_s=timeout_s)

    llm.ask = ask
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert report.llm_calls == len(llm.calls)
    assert llm.calls.count("write") >= 2


# ======================================================================
# LLM の判断で落ちる
# ======================================================================
def test_skips_when_the_target_judge_rejects(config, store, log):
    llm = ScriptedLlm(target={**GOOD_TARGET, "recommended": False, "reason": "無関係"})
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert report.planned == []
    assert any("対象外" in r for _, r in report.skips("judge"))
    assert "write" not in llm.calls  # 対象外なら生成もしない


def test_skips_when_the_reply_judge_rejects(config, store, log):
    llm = ScriptedLlm(judge={**GOOD_JUDGE, "specific": False})
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert report.planned == []
    assert any("審査で却下" in r for _, r in report.skips("judge"))


def test_skips_when_no_draft_survives_the_local_checks(config, store, log):
    llm = ScriptedLlm(reply="わかります！")
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert report.planned == []
    assert any("返信案を作れません" in r for _, r in report.skips("judge"))


def test_a_fabricated_experience_never_reaches_the_executor(config, store, log):
    """**このアカウントは商品を使っていない。**"""
    executor = FakeExecutor()
    llm = ScriptedLlm(reply="わたしも使ってみたけど、すごく良かったです〜")
    build(config, store, log, llm=llm, executor=executor).run(dry_run=False)
    assert executor.calls == []


# ======================================================================
# 収集の失敗
# ======================================================================
def test_collect_only_stops_before_calling_the_llm(config, store, log):
    llm = ScriptedLlm()
    report = build(config, store, log, llm=llm).run(dry_run=True, collect_only=True)
    assert llm.calls == []
    assert report.ranked >= 1


def test_the_session_is_always_closed(config, store, log):
    sessions = []

    def factory():
        session = FakeSession()
        sessions.append(session)
        return session

    runner = build(config, store, log)
    runner._session_factory = factory
    runner.run(dry_run=True)
    assert sessions[0].closed is True


# ======================================================================
# 収集元ごとの足切り
# ======================================================================
def test_target_accounts_get_a_looser_filter_than_the_timeline(config):
    """**相手の素性が違う。**

    タイムラインは偶然流れてきた知らない投稿。監視対象は人が選んだ相手。
    同じ厳しさで見る理由がない（2026-09-05 実測で、監視対象の新着は
    いいねが2〜3しか付いておらず min_likes=20 で全滅した）。
    """
    from src.engage.buzz import passes_filter, resolve_filter

    section = config.autoreply_section("filter")
    fresh_but_quiet = candidate(shortcode="NEW", likes=2, replies=1, age_hours=1.0)

    fresh_but_quiet.source = "timeline"
    ok, reason = passes_filter(fresh_but_quiet, resolve_filter(section, "timeline"))
    assert ok is False and "反応が少ない" in reason

    fresh_but_quiet.source = "accounts"
    ok, _ = passes_filter(fresh_but_quiet, resolve_filter(section, "accounts"))
    assert ok is True


def test_a_quiet_new_post_passes_only_for_target_accounts(config):
    """**相手の素性が違うところだけ差を付ける。**

    2026-09-07 に max_age_hours は両方とも24時間に揃えた（ホームTLの
    中央値が20.6時間で、12時間だと候補がまったく出なかったため）。
    残る差は「反応がまだ無い投稿を拾うか」で、人が選んだ相手なら拾う。
    """
    from src.engage.buzz import passes_filter, resolve_filter

    section = config.autoreply_section("filter")
    quiet = candidate(shortcode="QUIET", likes=2, replies=0, age_hours=1.0)

    assert passes_filter(quiet, resolve_filter(section, "timeline"))[0] is False
    assert passes_filter(quiet, resolve_filter(section, "accounts"))[0] is True


def test_a_buried_post_is_refused_more_readily_on_the_timeline(config):
    """返信が埋もれる帯の上限も、監視対象のほうが緩い。"""
    from src.engage.buzz import passes_filter, resolve_filter

    section = config.autoreply_section("filter")
    busy = candidate(shortcode="BUSY", likes=500, replies=70, age_hours=3.0)

    assert passes_filter(busy, resolve_filter(section, "timeline"))[0] is False
    assert passes_filter(busy, resolve_filter(section, "accounts"))[0] is True


def test_the_filter_override_does_not_leak_into_other_sources(config):
    """入れ子の dict がそのまま条件として読まれないこと。"""
    from src.engage.buzz import resolve_filter

    section = config.autoreply_section("filter")
    resolved = resolve_filter(section, "timeline")
    assert all(not isinstance(v, dict) for v in resolved.values())
    # 上書きしていない収集元には、素の値がそのまま来ること
    assert resolved["min_likes"] == section["min_likes"]
    assert resolved["max_replies"] == section["max_replies"]


# ======================================================================
# 巡回するアカウントをずらす
# ======================================================================
def test_profile_visits_rotate_so_one_run_opens_only_one(config, tmp_path):
    """**1回に全員のプロフィールを開かない。**

    2026-09-05 に13件を続けて開いたら、Threads がプロフィールの表示を
    拒否するようになった。1件ずつずらして薄く回る。
    """
    from src.config import TargetAccount
    from src.engage.sources.accounts import AccountsSource
    from src.storage.state import State

    # 出荷設定の監視対象は1件なので、回転そのものはテスト内で4件を組んで見る。
    # 相手を増やしたときに回転が効かないと、同じ相手のプロフィールばかり開く。
    config.autoreply_targets = [
        TargetAccount(username=f"acct{i}", note="テスト") for i in range(4)
    ]
    state = State(tmp_path / "state.json")
    source = AccountsSource(config, state)
    assert source.per_run == 1

    seen = []
    for _ in range(4):
        picked = source._todays_targets()
        assert len(picked) == 1
        seen.append(picked[0].username)

    assert len(set(seen)) == 4, f"同じ相手を繰り返し開いている: {seen}"


def test_the_rotation_wraps_around_to_the_first_account(config, tmp_path):
    from src.config import TargetAccount
    from src.engage.sources.accounts import AccountsSource
    from src.storage.state import State

    config.autoreply_targets = [
        TargetAccount(username=f"acct{i}", note="テスト") for i in range(4)
    ]
    state = State(tmp_path / "state.json")
    source = AccountsSource(config, state)
    total = len(source.targets)

    seen = [source._todays_targets()[0].username for _ in range(total + 1)]
    assert seen[0] == seen[total], "一周して先頭に戻らない"
    assert len(set(seen[:total])) == total, "一周で全員を回れていない"


def test_without_state_the_rotation_still_returns_something(config):
    """state が無い経路（テストや --collect-only の一部）でも落ちない。"""
    from src.engage.sources.accounts import AccountsSource

    assert len(AccountsSource(config, None)._todays_targets()) == 1


def test_skips_are_separated_by_stage(config, store, log):
    """**ルールの却下と LLM の却下を混ぜない。**

    ルールで落ちるものは数が多く理由も似る。先頭から並べると、
    1件ずつ意味の違う LLM の判断が埋もれてしまう。
    """
    llm = ScriptedLlm(target={**GOOD_TARGET, "recommended": False, "reason": "独り言"})
    report = build(config, store, log, llm=llm, sources=[
        candidate(shortcode="TOO_SHORT", text="短い"),
        candidate(shortcode="JUDGED", username="other"),
    ]).run(dry_run=True)

    assert [c for c, _ in report.skips("rule")] == ["TOO_SHORT"]
    assert [c for c, _ in report.skips("judge")] == ["JUDGED"]
    assert report.skips("verify") == []


# ======================================================================
# 監視対象には1日に何度でも返す（要件2）
# ======================================================================
def _accounts_candidate(shortcode: str, **kwargs) -> Candidate:
    c = candidate(shortcode=shortcode, username="poco_insta_life", **kwargs)
    c.source = "accounts"
    return c


def backdate(store, log, *, hours: float) -> None:
    """記録済みの返信を過去へずらす。間隔ではなく別の条件を試したいとき用。

    **固定した NOW からの相対で戻す。** 実時刻から戻すと、深夜に走らせたときに
    «昨日» に落ちて暦日の枠判定が変わってしまう。
    """
    when = (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")
    with store._conn:
        store._conn.execute("UPDATE threads_replies SET replied_at = ?", (when,))
    if log.path.exists():
        rows = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        for r in rows:
            r["replied_at"] = when
        log.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")


def test_a_second_reply_to_the_monitored_account_is_allowed_the_same_day(
        config, store, log):
    """**その日バズっている投稿に最大3件**返す、が成り立つこと。

    以前は7日クールダウンが効いて、1件目を返した時点で相手ごと除外していた。
    """
    first = build(config, store, log, sources=[_accounts_candidate("A1")])
    assert len(first.run(dry_run=False).posted) == 1

    # 間隔ではなくクールダウンを試したいので、時刻を戻す。
    # 監視対象は same_account_cooldown_hours = 3 なので4時間戻す。
    backdate(store, log, hours=4)

    # 2件目は別の文にする。同じ文を続けて出すのは類似度検査が正しく弾く。
    second = build(config, store, log, sources=[_accounts_candidate("A2")],
                   llm=ScriptedLlm(reply=ANOTHER_REPLY))
    report = second.run(dry_run=False)
    assert len(report.posted) == 1, f"2件目が返せない: {report.skipped}"
    assert store.replied_shortcodes() == {"A1", "A2"}


def test_the_monitored_account_still_gets_a_short_breather(config, store, log):
    """1日に何度でも返すが、**立て続けには返さない。**

    狙い撃ちに見えるのは、レート制限とは別の失敗の仕方
    （相手にブロック・報告される）。
    """
    build(config, store, log, sources=[_accounts_candidate("A1")]).run(dry_run=False)
    backdate(store, log, hours=1)   # 全体の間隔は越えたが、同一相手の3時間は未達

    report = build(config, store, log,
                   sources=[_accounts_candidate("A2")]).run(dry_run=False)
    assert report.posted == []
    assert any("返したばかり" in r for _, r in report.skips("rule"))


def test_the_timeline_still_refuses_a_recently_replied_account(config, store, log):
    """**タイムライン側のクールダウンは維持する。**

    監視対象を緩めたことが、知らない相手への連投に波及しないこと。
    """
    log.append("someone", "OLD", "先に返信済み")
    report = build(config, store, log).run(dry_run=True)
    assert report.planned == []
    assert any("返したばかり" in r for _, r in report.skips("rule"))


def test_a_monitored_account_post_found_on_the_timeline_counts_as_accounts(
        config, store, log):
    """**「どこで見つけたか」ではなく「誰の投稿か」で決める。**

    監視対象の投稿はホームTLにも流れてくる。collect_all は timeline を先に
    連結し、rank_candidates は先勝ちで重複排除するので、放っておくと
    source="timeline" になり要件2が静かに壊れる。
    """
    found = [candidate(shortcode="P1", username="poco_insta_life")]
    assert found[0].source == "timeline"

    runner = EngageRunner(
        config, store=store, engagement_log=log, llm=ScriptedLlm(),
        state=None, session_factory=FakeSession)
    runner._retag_monitored(found)
    assert found[0].source == "accounts"


# ======================================================================
# 経路ごとの枠
# ======================================================================
def test_a_source_that_used_its_quota_never_reaches_the_llm(config, store, log):
    """**LLM を呼ぶ前に枠を見る。** 使い切った経路に費用をかけない。"""
    for i in range(config.autoreply_section("daily_quota")["timeline"]):
        store.record_reply(shortcode=f"T{i}", username=f"u{i}",
                           reply_text="返信", source="timeline")

    backdate(store, log, hours=4)   # 間隔ではなく «枠» を試したい

    llm = ScriptedLlm()
    report = build(config, store, log, llm=llm).run(dry_run=True)
    assert llm.calls == []
    assert any("timeline の本日の枠" in r for _, r in report.skips("rule"))


def test_the_report_carries_the_remaining_budget(config, store, log):
    report = build(config, store, log).run(dry_run=True)
    assert "timeline" in report.budget and "accounts" in report.budget


# ======================================================================
# 返信の間隔
# ======================================================================
def test_the_interval_pauses_replies_without_stopping_collection(config, store, log):
    """**見送りの回は無駄ではない。**

    velocity は同じ投稿を2回見ないと出ないので、この回が次の回の
    velocity を作る。だから収集と観測は続ける。
    """
    store.record_reply(shortcode="JUST_NOW", username="a",
                       reply_text="さっき返した", source="timeline")
    backdate(store, log, hours=0.1)   # 固定した NOW の6分前に返信したことにする

    llm = ScriptedLlm()
    report = build(config, store, log, llm=llm).run(dry_run=True)

    assert report.paused, "間隔の見送りが報告されていない"
    assert report.stopped == "", "stopped を使うと候補一覧が消える"
    assert report.ranked >= 1, "収集と順位付けは続けること"
    assert llm.calls == [], "見送りの回で LLM を呼ばない"
    assert report.posted == []
    assert store.previous_sighting("ABC123") is not None, "観測は残すこと"


# ======================================================================
# 制限されたら次の実行も止める
# ======================================================================
def test_the_throttle_signal_stops_the_next_run_too(config, store, log, tmp_path):
    """**信号をここで消すと、90分後の定期実行がそのまま突っ込む。**"""
    from src.errors import ThrottledError
    from src.storage.state import State

    state = State(tmp_path / "state.json")
    runner = EngageRunner(
        config, store=store, engagement_log=log, llm=ScriptedLlm(),
        state=state, session_factory=FakeSession)
    runner._collect = lambda session, only, report: (_ for _ in ()).throw(
        ThrottledError("プロフィールの表示を拒否されました"))

    report = runner.run(dry_run=True)
    assert "拒否" in report.stopped
    assert state.get("autoreply_throttled_until"), "待機時刻を残していない"

    fresh = EngageRunner(
        config, store=store, engagement_log=log, llm=ScriptedLlm(),
        state=state, session_factory=FakeSession)
    assert "絞られた" in fresh.stop_reason(dry_run=True)


def test_the_backoff_expires(config, store, log, tmp_path):
    from datetime import datetime, timedelta

    from src.engage.runner import JST
    from src.storage.state import State

    state = State(tmp_path / "state.json")
    state.set("autoreply_throttled_until",
              (datetime.now(JST) - timedelta(minutes=1)).isoformat(timespec="seconds"))
    runner = EngageRunner(
        config, store=store, engagement_log=log, llm=ScriptedLlm(),
        state=state, session_factory=FakeSession)
    assert "絞られた" not in runner.stop_reason(dry_run=True)


# ======================================================================
# 審査で落ちて終わりにしない（ゴールは返信するまで）
# ======================================================================
class JudgeThenPass:
    """最初の審査で落として、直し案を渡された2回目で通すLLM。"""

    def __init__(self, *, rounds_to_fail: int = 1) -> None:
        self.rounds_to_fail = rounds_to_fail
        self.judged = 0
        self.calls: list[str] = []
        self.write_prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def ask(self, prompt: str, *, timeout_s=None) -> LlmResponse:
        if "返信して問題がないか" in prompt:
            self.calls.append("target")
            return LlmResponse(text="", data=GOOD_TARGET)
        if "落とす理由を探して" in prompt:
            self.judged += 1
            self.calls.append("judge")
            if self.judged <= self.rounds_to_fail:
                return LlmResponse(text="", data={
                    **GOOD_JUDGE, "publish": False, "score": 0.4, "ai_smell": 0.45,
                    "problems": ["硬い言い回し"],
                    "rewrite": "もっとくだけた言い方にする",
                })
            return LlmResponse(text="", data=GOOD_JUDGE)
        self.calls.append("write")
        self.write_prompts.append(prompt)
        reply = ANOTHER_REPLY if self.judged else GOOD_REPLY
        return LlmResponse(text="", data={"reply": reply, "touches": "無印"})


def test_a_rejected_reply_is_rewritten_and_posted(config, store, log):
    """**審査で落ちて終わりにしない。**

    2026-09-07 の実測では、生成まで到達した1件が ai_smell 0.35 で落ち、
    そこで候補ごと捨てていた。せっかく «返信してよい» と判断した相手を
    毎回逃していた。
    """
    llm = JudgeThenPass(rounds_to_fail=1)
    report = build(config, store, log, llm=llm).run(dry_run=False)

    assert len(report.posted) == 1, f"書き直しても投稿できていない: {report.skipped}"
    assert llm.judged == 2, "審査が1回で終わっている"
    assert store.recent_replies()[0].reply_text == ANOTHER_REPLY


def test_the_rewrite_hint_reaches_the_writer(config, store, log):
    """審査は «こう書けばいい» まで返す。捨てずに使う。"""
    llm = JudgeThenPass(rounds_to_fail=1)
    build(config, store, log, llm=llm).run(dry_run=False)

    retry_prompt = llm.write_prompts[-1]
    assert "もっとくだけた言い方にする" in retry_prompt
    assert "硬い言い回し" in retry_prompt
    assert "丸写しはしない" in retry_prompt


def test_the_rewrite_loop_is_bounded(config, store, log):
    """**無限には回さない。** 通らないものは諦めて次の候補へ。"""
    llm = JudgeThenPass(rounds_to_fail=99)
    runner = build(config, store, log, llm=llm)
    report = runner.run(dry_run=False)

    assert report.posted == []
    assert llm.judged == runner.max_judge_rounds
    assert any("審査で却下" in r for _, r in report.skips("judge"))


# ======================================================================
# 検索という収集元
# ======================================================================
def test_the_search_source_is_registered(config):
    """**収集元は REGISTRY に1行足すだけで増やせる。** その確認も兼ねる。"""
    from src.engage.sources import REGISTRY, build_sources

    assert "search" in REGISTRY
    names = [s.name for s in build_sources(config)]
    assert "search" in names


def test_search_keywords_rotate_so_one_run_hits_only_a_few(config, tmp_path):
    """検索ページも開きすぎれば絞られる。1回に引く語を絞って回す。"""
    from src.engage.sources.search import SearchSource
    from src.storage.state import State

    state = State(tmp_path / "state.json")
    source = SearchSource(config, state)
    assert source.per_run < len(source.keywords)

    seen = []
    for _ in range(3):
        words = source._todays_keywords()
        assert len(words) == source.per_run
        seen += words
    assert len(set(seen)) == len(seen), f"同じ語を繰り返し引いている: {seen}"


def test_search_keywords_wrap_around(config, tmp_path):
    from src.engage.sources.search import SearchSource
    from src.storage.state import State

    source = SearchSource(config, State(tmp_path / "state.json"))
    rounds = len(source.keywords) // source.per_run
    seen = [w for _ in range(rounds) for w in source._todays_keywords()]
    assert set(seen) == set(source.keywords), "一周で全部の語を引けていない"


def test_search_without_state_still_works(config):
    from src.engage.sources.search import SearchSource

    assert len(SearchSource(config, None)._todays_keywords()) == 2


def test_search_candidates_are_tagged_with_their_source(config, store, log):
    """検索由来には検索の枠とフィルタが当たること。"""
    from src.engage.buzz import resolve_filter

    section = config.autoreply_section("filter")
    assert resolve_filter(section, "search")["max_replies"] == 50
    assert resolve_filter(section, "search")["same_account_cooldown_days"] == 7
