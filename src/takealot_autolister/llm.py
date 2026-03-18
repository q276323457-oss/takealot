from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Any

import requests
from dotenv import load_dotenv

from .rules import RuleSet
from .types import ListingDraft, ProductSource


def _extract_json_block(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)

    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("json payload not found in model output")
    return json.loads(m.group(0))


def _clean_for_title(raw: str) -> str:
    txt = re.sub(r"\s+", " ", raw).strip()
    txt = txt.replace("【", "").replace("】", "")
    return txt


def _extract_model_token(text: str) -> str:
    src = str(text or "").strip()
    if not src:
        return ""
    patterns = [
        r"(?:model(?:\s*no\.?| number)?|型号)\s*[:：#]?\s*([a-z0-9][a-z0-9\-_./]{1,31})",
        r"\b([a-z]{1,4}\d{2,}[a-z0-9\-_]{0,16})\b",
        r"\b(\d{2,}[a-z]{1,4}[a-z0-9\-_]{0,16})\b",
    ]
    for p in patterns:
        m = re.search(p, src, flags=re.IGNORECASE)
        if not m:
            continue
        token = re.sub(r"[^A-Z0-9\-_./]+", "", m.group(1).upper()).strip("-_. /")
        if len(token) >= 3:
            return token[:24]
    return ""


def _guess_model_from_source(source: ProductSource) -> str:
    text_blob = " ".join(
        [
            str(source.title or ""),
            str(source.subtitle or ""),
            str(source.description or ""),
            str(source.price_text or ""),
            json.dumps(source.raw or {}, ensure_ascii=False),
        ]
    )
    return _extract_model_token(text_blob)


def _llm_config() -> tuple[str, str, str]:
    load_dotenv()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "qwen-plus").strip()
    return base_url, api_key, model


def _use_doubao() -> bool:
    """
    是否优先使用硅基流动。

    现在增加一个显式开关：
        DISABLE_SILICONFLOW=1 时完全禁用硅基流动，统一走 LLM_BASE_URL。
    """
    load_dotenv()
    if os.getenv("DISABLE_SILICONFLOW", "").strip() not in {"", "0"}:
        return False
    return bool(os.getenv("SILICONFLOW_API_KEY", "").strip())


def is_llm_available() -> bool:
    if _use_doubao():
        return True
    base_url, api_key, _ = _llm_config()
    return bool(base_url and api_key)


def _call_llm_json(prompt: str, *, temperature: float = 0.2) -> dict[str, Any]:
    return _extract_json_block(_call_llm_raw(prompt, temperature=temperature))


def _call_llm_raw(prompt: str, *, temperature: float = 0.2) -> str:
    """调用 LLM，返回原始文本响应（未解析）。"""
    # 优先硅基流动；如果调用失败，则自动回退到通用 LLM_BASE_URL
    if _use_doubao():
        from .siliconflow_llm import call_doubao_raw
        return call_doubao_raw(prompt, temperature=temperature)

    base_url, api_key, model = _llm_config()
    if not base_url or not api_key:
        raise RuntimeError("请在主界面填写文本 Key 并点击「保存配置」")

    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": float(temperature),
        "messages": [
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def ask_llm_for_takealot_category(
    source_category_path: list[str],
    source_title: str,
) -> list[str]:
    """
    当 CSV 匹配失败时，让 LLM 判断该商品在 Takealot 中属于哪个类目层级。
    返回完整类目路径，如 ['Home', 'Automotive', 'Motor Vehicle Electronics', 'Car Electronic Accessories']
    失败时返回空列表。
    """
    if not is_llm_available():
        return []

    zh_cats = " > ".join(source_category_path) if source_category_path else ""
    prompt = (
        "You are an expert on Takealot (South Africa's largest ecommerce platform) product categories.\n"
        "Given a product's 1688 (Chinese supplier) category and title, return the correct Takealot portal "
        "category navigation path as a JSON array.\n\n"
        "Takealot category structure (Division > Department) — use ONLY these exact names:\n"
        "Consumer Electronics: Cameras, Computer Components, Computers & Laptops, Electronic Accessories, Gaming, Mobile, Musical Instruments, TV & Audio, Wearable Tech\n"
        "Home: Automotive, DIY, Garden Pool & Patio, Homeware: Bed & Bathroom, Homeware: Kitchen & Decor, Large Appliances, Small Appliances\n"
        "Personal & Lifestyle: Beauty, Camping, Cycling, Fashion: Accessories, Fashion: Clothing, Fashion: Footwear, Luggage, Sport: Clothing & Footwear, Sport: Equipment\n"
        "Family: Baby, Pets, Toys\n"
        "Consumables: Health, Liquor, Non Perishable\n"
        "Media: Books, Movies, Music\n"
        "Office & Business: Industrial Business & Scientific, Office & Office Furniture, Stationery\n\n"
        "IMPORTANT rules:\n"
        "- Level 1 MUST be one of the 7 Divisions listed above\n"
        "- Level 2 MUST be one of the listed Departments for that Division\n"
        "- Do NOT invent category names — if unsure about Level 3+, stop at Level 2\n"
        "- Return STRICT JSON only: {\"path\": [\"Division\", \"Department\", \"Main Category\", \"Sub Category\"]}\n\n"
        f"Product 1688 category: {zh_cats}\n"
        f"Product title: {source_title}\n"
    )

    try:
        result = _call_llm_json(prompt, temperature=0.1)
        path = result.get("path", [])
        if isinstance(path, list) and len(path) >= 2:
            return [str(x).strip() for x in path if str(x).strip()]
    except Exception as e:
        print(f"[llm] ask_llm_for_takealot_category failed: {e}")
    return []


def fallback_generate_draft(source: ProductSource, rules: RuleSet) -> ListingDraft:
    base_title = _clean_for_title(source.title) or "Generic Kitchen Organizer"
    title = base_title[:72]
    model_no = _guess_model_from_source(source)
    subtitle = "Durable daily-use item with practical design"[:110]
    supplier_cat = " > ".join(source.category_path or [])
    cat_hint = f" ({supplier_cat})" if supplier_cat else ""
    features = (
        f"Features:\n"
        f"- {base_title}{cat_hint} — compact design for everyday use\n"
        f"- Durable construction with quality materials\n"
        f"- Easy to set up and use right out of the box\n\n"
        f"Specifications:\n"
        f"- Please verify exact dimensions and weight against supplier data before publishing\n"
        f"- Compatible with standard usage requirements\n\n"
        f"What You Get:\n"
        f"- 1 x {base_title}\n"
        f"- All included accessories as listed in What's in the Box"
    )

    draft = ListingDraft(
        title=title,
        subtitle=subtitle,
        key_features=features,
        whats_in_box=["1 x Main Product"],
        attributes={
            "brand": "",
            "material": "To be confirmed",
            "colour": "To be confirmed",
            "size": "To be confirmed",
            "weight": "To be confirmed",
            "model": model_no,
        },
        variants=[],
        compliance_notes=["Auto-generated fallback draft, verify all product facts manually."],
        source_url=source.source_url,
    )
    return draft


def _build_prompt(source: ProductSource, rules: RuleSet) -> str:
    c = rules.constraints

    # 提炼有用的产品属性（过滤掉尺寸/物流等非卖点字段）
    _SKIP_ATTR_KEYS = {
        "货源类别", "商品类型", "成色", "oem", "加工方式", "最快出货时间",
        "是否支持一件代发", "售后服务", "上市时间", "主要下游平台", "主要销售地区",
        "有可授权的自有品牌", "是否跨境出口专供货源", "体积", "重量",
        "宽(cm)", "高(cm)", "长(cm)", "barcod", "发布价",
        "9", "225", "货号", "成色", "最快出货时间",
    }
    raw_attrs = source.product_attrs or {}
    useful_attrs = {
        k: v for k, v in raw_attrs.items()
        if str(k).lower() not in _SKIP_ATTR_KEYS and not str(k).startswith("500GB")
        and len(str(v)) < 100 and len(str(k)) < 30
    }

    product_data = {
        "title": source.title,
        "category": " > ".join(source.category_path or []),
        "product_attributes": useful_attrs,
        "sku_options": source.sku_options or [],
        "source_url": source.source_url,
    }

    # 包装信息：有就用第一行（最常见规格），没有就让 LLM 自行估算
    pkg = (source.packaging_info or [{}])[0] if source.packaging_info else {}
    if pkg:
        product_data["packaging_dimensions"] = {
            "length_cm": pkg.get("length_cm", ""),
            "width_cm":  pkg.get("width_cm", ""),
            "height_cm": pkg.get("height_cm", ""),
            "weight_g":  pkg.get("weight_g", ""),
        }
        pkg_note = "Use the provided packaging_dimensions for packaged_length/width/height/weight_g."
    else:
        pkg_note = (
            "No packaging dimensions available. Estimate realistic packaged_length/width/height (cm) "
            "and weight_g based on the product type and size. Include these in attributes."
        )

    return f"""
You are an expert e-commerce copywriter for Takealot South Africa.
Generate STRICT JSON only, no markdown, no commentary.

Product data (may contain Chinese — translate and use the key info):
{json.dumps(product_data, ensure_ascii=False)}

REQUIREMENTS:
- title: Concise English product title, <= {c.get('title_max_len', 75)} chars.
  Focus on the product type and main benefit / use case.
  Do NOT include brand names, model numbers or detailed numeric specs (capacity, GB, inches, watts, etc.) in the title.
- subtitle: 1 sentence, <= {c.get('subtitle_max_len', 110)} chars. Highlight the top 1-2 unique selling points. Avoid repeating brand/model/specs.
- key_features: >= {c.get('key_features_min_len', 200)} chars. Write 4-6 bullet points using "- " prefix.
  Each bullet covers a different selling dimension: storage capacity & speed, build quality & materials,
  compatibility & connectivity, use cases, warranty/support. Be specific — use actual specs from product data.
- whats_in_box: list of items included (translate from Chinese if needed). Default ["1 x [Product Name]"] if unknown.
- attributes: extract from product data. brand: extract from 品牌 field, translate phonetically to English. colour in English.
  Include packaged_length, packaged_width, packaged_height (cm), packaged_weight (g) in attributes.
  {pkg_note}
- compliance_notes: note anything unverified (e.g. actual transfer speed, compatibility).
- No prohibited claims: "miracle", "guaranteed", "100% safe", "#1", "best in the world".
- English only. Takealot South Africa marketplace style.

JSON schema:
{{
  "title": "...",
  "subtitle": "...",
  "key_features": "- bullet 1\\n- bullet 2\\n- bullet 3\\n- bullet 4",
  "whats_in_box": ["1 x Product"],
  "attributes": {{"brand":"", "material":"", "colour":"", "size":"", "weight":"", "model":"",
                  "packaged_length":"", "packaged_width":"", "packaged_height":"", "packaged_weight":""}},
  "variants": [],
  "compliance_notes": ["..."]
}}
""".strip()


def generate_draft_with_llm(source: ProductSource, rules: RuleSet) -> ListingDraft:
    parsed = _call_llm_json(_build_prompt(source, rules), temperature=0.2)
    attrs = {str(k): str(v).strip() for k, v in (parsed.get("attributes") or {}).items()}
    if not attrs.get("model"):
        attrs["model"] = _guess_model_from_source(source)

    return ListingDraft(
        title=str(parsed.get("title", "")).strip(),
        subtitle=str(parsed.get("subtitle", "")).strip(),
        key_features=str(parsed.get("key_features", "")).strip(),
        whats_in_box=[str(x).strip() for x in parsed.get("whats_in_box", []) if str(x).strip()],
        attributes=attrs,
        variants=[],
        compliance_notes=[
            str(x).strip() for x in parsed.get("compliance_notes", []) if str(x).strip()
        ],
        source_url=source.source_url,
    )


def generate_portal_section_values(
    draft: ListingDraft,
    section_name: str,
    fields: list[dict[str, Any]],
) -> dict[str, str]:
    values, _ = generate_portal_section_values_debug(draft, section_name, fields)
    return values


def _build_portal_fill_input_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_fields = []
    for f in fields[:40]:
        key = str(f.get("key", "")).strip()
        if not key:
            continue
        input_fields.append(
            {
                "key": key,
                "label": str(f.get("label", "")).strip()[:120],
                "placeholder": str(f.get("placeholder", "")).strip()[:120],
                "type": str(f.get("type", "text")).strip(),
                "required": bool(f.get("required", False)),
                "options": [str(x).strip()[:80] for x in (f.get("options") or []) if str(x).strip()][:20],
            }
        )
    return input_fields


def _build_portal_fill_prompt(draft: ListingDraft, section_name: str, input_fields: list[dict[str, Any]]) -> str:
    return (
        "You are filling a Takealot seller form section.\n"
        "Return STRICT JSON only.\n\n"
        "Goal:\n"
        "- Fill required fields first.\n"
        "- Use realistic ecommerce values based on source data.\n"
        "- For optional fields: fill only when confident; otherwise leave empty (do not invent).\n"
        "- Brand: always leave empty (no brand / unbranded).\n"
        "- For dropdown/combobox, choose from provided options when available.\n"
        "- For Yes/No fields, decide from product facts; if uncertain prefer 'No' or leave empty when optional.\n"
        "- If field supports multiple values, output a concise comma-separated value list.\n"
        "- Keep values concise and valid for product listing forms.\n\n"
        "Output schema:\n"
        '{ "values": [{"key":"...", "value":"..."}] }\n\n'
        f"Section: {section_name}\n"
        f"Draft JSON: {json.dumps(asdict(draft), ensure_ascii=False)}\n"
        f"Fields JSON: {json.dumps(input_fields, ensure_ascii=False)}\n"
    )


def _parse_portal_fill_values(parsed: dict[str, Any], input_fields: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    vals = parsed.get("values", [])
    if not isinstance(vals, list):
        return out
    valid_keys = {str(x.get("key", "")).strip() for x in input_fields}
    for item in vals:
        if not isinstance(item, dict):
            continue
        k = str(item.get("key", "")).strip()
        v = str(item.get("value", "")).strip()
        if not k or not v:
            continue
        if k not in valid_keys:
            continue
        out[k] = v[:160]
    return out


def generate_listing_with_instructions(
    product_info: dict,
    portal_fields: list[dict],
    user_instructions: str = "",
) -> dict:
    """
    User-directed listing generation.
    Sends raw 1688 product data + portal fields + user instructions to LLM.
    Returns {"title", "subtitle", "key_features", "whats_in_box", "field_values"}.
    """
    field_defs = []
    for f in portal_fields[:30]:
        key = str(f.get("key", "")).strip()
        label = str(f.get("label", "")).strip()
        required = bool(f.get("required", False))
        opts = [str(x)[:60] for x in (f.get("options") or []) if str(x).strip()][:10]
        placeholder = str(f.get("placeholder", "")).strip()[:80]
        if key or label:
            field_defs.append({
                "key": key or label,
                "label": label,
                "required": required,
                "options": opts,
                "placeholder": placeholder,
            })

    # 从 product_attrs 提取中文属性摘要，辅助 LLM 理解
    attrs: dict = product_info.get("product_attrs", {})
    attrs_summary = ""
    if attrs:
        attrs_summary = "1688 product attributes:\n" + "\n".join(
            f"  {k}: {v}" for k, v in list(attrs.items())[:25]
        )

    default_instructions = (
        "You are helping list a product on Takealot (South Africa). "
        "Based on all available product data, generate a complete, compelling listing in English. "
        "Use the 1688 product attributes (model, weight, material, colour, specs etc.) to fill portal fields accurately. "
        "For the TITLE: focus only on the product type and the main benefit/use-case. "
        "Do NOT include brand names, model numbers, or detailed numeric specs (GB, inches, watts, litres, etc.) in the title. "
        "The title should read like a clean, generic product name plus its key use, not a spec sheet. "
        "Subtitle should complement the title with secondary selling points (you may mention specs there). "
        "Fill ALL portal fields where data is available — especially required ones."
    )
    instructions = user_instructions.strip() or default_instructions

    product_summary = json.dumps({
        k: v for k, v in product_info.items()
        if k not in ("raw", "image_urls", "price_text", "description") and v
    }, ensure_ascii=False)[:3000]

    prompt = (
        "You are an ecommerce listing assistant for Takealot (South Africa).\n"
        "Generate STRICT JSON only, no markdown, no commentary.\n\n"
        f"Instructions: {instructions}\n\n"
        f"Product data from 1688:\n{product_summary}\n"
        f"{attrs_summary}\n\n"
        f"Portal fields to fill:\n{json.dumps(field_defs, ensure_ascii=False)}\n\n"
        "Output JSON schema:\n"
        "{\n"
        '  "title": "...",\n'
        '  "subtitle": "...",\n'
        '  "key_features": "Features:\\n- Selling point 1\\n- Selling point 2\\n\\nSpecifications:\\n- Spec: value\\n- Spec: value",\n'
        '  "whats_in_box": ["1 x Product Name", "1 x Accessory"],\n'
        '  "field_values": {"field_key_or_label": "value"}\n'
        "}\n\n"
        "Hard rules:\n"
        "- title ≤ 75 chars, English only, no Chinese. Do NOT include brand names, model numbers "
        "or detailed numeric specs (capacity/GB, Hz, inches, watts, litres etc.) in the title. "
        "Focus on the product type and the main benefit/use-case.\n"
        "- subtitle ≤ 110 chars, English only. You may mention 1–2 key specs here, but avoid repeating brand/model.\n"
        "- key_features: Use paragraphs + hyphens format as required by the portal. "
        "Write 2-3 short paragraph blocks, each with a title line then hyphen-bulleted items:\n"
        "  Features:\n  - [selling point based on product]\n  - [selling point]\n  - [selling point]\n\n"
        "  Specifications:\n  - [spec: actual value]\n  - [spec: actual value]\n  - [spec: actual value]\n\n"
        "  Compatibility:\n  - [compatible OS/devices]\n  - [use case or target user]\n"
        "Use real specs from product data. Min 200 chars. Do NOT include variant info (colour/size variants).\n"
        "- whats_in_box: list each item separately (e.g. '1 x Smartwatch', '1 x Charging Cable'). "
        "Translate from Chinese. Default to ['1 x Product'] only if completely unknown.\n"
        "- Brand: always leave empty (no brand / unbranded).\n"
        "- field_values key: use the field's 'key' value (or 'label' if key is empty)\n"
        "- ALL required fields MUST have a value — never leave a required field empty. "
        "For unknown free-text required fields: use your best estimate based on product type/category. "
        "For unknown Yes/No fields: use 'No' as default.\n"
        "- For required number fields (type=number) with no exact spec in product data, estimate a realistic value "
        "based on product category (e.g. smartwatch/activity tracker: Screen Size ~1.4, Bezel Size ~44, Strap Size ~22). "
        "Always output a plain number with no units.\n"
        "- CRITICAL: For dropdown/select fields that have an 'options' list: you MUST copy the value EXACTLY "
        "as it appears in 'options'. Do NOT invent, modify, translate, combine, or approximate values. "
        "If no option is a good match, leave the field empty string (\"\"). "
        "Wrong: 'Lilac/Khaki', 'Albania', 'Ethnic' — these are invented. "
        "Right: pick word-for-word from the provided options list only.\n"
        "- For select fields with NO options listed but whose label is a Yes/No question (e.g. 'Does this...', 'Has...', 'Is...'), "
        "fill it with 'Yes' or 'No' based on product facts.\n"
        "- For packaging dimensions/weight: use provided values or estimate realistically from product type\n"
        "- Warranty: use 'Limited' as default if not specified\n"
        "- Model Number: use the model/货号 from product_attrs if available\n"
        "- Colour: translate 颜色 to English (Black/White/Silver/Gold/Blue/Red etc.); "
        "if multiple, pick the most common/default variant\n"
    )

    print(f"\n{'='*60}\n[LLM] 发送给豆包的完整提示词：\n{'='*60}\n{prompt}\n{'='*60}\n")

    raw_text = _call_llm_raw(prompt, temperature=0.2)

    print(f"\n{'='*60}\n[LLM] 豆包原始返回：\n{'='*60}\n{raw_text}\n{'='*60}\n")

    try:
        parsed = _extract_json_block(raw_text)
    except Exception as e:
        parsed = {"_parse_error": str(e)}

    parsed["_debug_prompt"] = prompt
    parsed["_debug_raw"] = raw_text
    return parsed


def generate_portal_section_values_debug(
    draft: ListingDraft,
    section_name: str,
    fields: list[dict[str, Any]],
) -> tuple[dict[str, str], str]:
    if not is_llm_available():
        return {}, "LLM not configured (missing LLM_BASE_URL/LLM_API_KEY)"
    if not fields:
        return {}, "No fields detected for AI fill"

    input_fields = _build_portal_fill_input_fields(fields)
    if not input_fields:
        return {}, "No usable fields detected for AI fill"

    prompt = _build_portal_fill_prompt(draft, section_name, input_fields)

    try:
        parsed = _call_llm_json(prompt, temperature=0.1)
    except Exception as e:
        err = str(e)
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = (resp.text or "")[:220]
                if body:
                    err = f"{err} | {body}"
            except Exception:
                pass
        return {}, err

    vals = _parse_portal_fill_values(parsed, input_fields)
    if vals:
        return vals, ""
    try:
        raw = json.dumps(parsed, ensure_ascii=False)[:220]
    except Exception:
        raw = str(parsed)[:220]
    return {}, f"LLM returned empty values: {raw}"
