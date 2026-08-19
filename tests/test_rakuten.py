"""楽天APIレスポンス解析とクライアント挙動のテスト。"""

from __future__ import annotations

import json

import pytest
import requests

from src.errors import AuthError, RateLimitError, TransientError
from src.http import HttpClient
from src.rakuten.client import RakutenClient
from src.rakuten.models import RakutenItem


# ======================================================================
# レスポンス解析
# ======================================================================
def test_parses_all_documented_fields(search_response):
    item = RakutenItem.from_api(search_response["Items"][0])

    assert item.item_code == "cosmeshop:10000123"
    assert item.item_price == 1980
    assert item.review_count == 1284
    assert item.review_average == pytest.approx(4.42)
    assert item.postage_flag == 0
    assert item.is_postage_free is True
    assert item.affiliate_rate == pytest.approx(3.0)
    assert item.point_rate == pytest.approx(2.0)
    assert item.shop_code == "cosmeshop"
    assert item.genre_id == "216131"
    assert item.affiliate_url.startswith("https://hb.afl.rakuten.co.jp/")
    # サムネイル指定が外れていること
    assert item.image_url and "_ex=" not in item.image_url


def test_parses_format_version_1_shape():
    """formatVersion=1 の {"Item": {...}} 形式も読める。"""
    payload = {
        "Item": {
            "itemCode": "s:1",
            "itemName": "テスト",
            "itemPrice": 1000,
            "itemUrl": "https://item.rakuten.co.jp/s/1/",
            "mediumImageUrls": [{"imageUrl": "https://img/a.jpg?_ex=128x128"}],
        }
    }
    item = RakutenItem.from_api(payload)
    assert item.item_code == "s:1"
    assert item.image_url == "https://img/a.jpg"


def test_missing_values_stay_none_not_zero():
    """取得できなかった値を 0 で埋めない（本文に書かないため）。"""
    item = RakutenItem.from_api(
        {
            "itemCode": "s:1",
            "itemName": "テスト",
            "itemPrice": 1000,
            "itemUrl": "https://item.rakuten.co.jp/s/1/",
        }
    )
    assert item.review_count is None
    assert item.review_average is None
    assert item.is_postage_free is None
    assert item.has_review is False


def test_rejects_item_without_required_fields():
    with pytest.raises(ValueError):
        RakutenItem.from_api({"itemName": "名前だけ"})


def test_affiliate_url_is_hashed_not_stored_raw(item):
    assert item.affiliate_url_hash is not None
    assert len(item.affiliate_url_hash) == 64
    assert item.affiliate_url not in item.affiliate_url_hash


def test_display_name_strips_promo_noise():
    item = RakutenItem.from_api(
        {
            "itemCode": "s:1",
            "itemName": "【送料無料】★クレンジングバーム 90g",
            "itemPrice": 1000,
            "itemUrl": "https://item.rakuten.co.jp/s/1/",
        }
    )
    assert item.display_name().startswith("クレンジングバーム")


# ======================================================================
# HTTP のリトライ・エラー分類
# ======================================================================
class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeSession:
    """呼び出しごとに用意した応答を返すセッション。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {},
                           "data": data or {}, "headers": headers or {}})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _client(responses, **kwargs):
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("backoff_base", 1.0)
    kwargs.setdefault("backoff_max", 0.01)  # テストを速く終わらせる
    return HttpClient(session=_FakeSession(responses), **kwargs)


def test_auth_error_is_not_retried():
    client = _client([_FakeResponse(401, text="unauthorized")])
    with pytest.raises(AuthError):
        client.get("https://example.test/")
    assert len(client.session.calls) == 1


def test_403_raises_auth_error():
    """楽天の新APIは Origin 不足で 403 を返す。リトライしても直らない。"""
    client = _client([_FakeResponse(403, text="forbidden")])
    with pytest.raises(AuthError):
        client.get("https://example.test/")


def test_rate_limit_is_retried_then_raised():
    client = _client([_FakeResponse(429, headers={"Retry-After": "0"}, text="rate")] * 3)
    with pytest.raises(RateLimitError):
        client.get("https://example.test/")
    assert len(client.session.calls) == 3  # 初回 + リトライ2回


def test_transient_error_recovers_on_retry():
    client = _client([_FakeResponse(503, text="oops"), _FakeResponse(200, {"ok": True})])
    response = client.get("https://example.test/")
    assert response.status_code == 200
    assert len(client.session.calls) == 2


def test_connection_error_is_transient_and_bounded():
    client = _client([requests.ConnectionError("boom")] * 3)
    with pytest.raises(TransientError):
        client.get("https://example.test/")
    assert len(client.session.calls) == 3  # 無限リトライしない


# ======================================================================
# RakutenClient
# ======================================================================
def _configure(config):
    object.__setattr__(config.credentials, "rakuten_application_id", "app")
    object.__setattr__(config.credentials, "rakuten_access_key", "key")
    object.__setattr__(config.credentials, "rakuten_affiliate_id", "aff")
    return config


def test_client_sends_both_credentials_and_origin(config, search_response):
    _configure(config)
    http = _client([_FakeResponse(200, search_response)])
    client = RakutenClient(config, http=http)

    client.search(genre_id=216131)

    call = http.session.calls[0]
    # 新APIは applicationId と accessKey の両方が必須
    assert call["params"]["applicationId"] == "app"
    assert call["headers"]["accessKey"] == "key"
    assert call["params"]["affiliateId"] == "aff"


def test_client_falls_back_to_query_param_access_key(config, search_response):
    """accessKey ヘッダー方式が 400 なら、クエリパラメータ方式へ切り替わる。"""
    _configure(config)
    http = _client(
        [_FakeResponse(400, text="accessKey must be present as a query parameter"),
         _FakeResponse(200, search_response)]
    )
    client = RakutenClient(config, http=http)

    items = client.search(genre_id=216131)

    assert len(items) == 3
    second = http.session.calls[1]
    assert second["params"]["accessKey"] == "key"
    assert "accessKey" not in second["headers"]


def test_client_falls_back_to_older_endpoint_version(config, search_response):
    _configure(config)
    http = _client(
        [_FakeResponse(404, text="not found"), _FakeResponse(404, text="not found"),
         _FakeResponse(200, search_response)]
    )
    client = RakutenClient(config, http=http)

    items = client.search(genre_id=216131)

    assert len(items) == 3
    used = http.session.calls[-1]["url"]
    assert used in config.rakuten["fallback_endpoints"]


def test_client_skips_unparseable_items(config, search_response):
    _configure(config)
    broken = dict(search_response)
    broken["Items"] = [*search_response["Items"], {"itemName": "壊れたデータ"}]
    http = _client([_FakeResponse(200, broken)])
    client = RakutenClient(config, http=http)

    items = client.search(genre_id=216131)
    assert len(items) == 3  # 壊れた1件だけ落ちる


def test_client_requires_credentials(config):
    from src.errors import MissingSecretError

    client = RakutenClient(config, http=_client([]))
    with pytest.raises(MissingSecretError):
        client.search(genre_id=216131)


# ======================================================================
# 商品名の表示
# ======================================================================
def _named(name: str):
    return RakutenItem.from_api(
        {"itemCode": "s:1", "itemName": name, "itemPrice": 1000,
         "itemUrl": "https://item.rakuten.co.jp/s/1/"}
    )


def test_display_name_does_not_cut_inside_brackets():
    """括弧の途中で切れると閉じ括弧の無い中途半端な表示になる。"""
    item = _named("モイストリファイン 化粧液【ファンケル 公式】 [FANCL 化粧水 保湿 うるおい]")
    shown = item.display_name()
    assert "[FANCL" not in shown
    assert shown.count("[") == shown.count("]")
    assert shown.count("【") == shown.count("】")


def test_display_name_keeps_short_names_intact():
    assert _named("クレンジングバーム 90g").display_name() == "クレンジングバーム 90g"


def test_display_name_does_not_end_with_separator():
    shown = _named("アミノ酸シャンプー 詰め替え用 大容量 / 送料無料 / 楽天限定").display_name()
    assert not shown.endswith(("/", "／", "・", "-", "|"))


def test_affiliate_wrapped_item_url_is_unwrapped():
    """affiliateId 指定時、楽天APIは itemUrl もアフィリエイトURLで返す。

    そのまま保存すると公開リポジトリにアフィリエイトIDが残るため、
    pc= に入っている素の商品URLへ戻す。
    """
    item = RakutenItem.from_api({
        "itemCode": "fancl-shop:10009885",
        "itemName": "化粧液",
        "itemPrice": 1540,
        "itemUrl": "https://hb.afl.rakuten.co.jp/hgc/g00sms5o.1s4mob08/"
                   "?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Ffancl-shop%2F3742-31%2F&m=x",
    })
    assert item.item_url == "https://item.rakuten.co.jp/fancl-shop/3742-31/"
    assert "hb.afl" not in item.item_url


def test_plain_item_url_is_untouched():
    item = RakutenItem.from_api({
        "itemCode": "s:1", "itemName": "テスト", "itemPrice": 1000,
        "itemUrl": "https://item.rakuten.co.jp/s/1/",
    })
    assert item.item_url == "https://item.rakuten.co.jp/s/1/"
