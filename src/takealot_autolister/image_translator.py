"""
产品图片生成模块

策略：
- 主图（第1张）：直接使用白底产品图，不做改动
- 副图（2-N张）：生成专业英文产品特性卡，每张卡片突出一个核心卖点：
    布局：左侧产品图 60% + 右侧英文特性文案

如果配置了 DashScope（千问视觉），会先分析原始产品图理解产品特性，
再生成更精准的英文卖点描述。
"""
from __future__ import annotations

import base64
import io
import os
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ─── 字体 ──────────────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/Arial.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ─── 颜色主题 ─────────────────────────────────────────────────────────────────
_THEME = {
    "bg":        (255, 255, 255),   # 白色背景
    "accent":    (0, 120, 215),     # 蓝色强调
    "text_dark": (30,  30,  30),    # 深色文字
    "text_mid":  (80,  80,  80),    # 中色文字
    "divider":   (220, 220, 220),   # 分隔线
    "icon_bg":   (235, 245, 255),   # 图标背景
}

# ─── AI 卖点生成 ──────────────────────────────────────────────────────────────

def _call_qwen_vl(image_path: Path, question: str) -> str:
    """
    调用视觉模型分析图片。
    优先使用豆包（DOUBAO_API_KEY 已配置时），否则 fallback 到 DashScope 千问视觉。
    """
    import requests
    from dotenv import load_dotenv
    load_dotenv()

    # 优先硅基流动视觉
    if os.getenv("SILICONFLOW_API_KEY", "").strip():
        try:
            from .siliconflow_llm import call_doubao_vision
            return call_doubao_vision(image_path, question)
        except Exception:
            pass

    # fallback: DashScope 千问视觉
    api_key  = os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL", "")
    model    = os.getenv("LLM_VISION_MODEL", "qwen-vl-plus")
    if not api_key or not base_url:
        return ""
    try:
        img = Image.open(str(image_path)).convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": question},
                ],
            }],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers=headers, json=payload, timeout=60,
        )
        resp.raise_for_status()
        return str(((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except Exception:
        return ""


def _generate_feature_cards_content(
    product_title: str,
    product_attrs: dict,
    n_cards: int,
    main_image_path: Path | None = None,
) -> list[dict]:
    """
    调用 LLM 生成 n_cards 张特性卡的内容。
    每张卡片有：headline（标题）、body（2行描述）、icon_emoji（emoji图标）

    如果有主图，先用 Qwen-VL 分析图片获取产品特性。
    """
    try:
        from .llm import _call_llm_json, is_llm_available
        if not is_llm_available():
            return _fallback_cards(product_title, product_attrs, n_cards)
    except Exception:
        return _fallback_cards(product_title, product_attrs, n_cards)

    # 可选：视觉分析
    visual_context = ""
    if main_image_path and main_image_path.exists():
        visual_context = _call_qwen_vl(
            main_image_path,
            "Briefly describe the product's key visual features and design in English (max 60 words)."
        )

    attrs_str = ", ".join(f"{k}: {v}" for k, v in list(product_attrs.items())[:20] if v)
    prompt = (
        "You are a product copywriter for Takealot South Africa (English-speaking market).\n"
        f"Product: {product_title}\n"
        f"Attributes: {attrs_str}\n"
        + (f"Visual description: {visual_context}\n" if visual_context else "")
        + f"\nCreate {n_cards} distinct product feature cards for e-commerce secondary images.\n"
        "Each card highlights ONE specific feature.\n"
        "Rules:\n"
        "- headline: max 4 words, ALL CAPS, punchy\n"
        "- body: 2 lines, max 8 words each, clear benefit-focused language\n"
        "- icon: a relevant emoji\n"
        f"Return JSON: {{\"cards\": [{{"
        "\"headline\": \"...\", \"body\": [\"line1\", \"line2\"], \"icon\": \"emoji\""
        f"}}]}}"
    )
    try:
        from .llm import _call_llm_json
        result = _call_llm_json(prompt, temperature=0.4)
        cards = result.get("cards", [])
        if cards and len(cards) >= n_cards:
            return cards[:n_cards]
    except Exception:
        pass
    return _fallback_cards(product_title, product_attrs, n_cards)


def _fallback_cards(title: str, attrs: dict, n: int) -> list[dict]:
    """LLM 失败时的回退卡片内容。"""
    base = [
        {"headline": "WIRELESS FREEDOM", "body": ["Bluetooth connectivity", "No tangled cables"],   "icon": "📶"},
        {"headline": "LONG BATTERY",     "body": ["Extended playtime",    "USB-C fast charging"],   "icon": "🔋"},
        {"headline": "PREMIUM SOUND",    "body": ["Rich bass response",   "Crystal clear audio"],   "icon": "🎵"},
        {"headline": "COMPACT DESIGN",   "body": ["Portable & lightweight","Fits any lifestyle"],    "icon": "📦"},
        {"headline": "EASY PAIRING",     "body": ["One-touch connect",    "Multi-device support"],  "icon": "🔗"},
        {"headline": "QUALITY BUILD",    "body": ["Durable materials",    "Built to last"],         "icon": "⭐"},
    ]
    return base[:n]


# ─── 图片渲染 ─────────────────────────────────────────────────────────────────

def _make_feature_card(
    product_image: Image.Image,
    headline: str,
    body: list[str],
    icon: str,
    canvas_size: int = 1600,
) -> Image.Image:
    """
    生成单张特性卡片图片（1600×1600）。

    布局：
    ┌──────────────────────────────────────────┐
    │  [产品图居中，占70%高度]                    │
    │                                          │
    ├──────────────────────────────────────────┤
    │  [ICON]  HEADLINE                        │  ← 强调色背景条
    │          body line 1                     │
    │          body line 2                     │
    └──────────────────────────────────────────┘
    """
    W = H = canvas_size
    canvas = Image.new("RGB", (W, H), _THEME["bg"])
    draw = ImageDraw.Draw(canvas)

    # ── 产品图区域（上 68%）──
    product_area_h = int(H * 0.68)
    pad = int(W * 0.08)
    fit_size = (W - pad * 2, product_area_h - pad)
    from PIL import ImageOps
    pimg = product_image.convert("RGBA")
    pimg_fit = ImageOps.contain(pimg, fit_size)
    # 合成白底
    bg_layer = Image.new("RGBA", pimg_fit.size, (255, 255, 255, 255))
    bg_layer.paste(pimg_fit, mask=pimg_fit.split()[3] if pimg_fit.mode == "RGBA" else None)
    x0 = (W - pimg_fit.width)  // 2
    y0 = pad + (product_area_h - pad - pimg_fit.height) // 2
    canvas.paste(bg_layer.convert("RGB"), (x0, y0))

    # ── 分隔线 ──
    draw.line([(pad, product_area_h), (W - pad, product_area_h)], fill=_THEME["divider"], width=2)

    # ── 文案区域（下 32%）──
    text_y = product_area_h + int(H * 0.025)
    text_area_h = H - product_area_h

    # 强调色左侧竖条
    bar_w = int(W * 0.012)
    draw.rectangle([(pad, text_y), (pad + bar_w, H - int(H*0.03))], fill=_THEME["accent"])

    text_x = pad + bar_w + int(W * 0.03)

    # 图标
    icon_size = int(H * 0.055)
    icon_font = _load_font(icon_size)
    draw.text((text_x, text_y), icon, font=icon_font, fill=_THEME["accent"], embedded_color=True)

    # 标题
    hl_size = int(H * 0.058)
    hl_font = _load_font(hl_size, bold=True)
    hl_x = text_x + icon_size + int(W * 0.02)
    draw.text((hl_x, text_y + int(H * 0.002)), headline, font=hl_font, fill=_THEME["accent"])

    # body 文字
    body_size = int(H * 0.038)
    body_font = _load_font(body_size)
    body_y = text_y + hl_size + int(H * 0.018)
    for line in body[:2]:
        draw.text((text_x + bar_w + int(W * 0.015), body_y), line, font=body_font, fill=_THEME["text_dark"])
        body_y += body_size + int(H * 0.01)

    # 品牌角标（右下角）
    brand_font = _load_font(int(H * 0.025))
    draw.text((W - int(W * 0.22), H - int(H * 0.04)), "Takealot Ready", font=brand_font, fill=_THEME["text_mid"])

    return canvas


# ─── 对外接口 ─────────────────────────────────────────────────────────────────

def translate_image_set(
    image_paths: list[Path],
    out_dir: Path,
    product_title: str = "",
    product_attrs: dict | None = None,
    skip_first: bool = True,
    min_images: int = 5,
) -> list[Path]:
    """
    生成 Takealot 标准产品图集：
    - image 1（主图）：直接复制白底图，不改动
    - image 2-N：生成专业英文产品特性卡片（最少 min_images 张）

    返回所有输出图片路径列表。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    attrs = product_attrs or {}

    if not image_paths:
        return []

    # 主图：原样保存
    results: list[Path] = []
    main_img_path = image_paths[0]
    main_out = out_dir / "translated_01.jpg"
    main_img = Image.open(str(main_img_path)).convert("RGB")
    main_img.save(str(main_out), format="JPEG", quality=95)
    results.append(main_out)

    # 需要生成多少张副图：取 max(原始副图数, min_images-1)
    n_secondary = max(len(image_paths) - 1, min_images - 1)

    # 生成每张副图的内容（通过 LLM / 视觉模型）
    cards = _generate_feature_cards_content(
        product_title=product_title,
        product_attrs=attrs,
        n_cards=n_secondary,
        main_image_path=main_img_path if main_img_path.exists() else None,
    )

    # 如果 cards 不够，用回退内容补充
    fallback = _fallback_cards(product_title, attrs, n_secondary)
    while len(cards) < n_secondary:
        cards.append(fallback[len(cards) % len(fallback)])

    # 生成卡片图片
    product_img = Image.open(str(main_img_path)).convert("RGBA")
    for i, card in enumerate(cards, start=2):
        out_path = out_dir / f"translated_{i:02d}.jpg"
        try:
            card_img = _make_feature_card(
                product_image=product_img,
                headline=str(card.get("headline", "FEATURE")).upper(),
                body=list(card.get("body", ["Great quality", "Best value"]))[:2],
                icon=str(card.get("icon", "⭐")),
            )
            card_img.save(str(out_path), format="JPEG", quality=93)
            results.append(out_path)
        except Exception:
            Image.open(str(main_img_path)).convert("RGB").save(str(out_path), format="JPEG", quality=93)
            results.append(out_path)

    return results
