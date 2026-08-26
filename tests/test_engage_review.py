"""返信文の検査と記録のテスト。

返信は他人の投稿に残る。自分の投稿と違って後から直せないので、
出す前に機械で止められるものは止める。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.engage.review import JST, EngagementLog, review


# ======================================================================
# 出してはいけない返信
# ======================================================================
@pytest.mark.parametrize("text", [
    "これいいですよね https://example.com",
    "詳しくは www.example.com にあります",
    "楽天で買えますよ rakuten.co.jp",
])
def test_urls_are_rejected(text):
    """コメント欄へのリンクは楽天アフィリエイト規約で禁止。

    アフィリエイトリンクでなくても、宣伝に見えるので入れない。
    """
    result = review(text)
    assert not result.ok
    assert any("URL" in p for p in result.problems)


@pytest.mark.parametrize("text", [
    "わかります！",
    "いいですね！素敵です！",
    "気になります〜！参考になります🤍",
])
def test_template_only_replies_are_rejected(text):
    """誰にでも使える返信は弾く。

    その投稿を読んだことが伝わらないと、返信する意味がない。
    """
    result = review(text)
    assert not result.ok
    assert any("定型句" in p for p in result.problems)


def test_efficacy_claims_are_rejected():
    """薬機法はリンクの有無に関係なく効く。"""
    result = review("それ使うとシミが消えるらしいですよ")
    assert not result.ok
    assert any("NG表現" in p for p in result.problems)


@pytest.mark.parametrize("text", [
    "似たようなのまとめてるのでプロフから見てください",
    "わたしのアカウントでも紹介してます",
])
def test_self_promotion_is_rejected(text):
    result = review(text)
    assert not result.ok
    assert any("誘導" in p for p in result.problems)


def test_long_replies_are_rejected():
    result = review("あ" * 200)
    assert not result.ok
    assert any("長すぎ" in p for p in result.problems)


def test_empty_is_rejected():
    assert not review("").ok
    assert not review("   ").ok


# ======================================================================
# 通ってよい返信
# ======================================================================
@pytest.mark.parametrize("text", [
    "無印だけでも十分だと思います〜。敏感肌だと足すほど荒れることあるので",
    "これ気になってました！乾燥する時期でも使いやすそうですか？",
    "わかります、名前で選べないやつ多すぎますよね😌",
    "同じの探してました。詰め替えってありましたっけ",
])
def test_specific_replies_pass(text):
    result = review(text)
    assert result.ok, result.summary()


def test_template_words_are_fine_with_substance():
    """定型句を使うこと自体は問題ない。それだけで終わらせないこと。"""
    assert review("わかります！わたしも詰め替えの袋あけるの下手です").ok


# ======================================================================
# 記録
# ======================================================================
def test_log_records_and_reads_back(tmp_path):
    log = EngagementLog(tmp_path / "e.jsonl")
    log.append("someone", "ABC123", "テスト返信")
    entries = log.all()
    assert len(entries) == 1
    assert entries[0].username == "someone"
    assert entries[0].shortcode == "ABC123"


def test_log_redacts_urls(tmp_path):
    """data/ は公開されるので、記録にURLを残さない。"""
    log = EngagementLog(tmp_path / "e.jsonl")
    log.append("u", "S", "見てね https://example.com/secret")
    raw = (tmp_path / "e.jsonl").read_text(encoding="utf-8")
    assert "example.com" not in raw
    assert "[url]" in raw


def test_counts_todays_replies(tmp_path):
    log = EngagementLog(tmp_path / "e.jsonl")
    assert log.replied_today() == 0
    log.append("a", "S1")
    log.append("b", "S2")
    assert log.replied_today() == 2


def test_recent_usernames_respects_the_cooldown(tmp_path):
    """同じ相手に張り付かないための一覧。"""
    path = tmp_path / "e.jsonl"
    old = (datetime.now(JST) - timedelta(days=30)).isoformat(timespec="seconds")
    recent = (datetime.now(JST) - timedelta(days=1)).isoformat(timespec="seconds")
    path.write_text(
        json.dumps({"username": "old_friend", "shortcode": "S1",
                    "replied_at": old, "text": ""}, ensure_ascii=False) + "\n"
        + json.dumps({"username": "new_friend", "shortcode": "S2",
                      "replied_at": recent, "text": ""}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    names = EngagementLog(path).recent_usernames(days=7)
    assert names == {"new_friend"}


def test_replied_shortcodes_prevents_double_replies(tmp_path):
    log = EngagementLog(tmp_path / "e.jsonl")
    log.append("u", "SHORT1")
    assert log.replied_shortcodes() == {"SHORT1"}


def test_missing_file_is_not_an_error(tmp_path):
    log = EngagementLog(tmp_path / "does-not-exist.jsonl")
    assert log.all() == []
    assert log.replied_today() == 0
    assert log.recent_usernames() == set()
