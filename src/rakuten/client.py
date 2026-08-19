"""楽天市場 商品検索API クライアント。

2026年のインフラ刷新で以下が変わっているため、旧実装は動かない:
  * ドメインが openapi.rakuten.co.jp（旧 app.rakuten.co.jp は 2026-05-13 停止）
  * applicationId と accessKey の**両方**が必須
  * accessKey は「ヘッダー」または「クエリパラメータ」で送る（Authorization: Bearer は不可）
  * Origin / Referer が無いと 403

accessKey の送り方は実運用で揺れが報告されているため、
ヘッダー方式を第一候補にし、400 が返ったらクエリパラメータ方式へ自動フォールバックする。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..config import Config
from ..errors import AuthError, NoDataError, TransientError
from ..http import HttpClient, RateLimiter
from .models import RakutenItem

logger = logging.getLogger(__name__)

# レスポンスから取り出すフィールド。elements で絞ると転送量とパース事故が減る。
ITEM_FIELDS = (
    "itemCode,itemName,catchcopy,itemPrice,itemUrl,affiliateUrl,affiliateRate,"
    "reviewCount,reviewAverage,postageFlag,availability,pointRate,"
    "mediumImageUrls,shopCode,shopName,genreId,taxFlag,creditCardFlag"
)


class RakutenClient:
    """商品検索APIの呼び出しと正規化を担う。"""

    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.credentials = config.credentials
        rk = config.rakuten

        self.endpoints: list[str] = [rk["endpoint"], *rk.get("fallback_endpoints", [])]
        self._active_endpoint: str | None = None
        # ヘッダー方式が使えるか。None=未判定, True/False=判定済み
        self._accesskey_in_header: bool | None = None

        origin = config.rakuten_origin
        self.http = http or HttpClient(
            connect_timeout=rk.get("connect_timeout", 10.0),
            read_timeout=rk.get("read_timeout", 20.0),
            max_retries=rk.get("max_retries", 4),
            backoff_base=rk.get("backoff_base_seconds", 1.5),
            backoff_max=rk.get("backoff_max_seconds", 30.0),
            rate_limiter=RateLimiter(rk.get("min_interval_seconds", 1.2)),
            default_headers={
                "Accept": "application/json",
                "User-Agent": rk.get("user_agent", "cosme-bot/1.0"),
                # 新APIは呼び出し元ドメインを検査する。デベロッパーコンソールの
                # 「許可Webサイト」に登録したURLと一致させること。
                "Origin": origin,
                "Referer": origin if origin.endswith("/") else origin + "/",
            },
        )

    # ------------------------------------------------------------------
    def _base_params(self) -> dict[str, Any]:
        self.credentials.require_rakuten()
        return {
            "applicationId": self.credentials.rakuten_application_id,
            "affiliateId": self.credentials.rakuten_affiliate_id,
            "format": "json",
            "formatVersion": 2,
            "elements": ITEM_FIELDS,
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        """エンドポイントと accessKey 送信方式のフォールバックを含む実呼び出し。"""
        endpoints = [self._active_endpoint] if self._active_endpoint else self.endpoints
        # ヘッダー方式 -> クエリ方式 の順。判定済みなら1通りだけ試す。
        if self._accesskey_in_header is None:
            modes = [True, False]
        else:
            modes = [self._accesskey_in_header]

        last_body = ""
        for endpoint in endpoints:
            for use_header in modes:
                request_params = dict(params)
                headers: dict[str, str] = {}
                if use_header:
                    headers["accessKey"] = self.credentials.rakuten_access_key or ""
                else:
                    request_params["accessKey"] = self.credentials.rakuten_access_key

                try:
                    response = self.http.get(endpoint, params=request_params, headers=headers)
                except AuthError:
                    # 403 は Origin 不一致の可能性が高い。方式を変えても直らないので即上げる。
                    raise

                if response.status_code == 200:
                    if self._active_endpoint != endpoint:
                        logger.info("楽天APIエンドポイント確定: %s", endpoint)
                    self._active_endpoint = endpoint
                    if self._accesskey_in_header != use_header:
                        logger.info(
                            "accessKey 送信方式確定: %s",
                            "header" if use_header else "query param",
                        )
                    self._accesskey_in_header = use_header
                    return response.json()

                last_body = response.text[:400]
                logger.warning(
                    "楽天API 応答 %s (endpoint=%s, accessKey=%s): %s",
                    response.status_code,
                    endpoint.rsplit("/", 1)[-1],
                    "header" if use_header else "query",
                    last_body,
                )

        raise TransientError(
            "楽天APIの呼び出しに失敗しました。エンドポイント/accessKey送信方式を"
            f"すべて試しました。最後の応答: {last_body}"
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("Items")
        if not items:
            return []
        # formatVersion=2 なら list[dict], =1 なら list[{"Item": dict}]
        return [entry.get("Item", entry) if isinstance(entry, dict) else entry for entry in items]

    def search(
        self,
        *,
        keyword: str | None = None,
        genre_id: int | str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        sort: str = "standard",
        hits: int | None = None,
        page: int = 1,
        postage_flag: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[RakutenItem]:
        """商品を1ページ分検索する。"""
        if keyword is None and genre_id is None:
            raise ValueError("keyword か genre_id のどちらかは必須です")

        params = self._base_params()
        params.update(
            {
                "sort": sort,
                "hits": hits or self.config.rakuten.get("hits_per_page", 30),
                "page": page,
                "availability": 1 if self.config.selection.get("require_available", True) else 0,
                "imageFlag": 1 if self.config.selection.get("require_image", True) else 0,
            }
        )
        if keyword:
            params["keyword"] = keyword
        if genre_id is not None:
            params["genreId"] = genre_id
        if min_price is not None:
            params["minPrice"] = min_price
        if max_price is not None:
            params["maxPrice"] = max_price
        if postage_flag is not None:
            params["postageFlag"] = postage_flag
        if extra:
            params.update(extra)

        payload = self._call(params)
        raw_items = self._extract_items(payload)

        items: list[RakutenItem] = []
        for raw in raw_items:
            try:
                items.append(RakutenItem.from_api(raw))
            except (ValueError, TypeError) as exc:
                logger.debug("商品のパースをスキップ: %s", exc)

        logger.info(
            "楽天API: genre=%s keyword=%s page=%d -> %d件取得 (%d件パース成功)",
            genre_id,
            keyword,
            page,
            len(raw_items),
            len(items),
        )
        return items

    def collect_candidates(
        self,
        genres: Iterable[dict[str, Any]],
        *,
        sorts: tuple[str, ...] = ("standard", "-reviewCount", "+itemPrice"),
        postage_flag: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        limit: int | None = None,
    ) -> list[RakutenItem]:
        """複数ジャンル・複数ソート順から候補プールを作る。

        1つのソート順だけだと同じ商品ばかり集まるので、観点を変えて集める。
        itemCode で重複排除する。
        """
        selection = self.config.selection
        lo = min_price if min_price is not None else selection["min_price"]
        hi = max_price if max_price is not None else selection["max_price"]
        cap = limit or selection.get("candidate_pool", 90)
        max_pages = self.config.rakuten.get("max_pages", 3)

        seen: set[str] = set()
        pool: list[RakutenItem] = []
        genre_list = list(genres)

        for page in range(1, max_pages + 1):
            for genre in genre_list:
                for sort in sorts:
                    if len(pool) >= cap:
                        logger.info("候補プールが上限 %d 件に達しました", cap)
                        return pool
                    try:
                        items = self.search(
                            genre_id=genre["id"],
                            min_price=lo,
                            max_price=hi,
                            sort=sort,
                            page=page,
                            postage_flag=postage_flag,
                        )
                    except TransientError as exc:
                        # 1ジャンルの失敗で全体を止めない
                        logger.warning("ジャンル %s の取得に失敗、スキップ: %s", genre["id"], exc)
                        continue

                    for item in items:
                        if item.item_code in seen:
                            continue
                        seen.add(item.item_code)
                        # ラベルはスコアリング・本文生成で使うので持ち回す
                        object.__setattr__(item, "raw", {**item.raw, "_genre_label": genre.get("label", "")})
                        pool.append(item)

        if not pool:
            raise NoDataError("楽天APIから候補商品を1件も取得できませんでした")

        logger.info("候補プール: %d件", len(pool))
        return pool
