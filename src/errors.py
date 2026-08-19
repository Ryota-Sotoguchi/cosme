"""例外の分類。

呼び出し側が「リトライすべきか」「運用を止めるべきか」「次の候補へ進むべきか」を
例外の型だけで判断できるようにしている。
"""

from __future__ import annotations


class CosmeError(Exception):
    """このプロジェクトが送出する例外の基底。"""


class ConfigError(CosmeError):
    """設定値・環境変数の不足や矛盾。リトライしても直らない。"""


class MissingSecretError(ConfigError):
    """必要な認証情報が環境変数に無い。"""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__("必要な環境変数が設定されていません: " + ", ".join(names))


class TransientError(CosmeError):
    """一時的な障害（接続失敗・5xx）。バックオフしてリトライしてよい。"""


class RateLimitError(TransientError):
    """レート制限（429）。retry_after 秒待ってからリトライする。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class AuthError(CosmeError):
    """認証・認可の失敗（401/403）。リトライしても直らないので即座に止める。"""


class NoDataError(CosmeError):
    """APIは正常応答したが、条件を満たすデータが無い。"""


class PostRejectedError(CosmeError):
    """投稿先（Threads）が投稿を拒否した。本文を変えない限り再送しても無駄。"""


class ComplianceSkip(CosmeError):
    """コンプライアンスチェックで不合格。この商品/本文はスキップして次へ進む。"""

    def __init__(self, message: str, violations: list[str] | None = None) -> None:
        self.violations = violations or []
        super().__init__(message)
