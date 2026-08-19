"""設定の読み込みと検証。

- config/config.toml … 運用方針（コミットする）
- 環境変数 / .env  … 認証情報（絶対にコミットしない）

標準ライブラリの tomllib を使うので追加依存は無い。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, MissingSecretError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _load_dotenv(path: Path) -> None:
    """.env を読んで、まだ設定されていない環境変数だけを埋める。

    python-dotenv を入れないための最小実装。既存の環境変数は上書きしない
    （GitHub Actions の Secrets が .env に負けないようにするため）。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Credentials:
    """認証情報。値そのものは絶対にログ・履歴へ出さない。"""

    rakuten_application_id: str | None
    rakuten_access_key: str | None
    rakuten_affiliate_id: str | None
    threads_access_token: str | None
    threads_user_id: str | None
    threads_app_secret: str | None
    github_pat: str | None

    def require_rakuten(self) -> None:
        missing = [
            name
            for name, value in (
                ("RAKUTEN_APPLICATION_ID", self.rakuten_application_id),
                ("RAKUTEN_ACCESS_KEY", self.rakuten_access_key),
                ("RAKUTEN_AFFILIATE_ID", self.rakuten_affiliate_id),
            )
            if not value
        ]
        if missing:
            raise MissingSecretError(missing)

    def require_threads(self) -> None:
        missing = [
            name
            for name, value in (("THREADS_ACCESS_TOKEN", self.threads_access_token),)
            if not value
        ]
        if missing:
            raise MissingSecretError(missing)

    def __repr__(self) -> str:  # 事故防止: 値を出さない
        def mark(v: str | None) -> str:
            return "set" if v else "unset"

        return (
            "Credentials("
            f"rakuten_application_id={mark(self.rakuten_application_id)}, "
            f"rakuten_access_key={mark(self.rakuten_access_key)}, "
            f"rakuten_affiliate_id={mark(self.rakuten_affiliate_id)}, "
            f"threads_access_token={mark(self.threads_access_token)}, "
            f"threads_user_id={mark(self.threads_user_id)}, "
            f"threads_app_secret={mark(self.threads_app_secret)}, "
            f"github_pat={mark(self.github_pat)})"
        )


@dataclass(frozen=True)
class ScheduleSlot:
    slot: str
    time_jst: str
    cron_utc: str
    post_type: str
    allow_affiliate: bool


@dataclass
class Config:
    """config.toml の内容 + 認証情報 + 実行モード。"""

    raw: dict[str, Any]
    credentials: Credentials
    dry_run: bool
    data_dir: Path
    config_path: Path
    schedule: list[ScheduleSlot] = field(default_factory=list)

    # --- セクションへのショートカット ---
    @property
    def rakuten(self) -> dict[str, Any]:
        return self.raw["rakuten"]

    @property
    def threads(self) -> dict[str, Any]:
        return self.raw["threads"]

    @property
    def selection(self) -> dict[str, Any]:
        return self.raw["selection"]

    @property
    def exclusion(self) -> dict[str, Any]:
        return self.raw["exclusion"]

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw["scoring"]

    @property
    def dedup(self) -> dict[str, Any]:
        return self.raw["dedup"]

    @property
    def compliance(self) -> dict[str, Any]:
        return self.raw["compliance"]

    @property
    def rotation(self) -> dict[str, Any]:
        return self.raw.get("rotation", {})

    @property
    def ramp_up(self) -> dict[str, Any]:
        return self.raw.get("ramp_up", {"enabled": False})

    @property
    def genres(self) -> list[dict[str, Any]]:
        return self.selection.get("genres", [])

    def slot(self, name: str) -> ScheduleSlot:
        for entry in self.schedule:
            if entry.slot == name:
                return entry
        raise ConfigError(
            f"スロット '{name}' は config.toml の [[schedule]] に定義されていません。"
            f" 定義済み: {[s.slot for s in self.schedule]}"
        )

    def slot_for_cron(self, cron: str) -> ScheduleSlot:
        """GitHub Actions の github.event.schedule からスロットを引く。

        ワークフロー側に slot 名を書かず config.toml を唯一の情報源にするための入口。
        """
        normalized = " ".join(cron.split())
        for entry in self.schedule:
            if " ".join(entry.cron_utc.split()) == normalized:
                return entry
        raise ConfigError(
            f"cron '{cron}' に対応する [[schedule]] がありません。"
            f" 定義済み: {[s.cron_utc for s in self.schedule]}"
        )

    @property
    def rakuten_origin(self) -> str:
        """楽天API に送る Origin。環境変数が設定ファイルより優先される。"""
        return os.environ.get("RAKUTEN_ORIGIN") or self.rakuten["origin"]

    @property
    def history_path(self) -> Path:
        return self.data_dir / "history.jsonl"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"


def load_config(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    dry_run: bool | None = None,
) -> Config:
    """設定を読み込む。認証情報が無くてもここでは失敗しない。

    認証情報の検証は、実際にAPIを呼ぶ直前に `require_*()` で行う。
    こうすることで、Secret未設定でも DRY_RUN 以外の全工程を組み立てられる。
    """
    _load_dotenv(PROJECT_ROOT / ".env")

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    for required in ("rakuten", "threads", "selection", "exclusion", "scoring", "dedup", "compliance"):
        if required not in raw:
            raise ConfigError(f"config.toml に [{required}] セクションがありません")

    schedule = [
        ScheduleSlot(
            slot=entry["slot"],
            time_jst=entry["time_jst"],
            cron_utc=entry["cron_utc"],
            post_type=entry["post_type"],
            allow_affiliate=bool(entry.get("allow_affiliate", False)),
        )
        for entry in raw.get("schedule", [])
    ]
    if not schedule:
        raise ConfigError("config.toml に [[schedule]] が1つも定義されていません")

    credentials = Credentials(
        rakuten_application_id=os.environ.get("RAKUTEN_APPLICATION_ID") or None,
        rakuten_access_key=os.environ.get("RAKUTEN_ACCESS_KEY") or None,
        rakuten_affiliate_id=os.environ.get("RAKUTEN_AFFILIATE_ID") or None,
        threads_access_token=os.environ.get("THREADS_ACCESS_TOKEN") or None,
        threads_user_id=os.environ.get("THREADS_USER_ID") or None,
        threads_app_secret=os.environ.get("THREADS_APP_SECRET") or None,
        github_pat=os.environ.get("GH_PAT") or None,
    )

    resolved_dry_run = _env_bool("DRY_RUN", True) if dry_run is None else dry_run

    return Config(
        raw=raw,
        credentials=credentials,
        dry_run=resolved_dry_run,
        data_dir=data_dir or DEFAULT_DATA_DIR,
        config_path=path,
        schedule=schedule,
    )
