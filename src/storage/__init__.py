"""投稿履歴と運用状態の永続化。

外部DBは使わない。リポジトリ内の JSONL / JSON を GitHub Actions がコミットバックする。
追加コスト0円、監査可能、かつ public リポジトリのスケジュール自動無効化
（60日間活動が無いと停止する）も同時に防げる。
"""

from .history import History, PostRecord
from .state import State

__all__ = ["History", "PostRecord", "State"]
