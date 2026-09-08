"""[autoreply] 設定の読み込み。

いちばん大事なのは **[autoreply] が無くても既存の運用が壊れないこと**。
必須セクションに足すと、このセクションを書いていない config.toml が
読めなくなり、投稿パイプラインごと止まる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import TargetAccount, load_config

MINIMAL = """
[rakuten]
endpoint = "https://example.test"
origin = "https://example.test"
[threads]
api_base = "https://example.test"
[selection]
min_price = 500
[exclusion]
keywords = []
[scoring]
[dedup]
[compliance]
pr_marker = "#PR"
[[schedule]]
slot = "noon"
time_jst = "12:15"
cron_utc = "15 3 * * *"
post_type = "product"
allow_affiliate = true
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_without_an_autoreply_section(tmp_path):
    """既存の config.toml をそのまま読めること。"""
    config = load_config(_write(tmp_path, MINIMAL), data_dir=tmp_path)
    assert config.autoreply == {}
    assert config.autoreply_targets == []
    assert config.autoreply_section("llm") == {}


def test_reads_nested_sections(tmp_path):
    config = load_config(
        _write(tmp_path, MINIMAL + """
[autoreply]
enabled = true
max_per_run = 2
[autoreply.llm]
model = "haiku"
[autoreply.filter]
max_age_hours = 6
"""),
        data_dir=tmp_path,
    )
    assert config.autoreply["enabled"] is True
    assert config.autoreply_section("llm")["model"] == "haiku"
    assert config.autoreply_section("filter")["max_age_hours"] == 6


def test_parses_target_accounts(tmp_path):
    config = load_config(
        _write(tmp_path, MINIMAL + """
[[autoreply.targets]]
username = "@with_at_sign"
note = "テスト"
[[autoreply.targets]]
username = "plain"
"""),
        data_dir=tmp_path,
    )
    assert config.autoreply_targets == [
        TargetAccount(username="with_at_sign", note="テスト", weight=1.0),
        TargetAccount(username="plain", note="", weight=1.0),
    ]


def test_skips_target_entries_without_a_username(tmp_path):
    """コメントアウトの書き損じで空エントリが残っても落ちない。"""
    config = load_config(
        _write(tmp_path, MINIMAL + """
[[autoreply.targets]]
note = "username を書き忘れた"
"""),
        data_dir=tmp_path,
    )
    assert config.autoreply_targets == []


def test_local_asset_paths_live_under_ignored_directories(tmp_path):
    """**公開リポジトリに載せてはいけないもの。**

    data/engage/ と .playwright/ は .gitignore 済み。
    パスがそこから外れたら、次の `git add data/` で公開される。
    """
    config = load_config(_write(tmp_path, MINIMAL), data_dir=tmp_path)
    assert config.engage_db_path.parent.name == "engage"
    assert config.engage_db_path.parent.parent == tmp_path
    assert config.autoreply_stop_path.parent == config.engage_db_path.parent
    assert ".playwright" in config.browser_profile_dir.parts


def test_browser_profile_dir_resolves_relative_to_the_repository(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL), data_dir=tmp_path)
    assert config.browser_profile_dir.is_absolute()


def test_browser_profile_dir_honours_an_absolute_override(tmp_path):
    config = load_config(
        _write(tmp_path, MINIMAL + f"""
[autoreply.browser]
profile_dir = "{tmp_path / 'elsewhere'}"
"""),
        data_dir=tmp_path,
    )
    assert config.browser_profile_dir == tmp_path / "elsewhere"


def test_the_cli_still_requires_live_to_post():
    """**投稿するには鍵が2つ要る。**

    2026-09-06 に運用を始めたので `enabled` は true になった。
    残った鍵は `--live` で、こちらは既定で降りている。
    `_autoreply_run` は `dry_run = not args.live` なので、
    ここが False の間は環境変数 DRY_RUN に関わらず投稿しない。
    """
    from src.main import build_parser

    args = build_parser().parse_args(["autoreply"])
    assert args.live is False, "--live を付けずに投稿できてはいけない"
    assert build_parser().parse_args(["autoreply", "--live"]).live is True


def test_every_target_account_says_why_it_is_there():
    """**理由の書いていない返信先を溜めない。**

    誰に返すかはこのアカウントの見え方を決める。あとから見て
    «なぜこの人に返しているのか» が分からない状態にしない。
    """
    for target in load_config().autoreply_targets:
        assert target.note.strip(), f"@{target.username} に note が無い"


def test_target_accounts_are_not_borrowed_from_the_style_references():
    """research/accounts.md は «文体の参考先» であって返信先ではない。

    あちらは体験談・効能断定・美容医療で伸ばしているアカウントを含む。
    そこへ返信すると触ってはいけない話題に踏み込む。
    """
    from pathlib import Path
    import re

    path = Path(__file__).resolve().parent.parent / "research" / "accounts.md"
    if not path.is_file():
        return
    referenced = set(re.findall(r"/@([A-Za-z0-9_.]+)", path.read_text(encoding="utf-8")))
    targets = {t.username for t in load_config().autoreply_targets}
    assert not (targets & referenced), f"文体の参考先が返信先に混ざっている: {targets & referenced}"


@pytest.mark.parametrize(
    "section",
    ["filter", "buzz", "browser", "llm", "sources", "ramp_up", "daily_quota"])
def test_shipped_config_defines_every_subsection(section):
    assert load_config().autoreply_section(section) != {}


# ======================================================================
# 一緒に動かすべき数値が離れないようにする
# ======================================================================
def test_the_source_quotas_fit_inside_the_shared_daily_cap():
    """**枠の合計が全体上限を超えていたら、設定が嘘をついている。**

    dedup の similarity_window と同じ扱い — 片方だけ動かすと
    静かに矛盾するので、テストで縛る。
    """
    config = load_config()
    quotas = {k: v for k, v in config.autoreply_section("daily_quota").items()
              if k != "default"}
    total = sum(int(v) for v in quotas.values())
    cap = int(config.engagement["max_per_day"])
    assert total <= cap, f"枠の合計 {total} が上限 {cap} を超えている"


def test_every_enabled_source_has_a_quota():
    """収集元を足したのに枠を書き忘れる、を防ぐ。"""
    config = load_config()
    quotas = config.autoreply_section("daily_quota")
    for source in config.autoreply_section("sources")["enabled"]:
        assert source in quotas, f"{source} の枠が無い"


def test_every_ramp_stage_names_every_enabled_source():
    """**ランプを素通りする収集元を作らない。**"""
    config = load_config()
    enabled = set(config.autoreply_section("sources")["enabled"])
    for stage in config.autoreply_section("ramp_up")["stages"]:
        named = set(stage) - {"until_day"}
        assert enabled <= named, f"{stage} が {enabled - named} を指定していない"


def test_the_reply_interval_is_longer_than_the_page_interval():
    """**別物なので統合しない。**

    min_interval_seconds はページ遷移の間隔（秒）、
    min_reply_interval_minutes は返信と返信の間隔（分）。
    """
    auto = load_config().autoreply
    page_seconds = float(auto["min_interval_seconds"])
    reply_seconds = float(auto["min_reply_interval_minutes"]) * 60
    assert reply_seconds > page_seconds * 100


def test_the_monitored_account_may_be_replied_to_more_than_once_a_day():
    """要件: その日バズっている投稿に最大3件。

    タイムライン側のクールダウンは維持されていること。
    """
    config = load_config()
    assert config.autoreply_filter("accounts")["same_account_cooldown_days"] == 0
    assert config.autoreply_filter("accounts")["same_account_cooldown_hours"] > 0
    assert config.autoreply_filter("timeline")["same_account_cooldown_days"] >= 7


def test_the_ranking_prefers_reaction_count():
    """要件: 反応数優先。ただし上限あり。"""
    config = load_config()
    buzz = config.autoreply_section("buzz")
    assert buzz["reaction_weight"] > buzz["base_weight"]
    assert buzz["reaction_weight"] > buzz["velocity_weight"]
    for source in ("timeline", "accounts"):
        assert config.autoreply_filter(source)["max_replies"] > 0


def test_the_reply_ceiling_favours_posts_where_a_reply_is_read():
    """**「一番バズっている投稿」と「返信が読まれる投稿」は別物。**

    返信が100件付いた投稿に足しても、誰もそこまでスクロールしない。
    2026-09-07 に 100 → 50 へ下げた。
    """
    config = load_config()
    assert config.autoreply_filter("timeline")["max_replies"] <= 50
    assert config.autoreply_filter("search")["max_replies"] <= 50


def test_search_has_a_real_share_of_the_daily_quota():
    """タイムラインの中身はフォローで決まる。検索はそれに依存しない。

    目的（コスメの露出）に対しては検索のほうが素直に当たるので、
    枠を «おまけ» にしない。
    """
    config = load_config()
    quota = config.autoreply_section("daily_quota")
    assert quota["search"] >= quota["timeline"] * 0.8
