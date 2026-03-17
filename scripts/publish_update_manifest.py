#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

try:
    import oss2
except Exception as e:
    raise SystemExit(f"缺少依赖 oss2：{e}\n请先执行: pip install oss2")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _must(name: str) -> str:
    v = _env(name)
    if not v:
        raise SystemExit(f"缺少环境变量：{name}")
    return v


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=True)

    parser = argparse.ArgumentParser(description="发布软件更新 manifest 到 OSS")
    parser.add_argument("--version", required=True, help="最新版本号，例如 1.0.3")
    parser.add_argument("--mac-url", default="", help="macOS 安装包下载链接（dmg/pkg/zip）")
    parser.add_argument("--win-url", default="", help="Windows 安装包下载链接（exe/msi/zip）")
    parser.add_argument("--notes", default="", help="更新说明")
    parser.add_argument("--force", action="store_true", help="是否强制更新")
    parser.add_argument("--manifest-key", default=_env("AUTO_UPDATE_MANIFEST_KEY", "takealot/updates/update_manifest.json"))
    args = parser.parse_args()

    key_id = _must("OSS_ACCESS_KEY_ID")
    key_secret = _must("OSS_ACCESS_KEY_SECRET")
    bucket_name = _must("OSS_BUCKET")
    endpoint = _must("OSS_ENDPOINT")
    base_url = _env("OSS_BASE_URL")

    manifest = {
        "latest_version": args.version.strip(),
        "force": bool(args.force),
        "notes": args.notes.strip(),
        "files": {
            "macos": args.mac_url.strip(),
            "windows": args.win_url.strip(),
        },
        "sha256": {
            "macos": "",
            "windows": "",
        },
    }

    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    key = args.manifest_key.strip().lstrip("/")
    bucket.put_object(key, payload, headers={"Content-Type": "application/json; charset=utf-8"})

    if base_url:
        url = f"{base_url.rstrip('/')}/{key}"
    else:
        url = f"https://{bucket_name}.{endpoint}/{key}"

    print("✅ manifest 发布成功")
    print(f"OSS Key: {key}")
    print(f"URL: {url}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

