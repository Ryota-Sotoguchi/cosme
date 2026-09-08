"""数値はおおよそで書く。

人は「1,980円」「レビュー1,284件、平均4.4」とは書かない。値札の桁をそのまま
並べると、読み手の判断を助けないうえ、機械が転記した文にしか見えない。

ただし **実際より安く見せない**（景表法・有利誤認）。
丸め方向はここで固定する。
"""

from __future__ import annotations

import re

import pytest

from src.content.facts import (
    approx_price,
    approx_review_average,
    build_facts,
    extract_numbers,
    inline_facts,
    normalize_number,
    sentence_facts,
)

from conftest import make_item

PRICES = (500, 750, 999, 1000, 1980, 2000, 3520, 7150, 9999, 12800, 13000, 19980)


def stated_amounts(text: str) -> list[int]:
    """本文に出ている「◯円」をすべて拾う。"""
    return [int(m.replace(",", "")) for m in re.findall(r"([\d,]+)円", text)]


# ======================================================================
@pytest.mark.parametrize("price", PRICES)
@pytest.mark.parametrize("style", range(4))
def test_price_is_never_stated_lower_than_it_is(price, style):
    """「◯円台」「◯円ちょっと」は下方向の丸めなので、幅を制限する。

    12,800円を「12,000円ちょっと」と書くと 800円ぶん安く見える。
    """
    text, _ = approx_price(price, style)
    amounts = stated_amounts(text)
    assert amounts, text

    stated = amounts[0]
    if "くらい" in text:
        assert stated >= price, f"{price}円 を {text} と切り上げていない"
    elif "ちょっと" in text:
        assert 0 <= price - stated <= price * 0.05, (
            f"{price}円 を {text} と書くと差が大きすぎる"
        )
    elif "台" in text:
        # 「3,500円台」は 3,500〜3,599 を指す。範囲に入っていること
        assert stated <= price, f"{price}円 は {text} に入らない"
        assert price - stated < max(100, stated * 0.1), f"{price}円 は {text} に入らない"
    else:
        assert stated == price, f"ぴったりでないのに {text}"


@pytest.mark.parametrize("price", PRICES)
@pytest.mark.parametrize("style", range(4))
def test_price_numbers_are_declared(price, style):
    """本文に出た数値は、必ず許可リストに入れて返すこと。

    compliance のデータ整合性チェックがここを見る。
    漏らすと「商品データに存在しない数値」で投稿ごと落ちる。
    """
    text, allowed = approx_price(price, style)
    for token in extract_numbers(text):
        assert token in allowed, f"{text} の {token} が許可されていない"


def test_exact_price_is_not_dressed_up():
    """ぴったりの値段に「台」「ちょっと」を付けない。嘘になる。"""
    for style in range(4):
        text, _ = approx_price(2000, style)
        assert "台" not in text and "ちょっと" not in text, text


# ======================================================================
# 件数の丸めを見ていた2本はここにあった。
#
# approx_review_count は「レビュー」という語を本文に出さない方針で
# 消えたので、走査する対象が無い（CLAUDE.md §4-2）。
# 語が戻っていないことは tests/test_no_review_word.py が見張る。


def test_review_average_never_prints_a_number():
    """平均は数字にしない。「4.4」は読み手の判断材料にならない。"""
    for average in (3.9, 4.2, 4.42, 4.71, 5.0):
        for style in range(3):
            text = approx_review_average(average, 500, style)
            assert not extract_numbers(text), text


def test_review_average_is_silent_when_there_is_little_data():
    assert approx_review_average(5.0, 3) == ""


def test_low_average_is_not_praised():
    assert approx_review_average(3.9, 500) == ""


# ======================================================================
def test_generated_facts_have_no_raw_price_tag():
    """組み立てた事実行に、値札そのままの桁が出ていないこと。"""
    item = make_item(item_price=1980, review_count=1284, review_average=4.42)
    facts = build_facts(item)
    body = "\n".join(facts.lines)

    assert "1,980円" not in body, body
    assert "1,284" not in body, body
    assert "4.4" not in body, body
    for token in extract_numbers(body):
        assert token in facts.allowed_numbers, f"{body} の {token} が許可されていない"


@pytest.mark.parametrize("style", range(5))
def test_sentence_and_inline_facts_declare_their_numbers(style):
    item = make_item(item_price=3520, review_count=6806, review_average=4.71)
    for text, allowed in (sentence_facts(item, style), inline_facts(item, style)):
        for token in extract_numbers(text):
            assert token in allowed, f"{text} の {token} が許可されていない"


def test_point_rate_stays_exact():
    """ポイント倍率は丸めない。もともと粗い整数で、人もそのまま口にする。"""
    item = make_item(item_price=1980, point_rate=20.0)
    body = "\n".join(build_facts(item).lines)
    assert "ポイント20倍" in body, body
    assert normalize_number("20") in build_facts(item).allowed_numbers
