from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class UpdateInfo:
    has_update: bool
    current_version: str
    latest_version: str
    download_url: str = ""
    notes: str = ""
    force: bool = False
    sha256: str = ""
    manifest_url: str = ""


def _norm_version(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(v or ""))
    if not parts:
        return (0,)
    nums = [int(p) for p in parts[:4]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def platform_key() -> str:
    p = os.sys.platform
    if p.startswith("darwin"):
        return "macos"
    if p.startswith("win"):
        return "windows"
    return "linux"


def manifest_url_from_env() -> str:
    direct = os.getenv("AUTO_UPDATE_MANIFEST_URL", "").strip()
    if direct:
        return direct

    base = os.getenv("OSS_BASE_URL", "").rstrip("/")
    key = os.getenv("AUTO_UPDATE_MANIFEST_KEY", "takealot/updates/update_manifest.json").strip().lstrip("/")
    if base:
        return f"{base}/{key}"
    return ""


def manifest_urls_from_env() -> list[str]:
    urls: list[str] = []
    direct = os.getenv("AUTO_UPDATE_MANIFEST_URL", "").strip()
    if direct:
        urls.append(direct)
    base = os.getenv("OSS_BASE_URL", "").rstrip("/")
    key = os.getenv("AUTO_UPDATE_MANIFEST_KEY", "takealot/updates/update_manifest.json").strip().lstrip("/")
    if base:
        fallback = f"{base}/{key}"
        if fallback not in urls:
            urls.append(fallback)
    return [u for u in urls if u]


def _pick_platform_value(data: dict[str, Any], key: str) -> str:
    # 支持以下结构：
    # files: {"macos": "...", "windows": "..."}
    # files: {"darwin": "...", "win32": "..."}
    # files: {"mac": "...", "win": "..."}
    alt_map = {
        "macos": ["darwin", "mac"],
        "windows": ["win32", "win"],
        "linux": ["linux"],
    }
    if key in data and isinstance(data.get(key), str):
        return str(data.get(key) or "").strip()
    for alt in alt_map.get(key, []):
        v = data.get(alt)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def check_for_update(current_version: str, timeout: int = 12) -> UpdateInfo:
    urls = manifest_urls_from_env()
    if not urls:
        return UpdateInfo(
            has_update=False,
            current_version=current_version,
            latest_version=current_version,
            manifest_url="",
        )

    data: dict[str, Any] | None = None
    chosen_url = ""
    last_err: Exception | None = None
    for url in urls:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "takealot-autolister-updater/1.0"})
            r.raise_for_status()
            j = r.json()
            if not isinstance(j, dict):
                raise RuntimeError("update manifest 格式无效：根节点不是对象")
            data = j
            chosen_url = url
            break
        except Exception as e:
            last_err = e
            continue
    if data is None:
        raise RuntimeError(f"无法获取更新清单：{last_err}")

    if not isinstance(data, dict):
        raise RuntimeError("update manifest 格式无效：根节点不是对象")

    latest = str(data.get("latest_version") or data.get("version") or "").strip()
    if not latest:
        raise RuntimeError("update manifest 缺少 latest_version")

    pkey = platform_key()
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    hashes = data.get("sha256") if isinstance(data.get("sha256"), dict) else {}
    download = _pick_platform_value(files, pkey) if isinstance(files, dict) else ""
    checksum = _pick_platform_value(hashes, pkey) if isinstance(hashes, dict) else ""

    has_update = _norm_version(latest) > _norm_version(current_version)
    return UpdateInfo(
        has_update=has_update,
        current_version=current_version,
        latest_version=latest,
        download_url=download,
        notes=str(data.get("notes") or data.get("release_notes") or "").strip(),
        force=bool(data.get("force", False)),
        sha256=checksum,
        manifest_url=chosen_url,
    )


def download_file(url: str, out_path: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout, stream=True, headers={"User-Agent": "takealot-autolister-updater/1.0"})
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    return out_path


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
