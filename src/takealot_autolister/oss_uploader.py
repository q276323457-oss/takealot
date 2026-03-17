"""
阿里云 OSS 图片上传模块

功能：
1. 把本地图片（白底处理后）调整到 800×800px JPG
2. 上传到阿里云 OSS，返回公开 URL 列表

环境变量（在 .env 中配置）：
    OSS_ACCESS_KEY_ID       阿里云 AccessKey ID
    OSS_ACCESS_KEY_SECRET   阿里云 AccessKey Secret
    OSS_BUCKET              Bucket 名称
    OSS_ENDPOINT            e.g. oss-cn-hangzhou.aliyuncs.com
    OSS_BASE_URL            公开访问域名，e.g. https://your-bucket.oss-cn-hangzhou.aliyuncs.com
    OSS_PREFIX              上传路径前缀，默认 takealot/images/
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

from PIL import Image

# ── OSS SDK ──────────────────────────────────────────────────────────────────
try:
    import oss2
    _OSS_AVAILABLE = True
except ImportError:
    _OSS_AVAILABLE = False

# ── 配置 ─────────────────────────────────────────────────────────────────────
_MIN_SIZE = 800       # Takealot 要求最小 600px，推荐 800px
_MAX_SIZE = 5000      # Takealot 要求最大 5000px
_TARGET_SIZE = 800    # 上传尺寸


def _load_env() -> dict:
    return {
        "key_id":     os.getenv("OSS_ACCESS_KEY_ID", ""),
        "key_secret": os.getenv("OSS_ACCESS_KEY_SECRET", ""),
        "bucket":     os.getenv("OSS_BUCKET", ""),
        "endpoint":   os.getenv("OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com"),
        "base_url":   os.getenv("OSS_BASE_URL", ""),
        "prefix":     os.getenv("OSS_PREFIX", "takealot/images/"),
    }


def _is_configured() -> bool:
    cfg = _load_env()
    return bool(cfg["key_id"] and cfg["key_secret"] and cfg["bucket"])


# ── 图片处理 ──────────────────────────────────────────────────────────────────

def _prepare_image(src: Path, target_size: int = _TARGET_SIZE) -> bytes:
    """
    打开图片，确保是 RGB，白色背景填充到正方形，调整到 target_size × target_size，
    转为 JPEG bytes。
    """
    img = Image.open(str(src)).convert("RGBA")

    # 合成白底
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    img_rgb = bg.convert("RGB")

    # 填充为正方形
    w, h = img_rgb.size
    side = max(w, h)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(img_rgb, ((side - w) // 2, (side - h) // 2))

    # 缩放
    if side != target_size:
        square = square.resize((target_size, target_size), Image.LANCZOS)

    buf = io.BytesIO()
    square.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()


def _oss_key(prefix: str, data: bytes, original_name: str) -> str:
    """生成 OSS key，使用内容 hash 避免重复上传。"""
    digest = hashlib.md5(data).hexdigest()[:16]
    stem = Path(original_name).stem[:30]
    ts = int(time.time())
    return f"{prefix}{stem}_{digest}_{ts}.jpg"


# ── 上传 ──────────────────────────────────────────────────────────────────────

def upload_images(
    image_paths: list[Path],
    prefix: str | None = None,
) -> list[str]:
    """
    处理并上传图片到 OSS。

    参数：
        image_paths: 本地图片路径列表
        prefix:      OSS 路径前缀（覆盖环境变量）

    返回：
        上传成功的公开 URL 列表（失败的跳过）
    """
    if not image_paths:
        return []

    cfg = _load_env()
    oss_prefix = prefix or cfg["prefix"]

    if not _is_configured():
        print("[oss_uploader] ✗ 未配置 OSS，请在 .env 中设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET")
        return []

    if not _OSS_AVAILABLE:
        print("[oss_uploader] ✗ 未安装 oss2，请运行: pip install oss2")
        return []

    auth = oss2.Auth(cfg["key_id"], cfg["key_secret"])
    bucket = oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])

    # 确定 base_url
    base_url = cfg["base_url"].rstrip("/")
    if not base_url:
        base_url = f"https://{cfg['bucket']}.{cfg['endpoint']}"

    urls: list[str] = []
    for path in image_paths:
        path = Path(path)
        if not path.exists():
            print(f"[oss_uploader] ✗ 文件不存在：{path}")
            continue
        try:
            data = _prepare_image(path)
            key = _oss_key(oss_prefix, data, path.name)
            bucket.put_object(key, data, headers={"Content-Type": "image/jpeg"})
            url = f"{base_url}/{key}"
            urls.append(url)
            print(f"[oss_uploader] ✓ {path.name} → {url}")
        except Exception as e:
            print(f"[oss_uploader] ✗ 上传失败 {path.name}: {e}")

    return urls


def upload_bytes_list(
    images: list[bytes],
    prefix: str | None = None,
    stem: str = "ai_image",
) -> list[str]:
    """
    处理并上传图片 bytes 列表到 OSS（用于 AI 生成图片直接上传）。

    参数：
        images: 图片 bytes 列表
        prefix: OSS 路径前缀（覆盖环境变量）
        stem:   文件名前缀

    返回：
        上传成功的公开 URL 列表
    """
    if not images:
        return []

    cfg = _load_env()
    oss_prefix = prefix or cfg["prefix"]

    if not _is_configured():
        print("[oss_uploader] ✗ 未配置 OSS，请在 .env 中设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET")
        return []

    if not _OSS_AVAILABLE:
        print("[oss_uploader] ✗ 未安装 oss2，请运行: pip install oss2")
        return []

    auth = oss2.Auth(cfg["key_id"], cfg["key_secret"])
    bucket = oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])

    base_url = cfg["base_url"].rstrip("/")
    if not base_url:
        base_url = f"https://{cfg['bucket']}.{cfg['endpoint']}"

    urls: list[str] = []
    for raw_bytes in images:
        try:
            # 白底 + 800×800 标准化
            img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img_rgb = bg.convert("RGB")
            w, h = img_rgb.size
            side = max(w, h)
            square = Image.new("RGB", (side, side), (255, 255, 255))
            square.paste(img_rgb, ((side - w) // 2, (side - h) // 2))
            if side != _TARGET_SIZE:
                square = square.resize((_TARGET_SIZE, _TARGET_SIZE), Image.LANCZOS)
            buf = io.BytesIO()
            square.save(buf, format="JPEG", quality=92, optimize=True)
            data = buf.getvalue()

            key = _oss_key(oss_prefix, data, stem)
            bucket.put_object(key, data, headers={"Content-Type": "image/jpeg"})
            url = f"{base_url}/{key}"
            urls.append(url)
            print(f"[oss_uploader] ✓ AI 生成图 → {url}")
        except Exception as e:
            print(f"[oss_uploader] ✗ 上传 AI 生成图失败: {e}")

    return urls
