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
_TAKEALOT_MULTI_PROMPT = """
根据我提供的产品图片，生成5张符合Takealot南非电商平台规则的产品展示图。

⚠️ **【必须严格遵守 - 生成5张独立图片】** ⚠️
你必须输出 **恰好5张** 不同的产品图片，每张都是独立的文件！

---

## 📸 **第1张：主图 - 纯白底展示**
**视角**：产品正前方，完整展示整体造型
**背景**：纯白色 (RGB 255,255,255) - 符合Takealot主图要求
**构图**：产品居中，占画面85-95% [citation:1]
**光影**：柔和均匀，无明显阴影
**分辨率**：不低于1024x1024像素，建议2048x2048 [citation:1]
**格式**：JPG或PNG，sRGB色彩模式 [citation:1]
**用途**：搜索结果页主图、产品列表展示

---

## 📸 **第2张：使用场景图 - 展示核心卖点**
**视角**：45度角或最佳展示角度
**背景**：简约生活场景（家居/办公室/户外等），干净不杂乱
**构图**：产品在实际使用环境中，展示尺寸感和使用方式
**卖点融入**：
- 如果产品有便携卖点：展示轻松携带/收纳
- 如果产品有功能卖点：展示正在使用的状态
- 如果产品有尺寸卖点：可通过场景元素暗示尺寸
**光影**：自然光线，温暖真实，符合南非本土审美
**用途**：让顾客想象产品在自己生活中的样子

---

## 📸 **第3张：功能/卖点特写图**
**视角**：45度角或局部特写
**背景**：简洁干净的背景（浅灰/米色/深色对比）
**构图**：聚焦产品的独特功能或核心卖点
**卖点融入**：
- 材质卖点：超特写展示纹理质感
- 工艺卖点：展示细节做工
- 创新设计：突出与众不同的部分
**光影**：侧光或环形光突出立体感和质感
**用途**：证明产品品质，打消质量顾虑

---

## 📸 **第4张：多角度/组合场景图**
**视角**：可展示2-3个不同角度组合，或产品与配件组合
**背景**：简洁生活场景或纯色背景
**构图**：
- 方案A：产品+所有配件整齐展示
- 方案B：同一场景展示产品的不同使用状态
- 方案C：产品正面+侧面+细节的小图组合
**卖点融入**：展示产品完整性、配件丰富度
**用途**：让顾客360度了解产品全貌

---

## 📸 **第5张：生活方式场景图**
**视角**：根据产品特点自由选择
**背景**：有氛围感的生活场景（户外/家庭/聚会等）
**构图**：产品融入生活方式中，有人体局部操作更佳
**卖点融入**：
- 如果是家居产品：展示装饰效果
- 如果是电子产品：展示使用便捷性
- 如果是服装配饰：展示搭配效果
**光影**：温暖自然光，营造氛围感
**用途**：情感营销，让顾客向往拥有后的生活

---

## 🎯 **Takealot平台统一要求 [citation:1]**

### 技术规范
- **格式**：JPG或PNG格式
- **色彩模式**：sRGB色彩模式（确保所有设备显示一致）
- **分辨率**：最低600x600像素，建议2048x2048像素
- **文件大小**：单张不超过2MB
- **构图**：产品占画面85-95%，不靠边

### 内容规范
- **文字**：❌ 不要任何文字、LOGO、水印、品牌标识
- **模特**：✅ 可用手部展示，但避免完整人脸（除非必要）
- **一致性**：5张图中的产品颜色、款式、材质必须完全一致
- **真实性**：图片必须真实反映产品，不得过度美化导致实物不符 [citation:1]
- **背景**：
  - 第1张：必须纯白底
  - 第2-5张：背景自由发挥，但要简洁干净，不喧宾夺主

---

## 🎯 **针对不同产品类型的卖点融入建议**

模型请根据实际产品类型，自动选择最合适的卖点：

| 产品类型 | 可融入的卖点 |
|----------|--------------|
| 家居用品 | 节省空间、易清洁、稳固耐用、装饰效果 |
| 电子产品 | 便携轻巧、长续航、快充、防水、智能功能 |
| 服装鞋包 | 透气舒适、耐磨、弹性好、易搭配 |
| 厨房用品 | 不粘、易清洗、多功能、耐用材质 |
| 美妆护肤 | 保湿、质地轻盈、易吸收、天然成分 |
| 户外用品 | 防水防尘、轻便携带、耐用抗摔 |
| 母婴用品 | 安全材质、易清洗、防漏设计、便携 |
| 工具类 | 多功能、省力设计、精准度高、耐用 |

🚨 **再次强调：必须输出5张独立的图片！**
- 第1张：纯白底主图（严格按平台规则）
- 第2张：使用场景图（融入核心卖点）
- 第3张：功能/卖点特写图
- 第4张：多角度/组合场景图
- 第5张：生活方式场景图

如果少一张或多一张，任务就算失败！
"""

# 视觉分析用的提问
_VISION_QUESTION = (
    "Describe this product precisely in English for an e-commerce listing. "
    "Include: product type, main material, color, key visual features, approximate size/shape. "
    "Keep it under 60 words. No marketing language."
)


def _download_bytes(url: str, timeout: int = 30) -> bytes | None:
    """下载 URL 图片，返回 bytes，失败返回 None。"""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _bytes_to_thumbnail(data: bytes, size: int = 200) -> bytes:
    """把图片 bytes 缩到缩略图尺寸，返回 JPEG bytes。"""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
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
        urls = reference_urls if reference_urls else self.source_urls[:1]

        # 下载所有参考图（最多3张）
        ref_images: list[bytes] = []
        for url in (urls or [])[:3]:
            print(f"[image_gen] 下载参考图：{url[:60]}...")
            data = _download_bytes(url)
            if data:
                ref_images.append(data)

        # ── 统一：Gemini（通过 Viviai）───────────────────────────────────────
        compressed: list[bytes] = []
        try:
            from .gemini_image import is_available as gemini_ok, generate_image as gemini_generate
            if gemini_ok():
                total = max(1, count)
                is_custom = bool(
                    prompt
                    and prompt != _EDIT_INSTRUCTION
                    and not prompt.startswith("Generate ONE single standalone product image only.")
                )

                # 统一用「一次请求生成 N 张图」的方式，减少收费次数
                # 自动模式下，把 5 种视角/场景写进一个综合 prompt，让模型输出多视角：
                if is_custom:
                    final_prompt = (
                        "Generate multiple DIFFERENT standalone product images. "
                        "Each image must show ONLY ONE product, no collages, no grids, no multi-panel layouts. "
                        "Do NOT add any text, watermarks, or logos.\n"
                        f"{prompt}"
                    )
                else:
                    base_desc = self.description or self.product_title or "product"
                    final_prompt = (
                        _TAKEALOT_MULTI_PROMPT.strip()
                        + "\n\n当前产品英文简述（仅供理解，不要写在图片里）："
                        + f"{base_desc}"
                    )

                # 不再通过 numberOfImages 强制张数，由提示词引导模型尽量给多张。
                print(f"[image_gen] Gemini 一次生成（提示希望 ~{total} 张）：{final_prompt[:140]}...")
                imgs = gemini_generate(
                    final_prompt,
                    reference_images_bytes=ref_images or None,
                    aspect_ratio="1:1",
                    n=1,
                )

                compressed: list[bytes] = []
                for img_bytes in imgs:
                    try:
                        compressed.append(_bytes_to_thumbnail(img_bytes, size=768))
                    except Exception:
                        compressed.append(img_bytes)

                print(f"[image_gen] Gemini 生成完成，共 {len(compressed)} 张")
                # 如果模型返回的张数比请求少，就按实际数量返回
                return compressed[: max(1, min(len(compressed), total))]
            # 没有配置 Gemini，直接抛错提示用户检查环境变量
            raise RuntimeError("Gemini 图片通道不可用，请检查 GEMINI_IMAGE_API_KEY / GEMINI_IMAGE_BASE_URL")
        except Exception as e:
            print(f"[image_gen] Gemini 生成失败：{e}")
            # 如果已经成功生成了部分图片，直接返回这些，避免全部丢失
            if compressed:
                print(f"[image_gen] Gemini 部分成功，返回已生成的 {len(compressed)} 张，跳过后续生成")
                return compressed
        # 走到这里说明 Gemini 也完全失败，只能抛错给上层，让 UI 提示「生成失败」
        raise RuntimeError("图片生成失败：Gemini 通道不可用，请检查 GEMINI_IMAGE_API_KEY")

    # ── 公开方法 ──────────────────────────────────────────────────────────────

    def generate(
        self,
        count: int = 4,
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
        count: int = 4,
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
