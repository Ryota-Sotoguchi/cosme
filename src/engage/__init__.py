"""他人の投稿への返信で、アカウントの露出を増やすための仕組み。

投稿数を増やしても、フォロワーがいない段階では見てもらえる相手が限られる。
他人の投稿に自然な返信をして、プロフィールを見に来てもらう導線を作る。

**返信数を増やすことが目的ではない。** 返信経由で知ってもらうのが目的なので、
数を追わず、返す価値のある投稿だけに返す。
"""

from .candidates import Candidate, parse_search_block, rank_candidates

__all__ = ["Candidate", "parse_search_block", "rank_candidates"]
