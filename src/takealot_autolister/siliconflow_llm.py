"""
硅基流动（SiliconFlow）LLM 适配器

支持接口：
- /v1/chat/completions  (OpenAI 兼容，文本/视觉)
- /v1/images/generations (FLUX 图像生成)

环境变量：
    SILICONFLOW_API_KEY   硅基流动 API Key
    SILICONFLOW_MODEL     文本模型，默认 deepseek-ai/DeepSeek-V3
    SILICONFLOW_VL_MODEL  视觉模型，默认 Qwen/Qwen2.5-VL-72B-Instruct
    SILICONFLOW_IMAGE_MODEL 图像模型，默认 black-forest-labs/FLUX.1-dev
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

_BASE_URL = "https://api.siliconflow.cn"
_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

# 全局 Session：trust_env=False 绕过 Windows 系统代理，避免 SSL EOF 问题
_SESSION = requests.Session()
_SESSION.trust_env = False


def _chat_base_url(model: str) -> str:
    """豆包模型走火山引擎，其余走硅基流动。"""
    if str(model).startswith("doubao"):
        return _DOUBAO_BASE_URL
    return _BASE_URL


def _chat_endpoint(model: str) -> str:
    """返回完整的 chat completions endpoint。"""
    if str(model).startswith("doubao"):
        return f"{_DOUBAO_BASE_URL}/chat/completions"
    return f"{_BASE_URL}/v1/chat/completions"
_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3"
_DEFAULT_VL_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"
_DEFAULT_IMAGE_MODEL = "Qwen/Qwen-Image-Edit-2509"


def _api_key() -> str:
    return (
        os.getenv("SILICONFLOW_API_KEY", "").strip()
        or os.getenv("DOUBAO_API_KEY", "").strip()
    )


def _model() -> str:
    return (
        os.getenv("SILICONFLOW_MODEL", "").strip()
        or os.getenv("DOUBAO_MODEL", "").strip()
        or _DEFAULT_MODEL
    )


def _vl_model() -> str:
    return (
        os.getenv("SILICONFLOW_VL_MODEL", "").strip()
        or os.getenv("DOUBAO_VL_MODEL", "").strip()
        or _DEFAULT_VL_MODEL
    )


def _image_model() -> str:
    return os.getenv("SILICONFLOW_IMAGE_MODEL", _DEFAULT_IMAGE_MODEL).strip() or _DEFAULT_IMAGE_MODEL


def is_doubao_available() -> bool:
    """保持兼容旧接口名称。"""
    return bool(_api_key())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _extract_json_block(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    return json.loads(m.group(0))


# ── 文本接口 ──────────────────────────────────────────────────────────────────

def call_doubao_json(prompt: str, *, temperature: float = 0.2) -> dict[str, Any]:
    """调用文本模型，要求返回 JSON（保持旧接口名）。"""
    return _extract_json_block(call_doubao_raw(prompt, temperature=temperature))


def call_doubao_raw(prompt: str, *, temperature: float = 0.2) -> str:
    """调用文本模型，返回原始文本（保持旧接口名）。"""
    m = _model()
    endpoint = _chat_endpoint(m)
    payload = {
        "model": m,
        "temperature": float(temperature),
        "messages": [
            {"role": "system", "content": "Return valid JSON only. No markdown, no explanation."},
            {"role": "user", "content": prompt},
        ],
    }
    resp = _SESSION.post(endpoint, headers=_headers(), json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return str(
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    )


def call_doubao_text(prompt: str, *, temperature: float = 0.3) -> str:
    """调用文本模型，返回纯文本（保持旧接口名）。"""
    m = _model()
    endpoint = _chat_endpoint(m)
    payload = {
        "model": m,
        "temperature": float(temperature),
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _SESSION.post(endpoint, headers=_headers(), json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return str(
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    )


# ── 视觉接口 ──────────────────────────────────────────────────────────────────

def call_doubao_vision(image_path: Path, question: str) -> str:
    """调用视觉模型分析本地图片（保持旧接口名）。"""
    img_bytes = Path(image_path).read_bytes()
    # 超过 4MB 先压缩
    if len(img_bytes) > 4 * 1024 * 1024:
        from PIL import Image
        img = Image.open(str(image_path)).convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        img_bytes = buf.getvalue()
    b64 = base64.b64encode(img_bytes).decode()
    return call_doubao_vision_url(f"data:image/jpeg;base64,{b64}", question)


def call_doubao_vision_url(image_url: str, question: str) -> str:
    """调用视觉模型分析图片 URL（保持旧接口名）。"""
    m = _vl_model()
    endpoint = _chat_endpoint(m)
    payload = {
        "model": _vl_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": question},
                ],
            }
        ],
    }
    try:
        resp = _SESSION.post(endpoint, headers=_headers(), json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return str(
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
    except Exception:
        return ""


# ── 图像生成接口（Qwen-Image-Edit-2509 img2img）──────────────────────────────

def generate_image(
    prompt: str,
    *,
    model: str | None = None,
    size: str = "1024x1024",
    n: int = 1,
    reference_image_bytes: bytes | None = None,
    reference_images_bytes: list[bytes] | None = None,   # 多图输入
) -> list[bytes]:
    """
    调用硅基流动图像生成/编辑，返回图片 bytes 列表。

    支持多图输入（image, image_2, image_3）+ batch_size 多输出，一次调用完成。

    参数：
        prompt:                  编辑指令 / 图像描述
        model:                   模型 ID
        size:                    输出尺寸（图生图模式忽略）
        n:                       batch_size，一次生成数量（1-4）
        reference_image_bytes:   单张参考图（兼容旧调用）
        reference_images_bytes:  多张参考图列表（优先于 reference_image_bytes）
    """
    # 整理参考图列表
    ref_list: list[bytes] = []
    if reference_images_bytes:
        ref_list = [b for b in reference_images_bytes if b]
    elif reference_image_bytes:
        ref_list = [reference_image_bytes]

    img_model = model or _image_model()
    endpoint = f"{_BASE_URL}/v1/images/generations"
    count = max(1, n)  # 外层循环控制，这里直接用 n

    payload: dict[str, Any] = {
        "model": img_model,
        "prompt": prompt,
        "batch_size": count,
        "num_inference_steps": 20,
        "guidance_scale": 7.5,
    }

    if ref_list:
        # 图生图：最多支持 3 张参考图，上传前先压缩到 1024px 以内
        def _compress_ref(raw: bytes, max_px: int = 1024) -> bytes:
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(raw)).convert("RGB")
            if max(img.size) > max_px:
                img.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        keys = ["image", "image_2", "image_3"]
        for key, img_bytes in zip(keys, ref_list[:3]):
            compressed = _compress_ref(img_bytes)
            b64 = base64.b64encode(compressed).decode()
            payload[key] = f"data:image/jpeg;base64,{b64}"
            print(f"[image_gen] 参考图 {key}: {len(img_bytes)//1024}KB → {len(compressed)//1024}KB")
        print(f"[image_gen] 图生图模式，{img_model}，{len(ref_list)} 张参考图 → batch_size={count}…")
    else:
        # 文生图
        _size_map = {
            "2k": "1024x1024", "2K": "1024x1024",
            "3k": "1024x1024", "3K": "1024x1024",
        }
        api_size = _size_map.get(size, size)
        if "x" not in api_size.lower():
            api_size = "1024x1024"
        payload["image_size"] = api_size
        payload["negative_prompt"] = "low quality, blurry, watermark, text, logo, people, shadow"
        print(f"[image_gen] 文生图模式，{img_model}，batch_size={count}…")

    resp = _SESSION.post(endpoint, headers=_headers(), json=payload, timeout=300)
    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text[:500]
        raise RuntimeError(f"SiliconFlow image API {resp.status_code}: {err_body}")

    data = resp.json()
    print(f"[image_gen] API 响应：{str(data)[:200]}")

    result: list[bytes] = []
    images_list = data.get("images") or data.get("data") or []
    for item in images_list:
        url = item.get("url", "")
        b64 = item.get("b64_json", "")
        if url:
            img_resp = _SESSION.get(url, timeout=60)
            img_resp.raise_for_status()
            result.append(img_resp.content)
        elif b64:
            result.append(base64.b64decode(b64))

    if not result:
        raise RuntimeError(f"API 返回数据中无图片。响应：{str(data)[:300]}")

    return result
