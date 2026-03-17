#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ensure_venv() {
  if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "未检测到 .venv，正在创建并安装依赖（首次会稍慢）..."
    python3 -m venv "$ROOT/.venv"
    "$ROOT/.venv/bin/python" -m pip install -U pip
    "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
  fi
}

run_py() {
  ensure_venv
  "$ROOT/.venv/bin/python" "$@"
}

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

init_license_keys() {
  run_py "$ROOT/scripts/init_license_keys.py"
}

gen_license_token_interactive() {
  local machine card_id days product
  read -r -p "请输入用户机器码: " machine
  if [ -z "${machine:-}" ]; then
    echo "机器码不能为空。"
    return 1
  fi
  read -r -p "请输入卡号标识（如 CARD-001）: " card_id
  if [ -z "${card_id:-}" ]; then
    echo "卡号不能为空。"
    return 1
  fi
  read -r -p "有效天数（默认 365）: " days
  days="${days:-365}"
  read -r -p "产品标识（默认 takealot-autolister）: " product
  product="${product:-takealot-autolister}"
  run_py "$ROOT/scripts/gen_license_token.py" --machine "$machine" --card-id "$card_id" --days "$days" --product "$product"
}

publish_manifest_interactive() {
  local ver mac_url win_url notes force yn
  read -r -p "版本号（如 1.0.8）: " ver
  if [ -z "${ver:-}" ]; then
    echo "版本号不能为空。"
    return 1
  fi
  read -r -p "mac 下载链接（可留空）: " mac_url
  read -r -p "win 下载链接（可留空）: " win_url
  read -r -p "更新说明（可留空）: " notes
  read -r -p "是否强制更新？(y/N): " yn
  force="false"
  if [ "${yn:-N}" = "y" ] || [ "${yn:-N}" = "Y" ]; then
    force="true"
  fi

  ensure_venv
  cmd=("$ROOT/.venv/bin/python" "$ROOT/scripts/publish_update_manifest.py" --version "$ver")
  if [ -n "${mac_url:-}" ]; then cmd+=(--mac-url "$mac_url"); fi
  if [ -n "${win_url:-}" ]; then cmd+=(--win-url "$win_url"); fi
  if [ -n "${notes:-}" ]; then cmd+=(--notes "$notes"); fi
  if [ "$force" = "true" ]; then cmd+=(--force); fi
  "${cmd[@]}"
}

while true; do
  clear
  echo "==========================================="
  echo " 西安众创 Takealot 一键工具箱"
  echo "==========================================="
  echo "1) 初始化 GitHub 云打包（首次用）"
  echo "2) 发布新版本并触发 Win 云打包"
  echo "3) 仅推送 main（不发版本）"
  echo "4) 打开 GitHub Actions 页面"
  echo "5) 初始化授权密钥（只做一次）"
  echo "6) 生成卡密（输入机器码）"
  echo "7) Mac 本地打包"
  echo "8) 发布更新清单到 OSS"
  echo "9) 退出"
  echo
  read -r -p "请输入选项(1-9): " CHOICE

  case "$CHOICE" in
    1)
      bash "$ROOT/scripts/setup_github_cloud_build.sh"
      ;;
    2)
      bash "$ROOT/scripts/release_win_cloud.sh"
      ;;
    3)
      bash "$ROOT/scripts/push_main_only.sh"
      ;;
    4)
      open_actions_page
      ;;
    5)
      init_license_keys
      ;;
    6)
      gen_license_token_interactive
      ;;
    7)
      bash "$ROOT/scripts/build_mac.sh"
      ;;
    8)
      publish_manifest_interactive
      ;;
    9)
      exit 0
      ;;
    *)
      echo "输入无效，请输入 1-9。"
      ;;
  esac

  echo
  read -r -p "按回车继续..."
done
