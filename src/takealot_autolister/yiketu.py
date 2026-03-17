"""
易可图（yiketu.com）图片翻译 API 适配器

API 文档：https://www.yuque.com/yiketuxiaotu-uyfb7/vudgx4
接口：POST https://open-api.yiketu.com/gw/translate_img_v2/translateImg

环境变量：
    YIKETU_APP_KEY     应用 appKey
    YIKETU_APP_SECRET  应用 appSecret
"""
from __future__ import annotations

import hashlib
import io
import os
import time
import uuid

import requests

_BASE_URL = "https://open-api.yiketu.com"


def _app_key() -> str:
    return os.getenv("YIKETU_APP_KEY", "").strip()


def _app_secret() -> str:
    return os.getenv("YIKETU_APP_SECRET", "").strip()


def is_available() -> bool:
    return bool(_app_key() and _app_secret())


def _sign(params: dict, app_secret: str) -> str:
    """
    签名算法：
    1. 所有参数按 key ASCII 升序排列
    2. 拼接 key+value（无分隔符）
    3. 首尾拼接 appSecret
    4. MD5 → 大写
    """
    sorted_items = sorted(params.items(), key=lambda x: x[0])
    plain = "".join(f"{k}{v}" for k, v in sorted_items)
    sign_str = app_secret + plain + app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def _compress(img_bytes: bytes, max_px: int = 1500, max_mb: int = 4) -> bytes:
    """压缩到 max_px 以内且 < max_mb MB。"""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    quality = 88
    while True:
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= max_mb * 1024 * 1024:
            return data
        quality -= 10
        max_px = int(max_px * 0.85)
        if quality < 40:
            return data


def _upload_to_oss(img_bytes: bytes) -> str:
    """上传到 OSS 并返回公开 URL（供易可图通过 imgUrl 访问）。"""
    from .oss_uploader import upload_bytes_list
    prefix = f"takealot/translate_tmp/"
    urls = upload_bytes_list([img_bytes], prefix=prefix, stem=f"tr_{uuid.uuid4().hex[:8]}")
    if not urls:
        raise RuntimeError("OSS 上传失败，无法获取 imgUrl")
    return urls[0]


def translate_image(
    img_bytes: bytes,
    source_lang: str = "zh",
    target_lang: str = "en",
    translate_product_text: bool = True,
) -> bytes:
    """
    调用易可图 V2 图片翻译接口，返回翻译后的图片 bytes。

    参数：
        img_bytes: 原图 bytes（会自动压缩）
        source_lang: 源语言代码，默认 zh（简体中文）
        target_lang: 目标语言代码，默认 en（英语）
        translate_product_text: 是否翻译商品主体文字
    """
    if not is_available():
        raise RuntimeError("未配置 YIKETU_APP_KEY / YIKETU_APP_SECRET")

    app_key = _app_key()
    app_secret = _app_secret()

    # 压缩图片
    small = _compress(img_bytes)
    print(f"[yiketu] 图片压缩: {len(img_bytes)//1024}KB → {len(small)//1024}KB")

    # 上传到 OSS 获取公开 URL
    print("[yiketu] 上传到 OSS…")
    img_url = _upload_to_oss(small)
    print(f"[yiketu] OSS URL: {img_url[:80]}…")

    # 构造请求参数（不含 sign）
    params: dict[str, str] = {
        "timestamp": str(int(time.time())),
        "appKey": app_key,
        "imgUrl": img_url,
        "sourceLanguage": source_lang,
        "targetLanguage": target_lang,
        "isTranslateProductText": "1" if translate_product_text else "0",
    }
    params["sign"] = _sign(params, app_secret)

    print(f"[yiketu] 调用翻译 API… ({source_lang} → {target_lang})")
    resp = requests.post(
        f"{_BASE_URL}/gw/translate_img_v2/translateImg",
        data=params,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[yiketu] 响应: {str(data)[:200]}")

    if data.get("result") != "success" or data.get("code") != 200:
        raise RuntimeError(f"易可图翻译失败: {data}")

    translate_url = data["data"]["translateImgUrl"]
    print(f"[yiketu] 下载翻译结果…")
    img_resp = requests.get(translate_url, timeout=60)
    img_resp.raise_for_status()
    return img_resp.content
