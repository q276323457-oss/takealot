"""
Gemini 图片生成适配器（通过 viviai.cc 代理）

接口：POST https://api.viviai.cc/v1beta/models/{model}:generateContent
认证：Authorization: Bearer <key>
官方格式：https://ai.google.dev/gemini-api/docs/image-generation

环境变量：
    GEMINI_IMAGE_API_KEY    API Key（单独配置，与 LLM_API_KEY 分开）
    GEMINI_IMAGE_BASE_URL   代理 base URL，默认 https://api.viviai.cc
    GEMINI_IMAGE_MODEL      模型，默认 gemini-2.5-flash-image-preview
"""
from __future__ import annotations

import base64
import io
import os

import requests
from PIL import Image

_DEFAULT_BASE_URL = "https://api.viviai.cc"
_DEFAULT_MODEL = "gemini-2.5-flash-image-preview"


def _make_session() -> requests.Session:
    """创建 requests Session，解决 Windows SSL/代理问题。
    重试逻辑由调用方 Python 层控制，不在 urllib3 层做，避免无日志的静默重试。
    """
    session = requests.Session()
    # trust_env=False：绕过 Windows 系统代理（注册表/IE 代理设置）
    session.trust_env = False
    # verify=False：绕过企业内网/杀毒软件自签证书导致的握手失败
    session.verify = False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _api_key() -> str:
    return os.getenv("GEMINI_IMAGE_API_KEY", "").strip()


def _base_url() -> str:
    return os.getenv("GEMINI_IMAGE_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.getenv("GEMINI_IMAGE_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def is_available() -> bool:
    return bool(_api_key())


def _compress(img_bytes: bytes, max_px: int = 1024) -> bytes:
    """压缩参考图到 max_px 以内，避免请求体过大。"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    if max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def generate_image(
    prompt: str,
    *,
    reference_images_bytes: list[bytes] | None = None,
    aspect_ratio: str = "1:1",
    n: int = 1,
) -> list[bytes]:
    """
    调用 Gemini 图片生成接口，返回图片 bytes 列表。

    参数：
        prompt:                  文字描述/指令
        reference_images_bytes:  参考图列表（图生图模式，可选）
        aspect_ratio:            宽高比，如 "1:1" / "4:3" / "16:9"
        n:                       一次生成数量（通过 numberOfImages 参数）
    """
    key = _api_key()
    model = _model()
    endpoint = f"{_base_url()}/v1beta/models/{model}:generateContent"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    # 构造 parts：参考图在前，文字在后
    parts: list[dict] = []
    if reference_images_bytes:
        for i, img_bytes in enumerate(reference_images_bytes[:6]):  # 最多 6 张
            compressed = _compress(img_bytes)
            b64 = base64.b64encode(compressed).decode()
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64,
                }
            })
            print(f"[gemini_img] 参考图 {i+1}: {len(img_bytes)//1024}KB → {len(compressed)//1024}KB")

    parts.append({"text": prompt})

    payload = {
        "contents": [
            {"parts": parts}
        ],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "numberOfImages": max(1, n),
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    mode = "图生图" if reference_images_bytes else "文生图"
    print(f"[gemini_img] {mode}，model={model}，aspect={aspect_ratio}，数量={n}")

    session = _make_session()
    last_err: Exception | None = None
    for attempt in range(1, 4):   # 最多尝试 3 次
        try:
            resp = session.post(endpoint, headers=headers, json=payload, timeout=90)
            break
        except Exception as e:
            last_err = e
            if attempt < 3:
                import time as _time
                wait = attempt * 3   # 3s, 6s
                print(f"[gemini_img] 第{attempt}次请求失败：{e}，{wait}s 后重试...")
                _time.sleep(wait)
            else:
                print(f"[gemini_img] 第{attempt}次请求失败：{e}，放弃")
    else:
        raise RuntimeError(f"Gemini 请求失败（已重试3次）：{last_err}")

    if not resp.ok:
        try:
            err = resp.json()
        except Exception:
            err = resp.text[:300]
        raise RuntimeError(f"Gemini API {resp.status_code}: {err}")

    data = resp.json()
    # 从 candidates[].content.parts 取图片
    results: list[bytes] = []
    candidates = data.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            b64_data = inline.get("data", "")
            if b64_data:
                results.append(base64.b64decode(b64_data))
                print(f"[gemini_img] ✅ 获得图片 {len(results)} 张")

    if not results:
        raise RuntimeError(f"Gemini 未返回图片。响应：{str(data)[:300]}")

    # 直接返回模型实际给出的所有图片；调用方自行决定如何使用。
    return results
