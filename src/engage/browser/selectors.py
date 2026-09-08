"""セレクタの一覧。**Threads の DOM が変わったときに直す唯一のファイル。**

## 推測で書かない

Threads の class は難読化されていて（`.x1i10hfl` のような）予告なく変わる。
ここに書くセレクタは `scripts/probe_threads_dom.py` で実測してから書く。

各エントリの `measured` は「実際のページで確かめたか」。False のものは
`autoreply --selfcheck` が警告する。**推測のまま本番で使わない。**

## 頑丈さの順番

    1. 構造・意味        a[href*="/post/"] / time[datetime] / article
    2. ARIA role + 名前  get_by_role("button", name="返信")
    3. data-* / aria-*   [aria-label*="いいね"]
    4. テキスト内容      最後の手段
    5. class             **絶対に使わない**

Meta の日本語 aria-label はローカライズキー由来なので、難読化 class より
はるかに寿命が長い。2 を主軸に置く。

## 静かな失敗を怖がる

セレクタが「見つからない」のは気づける。怖いのは**位置だけ変わって
別の数字を読み続ける**こと。順位付けだけが静かに劣化してエラーにならない。
`extract.py` が「候補の8割超で likes==0」を検知して止める。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...errors import SelectorMissError

# 難読化された class。**ここに当たるセレクタは書かない。**
OBFUSCATED_CLASS = re.compile(r"\.x[0-9a-z]{5,}")

# どこに現れるか。点検（--selfcheck）がどこで探すかを決める。
#
# これを分けないと点検が意味を持たない。返信欄はダイアログを開くまで
# 存在しないので、タイムラインで探して「無い」と言っても誤警報にしかならず、
# 毎回赤くなる点検は誰も見なくなる。
WHERE_FEED = "feed"    # タイムラインに常にある。**無ければ異常**
WHERE_MODAL = "modal"  # 返信ダイアログを開くと現れる
WHERE_RARE = "rare"    # 該当する投稿にだけ現れる。**無いのが正常**


@dataclass(frozen=True)
class Selector:
    """1つの要素をどう見つけるか。"""

    key: str
    css: str | None = None
    """第一候補。構造や属性で引くもの。"""

    role: tuple[str, str] | None = None
    """(role, アクセシブル名) で引く。Playwright の get_by_role に渡す。"""

    exact: bool = False
    """アクセシブル名を完全一致で見るか。

    Playwright の既定は部分一致なので、「投稿」で引くと
    「投稿オプション」まで拾ってしまう（2026-09-04 実測）。
    """

    fallbacks: tuple[str, ...] = ()
    """css が空振りしたときに順に試す。"""

    note: str = ""
    """なぜこの形か + 実測日。**空は許さない。**"""

    measured: bool = False
    """実際のページで確かめたか。False なら selfcheck が警告する。"""

    required: bool = False
    """これが引けなければ実行を止めるか。返信の入力欄と送信ボタンは必須。"""

    where: str = WHERE_FEED
    """どこで探せば見つかるか。点検がこれを見て探す場所を変える。"""

    def candidates(self) -> tuple[str, ...]:
        """試す CSS を順に返す。"""
        return tuple(c for c in (self.css, *self.fallbacks) if c)


def _s(key: str, **kwargs) -> Selector:
    return Selector(key=key, **kwargs)


# ======================================================================
# 投稿の読み取り
# ======================================================================
_POST = (
    _s(
        "post_permalink",
        css='a[href*="/post/"]',
        note="投稿の起点。2026-09-04 実測でホームTLに17個。"
             "**この a が包んでいるのは時刻表示**（「18時間」）であって本文ではない。"
             "本文を得るには祖先の投稿カードまで遡る。",
        measured=True,
        required=True,
    ),
    _s(
        "post_container",
        css='div[data-pressable-container="true"]',
        fallbacks=("article",),
        note="投稿カード。2026-09-04 実測: article は**0個**、"
             "data-pressable-container が15個。Threads は article を使っていない。",
        measured=True,
    ),
    _s(
        "post_timestamp",
        css="time[datetime]",
        note="2026-09-04 実測で15個。ISO8601（2026-09-03T12:31:13.000Z）が入る。"
             "表示は「18時間」だが datetime 属性のほうが正確。",
        measured=True,
    ),
    _s(
        "post_body",
        css='div[data-pressable-container="true"]',
        note="**本文だけを指すセレクタは無い。** 2026-09-04 実測で "
             'div[dir="auto"] は0個。投稿カードの innerText を'
             "「著者名 / 時刻 / 本文 / 反応数」として解釈する"
             "（parse_search_block と同じ方針。実データで検証済み）。",
        measured=True,
    ),
    _s("post_author", css='a[href^="/@"]',
       note="著者リンク。2026-09-04 実測で47個（1カードに複数ある）。"
            "カードを特定してから使うこと。",
       measured=True),
)

# ======================================================================
# 反応のアイコン
#
# **2026-09-04 実測: aria-label に数値は入っていない。**
# 入っているのは操作名だけ（「いいね！」「コメントする」）で、
# 件数はアイコンの隣のテキストにある。
#
# なので反応数は投稿カードの innerText を後ろから読んで得る
# （`extract.py` / `parse_search_block`）。ここのセレクタは
# 「返信ボタンがどれか」を特定するために使う。
#
# 文言が想像と違ったので記録しておく:
#     リポスト → 「再投稿」（「リポスト」ではない）
#     いいね  → 「「いいね！」」（鉤括弧と感嘆符が入る）
#
# **さらに、返信アイコンの文言はログイン状態で変わる。**
#
#     ログアウト時  svg|コメントする   15個 / svg|返信 は 0個
#     ログイン時    svg|返信          11個 / svg|コメントする は 0個
#
# ログアウトで測ったまま本番（＝常にログイン）で使うと 0個 になる。
# 自動返信はログインして動くので「返信」を第一候補に置く。
# ======================================================================
_METRICS = (
    _s("metric_like", css='svg[aria-label="「いいね！」"]',
       fallbacks=('[aria-label*="いいね"]',),
       note="2026-09-04 実測で11〜15個。鉤括弧と ！ を含む。件数は含まない。",
       measured=True),
    _s("metric_reply", css='svg[aria-label="返信"]',
       fallbacks=('svg[aria-label="コメントする"]',),
       note="2026-09-04 実測。**ログイン時は「返信」（11個）、"
            "ログアウト時は「コメントする」（15個）。** 件数は含まない。",
       measured=True),
    _s("metric_repost", css='svg[aria-label="再投稿"]',
       note="2026-09-04 実測で11〜15個。**「リポスト」では0個。**",
       measured=True),
    _s("metric_share", css='svg[aria-label="シェアする"]',
       note="2026-09-04 実測で11〜15個。",
       measured=True),
    _s("edited_marker", css='svg[aria-label="編集済み"]',
       note="2026-09-04 実測。編集された投稿にだけ付く（TLに1個あった）。"
            "無いのが正常なので、当たり0でも異常ではない。",
       measured=True,
       where=WHERE_RARE),
    _s("truncated_marker", css='svg[aria-label="もっと見る"]',
       note="2026-09-04 実測で12〜17個。**タイムラインの本文は省略されている。**"
            "順位付けに使った本文が全文とは限らない、という根拠"
            "（verify.py がセンシティブ語を再走査する理由）。",
       measured=True),
)

# ======================================================================
# 返信を書く
# ======================================================================
_REPLY = (
    _s("reply_button", css='svg[aria-label="返信"]',
       fallbacks=('svg[aria-label="コメントする"]',),
       note="2026-09-04 実測（ログイン後の投稿詳細ページで20個）。"
            "**ログアウトで測ると「コメントする」になる。** 自動返信は"
            "ログインして動くので「返信」が第一候補。svg を直接 click() して"
            "コンポーザが開くことを実測で確認済み。",
       measured=True, required=True),

    _s("reply_dialog", css='div[role="dialog"][aria-modal="true"]',
       fallbacks=('div[role="dialog"]',),
       note="2026-09-04 実測。返信アイコンを押す前は0個、押すと1個現れる。"
            "aria-modal='true' が付く。",
       measured=True,
       where=WHERE_MODAL),

    _s("reply_composer",
       css='div[role="dialog"] div[contenteditable="true"][aria-placeholder*="返信"]',
       fallbacks=('div[role="dialog"] div[contenteditable="true"]',
                  'div[contenteditable="true"][aria-placeholder*="返信"]'),
       note="2026-09-04 実測。Lexical エディタ（data-lexical-editor=\"true\"）。"
            "**ダイアログの中に限定すること。** 投稿詳細ページには新規投稿用の "
            "contenteditable が開く前から1個あり、限定しないとそちらを掴んで"
            "**相手への返信ではなく自分の新規投稿を書いてしまう。**"
            "開いた後は同じ属性の要素が2個返るので first を使う。"
            "aria-placeholder が「〜に返信…」になっているのが返信欄の目印。",
       measured=True, required=True,
       where=WHERE_MODAL),

    _s("reply_submit", role=("button", "投稿"), exact=True,
       note="2026-09-04 実測。aria-label は無く、ボタンのテキストが「投稿」。"
            "**exact=True が必須。** 部分一致だと「投稿オプション」も拾い、"
            "ホームTLでは13個に当たった。同じダイアログには「キャンセル」と"
            "「スレッドに追加」（空のとき aria-disabled=true）もある。",
       measured=True, required=True,
       where=WHERE_MODAL),
)

# ======================================================================
# 状態の判定
# ======================================================================
_STATE = (
    _s("login_wall", css='svg[aria-label="ログイン"]',
       fallbacks=('a[href*="/login"]',),
       note="2026-09-04 実測: ログアウト時に両方とも1個、"
            "**ログイン時は両方とも0個**。ログイン判定の主軸。"
            "input[name=\"username\"] は0個だった（別ページにある）。",
       measured=True,
       where=WHERE_RARE),
    _s("logged_in_marker", css='a[href="/activity"]',
       fallbacks=('svg[aria-label="インサイト"]', 'svg[aria-label="保存済み"]'),
       note="2026-09-04 実測: ログイン時2個、ログアウト時0個。"
            "**svg[aria-label*=\"ホーム\"] を使ってはいけない。**"
            " ログアウト状態でも1個出る（ナビゲーションは未ログインでも"
            "描画される）。これでログイン判定すると、未ログインのまま"
            "返信を試み続けることになる。",
       measured=True),
    _s("post_deleted_marker", css='text="この投稿は利用できません"',
       note="削除・非公開。**投稿直前の再確認で使う。**"
            "通常のTLには出ないので、削除された投稿を開いて要実測。",
       where=WHERE_RARE),
    _s("replies_disabled_marker", css='text="返信できません"',
       note="返信が制限されている投稿。該当する投稿を開いて要実測。",
       where=WHERE_RARE),
    _s("error_state", css='text="エラーが発生しました。後ほどもう一度実行してください。"',
       fallbacks=('text="エラーが発生しました"',),
       note="2026-09-05 実測。**Threads がこちらを絞っているときに出る。**"
            "13アカウントのプロフィールを連続で開いたあと、プロフィールだけが"
            "この状態になった（ホームTLは正常に描画された）。"
            "0件だったのか、止められたのかを区別するために要る。",
       measured=True, where=WHERE_RARE),
    _s("feed_root", css='div[data-pressable-container="true"]',
       fallbacks=("main",),
       note="スクロールの目印。2026-09-04 実測で main は**0個**。"
            "Threads は main を使っていない。",
       measured=True),
    _s("profile_posts_tab", role=("tab", "スレッド"),
       note="プロフィールの投稿タブ。ホームTLには無い。"
            "プロフィールページで要実測。",
       where=WHERE_RARE),
)


REGISTRY: dict[str, Selector] = {
    s.key: s for s in (*_POST, *_METRICS, *_REPLY, *_STATE)
}


def get(key: str) -> Selector:
    if key not in REGISTRY:
        raise SelectorMissError(key, sorted(REGISTRY))
    return REGISTRY[key]


def all_keys() -> tuple[str, ...]:
    return tuple(REGISTRY)


def unmeasured() -> tuple[str, ...]:
    """まだ実測で確かめていないもの。selfcheck が警告する。"""
    return tuple(key for key, s in REGISTRY.items() if not s.measured)


def required_keys() -> tuple[str, ...]:
    """引けなければ実行を止めるもの。"""
    return tuple(key for key, s in REGISTRY.items() if s.required)


def keys_where(where: str) -> tuple[str, ...]:
    """その場所で探すべきキー。"""
    return tuple(key for key, s in REGISTRY.items() if s.where == where)
