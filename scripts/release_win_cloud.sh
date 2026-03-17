#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== 一键发布 Windows 云打包 ==="
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

push_with_retry() {
  local cmd="$1"
  local max_try=3
  local i
  for i in $(seq 1 $max_try); do
    if eval "$cmd"; then
      return 0
    fi
    echo "第 $i/$max_try 次推送失败，10 秒后重试..."
    sleep 10
  done
  return 1
}

read -r -p "请输入发版号（例如 1.0.1）: " VER
if [ -z "${VER:-}" ]; then
  echo "版本号不能为空。"
  exit 1
fi

TAG="v$VER"

echo
echo "正在提交改动..."
git add .
if git diff --cached --quiet; then
  echo "没有改动需要提交。"
else
  git commit -m "release: $TAG"
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "标签 $TAG 已存在，跳过创建。"
else
  git tag "$TAG"
fi

echo
echo "正在推送 main + $TAG ..."
if ! push_with_retry "git push origin main"; then
  echo "推送 main 失败：请换网络后再重试。"
  exit 1
fi
if ! push_with_retry "git push origin $TAG"; then
  echo "推送 tag 失败：请换网络后再重试。"
  exit 1
fi

echo
echo "✅ 发布完成：GitHub Actions 会自动构建 Windows 包。"
echo "查看路径：GitHub 仓库 -> Actions -> Build Windows Package"
