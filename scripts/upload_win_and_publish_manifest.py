#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import warnings
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    import oss2
except Exception as e:
    raise SystemExit(f"缺少依赖 oss2：{e}\n请先执行: pip install oss2")

try:
    import requests
except Exception as e:
    raise SystemExit(f"缺少依赖 requests：{e}\n请先执行: pip install requests")

try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass


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


def _download_package_from_url(root: Path, package_url: str, version: str) -> Path:
    url = package_url.strip()
    if not url:
        raise SystemExit("package-url 为空")

    def _github_api_artifact_url(u: str) -> tuple[str, str, str] | None:
        """
        支持两种 GitHub 页面链接：
        1) https://github.com/{owner}/{repo}/actions/runs/{run_id}/artifacts/{artifact_id}
        2) https://github.com/{owner}/{repo}/actions/artifacts/{artifact_id}
        返回: (api_url, owner, repo)
        """
        m1 = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/actions/runs/\d+/artifacts/(\d+)(?:/.*)?$", u)
        if m1:
            owner, repo, aid = m1.group(1), m1.group(2), m1.group(3)
            return (f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{aid}/zip", owner, repo)
        m2 = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/actions/artifacts/(\d+)(?:/.*)?$", u)
        if m2:
            owner, repo, aid = m2.group(1), m2.group(2), m2.group(3)
            return (f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{aid}/zip", owner, repo)
        return None

    def _get_github_token(owner_hint: str = "") -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN"):
            v = _env(key)
            if v:
                return v
        # macOS: 尝试从 git 钥匙串读取
        if os.sys.platform.startswith("darwin"):
            user_candidates = [owner_hint, _env("GITHUB_USER", "")]
            for user in user_candidates:
                if not user:
                    continue
                try:
                    proc = subprocess.run(
                        ["git", "credential-osxkeychain", "get"],
                        input=f"protocol=https\nhost=github.com\nusername={user}\n",
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if proc.returncode == 0:
                        for line in proc.stdout.splitlines():
                            if line.startswith("password="):
                                token = line.split("=", 1)[1].strip()
                                if token:
                                    return token
                except Exception:
                    pass
        return ""

    headers = {"User-Agent": "takealot-autolister-uploader/1.0"}
    github_art = _github_api_artifact_url(url)
    if github_art:
        api_url, owner, _repo = github_art
        token = _get_github_token(owner_hint=owner)
        if not token:
            raise SystemExit(
                "这是 GitHub Actions Artifact 页面链接，需要 Token 才能下载。\n"
                "请先设置环境变量 GITHUB_TOKEN，或在工具箱前执行：\n"
                "export GITHUB_TOKEN=你的GitHubToken"
            )
        url = api_url
        headers["Authorization"] = f"token {token}"
        headers["Accept"] = "application/vnd.github+json"

    tmp_dir = root / ".runtime" / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    base = Path(parsed.path).name or f"TakealotAutoLister-win-{version}.zip"
    if not base.lower().endswith(".zip"):
        base = f"TakealotAutoLister-win-{version}.zip"
    final_path = tmp_dir / base
    part_path = final_path.with_suffix(final_path.suffix + ".part")

    print(f"⬇️ 从 URL 下载 Windows 包：{url}")
    with requests.get(url, stream=True, timeout=120, headers=headers) as r:
        r.raise_for_status()
        with part_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)

    if final_path.exists():
        final_path.unlink()
    part_path.rename(final_path)
    print(f"✅ 下载完成：{final_path}")
    return final_path.resolve()


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
    parser.add_argument("--package-url", default="", help="Windows zip 下载链接（例如 GitHub Release 链接）")
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

    package_url = args.package_url.strip()
    if package_url:
        package_path = _download_package_from_url(root, package_url, version)
    else:
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
