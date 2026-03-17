"""
有道智云图片翻译 API 适配器

接口文档：https://ai.youdao.com/DOCSIRMA/html/trans/api/tpfy/index.html
接口地址：POST https://openapi.youdao.com/ocrtransapi

环境变量：
    YOUDAO_APP_KEY     应用ID
    YOUDAO_APP_SECRET  应用密钥
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import time
import uuid

import requests

_API_URL = "https://openapi.youdao.com/ocrtransapi"


def _app_key() -> str:
    return os.getenv("YOUDAO_APP_KEY", "").strip()


def _app_secret() -> str:
    return os.getenv("YOUDAO_APP_SECRET", "").strip()


def is_available() -> bool:
    return bool(_app_key() and _app_secret())


def _sign(app_key: str, q: str, salt: str, curtime: str, app_secret: str) -> str:
    """
    签名算法（v3）：
    input = q前10字符 + len(q) + q后10字符  （q长度>20时）
    input = q                                （q长度≤20时）
    sign = SHA256(appKey + input + salt + curtime + appSecret)
    """
    if len(q) > 20:
        input_str = q[:10] + str(len(q)) + q[-10:]
    else:
        input_str = q
    raw = app_key + input_str + salt + curtime + app_secret
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compress(img_bytes: bytes, max_px: int = 1500) -> bytes:
    """压缩图片，有道限制 5MB 以内（编码后）。"""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    quality = 88
    while True:
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        # base64 后约是原来 4/3 倍，限制原始 < 3.7MB
        if len(data) < 3_700_000:
            return data
        quality -= 10
        max_px = int(max_px * 0.85)
        if quality < 40:
            return data


def translate_image(
    img_bytes: bytes,
    source_lang: str = "zh-CHS",
    target_lang: str = "en",
) -> bytes:
    """
    调用有道图片翻译 API，返回翻译后的渲染图片 bytes。

    参数：
        img_bytes:   原图 bytes（自动压缩）
        source_lang: 源语言，默认 zh-CHS（简体中文）
        target_lang: 目标语言，默认 en（英语）
    """
    if not is_available():
        raise RuntimeError("未配置 YOUDAO_APP_KEY / YOUDAO_APP_SECRET")

    app_key = _app_key()
    app_secret = _app_secret()

    # 压缩并转 base64（不含 data URL 头）
    small = _compress(img_bytes)
    q = base64.b64encode(small).decode("utf-8")
    print(f"[youdao] 图片压缩: {len(img_bytes)//1024}KB → {len(small)//1024}KB，base64长度={len(q)}")

    salt = uuid.uuid4().hex.upper()
    curtime = str(int(time.time()))
    sign = _sign(app_key, q, salt, curtime, app_secret)

    params = {
        "type": "1",              # Base64 上传
        "from": source_lang,
        "to": target_lang,
        "appKey": app_key,
        "salt": salt,
        "sign": sign,
        "signType": "v3",
        "curtime": curtime,
        "q": q,
        "render": "1",            # 要求返回渲染后的翻译图片
        "docType": "json",
    }

    print(f"[youdao] 调用翻译 API… ({source_lang} → {target_lang})")
    resp = requests.post(_API_URL, data=params, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    print(f"[youdao] 响应: {str(data)[:300]}")

    # 检查错误
    error_code = data.get("errorCode", "0")
    if error_code != "0":
        _ERROR_CODES = {
            "101": "缺少必填参数",
            "108": "应用ID无效",
            "110": "无相关服务实例，请在控制台绑定图片翻译服务实例",
            "202": "签名校验失败",
            "203": "IP不在白名单",
        }
        msg = _ERROR_CODES.get(error_code, f"未知错误码 {error_code}")
        raise RuntimeError(f"有道翻译失败 [{error_code}]: {msg}")

    # 获取渲染图片 URL 或 base64
    render_img = data.get("renderImg") or data.get("render_img") or ""
    if render_img:
        if render_img.startswith("http"):
            print("[youdao] 下载渲染图片…")
            img_resp = requests.get(render_img, timeout=60)
            img_resp.raise_for_status()
            return img_resp.content
        else:
            # base64 格式
            return base64.b64decode(render_img)

    # render 不可用时，尝试 resImg（部分版本字段名不同）
    res_img = data.get("resImg", "")
    if res_img:
        if res_img.startswith("http"):
            img_resp = requests.get(res_img, timeout=60)
            img_resp.raise_for_status()
            return img_resp.content
        return base64.b64decode(res_img)

    raise RuntimeError(f"有道翻译响应中无渲染图片字段。完整响应: {data}")
