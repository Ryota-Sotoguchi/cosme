"""候補の収集元。**追加だけで増やせる形にする。**

将来ここに足したいもの:

    ホームTL / 指定アカウント（今ある）
    検索 / トピック / 自分の投稿に返信してきた人 / フォロー中

どれも「ページを開いて投稿を集める」以上のことはしないので、
`CandidateSource` を満たすクラスを1つ書いて `REGISTRY` に1行足せば増える。
runner の側は何も変えなくていい。

## 1つ落ちても止めない

収集元ごとに独立して失敗しうる（ページ構造が変わった、そのアカウントが
消えた）。`collect_all()` は (候補, エラー) を返す。
`scripts/collect_candidates.py` と同じ方針で、例外にせず記録に残す。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from ...config import Config
from ...errors import ThrottledError
from ..candidates import Candidate

logger = logging.getLogger(__name__)


class CandidateSource(Protocol):
    """投稿を集めてくるもの。"""

    name: str

    def collect(self, session: Any, *, limit: int) -> list[Candidate]: ...


def _build_timeline(config: Config, state: Any | None) -> CandidateSource:
    from .timeline import TimelineSource

    return TimelineSource(config)


def _build_accounts(config: Config, state: Any | None) -> CandidateSource:
    from .accounts import AccountsSource

    return AccountsSource(config, state)


def _build_search(config: Config, state: Any | None) -> CandidateSource:
    from .search import SearchSource

    return SearchSource(config, state)


# 収集元を足すときはここに1行。
REGISTRY: dict[str, Callable[[Config, Any], CandidateSource]] = {
    "timeline": _build_timeline,
    "accounts": _build_accounts,
    "search": _build_search,
}


def build_sources(config: Config, *, only: list[str] | None = None,
                  state: Any | None = None) -> list[CandidateSource]:
    """設定から収集元を組む。"""
    enabled = only or list(
        config.autoreply_section("sources").get("enabled", ["timeline"]))

    sources: list[CandidateSource] = []
    for name in enabled:
        factory = REGISTRY.get(name)
        if factory is None:
            logger.warning("知らない収集元です: %s（使える名前: %s）",
                           name, sorted(REGISTRY))
            continue
        sources.append(factory(config, state))
    return sources


def collect_all(
    session: Any,
    sources: list[CandidateSource],
    *,
    limit_per_source: int = 40,
) -> tuple[list[Candidate], list[str]]:
    """全部の収集元から集める。(候補, エラー) を返す。"""
    found: list[Candidate] = []
    errors: list[str] = []

    for source in sources:
        try:
            got = source.collect(session, limit=limit_per_source)
        except ThrottledError:
            # **アカウント単位の状態であって、1つの収集元の失敗ではない。**
            # 次の収集元へ進んでも同じように拒否されるだけなので、上へ返す。
            raise
        except Exception as exc:  # noqa: BLE001 — 1つ落ちても次へ進む
            message = f"{source.name}: {type(exc).__name__}: {exc}"
            logger.warning("収集に失敗しました（%s）", message)
            errors.append(message)
            continue
        logger.info("収集しました（%s）: %d件", source.name, len(got))
        found += got

    return found, errors
