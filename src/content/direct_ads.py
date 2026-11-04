"""投稿に直接置く広告リンク（A8）。2026-11-04 追加。

## なぜ要るのか

収益の導線は «投稿 → 自サイト（年収相場チェッカー）→ A8 のバナー» の2段だった。
2026-11-01〜04 の A8 の実績は広告表示6・クリック2・発生0で、サイトへ人がほぼ来ていない。
本人の判断で、年収の記事の最後の1本に A8 のリンクを直接置く試験をする（型 guide_a8）。

## リンクはリポジトリに書かない

A8 のリンクには媒体を特定する ID（a8mat）が入る。投稿は公開されるので秘密ではないが、
public リポジトリへ複製する理由も無いので、楽天のアフィリエイトIDと同じく Secrets に置く。

    A8_LINKS='{"<id>": {"name": "<サービス名>", "url": "https://px.a8.net/svt/ejp?a8mat=..."}}'

GitHub Actions は Secrets の `A8_LINKS`、ローカルは `.env` から読む。
**未設定・読めないときは空**を返し、guide_a8 は自サイトの紹介に戻る（投稿は止めない）。

## 守ること

* 許すのは `px.a8.net` の https だけ。checker はここで読んだURLと**完全一致**するものだけ通す
* URL はログに出さない（読めなかったときも ID だけを出す）。
  公開データ（history）とログの両方で伏せる（storage/history.py・logging_setup.py）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ENV_NAME = "A8_LINKS"
ALLOWED_HOSTS = ("px.a8.net",)


@dataclass(frozen=True)
class DirectAd:
    id: str
    name: str
    url: str


def _valid_url(url: str) -> bool:
    if not url or any(ch.isspace() for ch in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc in ALLOWED_HOSTS and bool(parsed.query)


def load(environ: Mapping[str, str] | None = None) -> tuple[DirectAd, ...]:
    """設定された広告リンクを返す。未設定・壊れているときは空。"""
    raw = (os.environ if environ is None else environ).get(ENV_NAME, "").strip()
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("%s が JSON として読めません。A8 のリンクは使いません", ENV_NAME)
        return ()
    if not isinstance(data, dict):
        logger.error("%s は {id: {name, url}} の形にしてください", ENV_NAME)
        return ()

    ads: list[DirectAd] = []
    for ad_id, spec in data.items():
        name = str(spec.get("name", "")).strip() if isinstance(spec, dict) else ""
        url = str(spec.get("url", "")).strip() if isinstance(spec, dict) else ""
        if not name or not _valid_url(url):
            # URL そのものは出さない
            logger.error("%s の %s を読めません（name と https://px.a8.net/ のURLが要る）",
                         ENV_NAME, ad_id)
            continue
        ads.append(DirectAd(id=str(ad_id), name=name, url=url))
    return tuple(ads)


def urls(environ: Mapping[str, str] | None = None) -> set[str]:
    return {ad.url for ad in load(environ)}
