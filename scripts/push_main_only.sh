#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== 仅推送 main（不发版本） ==="
echo

if [ ! -d .git ]; then
  echo "当前目录不是 Git 仓库，请先运行：start_setup_github_cloud_build.command"
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "未配置 origin 远程，请先运行：start_setup_github_cloud_build.command"
  exit 1
fi

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

push_with_retry() {
  local max_try=3
  local i
  for i in $(seq 1 $max_try); do
    if git push origin main; then
      return 0
    fi
    echo "第 $i/$max_try 次推送失败，10 秒后重试..."
    sleep 10
  done
  return 1
}

stage_safe() {
  git add .
  local has_large=0
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    local sz
    sz=$(wc -c <"$f" | tr -d ' ')
    if [ "$sz" -ge 95000000 ]; then
      git reset -q HEAD -- "$f" || true
      echo "⚠️ 已跳过超大文件（>${sz} bytes）：$f"
      has_large=1
    fi
  done < <(git diff --cached --name-only)
  if [ "$has_large" -eq 1 ]; then
    echo "提示：发布包请走 OSS 上传流程，不要提交到 git。"
  fi
}

auto_setup_proxy

echo "正在提交改动..."
stage_safe
if git diff --cached --quiet; then
  echo "没有改动需要提交。"
else
  read -r -p "请输入提交说明（留空用默认）: " MSG
  MSG="${MSG:-chore: sync changes}"
  git commit -m "$MSG"
fi

echo
echo "正在推送 main ..."
if ! push_with_retry; then
  echo "推送 main 失败：请换网络后再重试。"
  exit 1
fi

echo
echo "✅ 已推送到 origin/main（未创建 tag，不触发发版流程）。"
