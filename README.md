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
