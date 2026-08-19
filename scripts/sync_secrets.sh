#!/usr/bin/env bash
#
# .env の値を GitHub Secrets へ同期する。
#
#   ./scripts/sync_secrets.sh
#
# 値は標準出力に一切出さない。表示するのは変数名と
# 「設定済み(N文字)」/「未設定」だけ。
#
# 前提: gh CLI にログイン済みであること（gh auth login）
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

ENV_FILE=".env"
GH="${GH_BIN:-gh}"
command -v "$GH" >/dev/null 2>&1 || GH="$HOME/.local/bin/gh"

# GitHub Secrets へ同期する変数。
# DRY_RUN と LOG_LEVEL はワークフロー側で決めるので同期しない。
SECRETS=(
  RAKUTEN_APPLICATION_ID
  RAKUTEN_ACCESS_KEY
  RAKUTEN_AFFILIATE_ID
  RAKUTEN_ORIGIN
  THREADS_ACCESS_TOKEN
  THREADS_APP_SECRET
  THREADS_USER_ID
  GH_PAT
)

# 空でも運用が止まらない変数
OPTIONAL=(THREADS_USER_ID GH_PAT)

is_optional() {
  local name="$1"
  for entry in "${OPTIONAL[@]}"; do
    [ "$entry" = "$name" ] && return 0
  done
  return 1
}

# --- 事前チェック ------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  echo "エラー: $ENV_FILE がありません。" >&2
  echo "  cp .env.example .env  してから値を記入してください。" >&2
  exit 1
fi

if ! command -v "$GH" >/dev/null 2>&1; then
  echo "エラー: gh CLI が見つかりません。" >&2
  exit 1
fi

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "エラー: gh にログインしていません。次を実行してください:" >&2
  echo "  $GH auth login" >&2
  exit 1
fi

REPO="$("$GH" repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
if [ -z "$REPO" ]; then
  echo "エラー: GitHub リポジトリを特定できませんでした。" >&2
  exit 1
fi

echo
echo "同期先リポジトリ: $REPO"
echo "─────────────────────────────────────────────"

# --- .env の読み込み（サブシェルに閉じ込めない） ------------------
# export せず、この関数経由でだけ値を取り出す。
read_env_value() {
  local key="$1"
  # 最後に現れた定義を採用。コメント行は無視。
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
    | sed 's/[[:space:]]*$//' \
    | sed 's/^"\(.*\)"$/\1/' \
    | sed "s/^'\(.*\)'\$/\1/" \
    | tail -n 1
}

missing=0
synced=0
skipped=0

for name in "${SECRETS[@]}"; do
  value="$(read_env_value "$name")"

  if [ -z "$value" ]; then
    if is_optional "$name"; then
      printf '  %-24s 未設定（任意なのでスキップ）\n' "$name"
      skipped=$((skipped + 1))
    else
      printf '  %-24s ❌ 未設定（必須）\n' "$name"
      missing=$((missing + 1))
    fi
    continue
  fi

  # 値そのものは表示しない。長さだけ出す。
  if printf '%s' "$value" | "$GH" secret set "$name" --repo "$REPO" >/dev/null 2>&1; then
    printf '  %-24s ✅ 同期済み（%d文字）\n' "$name" "${#value}"
    synced=$((synced + 1))
  else
    printf '  %-24s ⚠️  同期に失敗\n' "$name"
    missing=$((missing + 1))
  fi
done

echo "─────────────────────────────────────────────"
echo "同期 ${synced}件 / スキップ ${skipped}件 / 問題 ${missing}件"
echo

if [ "$missing" -gt 0 ]; then
  echo "必須の値が足りません。.env を確認してください。" >&2
  exit 1
fi

echo "完了。登録済みの Secret 一覧:"
"$GH" secret list --repo "$REPO"
