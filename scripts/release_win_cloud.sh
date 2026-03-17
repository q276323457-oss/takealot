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
git push origin main
git push origin "$TAG"

echo
echo "✅ 发布完成：GitHub Actions 会自动构建 Windows 包。"
echo "查看路径：GitHub 仓库 -> Actions -> Build Windows Package"
