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


# ======================================================================
# 候補収集（全ジャンルから均等に集める）
# ======================================================================
def _genre_response(genre_id, page, count=30):
    """ジャンルとページごとに別の商品を返す応答を作る。"""
    return _FakeResponse(200, {
        "Items": [
            {
                "itemCode": f"shop{genre_id}:{page}-{i}",
                "itemName": f"商品{genre_id}-{page}-{i}",
                "itemPrice": 1000 + i,
                "itemUrl": f"https://item.rakuten.co.jp/s/{genre_id}-{page}-{i}/",
                "genreId": str(genre_id),
                "reviewCount": 100,
                "reviewAverage": 4.5,
                "postageFlag": 0,
                "availability": 1,
                "mediumImageUrls": ["https://img/a.jpg"],
                "shopCode": f"shop{genre_id}",
            }
            for i in range(count)
        ]
    })


class _GenreAwareSession(_FakeSession):
    """genreId と page に応じた応答を返すセッション。"""

    def __init__(self):
        super().__init__([])

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {},
                           "data": data or {}, "headers": headers or {}})
        p = params or {}
        return _genre_response(p.get("genreId"), p.get("page", 1))


def _collector(config):
    _configure(config)
    http = HttpClient(session=_GenreAwareSession(), max_retries=0)
    return RakutenClient(config, http=http), http


def test_every_configured_genre_is_queried(config):
    """全ジャンルに問い合わせが飛ぶこと。

    以前は「ページ→ジャンル→ソート」の順で回してプール上限で打ち切っていたため、
    13ジャンル設定しても最初の1ジャンルしか取得できていなかった。
    """
    client, http = _collector(config)
    genres = [{"id": 100 + i, "label": f"L{i}"} for i in range(6)]

    client.collect_candidates(genres, limit=120, rotation_seed=0)

    queried = {c["params"]["genreId"] for c in http.session.calls}
    assert queried == {g["id"] for g in genres}


def test_pool_is_spread_across_genres(config):
    """1ジャンルがプールを占有しないこと。"""
    client, _ = _collector(config)
    genres = [{"id": 100 + i, "label": f"L{i}"} for i in range(5)]

    pool = client.collect_candidates(genres, limit=100, rotation_seed=0)

    labels = {}
    for item in pool:
        label = item.raw["_genre_label"]
        labels[label] = labels.get(label, 0) + 1

    assert len(labels) == 5
    # 1ジャンルの取り分は上限/ジャンル数 + 余裕を超えない
    assert max(labels.values()) <= 100 // 5 + 1


def test_rotation_seed_changes_which_items_are_collected(config):
    """日替わりで別の商品が見えること。

    毎回同じ条件で引くと同じ商品しか出ず、30日クールダウンで
    候補が枯れて投稿がスキップされ続ける。
    """
    genres = [{"id": 100 + i, "label": f"L{i}"} for i in range(4)]

    client_a, _ = _collector(config)
    client_b, _ = _collector(config)
    day1 = {i.item_code for i in client_a.collect_candidates(genres, limit=80, rotation_seed=1)}
    day2 = {i.item_code for i in client_b.collect_candidates(genres, limit=80, rotation_seed=2)}

    assert day1 != day2, "日が変わっても同じ商品しか取れていない"


def test_one_failing_genre_does_not_stop_collection(config):
    """1ジャンルが落ちても他のジャンルは集める。"""
    _configure(config)

    class _PartlyBroken(_GenreAwareSession):
        def request(self, method, url, params=None, data=None, headers=None, timeout=None):
            if (params or {}).get("genreId") == 101:
                self.calls.append({"params": params or {}, "headers": {}})
                return _FakeResponse(503, text="boom")
            return super().request(method, url, params, data, headers, timeout)

    http = HttpClient(session=_PartlyBroken(), max_retries=0, backoff_max=0.01)
    client = RakutenClient(config, http=http)
    genres = [{"id": 100 + i, "label": f"L{i}"} for i in range(4)]

    pool = client.collect_candidates(genres, limit=80, rotation_seed=0)
    labels = {i.raw["_genre_label"] for i in pool}
    assert "L1" not in labels          # 落ちたジャンル
    assert len(labels) == 3            # 残りは集まっている


def test_collect_raises_when_no_genres(config):
    from src.errors import NoDataError

    client, _ = _collector(config)
    with pytest.raises(NoDataError):
        client.collect_candidates([])
