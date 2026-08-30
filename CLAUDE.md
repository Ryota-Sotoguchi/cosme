# CLAUDE.md

このリポジトリを Claude Code で触るときの前提。**読まずに変更しないこと。**

## このプロジェクトは何か

楽天市場の商品を紹介し、アフェリエイトとして継続的な収益化をすることが目的。
Threads アカウント「コスメ買い物メモ｜コスパ美容」の自動運用システム。

## 最優先の前提

### 1. これは本番稼働中のシステム

実在の Threads アカウントに実際に投稿される。ローカルでの実験は
必ず `DRY_RUN=true`（既定値）または `preview` サブコマンドで行うこと。

```bash
python -m src.main preview --slot noon    # 生成して表示するだけ。保存も投稿もしない
python -m src.main post --slot noon --dry-run
```

### 2. コストは 0円 に保つ

- 投稿文の生成に **LLM API を使わない**。`src/content/` はルールベースの合成エンジン。
  「Claude に文章を書かせる」方向のリファクタは**この方針に反する**。
- GitHub Actions は public リポジトリなので実行時間無料。
- 外部DBを導入しない。履歴は `data/*.jsonl` をワークフローがコミットバックする。

### 3. Secret を絶対にコミットしない

- 認証情報は GitHub Secrets と `.env`（gitignore 済み）のみ。
- `data/` は public リポジトリに公開される。**アフィリエイトURLの生値も保存しない**
  （`History.append()` が保存時に自動で伏せる。この処理を外さないこと）。
- ログにも出ないよう `src/logging_setup.py` がマスクしている。

### 4. 架空の体験・口コミを書かない（最重要）

このアカウントは商品を使用していない。以下は**絶対に書かない**:

- 「使ってみた」「愛用」「リピ確定」「買ってよかった」「私の肌では」
- 「口コミでは○○という声が多い」「みんな○○と言っている」
- API から取得していない事実の補完

`src/compliance/rules.py` がこれらを機械的に検出して投稿を止める。
**このルールを緩める変更は原則として入れない。**

### 5. 美容・広告表現に注意

- 一般化粧品で標ぼうできる効能は薬機法で56項目に限定される。
  そもそもこのアカウントは**効能を語らない**設計にして、リスクを構造的に回避している。
- 「治る」「改善」「消える」「若返る」「必ず効く」等は `rules.py` で遮断。
- アフィリエイトリンクを含む投稿は、冒頭に **`【PR】`** が必須（ステマ規制）。
- 医薬品・医薬部外品・薬用・サプリ・育毛・美容医療は `config.toml` の
  `[exclusion]` で除外している。**この除外リストを安易に短くしない。**

### 6. 既存データを勝手に破棄しない

`data/history.jsonl` と `data/state.json` は運用の実績そのもの。

- `history.jsonl` … 重複防止（30日クールダウン）と類似度判定の根拠
- `state.json` … 運用開始日（ランプアップ判定）、ローテーション位置、トークン期限

消すと同じ商品を再投稿したり、ランプアップが最初からやり直しになる。
**フォーマットを変えるときは既存行を読める後方互換を維持すること。**

### 7. push 前に必ずテストする

```bash
python -m pytest tests/ -q     # 520件
python -m src.main selftest    # 認証情報なしで生成〜検証の経路を確認
```

テストは実際にバグを検出している（NG表現辞書へのパーツ混入、
データ整合性チェックの抜け道など）。落ちたら**テストを緩めるのではなく実装を直す**。

## アーキテクチャ

```
main.py → pipeline.py が全体を統括
  1. rakuten/client.py     商品取得
  2. selector/filters.py   除外（禁止カテゴリー・価格帯・在庫・レビュー）
  3. selector/scoring.py   スコアリング（料率の寄与に上限、偏りにペナルティ）
  4. content/builder.py    投稿文生成
  5. compliance/checker.py 独立した検証工程  ← ここで止まったら投稿しない
  6. threads/client.py     投稿 → GET で実在検証
  7. storage/history.py    記録
```

不合格なら別テンプレートで再生成 → それでもだめなら**その商品をスキップして次候補**。
1商品の問題で全体運用を止めない設計。

## 変更するときの注意点

| 対象 | 注意 |
|---|---|
| `content/templates.py` | **数値リテラルを書かない**。数値は `facts.py` 経由のみ。compliance のデータ整合性チェックが落ちる |
| `content/parts.py` | 追加した文言は `test_every_phrase_part_is_free_of_ng_expressions` で自動検査される。トピック文に数字を入れない |
| `config.toml` の `[[schedule]]` | `.github/workflows/post.yml` の cron と**両方**直す。`test_schedule.py` が突き合わせる |
| `rakuten/client.py` | 2026年の刷新で `accessKey` 必須・`Origin` 必須・ドメイン変更。記憶で書き換えない |
| `storage/history.py` | `append()` の URL 秘匿化を外さない |
| `config.toml` の `similarity_window` | **件数指定**なので、枠を増やすと射程の日数が縮む。`test_similarity_window_covers_two_weeks` が枠数 × 14日を要求する |
| リンクなし投稿の型を増やす／減らす | `Pipeline._no_link_fallbacks()` の順番も見る。在庫が尽きた型はここを辿って別の型に逃げる |

## 公式仕様（記憶で判断せず、変更時は再確認すること）

- 楽天: https://webservice.rakuten.co.jp/documentation/ichiba-item-search
  （エンドポイント `openapi.rakuten.co.jp/.../Search/20260701`、1秒1リクエスト以下）
- Threads: https://developers.facebook.com/docs/threads
  （500文字、250投稿/24h、長期トークン60日）
- GitHub Actions: public リポジトリは**60日間活動が無いとスケジュールが自動無効化**される。
  `post.yml` が `data/` をコミットバックすることで防いでいる。**この仕組みを外さないこと。**
