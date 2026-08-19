"""投稿前の独立検証工程。

生成器とは別レイヤーに置くことが重要。生成器のバグやテンプレート追加ミスを
ここで機械的に止める。問題があれば投稿しない。
"""

from .checker import CheckResult, ComplianceChecker, Violation

__all__ = ["CheckResult", "ComplianceChecker", "Violation"]
