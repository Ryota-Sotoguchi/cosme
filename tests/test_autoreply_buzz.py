"""伸びている投稿の見分け方の検査。

いちばん見たいのは「いいねの絶対数で選んでいないこと」と
「返信が埋もれる投稿を上に置かないこと」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.engage.buzz import (
    BUZZ_MAX,
    JST,
    buzz,
    observed_velocity,
    passes_filter,
    rank,
    reaction,
    reply_fit,
    resolve_filter,
)
from src.engage.candidates import Candidate

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=JST)

BEAUTY_TEXT = "プチプラのスキンケアで乾燥する季節をどう乗り切るか、みんなどうしてるんだろう"


def make(**kwargs) -> Candidate:
    base = dict(
        username="someone",
        shortcode="ABC123",
        text=BEAUTY_TEXT,
        likes=100,
        replies=10,
        age_hours=3.0,
    )
    base.update(kwargs)
    return Candidate(**base)


@dataclass
class FakeSighting:
    """EngageStore.previous_sighting() が返す形の最小版。"""

    last_seen_at: str
    likes: int = 0
    replies: int = 0


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


# ======================================================================
# 実測の伸び
# ======================================================================
def test_velocity_is_zero_without_a_previous_sighting():
    """初見の投稿は推測で埋めない。base だけで判断する。"""
    assert observed_velocity(make(), None, now=NOW) == 0.0


def test_velocity_rises_with_the_gain_per_hour():
    slow = observed_velocity(
        make(likes=120, replies=10), FakeSighting(ago(2), likes=100, replies=10), now=NOW)
    fast = observed_velocity(
        make(likes=900, replies=10), FakeSighting(ago(2), likes=100, replies=10), now=NOW)
    assert 0 < slow < fast <= 1.0


def test_velocity_weights_replies_more_than_likes():
    """コメント欄が動いている投稿のほうが、自分の返信も読まれる。

    同じ「10件増えた」でも、返信で増えたほうを高く見る（REPLY_WEIGHT 倍）。
    """
    before = FakeSighting(ago(2), likes=100, replies=0)
    by_likes = observed_velocity(make(likes=110, replies=0), before, now=NOW)
    by_replies = observed_velocity(make(likes=100, replies=10), before, now=NOW)
    assert by_replies > by_likes


def test_velocity_is_zero_when_the_interval_is_too_short():
    """分母が小さいと少しの差で点が暴れる。"""
    assert observed_velocity(
        make(likes=200), FakeSighting(ago(0.1), likes=100),
        min_hours=0.5, now=NOW) == 0.0


def test_velocity_clamps_a_decrease_to_zero():
    """いいねの取り消しで負になることがある。"""
    assert observed_velocity(
        make(likes=50, replies=0), FakeSighting(ago(2), likes=500, replies=0), now=NOW) == 0.0


def test_velocity_survives_an_unparsable_timestamp():
    assert observed_velocity(make(), FakeSighting("こわれた日付", likes=1), now=NOW) == 0.0


# ======================================================================
# 返信数の帯
# ======================================================================
def test_reply_fit_is_full_inside_the_band():
    assert reply_fit(3, low=3, high=60) == 1.0
    assert reply_fit(30, low=3, high=60) == 1.0
    assert reply_fit(60, low=3, high=60) == 1.0


def test_reply_fit_falls_off_below_the_band():
    """返信ゼロ＝コメント欄を誰も開いていない。"""
    assert reply_fit(0, low=3, high=60) < reply_fit(2, low=3, high=60) < 1.0


def test_reply_fit_falls_off_above_the_band():
    """返信500件では自分の返信が一番下に沈む。"""
    assert reply_fit(500, low=3, high=60) < 1.0
    assert reply_fit(5000, low=3, high=60) < reply_fit(500, low=3, high=60)


def test_reply_fit_never_reaches_zero():
    """伸びすぎた投稿も候補からは外さない。順位を下げるだけ。"""
    assert reply_fit(100000, low=3, high=60) > 0


# ======================================================================
# 総合点
# ======================================================================
def test_buzz_stays_within_range():
    for candidate in (make(likes=0, replies=0), make(likes=99999, replies=9999)):
        score = buzz(candidate)
        assert 0.0 <= score.total <= BUZZ_MAX


def test_buzz_keeps_the_breakdown_readable():
    score = buzz(make())
    assert {"base", "velocity", "reply_fit"} <= set(score.parts)
    assert "beauty" in score.parts  # 既存 score() の内訳も残す
    assert "%.2f" % score.total in score.explain()


def test_buzz_prefers_the_faster_growing_of_two_equal_posts():
    """**同じ数字でも、伸びている側を上に置く。**"""
    weights = {"base_weight": 1.0, "velocity_weight": 2.0, "reply_fit_weight": 1.0}
    steady = buzz(make(shortcode="A"), previous=FakeSighting(ago(2), likes=95, replies=10),
                  weights=weights, now=NOW)
    surging = buzz(make(shortcode="B"), previous=FakeSighting(ago(2), likes=10, replies=1),
                   weights=weights, now=NOW)
    assert surging.total > steady.total


def test_a_post_with_too_many_replies_is_filtered_out():
    """いいね1000超えで切る判定にしない、ということ。

    2026-09-06 に順位付けを反応数優先へ変えたので、巨大な投稿は
    «順位が下» ではなく «上限で候補から外れる» ようになった。
    狙いは同じ — 自分の返信が沈む投稿には返さない。
    """
    giant = make(shortcode="A", likes=50000, replies=4000, age_hours=48)
    ok, reason = passes_filter(giant, {"max_replies": 100})
    assert ok is False
    assert "返信が多すぎる" in reason

    right_sized = make(shortcode="B", likes=300, replies=25, age_hours=2)
    assert passes_filter(right_sized, {"max_replies": 100})[0] is True


def test_reaction_count_now_drives_the_ranking():
    """**反応数優先。** 同じ新しさなら、反応の多いほうを上に置く。"""
    weights = {"base_weight": 1.0, "reaction_weight": 3.0,
               "velocity_weight": 1.5, "reply_fit_weight": 0.0}
    loud = buzz(make(shortcode="A", likes=900, replies=40), weights=weights)
    quiet = buzz(make(shortcode="B", likes=30, replies=2), weights=weights)
    assert loud.total > quiet.total
    assert loud.reaction > quiet.reaction


def test_the_reply_ceiling_keys_on_replies_not_likes():
    """埋もれるのは «自分より上に何件コメントがあるか» で決まる。

    いいねでは埋もれない。だから上限は返信数で見る。
    """
    many_likes = make(likes=20000, replies=5)
    many_replies = make(likes=300, replies=400)
    assert passes_filter(many_likes, {"max_replies": 100})[0] is True
    assert passes_filter(many_replies, {"max_replies": 100})[0] is False


def test_reply_fit_is_shipped_at_zero_weight_but_still_computed():
    """関数は残す。戻すのは設定1行で済むようにしてある。"""
    score = buzz(make(replies=500), weights={"reply_fit_weight": 0.0})
    assert "reply_fit" in score.parts
    assert score.reply_fit < 1.0


def test_more_likes_never_lowers_the_score_on_its_own():
    """単調性。いいねが増えて点が下がるのは直感に反する。"""
    scores = [buzz(make(likes=n, replies=10)).total for n in (50, 200, 1000, 5000)]
    assert scores == sorted(scores)


# ======================================================================
# 足切り
# ======================================================================
@pytest.mark.parametrize(
    "candidate, fragment",
    [
        (make(text="整形のダウンタイムがつらい" + "あ" * 40), "センシティブ"),
        (make(text="相互フォローお願いします！プロフから飛べます" + "あ" * 40), "宣伝"),
        (make(text="短い"), "短すぎる"),
        (make(age_hours=48.0), "古すぎる"),
        (make(age_hours=None), "投稿時刻"),
        (make(likes=2), "反応が少ない"),
        (make(likes=999999), "伸びすぎ"),
        (make(is_reply=True), "リプライ"),
        (make(username=""), "特定できない"),
    ],
)
def test_filter_rejects_with_a_readable_reason(candidate, fragment):
    ok, reason = passes_filter(
        candidate,
        {"max_age_hours": 12, "min_likes": 20, "max_likes": 20000, "min_text_length": 30},
    )
    assert ok is False
    assert fragment in reason


def test_filter_accepts_a_good_candidate():
    ok, reason = passes_filter(
        make(), {"max_age_hours": 12, "min_likes": 20, "max_likes": 20000})
    assert ok is True
    assert reason == ""


# ======================================================================
# 並べ替え
# ======================================================================
def test_rank_separates_survivors_from_rejects_with_reasons():
    good = make(shortcode="GOOD")
    bad = make(shortcode="BAD", text="短い")
    picked, rejected = rank([good, bad], filt={"min_text_length": 30}, now=NOW)

    assert [c.shortcode for c, _ in picked] == ["GOOD"]
    assert rejected[0][0].shortcode == "BAD"
    assert "短すぎる" in rejected[0][1]


def test_rank_returns_highest_first():
    picked, _ = rank(
        [make(shortcode="MEH", likes=25, replies=0, age_hours=10),
         make(shortcode="HOT", likes=400, replies=30, age_hours=2)],
        filt={"min_likes": 20}, now=NOW)
    assert [c.shortcode for c, _ in picked][0] == "HOT"


def test_rank_honours_exclusions():
    picked, _ = rank(
        [make(shortcode="A", username="already_replied"), make(shortcode="B", username="fresh")],
        exclude_usernames={"already_replied"}, now=NOW)
    assert [c.username for c, _ in picked] == ["fresh"]


def test_rank_uses_the_store_for_velocity():
    class FakeStore:
        def previous_sighting(self, shortcode):
            return FakeSighting(ago(2), likes=10, replies=0) if shortcode == "SURGING" else None

    picked, _ = rank(
        [make(shortcode="SURGING"), make(shortcode="FIRST_SEEN")],
        store=FakeStore(), now=NOW)
    by_code = {c.shortcode: s for c, s in picked}
    assert by_code["SURGING"].velocity > 0
    assert by_code["FIRST_SEEN"].velocity == 0


# ======================================================================
# 日本語の投稿にだけ返す
# ======================================================================
@pytest.mark.parametrize(
    "label, text",
    [
        ("ベトナム語", "nắng chíu qua digi và máy film ☀️🥞 mình xin in4 quán với ạ nha"),
        ("英語", "This highlighter is honestly the best I have ever tried, so glowy"),
        ("韓国語", "이 하이라이터 진짜 좋아요 피부가 원래 예쁜 사람처럼 보여요 강추합니다"),
        ("絵文字だけ", "☀️🥞✨🫶🏻🥺💭🔥❤️‍🔥🙋🏻‍♀️👀🌿🎀🧴💄👛🕊️🍀🫧🌙⭐"),
    ],
)
def test_a_non_japanese_post_is_refused(label, text):
    """**日本語の投稿にだけ返す。**

    2026-09-07 に、ベトナム語の写真投稿へ日本語で返信してしまった。
    相手にも読めないし、日本のコスメを見てほしい人にも届かない。
    """
    ok, reason = passes_filter(
        make(text=text), {"min_text_length": 30, "require_japanese": True})
    assert ok is False, label
    assert "日本語" in reason


@pytest.mark.parametrize(
    "text",
    [
        "無印の化粧水しか使ってないけど、そろそろ何か足したほうがいいのかな",
        "楽天スーパーSALE始まったーーー！🔥……のに、なかなか売れません😭笑 みんな何買った？",
        "資生堂のハイライト、可愛すぎる…塗るだけで肌がキレイに見えるって評判なの納得",
    ],
)
def test_a_japanese_post_passes(text):
    assert passes_filter(
        make(text=text), {"min_text_length": 30, "require_japanese": True})[0] is True


def test_the_language_gate_can_be_turned_off():
    """設定で外せること（将来ほかの言語で運用する場合のため）。"""
    english = make(text="This highlighter is honestly the best I have ever tried, so glowy")
    assert passes_filter(
        english, {"min_text_length": 30, "require_japanese": False})[0] is True


# ======================================================================
# 発信ジャンルの外に返さない（2026-10-13）
#
# ホームTLのフォロー先がコスメ時代のまま残っているため、9/27〜9/30 に
# **タイムラインから出した返信4件のうち3件がコスメの話だった。**
# そのとき返してしまった投稿の本文をそのまま置いて、二度と通らないことを見張る。
#
# ここは**本物の config を使う**。語の一覧を手で書き写すと、
# config を直したときにテストだけが古いまま通ってしまう。
# ======================================================================

# 実際に返信してしまった相手の投稿（要旨）。対になる返信は engage.sqlite3 の #26〜#28
OFF_GENRE_POSTS = [
    "AUX PARADISのオーラムイドレとCLEANのウォームコットン、どっちの香りが好みか迷ってます",
    "kissの下地とマキアージュのクッションファンデ、重ねづけの順番で仕上がりが変わる気がする",
    "洗浄力の強さでメイクを落としきる派だったけど、クレンジングを変えたら肌の調子がいい",
]

ON_GENRE_POSTS = [
    "一次面接で給与の話をするのは早すぎますか。年収の希望を聞かれて答えに困りました",
    "上司に退職を伝えるタイミング、ボーナスの前と後どっちがいいんだろう",
    "簿記2級の勉強、仕事と並行してどれくらいで受かるものなんでしょうか",
]


def _filter(config, source):
    """実行時と同じ条件を作る（反応数と文字数の足切りは別のテストで見ている）。"""
    opts = dict(config.autoreply_filter(source))
    opts.update(min_likes=0, min_text_length=10, max_age_hours=None, max_replies=None)
    return opts


@pytest.mark.parametrize("text", OFF_GENRE_POSTS)
def test_the_timeline_no_longer_replies_to_the_old_genre(config, text):
    """タイムラインから出てきても通らない。**LLM より前に落とす**。"""
    ok, reason = passes_filter(make(text=text), _filter(config, "timeline"))
    assert ok is False
    assert "発信ジャンルの外" in reason or reason == "発信ジャンルの語が無い"


@pytest.mark.parametrize("text", OFF_GENRE_POSTS)
def test_the_search_also_drops_the_old_genre_by_word(config, text):
    """検索にコスメが混ざっても、語の一覧で落ちる（要求語は検索には掛けない）。

    「香り」だけの投稿のように、ブランド名しか手がかりが無いものは
    語の一覧では止まらない。タイムラインは要求語で、検索は引く語そのもので守る。
    """
    opts = _filter(config, "search")
    assert "require_topic_any" not in opts, "検索に要求語を掛けると二重になる"
    ok, reason = passes_filter(make(text=text), opts)
    if ok:
        # 止まらなかったもの（ブランド名だけの投稿）は、転職の語も入っていないこと。
        # 検索は転職の語で引くので、この形は検索結果には出てこない。
        timeline = _filter(config, "timeline")
        assert passes_filter(make(text=text), timeline)[1] == "発信ジャンルの語が無い"
    else:
        assert "発信ジャンルの外" in reason


@pytest.mark.parametrize("text", ON_GENRE_POSTS)
def test_an_on_genre_post_still_passes(config, text):
    for source in ("timeline", "search"):
        ok, reason = passes_filter(make(text=text), _filter(config, source))
        assert ok is True, f"{source}: {reason}"


def test_the_timeline_needs_a_genre_word_but_the_search_does_not(config):
    """検索は転職の語で引いた結果なので、同じ条件を二重に掛けない。"""
    neutral = make(text="今日はいい天気ですね。散歩でもしようかなと思っています")

    ok, reason = passes_filter(neutral, _filter(config, "timeline"))
    assert ok is False
    assert reason == "発信ジャンルの語が無い"

    assert passes_filter(neutral, _filter(config, "search"))[0] is True


def test_the_genre_filter_is_off_when_the_lists_are_empty():
    """設定を空にすれば従来どおり。ジャンルを戻すときに1行で外せる。"""
    cosme = make(text=OFF_GENRE_POSTS[2])
    assert passes_filter(cosme, {"off_topic_keywords": [], "require_topic_any": [],
                                 "min_text_length": 10, "min_likes": 0})[0] is True
