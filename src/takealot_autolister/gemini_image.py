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
import json
import os
import subprocess
import tempfile
from pathlib import Path

import requests
from PIL import Image

_DEFAULT_BASE_URL = "https://api.viviai.cc"
_DEFAULT_MODEL = "gemini-2.5-flash-image-preview"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _make_session() -> requests.Session:
    """创建 requests Session。重试逻辑只保留在 Python 层，避免隐式长时间卡住。"""
    session = requests.Session()
    # Windows 会自动读取系统代理（注册表/IE设置），代理做 SSL 深度检测时
    # 会导致 UNEXPECTED_EOF_WHILE_READING。trust_env=False 完全绕过系统代理。
    # Mac 上开代理可加速，Windows 上保持 False 避免 SSL 问题。
    import sys
    if sys.platform.startswith("win"):
        use_system_proxy = _env_flag("GEMINI_IMAGE_USE_SYSTEM_PROXY", default=False)
        if use_system_proxy:
            print("[gemini_img] Windows 使用系统代理配置（trust_env=True）")
        else:
            session.trust_env = False
            print("[gemini_img] Windows 绕过系统代理（trust_env=False）")
        session.verify = False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _post_with_curl(endpoint: str, headers: dict[str, str], payload: dict, timeout: int) -> tuple[int, bytes]:
    body_text = json.dumps(payload, ensure_ascii=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fp:
        fp.write(body_text)
        payload_file = fp.name

    try:
        cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--insecure",
            "--max-time",
            str(timeout),
            "--output",
            "-",
            "--write-out",
            "\n%{http_code}",
            "-X",
            "POST",
            endpoint,
        ]
        for key, value in headers.items():
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.extend(["--data-binary", f"@{payload_file}"])
        run_kwargs: dict[str, object] = {
            "capture_output": True,
            "check": False,
        }
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            run_kwargs["startupinfo"] = startupinfo
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(cmd, **run_kwargs)
        if completed.returncode != 0:
            err = (completed.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError(err or f"curl exit code {completed.returncode}")
        stdout = completed.stdout or b""
        split_at = stdout.rfind(b"\n")
        if split_at < 0:
            raise RuntimeError("curl 未返回 HTTP 状态码")
        body = stdout[:split_at]
        code_raw = stdout[split_at + 1 :].strip().decode("utf-8", errors="ignore")
        status = int(code_raw)
        return status, body
    finally:
        try:
            os.unlink(payload_file)
        except Exception:
            pass


def _post_with_browser(endpoint: str, headers: dict[str, str], payload: dict, timeout: int) -> tuple[int, bytes]:
    from playwright.sync_api import sync_playwright

    channel = os.getenv("BROWSER_CHANNEL", "msedge").strip() or "msedge"
    user_data_dir = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
    profile_directory = os.getenv("BROWSER_PROFILE_DIRECTORY", "Default").strip() or "Default"
    # 不自动绑定系统默认 Edge 目录。该目录常被正在运行的 Edge 占用，会导致 launch_persistent_context 直接失败。
    if user_data_dir:
        p = Path(user_data_dir)
        if not p.exists():
            user_data_dir = ""

    with sync_playwright() as pw:
        browser = None
        context = None
        try:
            if user_data_dir:
                try:
                    print(f"[gemini_img] 浏览器通道复用用户目录：{user_data_dir} / {profile_directory}")
                    context = pw.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        channel=channel if channel else None,
                        headless=True,
                        ignore_default_args=["--enable-automation"],
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            f"--profile-directory={profile_directory}",
                        ],
                        viewport={"width": 1280, "height": 900},
                    )
                except Exception as e:
                    print(f"[gemini_img] 复用用户目录失败，改用临时浏览器上下文：{e}")
                    context = None
            else:
                print("[gemini_img] 浏览器通道使用临时上下文")

            if context is None:
                browser = pw.chromium.launch(
                    channel=channel if channel else None,
                    headless=True,
                )
                context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.set_content("<html><body>ok</body></html>")
            result = page.evaluate(
                """
                async ({ endpoint, headers, payload, timeoutMs }) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort("timeout"), timeoutMs);
                  try {
                    const resp = await fetch(endpoint, {
                      method: "POST",
                      headers,
                      body: JSON.stringify(payload),
                      signal: controller.signal,
                    });
                    const text = await resp.text();
                    return { status: resp.status, text };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {
                    "endpoint": endpoint,
                    "headers": headers,
                    "payload": payload,
                    "timeoutMs": timeout * 1000,
                },
            )
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()

    status = int(result.get("status", 0) or 0)
    text = result.get("text", "")
    return status, text.encode("utf-8")


def _post_json(endpoint: str, headers: dict[str, str], payload: dict, timeout: int) -> tuple[int, bytes]:
    import sys

    if sys.platform.startswith("win"):
        # Windows 上只使用浏览器通道。你已经在浏览器里验证该链路很快，
        # 而 curl/requests 在部分网络环境下会极慢，因此不再回退。
        print("[gemini_img] Windows 只走浏览器通道（Playwright fetch）")
        return _post_with_browser(endpoint, headers, payload, timeout)

    session = _make_session()
    resp = session.post(endpoint, headers=headers, json=payload, timeout=timeout)
    return resp.status_code, resp.content


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

    last_err: Exception | None = None
    status_code = 0
    resp_body = b""
    for attempt in range(1, 4):   # 最多尝试 3 次
        try:
            import time as _time
            _t0 = _time.time()
            print(f"[gemini_img] 第{attempt}次发送请求...")
            status_code, resp_body = _post_json(
                endpoint,
                headers,
                payload,
                timeout=900,
            )
            print(f"[gemini_img] 收到响应 status={status_code}，body={len(resp_body)}B，耗时{_time.time()-_t0:.1f}s")
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

    if not (200 <= status_code < 300):
        try:
            err = json.loads(resp_body.decode("utf-8", errors="ignore"))
        except Exception:
            err = resp_body.decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"Gemini API {status_code}: {err}")

    try:
        data = json.loads(resp_body.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Gemini 返回 JSON 解析失败：{e}") from e
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
