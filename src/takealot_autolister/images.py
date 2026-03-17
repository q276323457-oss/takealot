from __future__ import annotations

import io
from pathlib import Path

import requests
from PIL import Image, ImageOps, ImageStat


def _has_chinese_text_heuristic(img: Image.Image) -> bool:
    """
    粗略检测图片是否含中文文字区域。
    方法：把图缩小后检查高对比度细线密度——中文字符在小图上呈现为密集高对比黑色细条。
    这是启发式方法，不是 OCR，但对 1688 详情图足够准确。
    """
    sample = img.convert("L").resize((128, 128))
    px = list(sample.getdata())
    n = len(px)
    # 深色像素比例（<80 灰度）
    dark = sum(1 for p in px if p < 80) / n
    # 中文文字详情图通常有 8-25% 深色像素（字迹），纯白底产品图 <5%
    # 超过 12% 且不超过 60% 认为有文字
    return 0.08 < dark < 0.60


def _dominant_color_variance(img: Image.Image) -> float:
    """返回图片颜色方差，越高说明颜色越丰富（详情图）。"""
    sample = img.convert("RGB")
    sample.thumbnail((64, 64))
    stat = ImageStat.Stat(sample)
    return sum(stat.stddev) / 3.0


def _is_white_background(img: Image.Image, threshold: float = 0.55) -> bool:
    """判断图片是否以白色/浅色为主背景。"""
    sample = img.convert("RGB")
    sample.thumbnail((128, 128))
    px = list(sample.getdata())
    white = sum(1 for r, g, b in px if r > 230 and g > 230 and b > 230) / len(px)
    return white >= threshold


def _is_usable_product_image(img: Image.Image, min_side: int = 120) -> bool:
    w, h = img.size
    if min(w, h) < min_side:
        return False
    sample = img.convert("RGB").copy()
    sample.thumbnail((256, 256))
    px = list(sample.getdata())
    if not px:
        return False
    n = len(px)
    non_white = sum(1 for r, g, b in px if not (r > 245 and g > 245 and b > 245)) / n
    if non_white < 0.015:
        return False
    stat = ImageStat.Stat(sample)
    avg_std = sum(stat.stddev) / max(1, len(stat.stddev))
    if avg_std < 4.0 and non_white < 0.12:
        return False
    return True


def _is_clean_product_image(img: Image.Image, is_first: bool = False) -> bool:
    """
    判断是否是适合上传 Takealot 的干净产品图：
    - 主图（第1张）：只接受白底图
    - 副图（后续）：拒绝中文字、过于丰富的颜色（详情图）
    """
    if is_first:
        # 主图必须白底
        return _is_white_background(img, threshold=0.50)
    else:
        # 副图：拒绝含中文文字的详情图
        if _has_chinese_text_heuristic(img):
            return False
        # 副图：拒绝颜色极为丰富的促销/信息图（stddev > 65）
        if _dominant_color_variance(img) > 65:
            return False
        return True



def download_images(image_urls: list[str], out_dir: Path, limit: int = 8) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for url in image_urls:
        if len(paths) >= limit:
            break
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content))
            if not _is_usable_product_image(img):
                continue
            is_first = len(paths) == 0
            if not _is_clean_product_image(img, is_first=is_first):
                continue
            ext = ".png" if "png" in (r.headers.get("content-type", "").lower()) else ".jpg"
            p = out_dir / f"raw_{len(paths)+1:02d}{ext}"
            p.write_bytes(r.content)
            paths.append(p)
        except Exception:
            continue
    return paths


def _remove_bg_if_enabled(img: Image.Image, remove_bg: bool) -> Image.Image:
    if not remove_bg:
        return img
    try:
        from rembg import remove
    except Exception:
        return img

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = remove(buf.getvalue())
    return Image.open(io.BytesIO(out)).convert("RGBA")


def make_white_background_image(
    input_path: Path,
    output_path: Path,
    canvas_size: int = 1600,
    remove_bg: bool = False,
) -> Path:
    img = Image.open(input_path).convert("RGBA")
    img = _remove_bg_if_enabled(img, remove_bg=remove_bg)

    bg = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))
    fit = ImageOps.contain(img, (int(canvas_size * 0.9), int(canvas_size * 0.9)))
    x = (canvas_size - fit.width) // 2
    y = (canvas_size - fit.height) // 2
    bg.paste(fit, (x, y), fit if fit.mode == "RGBA" else None)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(output_path, format="JPEG", quality=95)
    return output_path


def create_white_bg_set(
    raw_images: list[Path],
    out_dir: Path,
    remove_bg: bool = False,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for i, p in enumerate(raw_images, start=1):
        out = out_dir / f"white_{i:02d}.jpg"
        outputs.append(make_white_background_image(p, out, remove_bg=remove_bg))
    return outputs


def create_sku_cards(variants: list[dict[str, str]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for i, item in enumerate(variants, start=1):
        img = Image.new("RGB", (1600, 1600), (255, 255, 255))
        text = "SKU CARD\n\n"
        for k, v in item.items():
            text += f"{k}: {v}\n"

        # Default bitmap font to avoid extra font deps.
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        draw.text((120, 120), text, fill=(20, 20, 20))

        out = out_dir / f"sku_{i:02d}.jpg"
        img.save(out, format="JPEG", quality=95)
        outputs.append(out)
    return outputs
