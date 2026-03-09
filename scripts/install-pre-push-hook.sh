#!/bin/bash
# ローカルの fork リポジトリに pre-push 汚染チェックフックをインストールする
# Usage: bash install-pre-push-hook.sh <fork-repo-dir>
# Example: bash install-pre-push-hook.sh ~/Desktop/Claude_code/ezc3d

set -e

REPO_DIR="$1"

if [ -z "$REPO_DIR" ]; then
  echo "Usage: bash install-pre-push-hook.sh <fork-repo-dir>"
  exit 1
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "❌ $REPO_DIR は git リポジトリではありません"
  exit 1
fi

HOOK_PATH="$REPO_DIR/.git/hooks/pre-push"

# 既存フックがあれば上書き確認
if [ -f "$HOOK_PATH" ]; then
  echo "⚠️  既存の pre-push フックが見つかりました: $HOOK_PATH"
  read -r -p "上書きしますか？ [y/N]: " confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "キャンセルしました"
    exit 0
  fi
fi

cat > "$HOOK_PATH" << 'HOOK'
#!/bin/bash
# フォーク専用ファイルの upstream PR 混入を push 前にブロックする

FORK_ONLY=(
  ".coderabbit.yaml"
  ".github/workflows/codeql.yml"
)

# upstream リモートがなければスキップ
if ! git remote get-url upstream &>/dev/null; then
  exit 0
fi

git fetch upstream --quiet 2>/dev/null || exit 0

DEFAULT=$(git remote show upstream 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}')
if [ -z "$DEFAULT" ]; then
  exit 0
fi

CHANGED=$(git diff "upstream/$DEFAULT"...HEAD --name-only 2>/dev/null || echo "")

FAIL=false
for f in "${FORK_ONLY[@]}"; do
  if echo "$CHANGED" | grep -qxF "$f"; then
    echo ""
    echo "❌ PUSH BLOCKED: フォーク専用ファイルが diff に含まれています: $f"
    echo "   upstream PR に混入します。以下の手順で修正してください:"
    echo ""
    echo "   1. upstream/main から feature ブランチを切り直す:"
    echo "      git fetch upstream"
    echo "      git checkout -b <new-branch> upstream/$DEFAULT"
    echo "   2. 必要な変更のみを cherry-pick または手動で追加"
    echo ""
    FAIL=true
  fi
done

if [ "$FAIL" = true ]; then
  exit 1
fi

exit 0
HOOK

chmod +x "$HOOK_PATH"

echo "✅ pre-push フックを $REPO_DIR にインストールしました"
