#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_public_url(base_url: str, bucket_name: str, endpoint: str, key: str) -> str:
    key = key.lstrip("/")
    if base_url:
        return f"{base_url.rstrip('/')}/{key}"
    return f"https://{bucket_name}.{endpoint}/{key}"


def _find_win_package(root: Path, version: str, custom_path: str) -> Path:
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"指定的安装包不存在：{p}")
        return p

    exact = root / "dist" / f"TakealotAutoLister-win-{version}.zip"
    if exact.exists():
        return exact.resolve()

    cands = sorted((root / "dist").glob("TakealotAutoLister-win-*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    if cands:
        return cands[0].resolve()

    raise SystemExit("未找到 Windows 包。请先构建，或手动传 --package-path。")


def _load_existing_manifest(bucket: "oss2.Bucket", key: str) -> dict[str, Any]:
    try:
        obj = bucket.get_object(key.lstrip("/"))
        raw = obj.read()
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=True)

    parser = argparse.ArgumentParser(description="上传 Windows 包到 OSS，并更新 update manifest")
    parser.add_argument("--version", required=True, help="版本号，例如 1.1.1")
    parser.add_argument("--package-path", default="", help="本地 Windows zip 路径，留空则自动查找 dist/")
    parser.add_argument("--win-key", default="takealot/updates/TakealotAutoLister-win-{version}.zip", help="OSS 存储 key，支持 {version}")
    parser.add_argument("--manifest-key", default=_env("AUTO_UPDATE_MANIFEST_KEY", "takealot/updates/update_manifest.json"))
    parser.add_argument("--mac-url", default="", help="可选：mac 下载链接；留空会保留原 manifest 的 mac 链接")
    parser.add_argument("--notes", default="", help="更新说明")
    parser.add_argument("--force", action="store_true", help="是否强制更新")
    args = parser.parse_args()

    version = args.version.strip()
    if not version:
        raise SystemExit("version 不能为空")

    key_id = _must("OSS_ACCESS_KEY_ID")
    key_secret = _must("OSS_ACCESS_KEY_SECRET")
    bucket_name = _must("OSS_BUCKET")
    endpoint = _must("OSS_ENDPOINT")
    base_url = _env("OSS_BASE_URL")

    package_path = _find_win_package(root, version, args.package_path)
    win_key = args.win_key.format(version=version).strip().lstrip("/")
    manifest_key = args.manifest_key.strip().lstrip("/")
    if not win_key:
        raise SystemExit("win-key 不能为空")

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    print(f"⬆️ 上传 Windows 包到 OSS: {package_path}")
    print(f"   OSS Key: {win_key}")
    bucket.put_object_from_file(win_key, str(package_path), headers={"Content-Type": "application/zip"})
    win_url = _build_public_url(base_url, bucket_name, endpoint, win_key)
    win_sha = _sha256_file(package_path)

    existing = _load_existing_manifest(bucket, manifest_key)
    old_files = existing.get("files") if isinstance(existing.get("files"), dict) else {}
    old_sha = existing.get("sha256") if isinstance(existing.get("sha256"), dict) else {}

    mac_url = args.mac_url.strip() or str(old_files.get("macos") or "")
    mac_sha = str(old_sha.get("macos") or "")
    notes = args.notes.strip() or str(existing.get("notes") or "")

    manifest = {
        "latest_version": version,
        "force": bool(args.force),
        "notes": notes,
        "files": {
            "macos": mac_url,
            "windows": win_url,
        },
        "sha256": {
            "macos": mac_sha,
            "windows": win_sha,
        },
    }

    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    bucket.put_object(manifest_key, payload, headers={"Content-Type": "application/json; charset=utf-8"})
    manifest_url = _build_public_url(base_url, bucket_name, endpoint, manifest_key)

    print("✅ 发布完成")
    print(f"Windows URL: {win_url}")
    print(f"Manifest URL: {manifest_url}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
