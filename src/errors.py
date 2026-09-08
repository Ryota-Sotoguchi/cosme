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


class ThrottledError(TransientError):
    """Threads がこちらの操作を絞っている。

    2026-09-05 に実際に起きた: 13アカウントのプロフィールを続けて開いた直後、
    プロフィールだけが「エラーが発生しました。後ほどもう一度実行してください。」を
    返すようになった（ホームTLは正常のまま）。

    **これは1つの収集元の失敗ではなく、アカウント単位の状態。**
    握りつぶして次の収集元へ進んではいけないし、次の定期実行が
    そのまま突っ込んでもいけない。
    """


class LlmUnavailableError(ConfigError):
    """claude CLI が使えない。インストールされていないか認証が切れている。

    ConfigError の子なので、投稿パイプラインと同じ扱い（exit 1・リトライしない）
    になる。メッセージに復旧手順を入れること。
    """


class SelectorMissError(CosmeError):
    """Threads の DOM からセレクタが引けなかった。

    Threads は難読化された class を予告なく変える。必須の要素が見つからない
    のは「今日 DOM が変わった」ということなので、**推測で続行しない**。
    止めて `autoreply --selfcheck` とプローブで実測し直す。
    """

    def __init__(self, key: str, tried: list[str] | None = None) -> None:
        self.key = key
        self.tried = tried or []
        detail = f"（試したセレクタ: {self.tried}）" if self.tried else ""
        super().__init__(
            f"セレクタ '{key}' が見つかりません{detail}。"
            " Threads の DOM が変わった可能性があります。"
            " scripts/probe_threads_dom.py で実測し直してください。"
        )
