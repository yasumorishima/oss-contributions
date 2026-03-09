#!/bin/bash
# upstream/main から feature ブランチを切る（フォーク専用ファイル混入防止）
# Usage: bash new-feature-branch.sh <branch-name>
# Example: bash new-feature-branch.sh fix/issue-123-fix-bug
#
# 事前条件:
#   - upstream リモートが設定済み
#     (未設定の場合: git remote add upstream https://github.com/<upstream-repo>.git)

set -e

BRANCH_NAME="$1"

if [ -z "$BRANCH_NAME" ]; then
  echo "Usage: bash new-feature-branch.sh <branch-name>"
  exit 1
fi

# upstream リモート存在確認
if ! git remote get-url upstream &>/dev/null; then
  echo "❌ upstream リモートが未設定です"
  echo "   git remote add upstream https://github.com/<upstream-owner>/<repo>.git"
  exit 1
fi

# upstream の最新を取得
echo "⬇️  upstream の最新を fetch 中..."
git fetch upstream

# upstream のデフォルトブランチを検出（main / master）
DEFAULT=$(git remote show upstream | grep 'HEAD branch' | awk '{print $NF}')
echo "ℹ️  upstream デフォルトブランチ: $DEFAULT"

# upstream/main からブランチを切る
git checkout -b "$BRANCH_NAME" "upstream/$DEFAULT"

echo ""
echo "✅ ブランチ '$BRANCH_NAME' を upstream/$DEFAULT から作成しました"
echo ""
echo "⚠️  注意: fork の main にある .coderabbit.yaml / codeql.yml は"
echo "   このブランチには含まれていません（混入防止済み）"
