# cosme

楽天市場の商品を、**価格・レビュー・送料などの客観情報だけ**で紹介する
Threads アカウントの自動運用システム。

> **コスメ買い物メモ｜コスパ美容**
> 楽天で見つけた気になるコスメを、価格・レビュー・送料など公開情報ベースで整理🧴
> スキンケア・メイク・ヘアケア中心｜一部PR・アフィリエイトリンクを含みます

## 特徴

- **追加の月額コスト 0円** — GitHub Actions（public リポジトリは実行時間無料）+ 外部DBなし
- **投稿文の生成に LLM API を使わない** — ルールベースの合成エンジンで自然文を作る
- **使用体験を装わない** — 実際に使っていない商品を「使ってみた」と書かない。
  API から取得した事実だけを扱う
- **投稿前に独立したコンプライアンス検証** — 薬機法/ステマ規制/架空体験/データ整合性を機械的に検査
- **PCを閉じても動く** — ローカル環境に依存しない

## 仕組み

```
楽天API → 除外フィルタ → スコアリング → 商品選択
   → 投稿文生成 → Compliance Check → Threads投稿 → 実在検証 → 履歴保存
                        ↓ 不合格
                  別テンプレートで再生成 → だめなら次の商品へ
```

## 投稿スケジュール（JST・1日5投稿）

| 時刻 | スロット | 内容 | リンク |
|---|---|---|---|
| 07:30 | morning | 美容・買い物の観点 | なし |
| 12:15 | noon | 商品紹介 | **【PR】あり** |
| 18:00 | evening | 観点整理 | なし |
| 20:30 | night | 商品紹介 | **【PR】あり** |
| 22:30 | late | 比較／価格帯／レビュー整理／送料無料／リンクなし をローテーション | 可変 |

新規アカウントで広告を連投しないよう、**運用開始7日間はリンク投稿を1日1本**、
8〜14日目は2本に自動で抑える（`config.toml` の `[ramp_up]`）。

## セットアップ

### 1. 依存関係

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

必要なのは `requests` だけ。設定は標準ライブラリの `tomllib` で読む。

### 2. 認証情報

```bash
cp .env.example .env
```

`.env` に以下を設定する（**コミットしないこと**）。

| 変数 | 取得元 |
|---|---|
| `RAKUTEN_APPLICATION_ID` / `RAKUTEN_ACCESS_KEY` | [楽天ウェブサービス](https://webservice.rakuten.co.jp/) でアプリを新規登録 |
| `RAKUTEN_AFFILIATE_ID` | [楽天アフィリエイト](https://affiliate.rakuten.co.jp/) |
| `RAKUTEN_ORIGIN` | 楽天デベロッパーコンソールの「許可Webサイト」に登録したURL |
| `THREADS_ACCESS_TOKEN` | [Meta for Developers](https://developers.facebook.com/) の Threads アプリ |
| `THREADS_APP_SECRET` | 同上（長期トークンへの交換に必要） |

> **2026年の楽天API刷新について**
> ドメインが `openapi.rakuten.co.jp` に変わり、`applicationId` と `accessKey` の
> **両方**が必須になった。旧アプリIDは使えないので新規登録が必要。
> `Origin` ヘッダーが無いと 403 になる。

### 3. 本番用（GitHub Actions）

同じ名前で GitHub Secrets に登録する。

```bash
gh secret set RAKUTEN_APPLICATION_ID
gh secret set RAKUTEN_ACCESS_KEY
gh secret set RAKUTEN_AFFILIATE_ID
gh secret set RAKUTEN_ORIGIN
gh secret set THREADS_ACCESS_TOKEN
gh secret set THREADS_APP_SECRET
gh secret set GH_PAT              # 任意: トークン自動更新をSecretへ書き戻す場合
```

## コマンド

```bash
# 接続確認（楽天・Threads の疎通と実データの確認）
python -m src.main check

# 生成だけして表示する（保存も投稿もしない）
python -m src.main preview --slot noon

# 投稿（DRY_RUN 環境変数に従う。既定は true）
python -m src.main post --slot noon
python -m src.main post --slot noon --live    # 本番投稿

# スケジュールと次回実行時刻
python -m src.main schedule

# 認証情報なしで生成〜検証の経路を確認
python -m src.main selftest

# トークン管理
python -m src.main token --exchange <短命トークン>   # 60日トークンへ交換
python -m src.main token --refresh                   # 更新（24時間以上経過が条件）
```

## 自動返信（autoreply）— ローカル実行専用

伸び始めている他人の投稿に、Playwright で返信する経路。
露出を増やして、投稿を見てもらう入口を広げるのが目的。

**GitHub Actions では動かさない。** ログイン済みブラウザプロファイルが要り、
それは public リポジトリに置けないため。

### セットアップ

```bash
# 1. ブラウザ本体（requirements.txt とは分けてある）
pip install -r requirements-browser.txt
python3 -m playwright install chromium

# 2. システムライブラリ（**唯一 sudo が要るところ**）
#
#    `sudo playwright install-deps` は使えない。pip --user で入れると
#    root からパッケージが見えず、sudo は PATH も引き継がないため。
#    一覧は `python3 -m playwright install-deps --dry-run chromium` で出る。
sudo apt-get update && sudo apt-get install -y \
  libnspr4 libnss3 libasound2t64 libasound2-data \
  libfontenc1 libice6 libsm6 libxaw7 libxfont2 libxkbfile1 libxmu6 libxt6t64 \
  x11-xkb-utils xfonts-cyrillic xfonts-encodings xfonts-scalable xfonts-utils \
  xserver-common xvfb \
  fonts-liberation fonts-freefont-ttf fonts-unifont \
  fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf

# 3. LLM（サブスクリプションの claude CLI。API 課金なし）
#
#    sudo を避けるなら Node を ~/.local に展開する:
#      curl -fsSL https://nodejs.org/dist/v22.20.0/node-v22.20.0-linux-x64.tar.xz \
#        | tar -xJ --strip-components=1 -C "$HOME/.local"
#    ~/.profile が ~/.local/bin を PATH に足すので、新しい端末から有効になる。
npm install -g @anthropic-ai/claude-code
claude                                              # 初回だけ対話で認証

# 4. ログイン（手で。2段階認証もキャプチャも人がやる）
python -m src.main autoreply --login
```

> **日本語フォントを省かないこと。** `fonts-ipafont-gothic` が無いと
> 投稿本文が豆腐（□）になり、スクレイプした文字列も読めなくなる。

### コマンド

```bash
python -m src.main autoreply --llm-check     # claude CLI の疎通
python -m src.main autoreply --selfcheck     # セレクタが今日も効くか（20秒）
python -m src.main autoreply --collect-only  # 収集と順位付けだけ（LLMを呼ばない）
python -m src.main autoreply                 # DRY_RUN。返信案まで作って表示
python -m src.main autoreply --live          # 実際に返信する
python -m src.main autoreply --history       # これまでの自動返信
python -m src.main autoreply --explain <短縮ID>   # 1件の点数内訳と判断理由
```

### 何件・どの間隔で返すか

| | 1日の枠 | 相手 |
|---|---|---|
| タイムライン | 5件 | 反応の多い投稿。同じ相手には7日あける |
| 監視対象 | 3件 | `[[autoreply.targets]]`。**同じ相手に1日3件まで**（最短3時間おき） |
| 合計 | 8件 | `[engagement] max_per_day`。手動返信と共有 |

**返信と返信の間は45〜90分あける。** 実行をまたいで効く（前回の返信時刻から
計算するので、cron が何回発火しても間隔は縮まない）。間隔が足りない回は
収集と観測だけして返信を見送る — その回が次の回の伸び率の計算を作る。

**始めの2週間は自動で絞る**（`[autoreply.ramp_up]`）。1〜7日目は3件/日、
8〜14日目は5件/日、15日目以降に8件/日。日数は**自動返信が初めて返信した日**
から数える。人は何もしない。

### 1日にまわす回数

```cron
SHELL=/bin/bash
# タイムライン: 主力。2026-09-05 に絞られたときもホームTLは正常だった
7 7,9,11,13,15,17,19,21 * * * cd <repo> && sleep $((RANDOM % 1500)) && \
  flock -n /tmp/cosme-autoreply.lock env PATH=$HOME/.local/bin:$PATH \
  /usr/bin/python3 -m src.main autoreply --live --source timeline \
  >> /tmp/autoreply.log 2>&1

# 監視対象のプロフィール: 4回だけ。**開く回数そのものがリスク**
23 8,12,16,20 * * * cd <repo> && sleep $((RANDOM % 1500)) && \
  flock -n /tmp/cosme-autoreply.lock env PATH=$HOME/.local/bin:$PATH \
  /usr/bin/python3 -m src.main autoreply --live --source accounts \
  >> /tmp/autoreply.log 2>&1
```

**発火は返信ではなく機会。** 実測では1回あたりの候補は0〜5件で、その大半は
SNS運用系の挨拶投稿として却下される。8回発火しても8件は返せない。
件数を縛るのは上の表の枠と45〜90分の間隔で、どちらもコードで強制する。

`flock -n` は必須。2つのジョブが `.playwright/threads-profile` を同時に開くと
ブラウザの起動が落ちる。

止めたいときは crontab を消さなくても `touch data/engage/STOP` で足りる。

### 投稿するには鍵が2つ要る

`config.toml` の `[autoreply] enabled = true` **かつ** `--live`。
どちらか片方では投稿しない。既定は DRY_RUN。

止めたいときは設定を触らずに:

```bash
touch data/engage/STOP        # または AUTOREPLY_OFF=1
```

### 返信先アカウントを足す

`config/config.toml` の末尾に書く（`@` は付けない）。

```toml
[[autoreply.targets]]
username = "example_beauty"
note = "プチプラのレビュー。返信欄が動いている"
```

**`research/accounts.md` を流用しないこと。** あちらは文体の参考先で、
体験談・効能断定・美容医療で伸ばしているアカウントが入っている。
そこに返信すると触ってはいけない話題に踏み込む。

### 何を見て選んでいるか

いいねの絶対数では選ばない。**単位時間あたりどれだけ伸びているか**を見る。

| 要素 | 重み | 中身 |
|---|---|---|
| reaction | 3.0 | **反応の絶対数**（いいね + 返信×8）。ここが主軸 |
| base | 1.0 | 美容との関連度・勢い・新しさ（既存の `candidates.score()`） |
| velocity | 1.5 | 前回観測からの実測の伸び。初見の投稿は 0（推測で埋めない） |
| reply_fit | 0.0 | 返信数の逆U字。**上限に置き換えたので効いていない**（戻すのは設定1行） |

埋もれる投稿は減点ではなく**足切り**で外す（`max_replies`。タイムライン100件、
監視対象150件）。埋もれるかは「自分より上に何件コメントがあるか」で決まるので、
上限はいいねではなく返信数で見る。

### ログの見方

```bash
python -m src.main autoreply --history          # 返信の一覧。⚠️ は着弾未確認
python -m src.main autoreply --explain ABC123   # なぜ返した／返さなかったか
python -m src.main doctor                       # claude CLI とログインの状態
LOG_LEVEL=DEBUG python -m src.main autoreply    # 詳細
```

`data/engage/engage.sqlite3` に記録が入る（**gitignore 済み**）。

### ホームタイムラインだけでは候補が出ない（2026-09-04 実測）

Threads のホームは時系列ではなく**おすすめ順**なので、流れてくる投稿の
多くが19〜23時間前のもの。「返信が読まれるのは投稿から間もないうち」
という前提（`max_age_hours = 12`）と噛み合わない。

実測（各14件取得）:

| 収集元 | 12時間以内 | 経過時間の中央値 |
|---|---|---|
| ホーム（おすすめ） | 3/14 | 20.6時間 |
| `/following`（フォロー中） | **0/14** | 21.8時間 |

フォロー中のほうが新しいかと思ったが、**逆に悪かった**。
フォロー先の投稿頻度に依存するため。

**`[[autoreply.targets]]` を設定するのが本筋。** プロフィールは時系列で
並ぶので、よく投稿する美容アカウントを数件登録すれば新しい投稿が取れる。
既定では空なので、いまのままだとホーム頼みになって候補がほぼ出ない。

閾値を緩めて数を稼ぐこともできる（`[autoreply.filter]` の
`max_age_hours` / `min_likes`）が、**伸び切った投稿に返信しても
読まれない**という設計の前提を捨てることになるので、まず返信先の
アカウントを選ぶほうを勧める。

### 現時点の制限

- Threads の DOM は数週間で変わる前提。**投稿前に `--selfcheck` を通す。**
  必須セレクタは 2026-09-04 に実測済みだが、いつ壊れてもおかしくない
- **返信アイコンの文言はログイン状態で変わる**（ログイン時「返信」/
  ログアウト時「コメントする」）。測り直すときは必ずログインした状態で
- 返信の成果（24時間後のいいね数）を取る工程はまだ無い。
  DBの列と `update_outcome()` は用意してある
- ブラウザ自動操作はアカウント制限のリスクがある。**2026-09-05 に実際に絞られた**
  （プロフィールだけが「エラーが発生しました」を返すようになった）。
  検知したら6時間は全発火を止める（`autoreply_throttled_until`）
- 返信が実際に読まれたかを測る仕組みがまだ無いので、`max_replies` の
  100/150 は当て推量。成果ループ（`EngageStore.update_outcome()`）が次に来る変更

## DRY_RUN

`DRY_RUN=true`（既定）では、

楽天API → 商品取得 → スコアリング → 商品選択 → 投稿文生成 → Compliance Check → 最終本文表示

まで実行し、**Threads への POST だけを行わない**。

## テスト

```bash
.venv/bin/python -m pytest tests/ -q
```

145件。楽天レスポンス解析、スコアリング、除外カテゴリー、重複防止、価格・レビュー整合性、
NG表現、架空体験表現、PR表記、URLチェック、APIエラー、Rate Limit、トークン不足、
Secret不足、DRY_RUN、本番投稿処理、スケジュール整合性をカバーする。

## 設定

`config/config.toml` を編集する。コードを触らずに変えられるもの:

- 投稿時刻・投稿タイプ・ローテーション（`.github/workflows/post.yml` の cron も一緒に直すこと）
- 価格帯・対象ジャンル・レビュー下限
- 除外キーワード／ジャンル／ショップ
- スコアリングの重み、偏り抑制のペナルティ
- 再投稿クールダウン日数、類似度の閾値
- ランプアップ（初期のリンク投稿本数の制限）

## データ

| ファイル | 内容 |
|---|---|
| `data/history.jsonl` | 投稿履歴。重複防止と類似度判定の根拠 |
| `data/state.json` | 運用開始日、ローテーション位置、トークン期限 |

public リポジトリなので、**Secret とアフィリエイトURLの生値は保存しない**。
商品の同定は SHA-256 ハッシュで行う。

## コンプライアンス

投稿生成と投稿実行の間に、独立した検証工程を置いている。

- **薬機法** — 治療・改善・消失・若返り・発毛などの効能表現を遮断
- **効果保証** — 「絶対」「必ず」「100%」「副作用なし」を遮断
- **架空体験** — 「使ってみた」「愛用」「リピ確定」「私の肌では」を遮断
- **架空口コミ** — 「口コミでは」「みんな言っている」を遮断
- **ステマ規制** — アフィリエイトリンクを含む投稿は冒頭に `【PR】` を必須化
- **データ整合性** — 本文の数値がすべて商品データ由来かをコードで照合
- **重複** — 同一 itemCode / URL / 30日以内の再投稿 / 文章類似度 / テンプレート連続使用

不合格なら投稿せず、再生成 → 商品スキップの順で回復する。

## ライセンス / 免責

個人利用。投稿内容は楽天ウェブサービスから取得した投稿時点の情報に基づく。
価格・在庫・ポイントは変動する。
