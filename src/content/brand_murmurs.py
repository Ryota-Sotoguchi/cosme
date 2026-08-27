"""実在の商品データから作るつぶやき。

## なぜ必要か

つぶやき85件を数えたら、**固有名詞が1件も入っていなかった**。
誰でもない人が、何についてでもない話をしている状態で、
それが「AIアカウントっぽさ」の正体だった。

    うちの投稿      コスメの新作って情報多くて魅力的に見えるけど…
    伸びてる投稿    コスデコの値上げ辛い😭😭
                    ↑実在ブランド ↑実際の出来事 ↑その人の感情

## 何を言ってよいか

このアカウントは商品を**使っていない**。だから書けるのは
**商品データから機械的に確かめられる事実だけ**。

    ○ 「ファンケル、詰め替えあるんだ」    商品名に「詰め替え」がある
    ○ 「キールズの新作出てる」            商品名に「新作」がある
    × 「ファンケル、しっとりして良かった」  使用体験の捏造
    × 「ファンケルはコスパがいい」         こちらで裏を取れない評価

褒めない。薦めない。リンクも付けない。
「へえ」と気づいただけ、という距離感にとどめる。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..rakuten.models import RakutenItem


@dataclass(frozen=True)
class BrandMurmur:
    """商品名から確かめられる事実ひとつと、その言い方。"""

    id: str
    # 商品名にこの語があるときだけ使える
    trigger: str
    # {brand} にブランド名が入る
    templates: tuple[str, ...]


# trigger は商品名に対する単純な包含判定。
# **推測で増やさない。** 商品名を見れば誰でも確かめられる語だけ。
BRAND_MURMURS: tuple[BrandMurmur, ...] = (
    BrandMurmur("refill", "詰め替え", (
        "{brand}、詰め替えあるんだ",
        "{brand}に詰め替えあるの、いま知った",
        "{brand}の詰め替え、あるんだね",
    )),
    BrandMurmur("refill_kana", "つめかえ", (
        "{brand}、つめかえ用あるんだ",
        "{brand}のつめかえ、見つけた",
    )),
    BrandMurmur("additive_free", "無添加", (
        "{brand}って無添加なんだ",
        "{brand}、無添加って書いてある",
    )),
    BrandMurmur("non_silicone", "ノンシリコン", (
        "{brand}、ノンシリコンなんだ",
        "{brand}ってノンシリコンだったんだ",
    )),
    BrandMurmur("new", "新作", (
        "{brand}の新作出てる",
        "{brand}、新作あるんだ",
    )),
    BrandMurmur("large", "大容量", (
        "{brand}に大容量あるんだ",
        "{brand}の大容量、あるんだね",
    )),
    BrandMurmur("travel", "トラベル", (
        "{brand}のトラベルサイズあるんだ",
    )),
    BrandMurmur("set", "セット", (
        "{brand}、セットもあるんだ",
    )),
    BrandMurmur("limited", "限定", (
        "{brand}の限定、出てる",
    )),
    BrandMurmur("sensitive", "敏感肌", (
        "{brand}、敏感肌向けって書いてある",
    )),
)


def murmur_for(item: RakutenItem, *, cursor: int = 0) -> str | None:
    """商品からつぶやきを1本作る。作れなければ None。

    None を返すのは次のとき。呼び出し側は従来のつぶやきに戻すこと。

      * ブランド名が取れない（「メール便」を出すくらいなら黙る）
      * 商品名から確かめられる事実が無い
    """
    brand = item.brand_name
    if not brand:
        return None

    haystack = item.item_name or ""
    matched = [m for m in BRAND_MURMURS if m.trigger in haystack]
    if not matched:
        return None

    chosen = matched[cursor % len(matched)]
    template = chosen.templates[cursor % len(chosen.templates)]
    return template.format(brand=brand)
