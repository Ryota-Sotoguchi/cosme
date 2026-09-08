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

#### 例外1: 他人の投稿への**返信文**（`src/engage/`）

返信は相手の投稿に依存するので、テンプレート合成では書けない。ここだけ LLM を使う。

- **サブスクリプションの `claude` CLI をサブプロセスで呼ぶ**（`src/engage/llm.py`）。
  これなら追加課金が発生しないので「0円」の根拠は保たれる。
- **Anthropic の HTTP API は使わない。`anthropic` を `requirements.txt` に入れない。**
- **これ以上は緩めない。** 投稿文の生成は今後もルールベースのまま。

#### 例外2: 自動返信のローカル記録（`data/engage/engage.sqlite3`）

標準ライブラリの sqlite3。外部DBではなく、費用も依存も増えない。

- 見た投稿は年に数万行になり、TTL 剪定と成果の書き戻し（UPDATE）が要る。
  追記専用の JSONL には向かない形。
- **gitignore 済み。コミットしない。** 自動返信はローカル実行専用なので支障はない。
- 運用履歴（`data/history.jsonl` / `data/state.json`）は従来どおり JSONL。触らない。

### 3. Secret を絶対にコミットしない

- 認証情報は GitHub Secrets と `.env`（gitignore 済み）のみ。
- `data/` は public リポジトリに公開される。**アフィリエイトURLの生値も保存しない**
  （`History.append()` が保存時に自動で伏せる。この処理を外さないこと）。
- ログにも出ないよう `src/logging_setup.py` がマスクしている。

### 4. 使った人の感想は使う。自分が使ったことにはしない（最重要）

境界はここ。**「誰の感想か」を偽らない。**

**やること** — 実際に使った人の感想は投稿の主役にする。
`scripts/collect_reviews.py` が楽天のレビューから使用感を数え、
`data/voices.json` に貯める。`src/content/voices.py` の `voice_sentence()` が
「誰の感想か」の分かる文にする。

- ○ 「べたつかないって書いてる人が多かった」
- ○ 「さっぱりっていう声が多かった」
- ○ 「使った人は伸びがいいって言ってる」

使っていない人間の「いいと思う」には根拠が無いが、
何百人が同じ使用感を書いているのは事実で、値段や件数より強い。

**やらないこと** — このアカウントは商品を使用していない。

- × 「使ってみた」「愛用」「リピ確定」「買ってよかった」「私の肌では」
- × 出典を消した「さっぱりのタイプ」（誰の感想か分からず、こちらの体験に読める）
- × 拾っていないレビューの創作、API から取得していない事実の補完

拾ってよいのは**使用感（テクスチャ・香り・使い勝手）だけ**。
効能・肌の変化に触れたレビューは `FORBIDDEN_IN_VOICES` で集計から丸ごと外す
（薬機法。体験談を効能効果の証明に使うことはできない）。

`src/compliance/rules.py` が一人称の使用主張を機械的に検出して投稿を止める。
**このルールを緩める変更は原則として入れない。**
ただし *第三者の感想の引用* まで巻き込んで弾かないこと
（2026-09-03 まで巻き込んでいて、正直な言い方だけが禁止される状態だった）。

### 4-2. 「レビュー」という言葉を投稿文・返信文に出さない

§4 と衝突しない。**禁じているのは語であって、感想を使うことではない。**

- ○ 「べたつかないって書いてる人が多かった」（誰の感想かは分かる。語は使わない）
- × 「レビュー見てると…」「レビュー2,000件超え」「口コミでは…」

言い換えも**しない**。「買った人が多い」「たくさんの人が選んでる」は
compliance を通るが、レビュー ⊆ 購入者なので §4 の
「API から取得していない事実の補完」に触れる。

**データは選定とスコアリングに残す。**
`scoring.weights.review_count` / `selection.min_review_count` /
`min_review_average` はそのまま。客観性の背骨は読者から見えない場所に残る。
評価の言い回し（`approx_review_average` の「評価もかなり高い」）も残す
— レビュー由来だが語を使っていない。

なぜ: このアカウントの仕事は**日常で役に立つこと**を言って有益だと
見なされること。アフィリエイトリンクは「誰が貼ったか」で押されるので、
そこが崩れると収益目標そのものが崩れる。

`tests/test_no_review_word.py` が、文章パーツの全プール・**TEMPLATES を
走査して生成した投稿**・コードの文字列リテラル（AST）・返信文を検査する。
`src/engage/review.py` の `REVIEW_WORDS` が返信側を機械的に止める。

なお `approx_review_count`（おおよその件数）と `review_heavy` 型は、
存在理由がこの語そのものだったので廃止した。722表示はまとめ形式の
**形**の成果で、`postage_free` が同じ形で575表示を出している。

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
python -m pytest tests/ -q     # 1207件
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

### 自動返信（`src/engage/`）— 別系統・ローカル実行専用

他人の投稿に返信して露出を増やす経路。**GitHub Actions では動かさない。**

```
runner.py が全体を統括
  1. sources/          収集（search / timeline / accounts。追加は REGISTRY に1行）
  2. store.py          既読を除く（sqlite）
  3. buzz.py           順位付け（伸びの速度。既存 candidates.score() が土台）
  4. judge.py          返信して**安全か**（LLM）
  5. writer.py         返信を書く（LLM）→ review() と similarity() で機械検査
  6. judge.py          出していいか（LLM）→ 落ちたら指摘を返して書き直す
  7. verify.py         **投稿直前の再確認**  ← 合わなければ返さない
  8. executor.py       投稿 → 着弾確認
  9. store.py + review.py  記録（sqlite と JSONL の両方）
```

守っていること:

- **投稿するには鍵が2つ要る。** `[autoreply] enabled = true` と `--live`。既定は DRY_RUN
- **枠は収集元ごと**（`[autoreply.daily_quota]`）。全体の上限
  （`[engagement] max_per_day`）は**手動返信と共有**する。
  Meta が見るのは経路ではなくアカウント単位の合計
- **返信と返信の間は45〜90分あける。** 実行をまたいで効く（前回の返信時刻から計算）
- **絞られたら6時間止まる。** `ThrottledError` → `autoreply_throttled_until`。
  2026-09-05 に実際に絞られたので入れた。信号を握りつぶすと次の発火が突っ込む
- **日本語の投稿にだけ返す。** 文言と感情を合わせる（`prompts.py`）
- **判断できなかったら返さない。** LLM の応答が読めなければ却下に倒す
- **着弾を確認できなくても再送しない。** 二重投稿になる
- **ブラウザ操作は `browser/session.py` だけが playwright を import する。**
  `tests/test_autoreply_no_browser_import.py` がこの境界を見張る
- **セレクタは `browser/selectors.py` に集約し、推測で書かない。**
  `scripts/probe_threads_dom.py` で実測してから `measured=True` にする

## 定期的にやること

作って終わりにすると、設計時に見えなかった問題が積み上がる。
初回の分析（2026-09-03）で出たのは、どれも運用を始めるまで
分からなかったものだった（リンクが3ホップ先／反応数が自己リプライで汚染／
定期実行が最大11時間半ずれる）。同種の問題は出続ける前提で回す。

| 頻度 | やること |
|---|---|
| 毎日（自動） | `insights` が成績を取得。`research` が競合の投稿とレビューの使用感を集める |
| 毎週（自動） | `review.yml` が `report` を `research/review/YYYY-MM-DD.md` に残す |
| 毎週（人） | その週のファイルの **点検** 欄だけ見る。空なら config を触らない |
| 月1（人） | 楽天のレポートCSVを `revenue --csv` で取り込む。これが無いと CTR も EPC も出ない |

**自動では config を変えない。** 標本が薄いうちに枠を組み替えて
失敗した前例がある（n=3〜6 の実績で組み替え、翌週に根拠が消えた）。
点検欄が「標本が5本未満の型がある」と言っているうちは、その型を動かさない。

## 変更するときの注意点

| 対象 | 注意 |
|---|---|
| `content/templates.py` | **数値リテラルを書かない**。数値は `facts.py` 経由のみ。compliance のデータ整合性チェックが落ちる |
| `content/parts.py` | 追加した文言は `test_every_phrase_part_is_free_of_ng_expressions` で自動検査される。トピック文に数字を入れない |
| `content/facts.py` | 数値は**おおよそで出す**（`approx_price` / `approx_review_count`）。値札の桁をそのまま書かない。丸めは**実際より安く見せない向き**に固定してあり `test_approx_numbers.py` が見張る |
| `content/voices.py` | 拾ってよいのは使用感だけ。効能の語を `TEXTURE_WORDS` に入れない。文型は必ず「誰の感想か」が分かる形（`voice_sentence`） |
| `config.toml` の `[[schedule]]` | `.github/workflows/post.yml` の cron と**両方**直す。`test_schedule.py` が突き合わせる |
| `config.toml` の `max_drift_minutes` | 定期実行のずれは実測で中央値232分ある。**小さくすると全投稿がスキップされる**（90分にして直近30本すべてが該当した）。時間帯の食い違いは `parts.time_band_at` が実際の時刻から言い回しを選ぶことで直してある |
| `src/engage/` | 自動返信は**ローカル実行専用**。`.playwright/`（セッション Cookie）と `data/engage/` は絶対にコミットしない。ワークフローが `git add data/` を実行するので、.gitignore を外さないこと |
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
