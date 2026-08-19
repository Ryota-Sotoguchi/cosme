"""投稿文の生成。

LLM は使わない。商品データによる条件分岐と文章パーツの組み合わせで自然文を作る。
通常運転で追加のAI API課金が発生しない構成にするための中核。
"""

from .builder import ContentBuilder, Draft

__all__ = ["ContentBuilder", "Draft"]
