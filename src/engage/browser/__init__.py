"""Threads Web のブラウザ操作。

## playwright を import するのは session.py だけ

他のモジュールは `page` を受け取るだけで、playwright の型にも API にも
依存しない。こうしておくと、

  * playwright を入れていない環境でも `src.engage.*` を import できる
  * ブラウザを起動せずにロジックをテストできる
  * requirements.txt にブラウザ依存を持ち込まずに済む
    （投稿パイプラインはブラウザを必要としない。
      .github/workflows/research.yml のコメント参照）

playwright は requirements-browser.txt に分けてある。
`tests/test_autoreply_no_browser_import.py` がこの境界を機械的に見張る。

## ログイン状態を使う

`scripts/collect_threads.py` の研究収集は**ログインしない**。
あちらは CI から共有IPで動くので、アカウントを機械操作に晒さない。

こちらは自分のマシンから自分のIPで動き、永続プロファイルで
ログイン状態を保つ。返信するにはログインが要るため。
"""
