from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import load_config
from src.rakuten.models import RakutenItem
from src.storage.history import History
from src.storage.state import State

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def make_item(
    *,
    item_code: str = "shop:0001",
    item_name: str = "モイストクレンジングバーム 90g",
    item_price: int = 1980,
    review_count: int | None = 1284,
    review_average: float | None = 4.42,
    postage_flag: int | None = 0,
    availability: int | None = 1,
    affiliate_rate: float | None = 3.0,
    point_rate: float | None = 2.0,
    genre_id: str = "216131",
    shop_code: str = "shop",
    shop_name: str = "テストショップ",
    image: bool = True,
    catchcopy: str | None = None,
    genre_label: str = "スキンケア",
) -> RakutenItem:
    payload = {
        "itemCode": item_code,
        "itemName": item_name,
        "itemPrice": item_price,
        "itemUrl": f"https://item.rakuten.co.jp/{shop_code}/{item_code.split(':')[-1]}/",
        "affiliateUrl": f"https://hb.afl.rakuten.co.jp/hgc/abc/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2F{shop_code}%2F",
        "reviewCount": review_count,
        "reviewAverage": review_average,
        "postageFlag": postage_flag,
        "availability": availability,
        "affiliateRate": affiliate_rate,
        "pointRate": point_rate,
        "mediumImageUrls": ["https://thumbnail.image.rakuten.co.jp/x.jpg?_ex=128x128"] if image else [],
        "shopCode": shop_code,
        "shopName": shop_name,
        "genreId": genre_id,
        "catchcopy": catchcopy,
    }
    item = RakutenItem.from_api(payload)
    object.__setattr__(item, "raw", {**item.raw, "_genre_label": genre_label})
    return item


@pytest.fixture
def item():
    return make_item()


@pytest.fixture
def items():
    return [
        make_item(item_code="shopa:1", item_name="モイストクレンジングバーム 90g",
                  item_price=1980, shop_code="shopa"),
        make_item(item_code="shopb:2", item_name="ミネラルファンデーション SPF30 12g",
                  item_price=2750, review_count=431, review_average=4.18,
                  point_rate=1.0, shop_code="shopb", genre_label="メイク"),
        make_item(item_code="shopc:3", item_name="アミノ酸シャンプー 詰め替え 400ml",
                  item_price=1280, review_count=3902, review_average=4.55,
                  postage_flag=1, point_rate=3.0, shop_code="shopc", genre_label="ヘアケア"),
    ]


@pytest.fixture
def config(tmp_path):
    """本物の config.toml を使い、data ディレクトリだけ一時領域へ向ける。"""
    return load_config(
        config_path=PROJECT_ROOT / "config" / "config.toml",
        data_dir=tmp_path,
        dry_run=True,
    )


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


@pytest.fixture
def history(tmp_path):
    return History(tmp_path / "history.jsonl")


@pytest.fixture
def search_response():
    return json.loads((FIXTURES / "rakuten_search.json").read_text(encoding="utf-8"))
