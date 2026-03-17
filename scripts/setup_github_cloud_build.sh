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
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
git config --global http.version HTTP/1.1

auto_setup_proxy() {
  if git config --global --get https.proxy >/dev/null 2>&1; then
    return 0
  fi
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    echo "检测到 Clash 代理端口 127.0.0.1:7890，自动配置 git 代理..."
    git config --global http.proxy http://127.0.0.1:7890
    git config --global https.proxy http://127.0.0.1:7890
    git config --global http.https://github.com.proxy http://127.0.0.1:7890
  fi
}

push_main() {
  local max_try=3
  local i
  for i in $(seq 1 $max_try); do
    if git push -u origin main; then
      return 0
    fi
    echo "第 $i/$max_try 次推送失败，10 秒后重试..."
    sleep 10
  done
  return 1
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
auto_setup_proxy

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
  echo "推送失败，可能是认证或网络问题。"
  echo "如果之前没配过 Token，请继续输入。"
  echo "Token 创建入口: https://github.com/settings/tokens/new"
  echo "权限建议: repo（私有仓库也可推送）"
  if store_github_cred; then
    echo "已写入 macOS 钥匙串，正在重试推送（最多 3 次）..."
    if ! push_main; then
      echo "仍然失败：请换个网络（手机热点）后再双击重试。"
      exit 1
    fi
  else
    exit 1
  fi
fi

echo
echo "✅ 初始化完成"
echo "下一步：打开 GitHub 仓库 -> Actions -> Build Windows Package -> Run workflow"
