#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== GitHub 云打包初始化 ==="
echo "项目路径: $ROOT"
echo

if ! command -v git >/dev/null 2>&1; then
  echo "未检测到 git，请先安装 git 后重试。"
  exit 1
fi

if [ ! -d .git ]; then
  echo "检测到当前不是 Git 仓库，正在初始化..."
  git init
  git branch -M main
fi

if ! git config user.name >/dev/null; then
  git config user.name "Takealot Builder"
fi
if ! git config user.email >/dev/null; then
  git config user.email "builder@example.com"
fi

git config --global credential.helper osxkeychain

push_main() {
  git push -u origin main
}

store_github_cred() {
  local gh_user gh_token
  read -r -p "请输入 GitHub 用户名: " gh_user
  read -r -s -p "请输入 GitHub Token（不会回显）: " gh_token
  echo
  if [ -z "${gh_user:-}" ] || [ -z "${gh_token:-}" ]; then
    echo "用户名或 Token 为空，无法继续。"
    return 1
  fi
  cat <<EOF | git credential-osxkeychain store
protocol=https
host=github.com
username=$gh_user
password=$gh_token
EOF
}

echo
if git remote get-url origin >/dev/null 2>&1; then
  echo "当前远程 origin: $(git remote get-url origin)"
else
  read -r -p "请输入 GitHub 仓库地址（例如 https://github.com/你/仓库.git）: " REMOTE_URL
  if [ -z "${REMOTE_URL:-}" ]; then
    echo "未输入仓库地址，已取消。"
    exit 1
  fi
  git remote add origin "$REMOTE_URL"
fi

echo
echo "正在提交当前项目文件..."
git add .
if git diff --cached --quiet; then
  echo "没有新增改动需要提交。"
else
  git commit -m "chore: setup github windows cloud build"
fi

echo
echo "正在推送到 GitHub main 分支..."
if ! push_main; then
  echo
  echo "检测到 GitHub 认证失败，需要一次性配置账号令牌。"
  echo "Token 创建入口: https://github.com/settings/tokens/new"
  echo "权限建议: repo（私有仓库也可推送）"
  if store_github_cred; then
    echo "已写入 macOS 钥匙串，正在重试推送..."
    push_main
  else
    exit 1
  fi
fi

echo
echo "✅ 初始化完成"
echo "下一步：打开 GitHub 仓库 -> Actions -> Build Windows Package -> Run workflow"
