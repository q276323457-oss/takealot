#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

open_actions_page() {
  local remote url
  remote="$(git remote get-url origin 2>/dev/null || true)"
  if [ -z "$remote" ]; then
    echo "未检测到 origin 仓库地址。"
    return 0
  fi
  url="$remote"
  url="${url%.git}"
  if [[ "$url" == git@github.com:* ]]; then
    url="https://github.com/${url#git@github.com:}"
  fi
  open "$url/actions"
}

while true; do
  clear
  echo "==========================================="
  echo " 西安众创 Takealot 一键工具箱"
  echo "==========================================="
  echo "1) 初始化 GitHub 云打包（首次用）"
  echo "2) 发布新版本并触发 Win 云打包"
  echo "3) 打开 GitHub Actions 页面"
  echo "4) 退出"
  echo
  read -r -p "请输入选项(1-4): " CHOICE

  case "$CHOICE" in
    1)
      bash "$ROOT/scripts/setup_github_cloud_build.sh"
      ;;
    2)
      bash "$ROOT/scripts/release_win_cloud.sh"
      ;;
    3)
      open_actions_page
      ;;
    4)
      exit 0
      ;;
    *)
      echo "输入无效，请输入 1-4。"
      ;;
  esac

  echo
  read -r -p "按回车继续..."
done
