"""
产品白底图生成模块

流程：
1. 用豆包视觉模型（doubao-seed-2-0-pro）分析 1688 原图，生成英文产品描述
2. 用豆包 Seedream 图像生成模型生成白底合规图
3. 支持对话式修改（用户追加指令迭代生成）

使用方法：
    from takealot_autolister.image_generator import ImageGeneratorSession
    session = ImageGeneratorSession(source_image_urls, product_title)
    images = session.generate()          # 初次生成
    images = session.refine("背景再白一点") # 对话修改
"""
from __future__ import annotations

import io
import time
from typing import Any
import os

import requests
from PIL import Image

from .siliconflow_llm import call_doubao_vision_url, generate_image as _sf_generate_image
from .wuyin_image import is_available as wuyin_ok, generate_image as wuyin_generate

# 全局 Session：Windows 上 trust_env=False 绕过系统代理，Mac 保持默认允许代理加速
_SESSION = requests.Session()
if __import__("sys").platform.startswith("win"):
    _SESSION.trust_env = False

# 图生图编辑指令（Gemini / Qwen-Image-Edit 通用）
_EDIT_INSTRUCTION = (
    "Generate ONE single standalone product image. "
    "IMPORTANT: Do NOT create collages, grids, multi-panel layouts, or combine multiple views into one image. "
    "Output exactly one image showing the product alone. "
    "Pure white background. Remove any existing background completely. "
    "Keep the product shape, color, and details exactly as shown in the reference. "
    "No shadows, no text, no watermarks, no people, no extra objects. "
    "Professional e-commerce product photo, centered, high resolution."
)

# 副图变体指令（通用，适用于任何产品类型，英文）
# 5张循环：主图白底 + 4张场景/卖点图，符合 Takealot 平台图片规范
_VARIANT_INSTRUCTIONS = [
    # 0: 主图 — 纯白底正面主图（Takealot 强制要求）
    (
        "Pure white background (RGB 255,255,255). Front-facing product shot, perfectly centered, "
        "product filling 85-95% of the frame. Soft even lighting, no harsh shadows. "
        "No text, no watermarks, no logos. Professional e-commerce main image, high resolution."
    ),
    # 1: 副图1 — 使用场景图（展示核心卖点与使用方式）
    (
        "Show the product in a real-life usage scenario — clean, uncluttered lifestyle setting "
        "(home, office, or outdoor environment). Use a 45-degree angle or the most flattering view. "
        "Demonstrate how the product is used, carried, or stored, hinting at its key selling point "
        "(portability, functionality, or size). Warm natural lighting. No text, no watermarks."
    ),
    # 2: 副图2 — 功能/卖点特写图（突出独特设计与品质）
    (
        "Close-up or 45-degree angle shot focusing on the product's unique feature or core selling point. "
        "Emphasize material texture, surface craftsmanship, or innovative design detail. "
        "Clean simple background (light gray, beige, or contrasting dark tone). "
        "Side lighting or ring lighting to enhance 3D depth and tactile quality. No text, no watermarks."
    ),
    # 3: 副图3 — 多角度/配件组合图（展示产品完整性）
    (
        "Show the product together with all included accessories neatly arranged, or display the product "
        "from 2-3 complementary angles in a single clean composition. "
        "Clean lifestyle scene or solid color background. "
        "Highlight the completeness and value of the package. No text, no watermarks."
    ),
    # 4: 副图4 — 生活方式场景图（情感营销，营造氛围）
    (
        "Show the product integrated into an aspirational lifestyle scene "
        "(outdoor, home, family, or social setting). Partial human interaction (hands or arms) is welcome "
        "but avoid full faces. Warm atmospheric natural lighting. "
        "The scene should make viewers imagine owning the product in their daily life. "
        "No text, no watermarks."
    ),
    # 5+: 额外备用
    (
        "Pure white background. Side profile showing depth, ports, buttons, "
        "or connectivity features. Clean studio lighting. No text, no watermarks."
    ),
    (
        "Pure white background. Rear view revealing labels, vents, or unique "
        "design elements on the back. Professional e-commerce style. No text, no watermarks."
    ),
    (
        "Pure white background. Isometric angle showing three sides simultaneously, "
        "giving a complete 3D impression. Clean studio lighting. No text, no watermarks."
    ),
]


_STYLE_SUFFIX = (
    "professional e-commerce product photo, pure white background, "
    "clean studio lighting, no shadows, no text, no watermarks, no people, "
    "centered product, high resolution, sharp focus"
)

# 固定的多图提示词骨架（中文）
_TAKEALOT_MULTI_PROMPT = """根据我提供的产品图片，生成5张符合Takealot南非电商平台规则的产品展示图。产品颜色、款式必须与参考图完全一致，不得改变。

必须严格遵守：输出恰好5张不同的产品图片，每张独立。

第1张：主图，纯白色背景（RGB 255,255,255），产品正前方完整展示，产品居中占画面85-95%，柔和均匀光影，无阴影，无文字水印。

第2张：使用场景图，45度角或最佳展示角度，简约生活场景背景（家居/办公室/户外），展示产品实际使用方式和尺寸感，自然温暖光线。

第3张：功能特写图，聚焦产品独特功能或核心卖点，简洁纯色背景（浅灰或米色），侧光突出材质纹理和立体感。

第4张：多角度或配件组合图，展示产品与所有配件，或2-3个不同角度组合，体现产品完整性和配件丰富度。

第5张：生活方式场景图，有氛围感的生活场景（户外/家庭/聚会），产品融入使用场景，温暖自然光，可有手部局部动作。

所有图片要求：无文字、无LOGO、无水印、无品牌标识，sRGB色彩，最低分辨率600x600。5张图产品颜色款式必须完全一致。
"""

# 视觉分析用的提问
_VISION_QUESTION = (
    "Describe this product precisely in English for an e-commerce listing. "
    "Include: product type, main material, color, key visual features, approximate size/shape. "
    "Keep it under 60 words. No marketing language."
)


def _download_bytes(url: str, timeout: int = 15) -> bytes | None:
    """下载 URL 图片，返回 bytes，失败返回 None。trust_env=False 绕过 Windows 系统代理。"""
    try:
        resp = _SESSION.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _bytes_to_thumbnail(data: bytes, size: int = 200) -> bytes:
    """把图片 bytes 缩到缩略图尺寸，返回 JPEG bytes。"""
    import io as _io
    buf_in = _io.BytesIO(data)
    img = Image.open(buf_in)
    # webp/png 等转 RGB，避免 Win 上 webp 解码问题
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")
    img.thumbnail((size, size), Image.LANCZOS)
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class ImageGeneratorSession:
    """
    管理一次产品的图片生成会话，支持对话式迭代修改。

    属性：
        source_urls:    1688 原图 URL 列表
        product_title:  产品标题（辅助理解）
        description:    豆包视觉模型生成的英文产品描述
        current_prompt: 当前生成图片使用的 prompt
        history:        对话修改历史 [(user_instruction, generated_images)]
        last_images:    最后一次生成的图片 bytes 列表
    """

    def __init__(self, source_urls: list[str], product_title: str = "") -> None:
        self.source_urls = source_urls
        self.product_title = product_title
        self.description: str = ""
        self.current_prompt: str = ""
        self.history: list[tuple[str, list[bytes]]] = []
        self.last_images: list[bytes] = []
        self._reference_image_bytes: bytes | None = None   # 第一张原图缓存（图生图用）

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _analyze_source(self) -> str:
        """用视觉模型分析第一张原图，返回英文产品描述。"""
        if not self.source_urls:
            return self.product_title or "product"

        url = self.source_urls[0]
        print(f"[image_gen] 正在分析原图：{url[:60]}...")
        desc = call_doubao_vision_url(url, _VISION_QUESTION)
        if not desc or len(desc) < 5:
            # fallback：用标题
            desc = self.product_title or "product"
        print(f"[image_gen] 产品描述：{desc[:100]}")
        return desc.strip()

    def _build_prompt(self, user_instruction: str = "", has_reference: bool = False) -> str:
        """
        组合最终 prompt。

        有参考图（img2img）：
          - 有用户指令：直接用用户指令（Gemini 理解中/英文，无需拼内置指令）
          - 无用户指令：用内置编辑指令
        无参考图（文生图）：用产品描述 + 风格后缀
        """
        if has_reference:
            if user_instruction:
                # 防止 Gemini 生成拼图，强制加单张约束前缀
                return (
                    "Generate ONE single standalone product image only. "
                    "Do NOT create collages, grids, or multi-panel layouts. "
                    f"{user_instruction}"
                )
            return _EDIT_INSTRUCTION
        else:
            base = f"{self.description or self.product_title or 'product'}, {_STYLE_SUFFIX}"
            if user_instruction:
                base = f"{base}. {user_instruction}"
            return base

    def _call_generate(
        self,
        prompt: str,
        count: int = 1,
        reference_urls: list[str] | None = None,
    ) -> list[bytes]:
        """调用图像生成 API。

        当前策略：只走 Gemini（通过 Viviai 网关），不再使用五音 / 硅基流动回退。

        - 有参考图：走图生图（img2img），参考图来自 1688 原图。
        - 无参考图：走文生图，使用视觉分析得到的产品描述。
        - count 控制总张数，内部为每一张构造不同变体指令，避免 4 张完全一样。
        """
        import time as _time
        urls = reference_urls if reference_urls else self.source_urls[:1]

        # 下载所有参考图（最多3张）
        ref_images: list[bytes] = []
        for url in (urls or [])[:3]:
            print(f"[image_gen] 下载参考图：{url[:60]}...")
            _t0 = _time.time()
            data = _download_bytes(url)
            print(f"[image_gen] 下载耗时 {_time.time()-_t0:.1f}s，{'成功' if data else '失败'} {len(data)//1024 if data else 0}KB")
            if data:
                ref_images.append(data)

        # ── 统一：Gemini（通过 Viviai）───────────────────────────────────────
        compressed: list[bytes] = []
        _last_err: Exception | None = None
        try:
            from .gemini_image import is_available as gemini_ok, generate_image as gemini_generate
            if gemini_ok():
                total = max(1, count)
                is_custom = bool(
                    prompt
                    and prompt != _EDIT_INSTRUCTION
                    and not prompt.startswith("Generate ONE single standalone product image only.")
                )

                if is_custom:
                    final_prompt = (
                        "Generate multiple DIFFERENT standalone product images. "
                        "Each image must show ONLY ONE product, no collages, no grids, no multi-panel layouts. "
                        "Do NOT add any text, watermarks, or logos.\n"
                        f"{prompt}"
                    )
                else:
                    base_desc = self.description or self.product_title or ""
                    desc_suffix = f"，产品描述：{base_desc}" if base_desc else ""
                    final_prompt = _TAKEALOT_MULTI_PROMPT.rstrip() + desc_suffix

                print(f"[image_gen] Gemini 一次生成（提示希望 ~{total} 张）：{final_prompt[:140]}...")
                _t1 = _time.time()
                imgs = gemini_generate(
                    final_prompt,
                    reference_images_bytes=ref_images or None,
                    aspect_ratio="1:1",
                    n=1,
                )
                print(f"[image_gen] Gemini API 耗时 {_time.time()-_t1:.1f}s，返回 {len(imgs)} 张原始图")

                compressed: list[bytes] = []
                for idx, img_bytes in enumerate(imgs):
                    _t2 = _time.time()
                    try:
                        compressed.append(_bytes_to_thumbnail(img_bytes, size=768))
                    except Exception:
                        compressed.append(img_bytes)
                    print(f"[image_gen] 缩略图处理[{idx}] 耗时 {_time.time()-_t2:.1f}s，{len(img_bytes)//1024}KB→{len(compressed[-1])//1024}KB")

                print(f"[image_gen] Gemini 生成完成，共 {len(compressed)} 张")
                return compressed[: max(1, min(len(compressed), total))]
            # 没有配置 Gemini，直接抛错提示用户检查环境变量
            raise RuntimeError("Gemini 图片通道不可用，请检查 GEMINI_IMAGE_API_KEY / GEMINI_IMAGE_BASE_URL")
        except Exception as e:
            _last_err = e
            print(f"[image_gen] Gemini 生成失败：{e}")
            # 如果已经成功生成了部分图片，直接返回这些，避免全部丢失
            if compressed:
                print(f"[image_gen] Gemini 部分成功，返回已生成的 {len(compressed)} 张，跳过后续生成")
                return compressed
        # 走到这里说明 Gemini 也完全失败，只能抛错给上层，让 UI 提示「生成失败」
        raise RuntimeError(f"图片生成失败：{_last_err or 'Gemini 通道不可用，请检查 GEMINI_IMAGE_API_KEY'}")

    # ── 公开方法 ──────────────────────────────────────────────────────────────

    def generate(
        self,
        count: int = 5,
        reference_urls: list[str] | None = None,
    ) -> list[bytes]:
        """
        初次生成白底图。

        有参考图：直接用编辑指令（跳过视觉分析，更快）
        无参考图：先视觉分析再文生图
        """
        has_ref = bool(reference_urls or self.source_urls)

        if reference_urls:
            self._reference_image_bytes = None

        if not has_ref:
            # 无参考图，走文生图
            self.description = self._analyze_source()

        self.current_prompt = self._build_prompt(has_reference=has_ref)
        print(f"[image_gen] prompt: {self.current_prompt[:120]}")
        images = self._call_generate(self.current_prompt, count, reference_urls=reference_urls)
        self.last_images = images
        self.history = [("", images)]
        return images

    def refine(
        self,
        user_instruction: str,
        count: int = 5,
        reference_urls: list[str] | None = None,
    ) -> list[bytes]:
        """
        对话式修改：根据用户追加指令重新生成。
        """
        has_ref = bool(reference_urls or self._reference_image_bytes or self.source_urls)

        if reference_urls:
            self._reference_image_bytes = None

        if not has_ref and not self.description:
            self.description = self._analyze_source()

        self.current_prompt = self._build_prompt(user_instruction, has_reference=has_ref)
        print(f"[image_gen] refine prompt: {self.current_prompt[:120]}")
        images = self._call_generate(self.current_prompt, count, reference_urls=reference_urls)
        if images:
            self.last_images = images
            self.history.append((user_instruction, images))
        return images

    def get_source_thumbnails(self, size: int = 200) -> list[bytes]:
        """下载原图并生成缩略图 bytes 列表，用于 UI 展示。"""
        result = []
        for url in self.source_urls[:8]:  # 最多展示 8 张原图
            data = _download_bytes(url)
            if data:
                try:
                    result.append(_bytes_to_thumbnail(data, size))
                except Exception:
                    pass
        return result
