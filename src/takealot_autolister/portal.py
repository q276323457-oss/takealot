from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import sync_playwright

from .llm import generate_portal_section_values_debug
from .types import ListingDraft


class NeedLoginError(RuntimeError):
    pass


class PortalFormNotReadyError(RuntimeError):
    pass


_CATEGORY_BLOCK_WORDS = [
    "need to submit",
    "unlock this category",
    "not available",
    "coming soon",
]


def _to_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _category_overrides_path(selectors_cfg_path: str | Path) -> Path:
    base = Path(selectors_cfg_path).resolve().parent.parent
    return (base / "input" / "category_overrides.yaml").resolve()


def _load_category_overrides(selectors_cfg_path: str | Path) -> list[dict[str, Any]]:
    path = _category_overrides_path(selectors_cfg_path)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        scp = item.get("source_category_path")
        tgt = item.get("takealot_path")
        if not isinstance(scp, list) or not isinstance(tgt, list):
            continue
        src_path = [str(x).strip() for x in scp if str(x).strip()]
        tgt_path = [str(x).strip() for x in tgt if str(x).strip()]
        if not src_path or not tgt_path:
            continue
        keywords = item.get("keywords") or []
        if isinstance(keywords, list):
            kws = [str(k).strip() for k in keywords if str(k).strip()]
        else:
            kws = []
        out.append(
            {
                "source_category_path": src_path,
                "takealot_path": tgt_path,
                "keywords": kws,
            }
        )
    return out


def _match_override_path(
    overrides: list[dict[str, Any]],
    source_category_path: list[str] | None,
    source_title: str = "",
) -> list[str]:
    scp = [str(x).strip() for x in (source_category_path or []) if str(x).strip()]
    if not overrides:
        return []
    # 1) exact match on source_category_path
    for ov in overrides:
        if ov.get("source_category_path") == scp and ov.get("takealot_path"):
            return list(ov["takealot_path"])
    # 2) keyword-based match
    query = " ".join(scp + [source_title]).lower()
    best: list[str] | None = None
    best_score = 0
    for ov in overrides:
        tgt = ov.get("takealot_path")
        if not tgt:
            continue
        kws = [str(k).lower() for k in ov.get("keywords", []) if str(k).strip()]
        if not kws:
            continue
        score = sum(1 for kw in kws if kw and kw in query)
        if score > best_score:
            best_score = score
            best = list(tgt)
    if best_score > 0 and best:
        return best
    return []


def load_selectors(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid selectors file: {path}")
    return data


def _best_effort_fill(page, selector: str, value: str) -> bool:
    if not selector or not value:
        return False
    try:
        loc = page.locator(selector).first
        loc.wait_for(timeout=3000)
        loc.fill(value)
        return True
    except Exception:
        return False


def _apply_attributes(page, selectors: dict[str, str], attrs: dict[str, str]) -> dict[str, bool]:
    status: dict[str, bool] = {}
    for key, val in attrs.items():
        s = selectors.get(key)
        if not s:
            status[key] = False
            continue
        status[key] = _best_effort_fill(page, s, val)
    return status


def _check_login_required(page, cfg: dict[str, Any]) -> bool:
    markers = cfg.get("portal", {}).get("login_required_markers", [])
    for m in markers:
        try:
            if page.locator(m).first.count() > 0:
                return True
        except Exception:
            continue
    return False


def _detect_incomplete_sections(page) -> list[str]:
    try:
        texts = page.evaluate(
            """
() => {
  const out = [];
  const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,label,div,span'));
  for (const n of nodes) {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 120) continue;
    if (t.includes('None')) out.push(t);
  }
  return out.slice(0, 200);
}
"""
        )
    except Exception:
        return []

    sections: list[str] = []
    keywords = [
        "Product Category",
        "Product Variants",
        "Product Attributes",
        "Product Details",
        "Product Images",
        "Product Identifiers",
    ]
    for t in texts:
        s = str(t)
        for k in keywords:
            if k in s and (k not in sections):
                sections.append(k)
    return sections


def _section_next_enabled(page, section_name: str) -> bool:
    try:
        btn = page.locator(f"section[data-sectionname='{section_name}'] button:has-text('Next')").first
        return btn.count() > 0 and btn.is_enabled(timeout=1200)
    except Exception:
        return False


def _click_section_next(page, section_name: str) -> bool:
    try:
        btn = page.locator(f"section[data-sectionname='{section_name}'] button:has-text('Next')").first
        if btn.count() > 0:
            try:
                btn.scroll_into_view_if_needed(timeout=1200)
            except Exception:
                pass
            if btn.is_enabled(timeout=1000):
                try:
                    btn.click(timeout=2500)
                except Exception:
                    btn.click(timeout=2500, force=True)
                return True
        return bool(
            page.evaluate(
                """
(sec) => {
  const s = document.querySelector(`section[data-sectionname="${sec}"]`);
  if (!s) return false;
  const b = Array.from(s.querySelectorAll('button')).find(x => /next/i.test((x.textContent || '').trim()));
  if (!b || b.disabled) return false;
  b.click();
  return true;
}
""",
                section_name,
            )
        )
    except Exception:
        return False


def _activate_section(page, section_name: str) -> None:
    try:
        page.locator(f"section[data-sectionname='{section_name}'] .ZorkSection__header").first.click(force=True, timeout=3000)
        page.wait_for_timeout(400)
    except Exception:
        pass
    # Some sections require entering edit mode via the icon at right side.
    try:
        edit_btn = page.locator(
            f"section[data-sectionname='{section_name}'] button[aria-label*='edit' i], "
            f"section[data-sectionname='{section_name}'] button:has(i), "
            f"section[data-sectionname='{section_name}'] .fa-pen, "
            f"section[data-sectionname='{section_name}'] [data-testid*='edit' i]"
        ).first
        if edit_btn.count() > 0:
            try:
                edit_btn.click(timeout=1200)
                page.wait_for_timeout(250)
            except Exception:
                pass
    except Exception:
        pass


def _get_category_items(page) -> list[dict[str, Any]]:
    try:
        return page.evaluate(
            """
() => {
  // Try scoped selector first, fall back to global if section not found
  let nodes = Array.from(document.querySelectorAll("section[data-sectionname='Product Category'] .ZorkMillerColumns__item"));
  if (nodes.length === 0) {
    nodes = Array.from(document.querySelectorAll(".ZorkMillerColumns__item"));
  }
  return nodes.map(n => {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim();
    const r = n.getBoundingClientRect();
    return { txt: t, low: t.toLowerCase(), x: Math.round(r.x), y: Math.round(r.y) };
  });
}
"""
        )
    except Exception:
        return []


def _category_columns_snapshot(page) -> dict[str, list[str]]:
    items = _get_category_items(page)
    cols: dict[int, list[str]] = {}
    for i in items:
        try:
            x = int(i.get("x", 0))
            txt = str(i.get("txt", "")).strip()
        except Exception:
            continue
        if not txt:
            continue
        cols.setdefault(x, [])
        if txt not in cols[x]:
            cols[x].append(txt)
    out: dict[str, list[str]] = {}
    for idx, x in enumerate(sorted(cols.keys()), start=1):
        out[f"col_{idx}_x{x}"] = cols[x]
    return out


def _click_category_item(page, x: int, txt: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
(p) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  // Try scoped selector first, fall back to global
  let nodes = Array.from(document.querySelectorAll("section[data-sectionname='Product Category'] .ZorkMillerColumns__item"));
  if (nodes.length === 0) {
    nodes = Array.from(document.querySelectorAll(".ZorkMillerColumns__item"));
  }
  // Primary match: same column (x) + exact text
  let hit = nodes.find(n => Math.abs(n.getBoundingClientRect().x - p.x) < 8 && norm(n.textContent) === p.txt);
  // Fallback: exact text match in any column (in case x shifted after scroll)
  if (!hit) hit = nodes.find(n => norm(n.textContent) === p.txt);
  if (!hit) return false;
  hit.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
  hit.click();
  return true;
}
""",
                {"x": x, "txt": txt},
            )
        )
    except Exception:
        return False


def _norm_text(s: str) -> str:
    t = str(s or "").strip().lower()
    t = re.sub(r"[›>]+", " ", t)
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", t)
    return " ".join(t.split())


def _parse_category_path(raw: str) -> list[str]:
    parts = [x.strip() for x in str(raw or "").split(">")]
    return [x for x in parts if x]


def _complete_category_by_path(page, category_path: list[str]) -> bool:
    """
    按给定的完整类目路径在 Portal 左侧 Miller 列表中逐层点击。

    特别处理两类 Portal / CSV 不一致的情况：
    1) CSV 路径里带有 ID（如 "Storage Devices (15639)"），而前台只显示名称；
       点击时忽略括号内的纯数字 ID，仅按名称匹配。
    2) Portal 某些层级会把多级折叠到同一行（如 "Storage Devices -> Portable HDD"），
       当行文本同时包含当前层和下一层名称时，视为“一次点击完成两级”，
       从 remaining 中一次性弹出两个层级，避免下一轮再去寻找已包含的 leaf。
    """
    if not category_path:
        return False

    _activate_section(page, "Product Category")

    # Wait for miller column items to actually render (SPA may be slow)
    try:
        page.wait_for_selector(".ZorkMillerColumns__item", timeout=15000)
    except Exception:
        print("[probe] ⚠️  等待 .ZorkMillerColumns__item 超时，继续尝试...")

    deadline = time.time() + 60

    last_x = -10_000
    remaining = list(category_path)

    while remaining and time.time() < deadline:
        want = remaining[0]
        # 原始规范化（保留数字），以及去掉 "(12345)" 后的规范化，供匹配使用。
        want_n = _norm_text(want)
        want_core = _norm_text(_strip_category_id(want))
        hit = None
        level_start = time.time()
        PER_LEVEL_TIMEOUT = 7  # seconds before trying to skip this level

        while time.time() < deadline:
            items = _get_category_items(page)
            if not items:
                page.wait_for_timeout(600)
                continue

            # Log items on first check per level (for debugging)
            if time.time() - level_start < 1.5:
                visible_txts = [str(i.get("txt", ""))[:30] for i in items[:8]]
                print(f"[probe] 寻找 '{want}'，当前可见 {len(items)} 项：{visible_txts}")

            exact: list[tuple[int, Any]] = []
            fuzzy: list[tuple[int, Any]] = []
            for i in items:
                txt_raw = str(i.get("txt", ""))
                txt_n = _norm_text(txt_raw)
                # Portal sometimes shows compound text like "Wearable Tech -> Activity Trackers"
                # Also try matching against just the leaf part after "->"
                leaf_raw = txt_raw.rsplit("->", 1)[-1] if "->" in txt_raw else txt_raw
                txt_leaf_n = _norm_text(leaf_raw)
                # 去掉 ID（"(15639)"）后的规范化文本，用于忽略纯数字 ID。
                txt_core = _norm_text(_strip_category_id(txt_raw))
                txt_leaf_core = _norm_text(_strip_category_id(leaf_raw))
                if not txt_n:
                    continue
                x = int(i.get("x", 0))
                # 先做严格匹配：包含 ID 与不包含 ID 的两套等值判定。
                if (
                    txt_n == want_n
                    or txt_leaf_n == want_n
                    or (want_core and txt_core == want_core)
                    or (want_core and txt_leaf_core == want_core)
                ):
                    exact.append((x, i))
                # 再做模糊匹配：基于去 ID 后的核心文本做包含判断。
                elif want_core and (
                    want_core in txt_core
                    or txt_core in want_core
                    or want_core in txt_leaf_core
                    or txt_leaf_core in want_core
                ):
                    fuzzy.append((x, i))

            pool = exact if exact else fuzzy
            if pool:
                # Prefer candidate to the right of previous click.
                right = [p for p in pool if p[0] > last_x]
                chosen = sorted(right, key=lambda z: z[0])[0] if right else sorted(pool, key=lambda z: z[0], reverse=True)[0]
                hit = chosen[1]

            if hit is not None:
                break

            # After per-level timeout: if this level is not found but a later level IS
            # visible, skip the current level (portal hierarchy may differ from CSV)
            if time.time() - level_start > PER_LEVEL_TIMEOUT and len(remaining) > 1:
                skipped = False
                for next_want in remaining[1:]:
                    next_n = _norm_text(next_want)
                    next_core = _norm_text(_strip_category_id(next_want))
                    for i in items:
                        txt_raw = str(i.get("txt", ""))
                        txt_n = _norm_text(txt_raw)
                        leaf_raw = txt_raw.rsplit("->", 1)[-1] if "->" in txt_raw else txt_raw
                        txt_leaf_n = _norm_text(leaf_raw)
                        txt_core = _norm_text(_strip_category_id(txt_raw))
                        txt_leaf_core = _norm_text(_strip_category_id(leaf_raw))
                        if (
                            next_n == txt_n
                            or next_n == txt_leaf_n
                            or (next_core and (next_core == txt_core or next_core == txt_leaf_core))
                            or (next_core and (next_core in txt_leaf_core or txt_leaf_core in next_core))
                        ):
                            print(f"[probe] 类目层级 '{want}' 在 portal 中不存在，跳过 → 尝试 '{next_want}'")
                            remaining = [nw for nw in remaining if nw != want]
                            skipped = True
                            break
                    if skipped:
                        break
                if skipped:
                    break  # restart outer while with new remaining[0]

            page.wait_for_timeout(500)

        if hit is None:
            # Level still not found even after skip attempts
            if remaining:
                remaining.pop(0)
            continue

        # 检查命中的这一行是否同时包含当前层和下一层（例如 "Storage Devices -> Portable HDD"），
        # 如是，则视为一次点击完成两级导航。
        combined_levels = 1
        if len(remaining) >= 2:
            try:
                txt_raw = str(hit.get("txt", ""))
            except Exception:
                txt_raw = ""
            hit_core = _norm_text(_strip_category_id(txt_raw))
            next_want = remaining[1]
            next_core = _norm_text(_strip_category_id(next_want))
            if next_core and hit_core and next_core in hit_core:
                combined_levels = 2

        if not _click_category_item(page, int(hit["x"]), str(hit["txt"])):
            return False
        last_x = int(hit["x"])
        page.wait_for_timeout(900)
        remaining.pop(0)
        if combined_levels == 2 and remaining:
            remaining.pop(0)

    if _section_next_enabled(page, "Product Category"):
        _click_section_next(page, "Product Category")
        page.wait_for_timeout(800)
        return True
    return False


def _complete_category_section_heuristic(page, draft: ListingDraft) -> bool:
    _activate_section(page, "Product Category")

    title_low = (draft.title or "").lower()
    prefer_home = any(k in title_low for k in ["kitchen", "home", "storage", "rack", "organizer", "bath", "clean"])

    clicked: set[tuple[int, str]] = set()
    deadline = time.time() + 35
    while time.time() < deadline:
        if _section_next_enabled(page, "Product Category"):
            _click_section_next(page, "Product Category")
            page.wait_for_timeout(800)
            return True

        items = _get_category_items(page)
        if not items:
            page.wait_for_timeout(600)
            continue

        xs = sorted({int(i["x"]) for i in items})
        pick: dict[str, Any] | None = None

        if prefer_home and len(clicked) == 0:
            first_col = [i for i in items if int(i["x"]) == xs[0]]
            first_col.sort(key=lambda z: int(z["y"]))
            for i in first_col:
                if str(i["low"]).startswith("home"):
                    pick = i
                    break

        if pick is None:
            for x in reversed(xs):
                cands = [i for i in items if int(i["x"]) == x]
                cands.sort(key=lambda z: int(z["y"]))
                for i in cands:
                    low = str(i["low"])
                    if any(w in low for w in _CATEGORY_BLOCK_WORDS):
                        continue
                    key = (int(i["x"]), str(i["txt"]))
                    if key in clicked:
                        continue
                    pick = i
                    break
                if pick is not None:
                    break

        if pick is None:
            return False

        ok = _click_category_item(page, int(pick["x"]), str(pick["txt"]))
        clicked.add((int(pick["x"]), str(pick["txt"])))
        page.wait_for_timeout(900 if ok else 400)

    return False


def _strip_category_id(text: str) -> str:
    t = re.sub(r"\(\d+\)", "", str(text or ""))
    t = t.replace("›", " ").replace(">", " ")
    return " ".join(t.split())


def _tokenize_en(text: str) -> set[str]:
    t = _norm_text(_strip_category_id(text))
    stop = {
        "and",
        "for",
        "with",
        "the",
        "of",
        "to",
        "in",
        "general",
        "durable",
        "daily",
        "use",
        "item",
        "items",
        "product",
        "products",
        "standard",
        "mixed",
        "confirmed",
        "unbranded",
    }
    out = {x for x in t.split() if len(x) >= 2 and x not in stop}
    return out


def _resolve_catalog_csv_path(cfg: dict[str, Any], selectors_cfg_path: str | Path) -> Path:
    raw = str(cfg.get("portal", {}).get("category_catalog_csv", "input/takealot_categories.csv")).strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    return (Path(selectors_cfg_path).resolve().parent.parent / p).resolve()


def _load_takealot_catalog(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        header_found = False
        for row in r:
            if not header_found:
                if len(row) >= 4 and str(row[0]).strip().lower() == "division" and str(row[3]).strip().lower().startswith("lowest"):
                    header_found = True
                continue
            if len(row) < 4:
                continue
            division = str(row[0]).strip()
            department = str(row[1]).strip()
            main = _strip_category_id(str(row[2]).strip())
            # 允许扩展中文列：Division_ZH / Department_ZH / Main_ZH / Lowest_ZH
            div_zh = str(row[6]).strip() if len(row) > 6 else ""
            dept_zh = str(row[7]).strip() if len(row) > 7 else ""
            main_zh = str(row[8]).strip() if len(row) > 8 else ""
            low_zh = str(row[9]).strip() if len(row) > 9 else ""
            # 必须先分割 "->"，再去掉 ID（_strip_category_id 会把 ">" 替换成空格，
            # 导致分割符消失）。
            lowest_raw_orig = str(row[3]).strip()
            lowest_parts = [_strip_category_id(p).strip() for p in lowest_raw_orig.split("->") if p.strip()]
            lowest_parts = [p for p in lowest_parts if p]
            lowest = lowest_parts[-1] if lowest_parts else _strip_category_id(lowest_raw_orig)
            if not (division or department or main or lowest):
                continue
            path: list[str] = []
            for x in [division, department, main] + lowest_parts:
                if not x:
                    continue
                if path and _norm_text(path[-1]) == _norm_text(x):
                    continue
                path.append(x)
            if not path:
                continue
            tokens = _tokenize_en(" ".join(path))
            rows.append(
                {
                    "path": path,
                    "division": division,
                    "department": department,
                    "main": main,
                    "lowest": lowest,
                    "division_zh": div_zh,
                    "department_zh": dept_zh,
                    "main_zh": main_zh,
                    "lowest_zh": low_zh,
                    "tokens": sorted(tokens),
                }
            )
    return rows


def _patch_required_fields_for_category(category_path: list[str], fields: list[dict[str, Any]]) -> None:
    """
    对少数平台标记不一致的类目做手动补丁。

    例：Nail Tools 类目（Hand Foot & Nail 工具），前台 UI 把
        - Hand Foot And Nail Tool Type
        - Main Material/Fabric
    也作为必填，但 DOM 中未稳定暴露 required 标记，probe 可能识别为选填。
    这里根据类目路径 + 字段 label 强制把它们标记为必填。
    """
    if not category_path or not fields:
        return
    lowest = str(category_path[-1]).lower()
    # Nail Tools (30270)
    if "nail tools" in lowest:
        for f in fields:
            lbl = str(f.get("label", "")).strip()
            if lbl in {"Hand Foot And Nail Tool Type", "Body Care Tool Type", "Main Material/Fabric"}:
                f["required"] = True


def _auto_match_path_from_catalog(
    catalog: list[dict[str, Any]],
    draft: ListingDraft,
    source_title: str = "",
    source_category_path: list[str] | None = None,
    min_score: int = 2,
) -> tuple[list[str], dict[str, Any]]:
    def _phrase_hit(phrase_norm: str, query_norm: str, query_tokens: set[str]) -> bool:
        p = str(phrase_norm or "").strip()
        if not p:
            return False
        toks = p.split()
        if len(toks) == 1:
            return toks[0] in query_tokens
        return f" {p} " in f" {query_norm} "

    def _looks_like_company_name(text: str) -> bool:
        t = str(text or "")
        hints = ["有限公司", "供应链", "商贸", "贸易", "科技", "公司", "店铺"]
        return any(x in t for x in hints)

    zh_alias = {
        # 音频
        "蓝牙耳机": "consumer electronics audio bluetooth earphones earbuds headset",
        "耳机": "earphones headset",
        "音箱": "speaker audio",
        "蓝牙音箱": "bluetooth speaker audio",
        # 手机配件
        "手机壳": "phone case",
        "数据线": "usb cable",
        "充电器": "charger",
        # 家居
        "收纳": "storage organizer",
        "厨房": "kitchen",
        "清洁": "cleaning",
        # 汽车
        "车载": "motor vehicle automotive car",
        "汽车": "automotive car motor vehicle",
        "行车记录仪": "dashcam camera motor vehicle car",
        "导航": "navigation gps motor vehicle",
        "倒车": "parking camera motor vehicle",
        "车机": "car audio head unit motor vehicle",
        "汽车音响": "car audio motor vehicle speaker",
        "车充": "car charger motor vehicle",
        # 智能家居
        "智能灯": "smart lighting led",
        "灯带": "led strip light",
        # 投影
        "投影仪": "projector",
        # 路由
        "路由器": "router wifi networking",
        # 美发工具 — 注意：不加 "accessories"，避免匹配到 Pets > Accessories 等同名类目
        "卷发": "curler curl hair styling beauty care",
        "卷发器": "curler curl hair styling beauty care",
        "卷发棒": "curler curl hair styling beauty care",
        "烫发": "curler curl hair styling beauty care",
        "直发": "straightener flat iron hair styling beauty care",
        "直发器": "straightener flat iron hair styling beauty care",
        "直发板": "straightener flat iron hair styling beauty care",
        "吹风机": "hair dryer blow dryer styling beauty care",
        "电吹风": "hair dryer blow dryer styling beauty care",
        "美发": "hair styling beauty care",
        "美发梳": "hair brush comb styling beauty care",
        "梳": "comb brush hair styling beauty",
        "电动梳": "electric comb brush hair styling beauty",
        "发型": "hair styling beauty",
        "电动": "electric",
        # 美容个护
        "护肤": "skin care beauty",
        "口红": "makeup lipstick beauty",
        "美甲": "nail care beauty",
        "电动牙刷": "toothbrush oral care",
        "剃须": "shaving razor personal care",
        "按摩": "massage health care",
        # 母婴 / 吸奶器
        "吸奶器": "family baby breast pump breastfeeding electric manual",
        "电动吸奶器": "family baby breast pump electric breastfeeding",
        "手动吸奶器": "family baby breast pump manual breastfeeding",
        "医用吸奶器": "family baby breast pump medical electric manual",
        "奶泵": "family baby breast pump",
    }
    source_category_path = source_category_path or []
    query_parts: list[str] = []
    if source_category_path:
        query_parts.append(" ".join(str(x) for x in source_category_path))
    if source_title and not _looks_like_company_name(source_title):
        query_parts.append(str(source_title))
    # If source signals are weak, fallback to draft title/subtitle only (avoid noisy features text).
    if not query_parts:
        query_parts.append(str(draft.title or ""))
        query_parts.append(str(draft.subtitle or ""))

    query = " ".join(query_parts).lower()
    for zh, en in zh_alias.items():
        if zh in query:
            query += " " + en

    # 自动提取中英混合文本中的英文词（如"无线CarPlay"→ 提取"carplay"加入搜索词）
    en_embedded = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{1,}", " ".join(query_parts))
    if en_embedded:
        query += " " + " ".join(en_embedded).lower()

    q_tokens = _tokenize_en(query)
    q_norm = _norm_text(query)

    best: dict[str, Any] | None = None
    best_score = -1
    ranked: list[dict[str, Any]] = []
    for row in catalog:
        row_tokens = set(str(x) for x in row.get("tokens", []))
        # Fuzzy overlap: full match + substring (handles curler/curlers, dryer/dryers, etc.)
        fuzzy_matched: set[str] = set()
        for qt in q_tokens:
            for rt in row_tokens:
                if qt == rt:
                    fuzzy_matched.add(qt)
                    break
                elif len(qt) >= 4 and len(rt) >= 4 and (qt in rt or rt in qt):
                    fuzzy_matched.add(qt)
                    break
        overlap = len(fuzzy_matched)
        main_norm = _norm_text(str(row.get("main", "")))
        low_norm = _norm_text(str(row.get("lowest", "")))
        # 把中文列也并入匹配文本，方便用中文类目提高命中率
        div_zh = str(row.get("division_zh", "")).strip()
        dept_zh = str(row.get("department_zh", "")).strip()
        main_zh = str(row.get("main_zh", "")).strip()
        low_zh = str(row.get("lowest_zh", "")).strip()
        row_text = _norm_text(
            " ".join(
                [
                    str(row.get("division", "")),
                    str(row.get("department", "")),
                    str(row.get("main", "")),
                    str(row.get("lowest", "")),
                    div_zh,
                    dept_zh,
                    main_zh,
                    low_zh,
                ]
            )
        )
        score = overlap
        if _phrase_hit(main_norm, q_norm, q_tokens):
            score += 4
        if _phrase_hit(low_norm, q_norm, q_tokens):
            score += 8

        # Intent-based boosts to avoid drifting into unrelated audio/camera/etc paths.
        if any(k in query for k in ["蓝牙耳机", "earphones", "earbuds", "headset", "headphones"]):
            if "cellphone headsets" in row_text:
                score += 10
            elif "standard headphones" in row_text:
                score += 8
            elif "headsets microphones" in row_text:
                score += 6
            elif "sport headphones" in row_text or "studio headphones" in row_text:
                score += 4
            elif "gaming headphones" in row_text:
                score += 2
            if any(
                k in row_text
                for k in [
                    "camera",
                    "audio recording",
                    "adapters",
                    "cables",
                    "amplifiers",
                    "dj",
                    "pet medicine",
                    "ear care",
                ]
            ):
                score -= 3

        # 汽车类：CarPlay / 行车记录仪 / 车载等
        if any(k in query for k in ["carplay", "car play", "车载", "汽车", "行车记录仪", "导航", "车机", "dashcam"]):
            if "motor vehicle" in row_text or "automotive" in row_text:
                score += 10
            if "car electronic" in row_text:
                score += 6
            if "motor vehicle electronics" in row_text:
                score += 4
            # 防止漂移到消费电子
            if any(k in row_text for k in ["cellphone", "laptop", "speaker", "headset"]):
                score -= 4
        item = {
            "score": score,
            "path": row.get("path", []),
            "division": row.get("division", ""),
            "department": row.get("department", ""),
            "main": row.get("main", ""),
            "lowest": row.get("lowest", ""),
        }
        ranked.append(item)
        if score > best_score:
            best_score = score
            best = item

    ranked.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
    debug = {
        "best_score": best_score,
        "top_candidates": ranked[:8],
    }
    if best and best_score >= max(1, int(min_score)):
        return [str(x) for x in best.get("path", []) if str(x).strip()], debug
    return [], debug


def find_probe_category_path(
    source_category_path: list[str],
    source_title: str,
    selectors_cfg_path: str | Path,
) -> list[str]:
    """
    给定 1688 原始类目路径（可含中文）和商品标题，
    在 takealot_categories.csv 中智能匹配，返回 Takealot 完整门户导航路径。

    优先级：
    1. selectors.yaml portal.category_keyword_paths 手动规则（精确覆盖）
    2. CSV 全文评分匹配（_auto_match_path_from_catalog）
    3. CSV 叶子精确匹配（exact match on lowest/main field）
    """
    try:
        cfg = load_selectors(selectors_cfg_path)
    except Exception:
        return []

    # --- 0. category_overrides.yaml （用户记忆过的映射，最高优先级） ---
    try:
        overrides = _load_category_overrides(selectors_cfg_path)
    except Exception:
        overrides = []
    if overrides:
        path_override = _match_override_path(
            overrides,
            source_category_path=source_category_path,
            source_title=source_title,
        )
        if path_override:
            return path_override

    # --- 1. 手动关键词规则（最高优先级）---
    query_kw = " ".join(str(x) for x in source_category_path).lower() + " " + source_title.lower()
    rules = cfg.get("portal", {}).get("category_keyword_paths", [])
    best_kw_path: list[str] = []
    best_kw_score = 0
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            kws = [str(k).strip().lower() for k in r.get("keywords", []) if str(k).strip()]
            path_raw = r.get("path", [])
            path = [str(x).strip() for x in path_raw if str(x).strip()] if isinstance(path_raw, list) else []
            if not kws or not path:
                continue
            score = sum(1 for kw in kws if kw in query_kw)
            if score > best_kw_score:
                best_kw_score = score
                best_kw_path = path
    if best_kw_score > 0 and best_kw_path:
        return best_kw_path

    # --- 2. CSV 全文评分匹配 ---
    try:
        csv_path = _resolve_catalog_csv_path(cfg, selectors_cfg_path)
        catalog = _load_takealot_catalog(csv_path)
    except Exception:
        return []
    if not catalog:
        return []

    class _FakeDraft:
        title = source_title
        subtitle = ""

    path_csv, _dbg = _auto_match_path_from_catalog(
        catalog,
        _FakeDraft(),  # type: ignore[arg-type]
        source_title=source_title,
        source_category_path=source_category_path,
        min_score=2,
    )
    if path_csv:
        return path_csv

    # --- 3. 叶子精确匹配（fallback）---
    for part in reversed(source_category_path):
        leaf = _norm_text(_strip_category_id(part))
        if not leaf:
            continue
        for row in catalog:
            for field in ("lowest", "main"):
                if _norm_text(_strip_category_id(str(row.get(field, "")))) == leaf:
                    full = [_strip_category_id(p).strip() for p in row.get("path", []) if p]
                    if full:
                        return full

    # --- 4. LLM 兜底：CSV 全部匹配失败时问豆包 ---
    try:
        from .llm import ask_llm_for_takealot_category
        llm_path = ask_llm_for_takealot_category(source_category_path, source_title)
        if llm_path:
            print(f"[probe] 🤖 LLM 兜底类目：{' > '.join(llm_path)}")
            return llm_path
    except Exception as _llm_e:
        print(f"[probe] LLM 兜底失败：{_llm_e}")

    return []


def _resolve_category_path(
    cfg: dict[str, Any],
    draft: ListingDraft,
    selectors_cfg_path: str | Path,
    source_title: str = "",
    source_category_path: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    env_path = os.getenv("TAKEALOT_CATEGORY_PATH", "").strip()
    if env_path:
        return _parse_category_path(env_path), {"method": "env"}

    raw = cfg.get("portal", {}).get("category_path", [])
    if isinstance(raw, str):
        parsed = _parse_category_path(raw)
        if parsed:
            return parsed, {"method": "config_path"}
    if isinstance(raw, list):
        parsed = [str(x).strip() for x in raw if str(x).strip()]
        if parsed:
            return parsed, {"method": "config_path"}

    # category_overrides.yaml: 用户记忆过的映射（第二优先级）
    try:
        overrides = _load_category_overrides(selectors_cfg_path)
    except Exception:
        overrides = []
    if overrides:
        path_override = _match_override_path(
            overrides,
            source_category_path=source_category_path or [],
            source_title=source_title or draft.title,
        )
        if path_override:
            return path_override, {
                "method": "override",
                "source_category_path": source_category_path or [],
                "source_title": source_title or draft.title,
            }

    # Auto match from full Takealot category catalog CSV.
    csv_path = _resolve_catalog_csv_path(cfg, selectors_cfg_path)
    min_score = int(cfg.get("portal", {}).get("category_match_min_score", 2))
    catalog = _load_takealot_catalog(csv_path)
    if catalog:
        path_csv, debug = _auto_match_path_from_catalog(
            catalog=catalog,
            draft=draft,
            source_title=source_title,
            source_category_path=source_category_path or [],
            min_score=min_score,
        )
        if path_csv:
            return path_csv, {"method": "csv_auto", "csv": str(csv_path), **debug}
        csv_debug = {"method": "csv_auto_nohit", "csv": str(csv_path), **debug}
    else:
        csv_debug = {"method": "csv_missing", "csv": str(csv_path)}

    # Fallback from keyword rules.
    text = " ".join(
        [
            str(source_title or ""),
            " ".join(str(x) for x in (source_category_path or [])),
            str(draft.title or ""),
            str(draft.subtitle or ""),
            str(draft.key_features or ""),
            " ".join(str(x) for x in (draft.whats_in_box or [])),
        ]
    ).lower()
    rules = cfg.get("portal", {}).get("category_keyword_paths", [])
    best_path: list[str] = []
    best_score = 0
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            kws_raw = r.get("keywords", [])
            path_raw = r.get("path", [])
            kws = [str(k).strip().lower() for k in kws_raw if str(k).strip()] if isinstance(kws_raw, list) else []
            if not kws:
                continue
            if isinstance(path_raw, str):
                path = _parse_category_path(path_raw)
            elif isinstance(path_raw, list):
                path = [str(x).strip() for x in path_raw if str(x).strip()]
            else:
                path = []
            if not path:
                continue
            score = 0
            for kw in kws:
                if kw in text:
                    score += 1
            if score > best_score:
                best_score = score
                best_path = path
    if best_score > 0:
        return best_path, {"method": "keyword_rule", "keyword_score": best_score, "csv_debug": csv_debug}
    return [], {"method": "none", "csv_debug": csv_debug}


def _read_selected_category(page) -> str:
    try:
        txt = page.evaluate(
            """
() => {
  const sec = document.querySelector("section[data-sectionname='Product Category']");
  if (!sec) return '';
  const t = (sec.textContent || '').replace(/\\s+/g, ' ').trim();
  return t || '';
}
"""
        )
    except Exception:
        return ""
    return str(txt or "")


def _selected_category_matches_path(selected_text: str, category_path: list[str]) -> bool:
    if not category_path:
        return True
    s = _norm_text(selected_text)
    if not s:
        return False
    pos = 0
    for seg in category_path:
        t = _norm_text(seg)
        if not t:
            continue
        idx = s.find(t, pos)
        if idx < 0:
            return False
        pos = idx + len(t)
    return True


def _preferred_variant_option(draft: ListingDraft) -> str:
    if not draft.variants:
        return "None"
    keys: set[str] = set()
    for item in draft.variants:
        if not isinstance(item, dict):
            continue
        for k in item.keys():
            kk = str(k).strip().lower()
            if not kk or kk in {"sku", "name", "id"}:
                continue
            keys.add(kk)
    if any("colour" in k or "color" in k for k in keys):
        return "Colour"
    if any("size" in k for k in keys):
        return "Size"
    if keys:
        return sorted(keys)[0].title()
    return "None"


def _variant_choice_is_none(choice: str) -> bool:
    v = str(choice or "").strip().lower()
    return (not v) or v in {"none", "-", "n/a", "na"}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _first_number(text: str) -> float | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _is_placeholder_text(v: str) -> bool:
    t = str(v or "").strip().lower()
    if not t:
        return True
    return t in {
        "-",
        "n/a",
        "na",
        "none",
        "null",
        "to be confirmed",
        "unknown",
        "待确认",
        "无",
    }


def _is_placeholder_combo_value(v: str) -> bool:
    t = str(v or "").strip().lower()
    if not t:
        return True
    bad = {
        "-",
        "n/a",
        "na",
        "none",
        "optional",
        "select",
        "choose",
        "choose option",
        "choose more",
        "not selected",
    }
    if t in bad:
        return True
    return ("choose" in t) or ("select" in t and len(t) <= 20)


def _derive_weight_g(draft: ListingDraft) -> int:
    attrs = draft.attributes or {}
    raw = str(attrs.get("weight", "")).strip().lower()
    if not raw:
        raw = " ".join(
            [
                str(draft.subtitle or ""),
                str(draft.key_features or ""),
                str(attrs.get("size", "")),
            ]
        ).lower()
    n = _first_number(raw)
    if n is None:
        return 300
    if "kg" in raw:
        return max(1, int(round(n * 1000)))
    if "g" in raw:
        return max(1, int(round(n)))
    if n <= 10:
        return max(1, int(round(n * 1000)))
    return max(1, int(round(n)))


def _derive_dimensions_cm(draft: ListingDraft) -> tuple[int, int, int]:
    attrs = draft.attributes or {}
    raw = " ".join(
        [
            str(attrs.get("size", "")),
            str(draft.subtitle or ""),
            str(draft.key_features or ""),
        ]
    ).strip().lower()
    nums = re.findall(r"(\d+(?:\.\d+)?)", raw)
    if len(nums) >= 3:
        try:
            vals = [float(x) for x in nums[:3]]
            if "mm" in raw:
                vals = [v / 10.0 for v in vals]
            ints = [max(1, int(round(v))) for v in vals]
            return ints[0], ints[1], ints[2]
        except Exception:
            pass
    return 18, 12, 8


def _extract_model_from_text(text: str) -> str:
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
        token = re.sub(r"[^A-Z0-9\-_./]+", "", m.group(1).upper())
        token = token.strip("-_. /")
        if len(token) >= 3:
            return token[:24]
    return ""


def _auto_model_number(draft: ListingDraft) -> str:
    seed = "|".join(
        [
            str(draft.title or ""),
            str(draft.subtitle or ""),
            str(draft.source_url or ""),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest().upper()
    return f"MDL-{digest[:8]}"


def _derive_model_number(draft: ListingDraft) -> str:
    attrs = draft.attributes or {}
    for k in ["model", "model number", "model_number", "model no", "型号"]:
        v = str(attrs.get(k, "")).strip()
        if _is_placeholder_text(v):
            continue
        token = _extract_model_from_text(v) or re.sub(r"[^A-Z0-9\-_./]+", "", v.upper()).strip("-_. /")
        if len(token) >= 3:
            return token[:24]

    text_blob = " ".join(
        [
            str(draft.title or ""),
            str(draft.subtitle or ""),
            str(draft.key_features or ""),
            " ".join(str(v) for v in attrs.values()),
        ]
    )
    token = _extract_model_from_text(text_blob)
    if token:
        return token
    return _auto_model_number(draft)


def _variant_candidates(draft: ListingDraft, target: str) -> list[str]:
    out: list[str] = []

    def add(v: str) -> None:
        s = str(v or "").strip()
        if not s:
            return
        if s.lower() in {x.lower() for x in out}:
            return
        out.append(s)

    add(target)
    t = _normalize_text(target)
    if t in {"colour", "color"}:
        add("Colour")
        add("Color")
        add("Colour / Size")
        add("Color / Size")
        add("Colour and Size")
        add("Color and Size")
        add("Size and Colour")
        add("Size and Color")
    if t == "size":
        add("Size")
        add("Size / Colour")
        add("Size / Color")
    if not draft.variants:
        add("None")
    return out


def _select_variant_from_dropdown(page, candidates: list[str]) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """
(args) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const sec = document.querySelector("section[data-sectionname='Product Variants']");
  if (!sec) return { clicked: '', options: [], inputValue: '', sectionText: '' };
  const combo = sec.querySelector("input[role='combobox']");
  if (!combo) return { clicked: '', options: [], inputValue: '', sectionText: (sec.textContent || '').replace(/\\s+/g, ' ').trim() };

  const roots = [];
  const controlsId = combo.getAttribute('aria-controls') || '';
  if (controlsId) {
    const root = document.getElementById(controlsId);
    if (root) roots.push(root);
  }
  for (const el of Array.from(document.querySelectorAll("[role='listbox'], [id*='listbox'], .select__menu, .Select-menu-outer, .menu"))) {
    if (isVisible(el)) roots.push(el);
  }
  if (roots.length === 0) roots.push(document.body);

  const optionNodes = [];
  for (const root of roots) {
    const nodes = Array.from(root.querySelectorAll("[role='option'], li, .select__option, [class*='option']"));
    for (const n of nodes) {
      if (!isVisible(n)) continue;
      const text = (n.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!text || text.length > 80) continue;
      optionNodes.push({ el: n, text, ntext: norm(text) });
    }
  }
  const uniq = [];
  const seen = new Set();
  for (const it of optionNodes) {
    if (seen.has(it.ntext)) continue;
    seen.add(it.ntext);
    uniq.push(it);
  }

  const wants = (args.candidates || []).map(norm).filter(Boolean);
  let picked = null;
  for (const w of wants) {
    picked = uniq.find(o => o.ntext === w);
    if (picked) break;
    picked = uniq.find(o => o.ntext.startsWith(w));
    if (picked) break;
    picked = uniq.find(o => o.ntext.includes(w));
    if (picked) break;
  }

  if (!picked && wants.length > 0 && !wants.includes('none')) {
    picked = uniq.find(o => o.ntext !== 'none') || null;
  }
  if (!picked && wants.includes('none')) {
    picked = uniq.find(o => o.ntext === 'none') || null;
  }

  let clicked = '';
  if (picked && picked.el) {
    try {
      picked.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      picked.el.click();
      clicked = picked.text || '';
    } catch (e) {
      // no-op
    }
  }

  return {
    clicked,
    options: uniq.slice(0, 20).map(x => x.text),
    inputValue: (combo.value || '').trim(),
    sectionText: (sec.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 400),
  };
}
""",
            {"candidates": candidates},
        )
    except Exception:
        return {"clicked": "", "options": [], "inputValue": "", "sectionText": ""}
    if isinstance(result, dict):
        return result
    return {"clicked": "", "options": [], "inputValue": "", "sectionText": ""}


def _pick_visible_dropdown_option(page, candidates: list[str], *, allow_fallback: bool = False) -> str:
    try:
        picked = page.evaluate(
            """
(args) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const wants = (args.candidates || []).map(norm).filter(Boolean);
  const nodes = Array.from(document.querySelectorAll("[role='option'], li, .select__option, [class*='option']"))
    .filter(isVisible)
    .map((el) => ({
      el,
      text: (el.textContent || '').replace(/\\s+/g, ' ').trim(),
      ntext: norm(el.textContent || '')
    }))
    .filter(x => x.text && x.text.length <= 80);
  const uniq = [];
  const seen = new Set();
  for (const x of nodes) {
    if (seen.has(x.ntext)) continue;
    seen.add(x.ntext);
    uniq.push(x);
  }
  let target = null;
  for (const w of wants) {
    target = uniq.find(x => x.ntext === w) || uniq.find(x => x.ntext.startsWith(w)) || uniq.find(x => x.ntext.includes(w));
    if (target) break;
  }
  if (!target && wants.length > 0 && args.allowFallback) {
    target = uniq.find(x => x.ntext !== 'none') || null;
  }
  if (!target && args.allowFallback) {
    target = uniq.find(x => !['none', 'choose option', 'click to choose', 'select', 'optional'].includes(x.ntext)) || uniq[0] || null;
  }
  if (!target || !target.el) return '';
  try {
    target.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    target.el.click();
  } catch (e) {
    return '';
  }
  return target.text || '';
}
""",
            {"candidates": candidates, "allowFallback": bool(allow_fallback)},
        )
    except Exception:
        return ""
    return str(picked or "").strip()


def _extract_variant_rows(draft: ListingDraft) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for i, item in enumerate(draft.variants, start=1):
        if not isinstance(item, dict):
            continue
        colour = str(item.get("colour") or item.get("color") or "").strip()
        sku = str(item.get("sku") or "").strip() or f"AUTO-SKU-{i:03d}"
        barcode = str(item.get("barcode") or item.get("ean") or item.get("gtin") or item.get("upc") or "").strip()
        rows.append({"colour": colour, "sku": sku, "barcode": barcode})
    return rows


def _estimate_variant_row_count(page) -> int:
    try:
        cnt = page.locator("section[data-sectionname='Product Variants'] input[role='combobox']").count()
    except Exception:
        return 0
    # first combobox is variant dimension type (None/Colour/Size), remaining are row comboboxes
    if cnt <= 1:
        return 0
    return max(0, (cnt - 1) // 2)


def _ensure_variant_row_count(page, target_rows: int) -> int:
    rows_now = _estimate_variant_row_count(page)
    if rows_now >= target_rows:
        return rows_now
    for _ in range(max(0, target_rows - rows_now + 2)):
        try:
            btn = page.locator("section[data-sectionname='Product Variants'] :text('Add new variant')").first
            if btn.count() == 0:
                break
            btn.click(timeout=1500)
            page.wait_for_timeout(450)
        except Exception:
            break
        rows_now = _estimate_variant_row_count(page)
        if rows_now >= target_rows:
            break
    return rows_now


def _trim_variant_rows(page, target_rows: int) -> int:
    rows_now = _estimate_variant_row_count(page)
    if rows_now <= target_rows:
        return rows_now
    for _ in range(max(0, rows_now - target_rows + 2)):
        try:
            del_btn = page.locator(
                "section[data-sectionname='Product Variants'] button[aria-label*='delete' i], "
                "section[data-sectionname='Product Variants'] button:has(i[class*='trash']), "
                "section[data-sectionname='Product Variants'] button:has(i[class*='bin'])"
            ).last
            if del_btn.count() == 0:
                break
            del_btn.click(timeout=1200)
            page.wait_for_timeout(450)
        except Exception:
            break
        rows_now = _estimate_variant_row_count(page)
        if rows_now <= target_rows:
            break
    return rows_now


def _variant_images_for_rows(image_paths: list[Path], row_count: int) -> list[Path]:
    if row_count <= 0:
        return []
    white_imgs = [p for p in image_paths if "/images_white/" in str(p) and p.exists()]
    sku_imgs = [p for p in image_paths if "/images_sku/" in str(p) and p.exists()]
    chosen = white_imgs if white_imgs else sku_imgs
    if not chosen:
        return []
    out: list[Path] = []
    for i in range(row_count):
        out.append(chosen[i % len(chosen)])
    return out


def _has_variants_missing_values(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
() => {
  const pats = [
    'one or more variants are missing values',
    'please complete the red highlighted fields',
    'variants are missing values'
  ];
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll('[role="alert"], .alert, .text-danger, *'));
  for (const n of nodes) {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!t) continue;
    if (!pats.some(p => t.includes(p))) continue;
    if (isVisible(n)) return true;
  }
  return false;
}
"""
            )
        )
    except Exception:
        return False


def _fill_variant_rows(page, draft: ListingDraft, image_paths: list[Path]) -> dict[str, Any]:
    _activate_section(page, "Product Variants")
    rows = _extract_variant_rows(draft)
    debug: dict[str, Any] = {
        "target_rows": len(rows),
        "rows_detected": 0,
        "rows_after_trim": 0,
        "colour_filled": 0,
        "sku_filled": 0,
        "barcode_filled": 0,
        "images_uploaded": 0,
        "row_colours": [r.get("colour", "") for r in rows],
    }
    if not rows:
        return debug

    rows_detected = _ensure_variant_row_count(page, len(rows))
    debug["rows_detected"] = rows_detected
    rows_after_trim = _trim_variant_rows(page, len(rows))
    debug["rows_after_trim"] = rows_after_trim
    # If portal keeps extra rows we cannot remove, fill all visible rows to avoid "missing values".
    target_rows = max(len(rows), max(rows_after_trim, 0))

    # Fill row primary-colour comboboxes only.
    # Combobox order is usually: [dimension selector] + [row1 primary,row1 secondary,row2 primary,row2 secondary...]
    try:
        combos = page.locator("section[data-sectionname='Product Variants'] input[role='combobox']")
        total = combos.count()
    except Exception:
        total = 0
    if total > 1:
        for row_idx in range(target_rows):
            i = 1 + row_idx * 2
            if i >= total:
                break
            src_idx = min(row_idx, len(rows) - 1)
            colour = str(rows[src_idx].get("colour", "")).strip()
            if not colour:
                continue
            try:
                cb = combos.nth(i)
                vnow = (cb.input_value(timeout=900) or "").strip()
                if vnow and _normalize_text(vnow) not in {"choose more", "choose option", "optional"}:
                    continue
                cb.click(timeout=1300)
                page.wait_for_timeout(150)
                picked = _pick_visible_dropdown_option(page, [colour], allow_fallback=True)
                if not picked:
                    cb.fill(colour, timeout=1500)
                    page.keyboard.press("ArrowDown")
                    page.keyboard.press("Enter")
                page.wait_for_timeout(220)
                vnew = (cb.input_value(timeout=900) or "").strip()
                if vnew:
                    debug["colour_filled"] = int(debug["colour_filled"]) + 1
            except Exception:
                continue

    # Fill SKU and Barcode text boxes in row order by visible field hints.
    try:
        sku_inputs = page.locator(
            "section[data-sectionname='Product Variants'] input[placeholder*='SKU' i], "
            "section[data-sectionname='Product Variants'] input[aria-label*='SKU' i], "
            "section[data-sectionname='Product Variants'] input[name*='sku' i], "
            "section[data-sectionname='Product Variants'] input[id*='sku' i]"
        )
        for i in range(min(sku_inputs.count(), target_rows)):
            src_idx = min(i, len(rows) - 1)
            sku = str(rows[src_idx].get("sku", "")).strip()
            if not sku:
                continue
            inp = sku_inputs.nth(i)
            now = (inp.input_value(timeout=900) or "").strip()
            if not now:
                inp.click(timeout=1200)
                inp.fill(sku, timeout=1600)
                inp.blur(timeout=800)
            if (inp.input_value(timeout=900) or "").strip():
                debug["sku_filled"] = int(debug["sku_filled"]) + 1
    except Exception:
        pass

    try:
        barcode_inputs = page.locator(
            "section[data-sectionname='Product Variants'] input[placeholder*='barcode' i], "
            "section[data-sectionname='Product Variants'] input[aria-label*='barcode' i], "
            "section[data-sectionname='Product Variants'] input[name*='barcode' i], "
            "section[data-sectionname='Product Variants'] input[id*='barcode' i], "
            "section[data-sectionname='Product Variants'] input[name*='ean' i], "
            "section[data-sectionname='Product Variants'] input[id*='ean' i]"
        )
        for i in range(min(barcode_inputs.count(), target_rows)):
            src_idx = min(i, len(rows) - 1)
            code = str(rows[src_idx].get("barcode", "")).strip()
            if not code:
                continue
            inp = barcode_inputs.nth(i)
            now = (inp.input_value(timeout=900) or "").strip()
            if not now:
                inp.click(timeout=1200)
                inp.fill(code, timeout=1600)
                inp.blur(timeout=800)
            if (inp.input_value(timeout=900) or "").strip():
                debug["barcode_filled"] = int(debug["barcode_filled"]) + 1
    except Exception:
        pass

    # Upload one primary image per row when possible.
    variant_imgs = _variant_images_for_rows(image_paths, target_rows)
    debug["image_files"] = [str(p) for p in variant_imgs]
    if variant_imgs:
        # Path A: direct file inputs in variants section.
        try:
            file_inputs = page.locator("section[data-sectionname='Product Variants'] input[type='file']")
            fc = file_inputs.count()
        except Exception:
            fc = 0
        if fc > 0:
            for i in range(min(fc, len(variant_imgs))):
                try:
                    file_inputs.nth(i).set_input_files(str(variant_imgs[i]), timeout=20000)
                    debug["images_uploaded"] = int(debug["images_uploaded"]) + 1
                    page.wait_for_timeout(1200)
                except Exception:
                    continue
        else:
            # Path B: click "Primary Image" tiles and respond via file chooser.
            try:
                tiles = page.locator("section[data-sectionname='Product Variants'] :text('Click to add Primary Image')")
                tc = tiles.count()
            except Exception:
                tc = 0
            for i in range(min(tc, len(variant_imgs))):
                try:
                    with page.expect_file_chooser(timeout=2500) as fc_event:
                        tiles.nth(i).click(timeout=1500)
                    chooser = fc_event.value
                    chooser.set_files(str(variant_imgs[i]))
                    debug["images_uploaded"] = int(debug["images_uploaded"]) + 1
                    page.wait_for_timeout(1200)
                except Exception:
                    continue

    return debug


def _fill_variants(page, draft: ListingDraft) -> tuple[str, dict[str, Any]]:
    _activate_section(page, "Product Variants")
    target = _preferred_variant_option(draft)
    candidates = _variant_candidates(draft, target)
    debug: dict[str, Any] = {
        "target": target,
        "candidates": candidates,
        "options_seen": [],
        "clicked": "",
        "input_value": "",
        "section_text": "",
    }
    choice = ""

    try:
        inp = page.locator("section[data-sectionname='Product Variants'] input[role='combobox']").first
        if inp.count() == 0:
            return "", debug

        inp.click(timeout=1500)
        page.wait_for_timeout(350)
        picked = _select_variant_from_dropdown(page, candidates)
        debug["options_seen"] = picked.get("options", []) if isinstance(picked, dict) else []
        debug["clicked"] = str(picked.get("clicked", "")).strip() if isinstance(picked, dict) else ""
        debug["input_value"] = str(picked.get("inputValue", "")).strip() if isinstance(picked, dict) else ""
        debug["section_text"] = str(picked.get("sectionText", "")).strip() if isinstance(picked, dict) else ""

        page.wait_for_timeout(450)
        val_now = (inp.input_value(timeout=1200) or "").strip()
        if val_now:
            choice = val_now
        elif debug["clicked"]:
            choice = str(debug["clicked"])

        # keyboard fallback for some combo implementations
        if not choice and candidates:
            inp.click(timeout=1200)
            inp.fill(candidates[0], timeout=1800)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            val_now = (inp.input_value(timeout=1200) or "").strip()
            if val_now:
                choice = val_now

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.locator("section[data-sectionname='Product Variants']").first.click(timeout=800)
        except Exception:
            pass
    except Exception:
        return "", debug

    if _variant_choice_is_none(choice):
        return "None" if _normalize_text(target) == "none" else "", debug
    return str(choice).strip(), debug


def _advance_variants_section(page) -> bool:
    _activate_section(page, "Product Variants")
    # Retry several times because Next button may enable a bit later after value commit.
    for _ in range(5):
        if _section_next_enabled(page, "Product Variants"):
            if _click_section_next(page, "Product Variants"):
                page.wait_for_timeout(700)
                return True
        else:
            # Try clicking even when quick enabled-check misses transient state.
            if _click_section_next(page, "Product Variants"):
                page.wait_for_timeout(700)
                return True
        page.wait_for_timeout(500)
    return False


def _collect_section_field_specs(page, section_name: str) -> list[dict[str, Any]]:
    _activate_section(page, section_name)
    try:
        out = page.evaluate(
            """
(sectionName) => {
  const sec = document.querySelector(`section[data-sectionname="${sectionName}"]`);
  if (!sec) return [];
  const result = [];
  const nodes = Array.from(sec.querySelectorAll('input, textarea'));
  let idx = 0;
  for (const el of nodes) {
    if (!el || el.disabled) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['file', 'radio', 'checkbox', 'hidden'].includes(type)) continue;

    idx += 1;
    const key = `ai-${sectionName.replace(/\\s+/g,'-').toLowerCase()}-${idx}`;
    el.setAttribute('data-autolister-key', key);

    const label =
      (el.closest('label')?.textContent || '') ||
      (el.closest('div')?.querySelector('label')?.textContent || '') ||
      '';
    const placeholder = el.getAttribute('placeholder') || '';
    const ariaLabel = el.getAttribute('aria-label') || '';
    const hintText = (el.closest('div')?.textContent || '').slice(0, 260);
    const isRequired =
      el.required ||
      el.getAttribute('aria-required') === 'true' ||
      /required/i.test(hintText);
    const isCombo = (el.getAttribute('role') || '').toLowerCase() === 'combobox';

    result.push({
      key,
      label: (label || ariaLabel || '').replace(/\\s+/g, ' ').trim(),
      placeholder: (placeholder || '').replace(/\\s+/g, ' ').trim(),
      hint: hintText.replace(/\\s+/g, ' ').trim(),
      type: isCombo ? 'combobox' : 'text',
      required: !!isRequired,
      options: [],
    });
  }
  return result.slice(0, 80);
}
""",
            section_name,
        )
        if isinstance(out, list):
            return [x for x in out if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _fill_ai_suggested_values(page, section_name: str, draft: ListingDraft, evidence: dict[str, Any]) -> int:
    specs = _collect_section_field_specs(page, section_name)
    if not specs:
        return 0
    suggestions, err = generate_portal_section_values_debug(draft, section_name, specs)
    if err:
        evidence.setdefault("ai_fill_error", {})
        evidence["ai_fill_error"][section_name] = err
    if not suggestions:
        return 0
    spec_by_key = {str(s.get("key", "")): s for s in specs if isinstance(s, dict)}
    changed = 0
    for key, val in suggestions.items():
        if not val:
            continue
        spec = spec_by_key.get(str(key), {})
        hint_txt = (
            f"{spec.get('label', '')} {spec.get('placeholder', '')} {spec.get('hint', '')}".strip().lower()
            if isinstance(spec, dict)
            else ""
        )
        # Business rule: brand field should be left blank.
        if "brand" in hint_txt:
            continue
        try:
            loc = page.locator(f"section[data-sectionname='{section_name}'] [data-autolister-key='{key}']").first
            if loc.count() == 0:
                continue
            role = str(loc.get_attribute("role") or "").lower()
            if role == "combobox":
                loc.click(timeout=1200)
                loc.fill(str(val), timeout=1800)
                page.keyboard.press("Enter")
                page.wait_for_timeout(180)
            else:
                loc.click(timeout=1200)
                loc.fill(str(val), timeout=2000)
                loc.blur(timeout=1200)
            changed += 1
        except Exception:
            continue
    if changed > 0:
        evidence.setdefault("ai_fill", {})
        prev = int(evidence["ai_fill"].get(section_name, 0))
        evidence["ai_fill"][section_name] = prev + changed
    return changed


def _fill_generic_text_fields(page, section_name: str, draft: ListingDraft) -> None:
    _activate_section(page, section_name)
    colour = str(draft.attributes.get("colour", "")).strip()
    material = str(draft.attributes.get("material", "")).strip()
    size = str(draft.attributes.get("size", "")).strip()
    weight = str(draft.attributes.get("weight", "")).strip()
    pkg_l, pkg_w, pkg_h = _derive_dimensions_cm(draft)
    pkg_weight_g = _derive_weight_g(draft)
    pkg_weight_kg = f"{(pkg_weight_g / 1000.0):.3f}".rstrip("0").rstrip(".")
    pkg_dims = f"{pkg_l} x {pkg_w} x {pkg_h}"
    model_no = _derive_model_number(draft)
    value_map = {
        # Business rule: leave brand empty by default.
        "brand": "",
        "material": material,
        "colour": colour,
        "color": colour,
        "size": size,
        "weight": weight,
        "packaged weight": str(pkg_weight_g),
        "packaged weight (g)": str(pkg_weight_g),
        "packaged weight (kg)": pkg_weight_kg,
        "product weight (g)": str(pkg_weight_g),
        "product weight (kg)": pkg_weight_kg,
        "weight (g)": str(pkg_weight_g),
        "weight (kg)": pkg_weight_kg,
        "packaged height": str(pkg_h),
        "packaged length": str(pkg_l),
        "packaged width": str(pkg_w),
        "packaged dimensions": pkg_dims,
        "product dimensions": pkg_dims,
        "dimensions": pkg_dims,
        "assembled product height": str(pkg_h),
        "assembled product length": str(pkg_l),
        "assembled product width": str(pkg_w),
        "assembled product dimensions": pkg_dims,
        "title": draft.title,
        "name": draft.title,
        "subtitle": draft.subtitle,
        "description": draft.key_features[:250],
        "detail": draft.key_features[:250],
        "feature": draft.key_features[:250],
        "model": model_no,
    }
    try:
        page.evaluate(
            """
(args) => {
  const sectionName = args.sectionName;
  const map = args.map || {};
  const sec = document.querySelector(`section[data-sectionname="${sectionName}"]`);
  if (!sec) return 0;

  const pickValue = (hint) => {
    const h = (hint || '').toLowerCase();
    for (const k of Object.keys(map)) {
      if (h.includes(k)) return map[k];
    }
    return '';
  };

  const nodes = Array.from(sec.querySelectorAll('input, textarea'));
  let changed = 0;

  const setNativeValue = (el, val) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) {
      desc.set.call(el, String(val));
    } else {
      el.value = String(val);
    }
  };

  for (const el of nodes) {
    if (el.disabled) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['file', 'radio', 'checkbox', 'hidden'].includes(type)) continue;
    const valNow = (el.value || '').trim();
    const hint = [
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('aria-label') || '',
      (el.closest('label')?.textContent || ''),
      (el.parentElement?.textContent || '').slice(0, 100),
    ].join(' ').toLowerCase();
    if (valNow) {
      const isZero = /^0+(\\.0+)?$/.test(valNow);
      const isDimWeight = /height|width|length|depth|dimension|weight|packaged|assembled/.test(hint);
      if (!(isZero && isDimWeight)) continue;
    }
    if (hint.includes('brand')) continue;
    const v = pickValue(hint) || (el.tagName.toLowerCase() === 'textarea' ? (map.description || '') : '');
    if (!v) continue;
    el.focus();
    setNativeValue(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    changed += 1;
  }
  return changed;
}
""",
            {"sectionName": section_name, "map": value_map},
        )
    except Exception:
        pass


def _clear_brand_fields(page, section_name: str) -> int:
    _activate_section(page, section_name)
    try:
        changed = page.evaluate(
            """
(sectionName) => {
  const sec = document.querySelector(`section[data-sectionname="${sectionName}"]`);
  if (!sec) return 0;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const setNativeValue = (el, val) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, String(val));
    else el.value = String(val);
  };
  let n = 0;
  const fields = Array.from(sec.querySelectorAll("input, textarea, [role='combobox']"));
  for (const el of fields) {
    if (!isVisible(el)) continue;
    const hint = norm([
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('aria-label') || '',
      (el.closest('label')?.textContent || ''),
      (el.closest('div')?.textContent || '').slice(0, 220),
    ].join(' '));
    if (!hint.includes('brand')) continue;
    const tag = (el.tagName || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea') continue;
    if (!String(el.value || '').trim()) continue;
    el.focus();
    setNativeValue(el, '');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    n += 1;
  }
  return n;
}
""",
            section_name,
        )
        return int(changed or 0)
    except Exception:
        return 0


def _fill_packaged_dimensions_by_label(page, section_name: str, draft: ListingDraft) -> int:
    _activate_section(page, section_name)
    pkg_l, pkg_w, pkg_h = _derive_dimensions_cm(draft)
    pkg_weight_g = _derive_weight_g(draft)
    try:
        changed = page.evaluate(
            """
(args) => {
  const sec = document.querySelector(`section[data-sectionname="${args.sectionName}"]`);
  if (!sec) return 0;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const setNativeValue = (el, val) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, String(val));
    else el.value = String(val);
  };
  const pairs = [
    { key: 'packaged height', val: String(args.h) },
    { key: 'packaged length', val: String(args.l) },
    { key: 'packaged width', val: String(args.w) },
    { key: 'packaged weight', val: String(args.g) },
  ];
  let changed = 0;
  const nodes = Array.from(sec.querySelectorAll('label, div, span, p'));
  for (const p of pairs) {
    let target = null;
    for (const n of nodes) {
      if (!isVisible(n)) continue;
      const t = norm(n.textContent || '');
      if (!t.includes(p.key)) continue;
      let cur = n.closest('div');
      for (let i = 0; i < 5 && cur; i += 1) {
        const cands = Array.from(cur.querySelectorAll("input:not([type='hidden']):not([type='file']), textarea"))
          .filter(x => isVisible(x) && !x.disabled);
        if (cands.length >= 1 && cands.length <= 4) {
          target = cands[0] || null;
          break;
        }
        cur = cur.parentElement;
      }
      if (target) break;
    }
    if (!target) continue;
    const now = norm(target.value || '');
    const numeric = Number((now || '0').replace(/[^0-9.\\-]/g, ''));
    if (now && Number.isFinite(numeric) && numeric > 0) continue;
    target.focus();
    setNativeValue(target, p.val);
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    target.dispatchEvent(new Event('blur', { bubbles: true }));
    changed += 1;
  }
  return changed;
}
""",
            {
                "sectionName": section_name,
                "l": pkg_l,
                "w": pkg_w,
                "h": pkg_h,
                "g": pkg_weight_g,
            },
        )
        return int(changed or 0)
    except Exception:
        return 0


def _fill_required_inputs(page, section_name: str, draft: ListingDraft) -> int:
    _activate_section(page, section_name)
    pkg_l, pkg_w, pkg_h = _derive_dimensions_cm(draft)
    pkg_weight_g = _derive_weight_g(draft)
    pkg_weight_kg = f"{(pkg_weight_g / 1000.0):.3f}".rstrip("0").rstrip(".")
    model_no = _derive_model_number(draft)
    try:
        return int(
            page.evaluate(
                """
(args) => {
  const sec = document.querySelector(`section[data-sectionname="${args.sectionName}"]`);
  if (!sec) return 0;

  const fallback = {
    title: args.title || 'Generic Product',
    model: args.model || 'MDL-AUTO-0001',
    num: String(args.num || '12'),
    len: String(args.len || args.num || '12'),
    wid: String(args.wid || args.num || '12'),
    hei: String(args.hei || args.num || '12'),
    weightG: String(args.weightG || '200'),
    weightKg: String(args.weightKg || '0.2'),
    text: 'To be confirmed',
  };

  const setNativeValue = (el, val) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, String(val));
    else el.value = String(val);
  };

  const requiredErrors = Array.from(sec.querySelectorAll('*')).filter(n => {
    const t = (n.textContent || '').trim().toLowerCase();
    if (!t) return false;
    if (!t.includes('this field is required')) return false;
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });

  let changed = 0;
  const touched = new Set();
  const fillNode = (el) => {
    if (!el || touched.has(el)) return;
    touched.add(el);
    if (el.disabled) return;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['file', 'radio', 'checkbox', 'hidden'].includes(type)) return;
    const now = (el.value || '').trim();
    if (now) return;

    const hint = [
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('aria-label') || '',
      (el.closest('label')?.textContent || ''),
      (el.closest('div')?.textContent || '').slice(0, 300),
    ].join(' ').toLowerCase();

    let v = fallback.text;
    if (hint.includes('brand')) return;
    if (hint.includes('model')) v = fallback.model;
    else if (hint.includes('height')) v = fallback.hei;
    else if (hint.includes('length') || hint.includes('depth')) v = fallback.len;
    else if (hint.includes('width')) v = fallback.wid;
    else if (hint.includes('weight') && (hint.includes('(g') || hint.includes(' gram'))) v = fallback.weightG;
    else if (hint.includes('weight') && hint.includes('(kg')) v = fallback.weightKg;
    else if (hint.includes('weight')) v = fallback.weightKg;
    else if (hint.includes('dimension')) v = `${fallback.len} x ${fallback.wid} x ${fallback.hei}`;
    else if (hint.includes('title') || hint.includes('name')) v = fallback.title;
    else if (hint.includes('ean') || hint.includes('gtin') || hint.includes('barcode') || hint.includes('upc')) v = '0000000000000';

    if (type === 'number') {
      if (!/^[-+]?\\d+(\\.\\d+)?$/.test(String(v))) v = fallback.num;
    }

    el.focus();
    setNativeValue(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    changed += 1;
  };

  for (const err of requiredErrors) {
    const wrap = err.closest('div');
    if (!wrap) continue;
    const target = wrap.querySelector('input, textarea') || wrap.parentElement?.querySelector('input, textarea');
    if (target) fillNode(target);
  }

  const invalids = Array.from(sec.querySelectorAll("input[aria-invalid='true'], textarea[aria-invalid='true']"));
  for (const el of invalids) fillNode(el);

  return changed;
}
""",
                {
                    "sectionName": section_name,
                    "title": draft.title,
                    "model": model_no,
                    "num": 12,
                    "len": pkg_l,
                    "wid": pkg_w,
                    "hei": pkg_h,
                    "weightG": pkg_weight_g,
                    "weightKg": pkg_weight_kg,
                },
            )
            or 0
        )
    except Exception:
        return 0


def _get_visible_dropdown_options(page) -> list[str]:
    try:
        out = page.evaluate(
            """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll("[role='option'], li, .select__option, [class*='option']"));
  const seen = new Set();
  const out = [];
  for (const n of nodes) {
    if (!isVisible(n)) continue;
    const t = norm(n.textContent || '');
    if (!t || t.length > 100) continue;
    const k = t.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(t);
  }
  return out.slice(0, 40);
}
"""
        )
    except Exception:
        return []
    if isinstance(out, list):
        return [str(x).strip() for x in out if str(x).strip()]
    return []


def _draft_facts_text(draft: ListingDraft) -> str:
    attrs = " ".join(str(v) for v in (draft.attributes or {}).values())
    return " ".join(
        [
            str(draft.title or ""),
            str(draft.subtitle or ""),
            str(draft.key_features or ""),
            attrs,
            " ".join(str(x) for x in (draft.whats_in_box or [])),
        ]
    ).lower()


def _text_has_any(text: str, keywords: list[str]) -> bool:
    t = str(text or "").lower()
    return any(str(k).lower() in t for k in keywords if k)


def _extract_warranty_months(draft: ListingDraft) -> int:
    t = _draft_facts_text(draft)
    m = re.search(r"(\d{1,2})\s*(?:month|months|个月)", t, flags=re.IGNORECASE)
    if m:
        try:
            return max(1, int(m.group(1)))
        except Exception:
            pass
    m = re.search(r"(\d{1,2})\s*(?:year|years|年)", t, flags=re.IGNORECASE)
    if m:
        try:
            return max(1, int(m.group(1))) * 12
        except Exception:
            pass
    return 12


def _infer_yes_no_for_hint(hint: str, draft: ListingDraft) -> str | None:
    h = str(hint or "").lower()
    facts = _draft_facts_text(draft)

    if "water resistant" in h or "waterproof" in h or "防水" in h:
        return "Yes" if _text_has_any(facts, ["waterproof", "water resistant", "ipx", "ip67", "ip68", "防水"]) else "No"
    if "rechargeable" in h or "充电" in h:
        return "Yes" if _text_has_any(facts, ["rechargeable", "recharge", "usb charging", "lithium", "battery"]) else "No"
    if "integrated remote" in h or ("remote" in h and "control" in h):
        return "Yes" if _text_has_any(facts, ["remote control", "remote", "with remote"]) else "No"
    if "proudly south african" in h or "made in south africa" in h:
        return "Yes" if _text_has_any(facts, ["south africa", "south african", "南非"]) else "No"
    if "bluetooth" in h:
        return "Yes" if _text_has_any(facts, ["bluetooth", "bt "]) else "No"
    return None


def _combo_current_value(loc) -> str:
    try:
        out = loc.evaluate(
            """
(el) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const direct = norm(el.value || '');
  if (direct) return direct;
  const ariaNow = norm(el.getAttribute('aria-valuetext') || '');
  if (ariaNow) return ariaNow;
  const single = el.querySelector(".select__single-value, [class*='singleValue'], .Select-value-label, [class*='selected-value']");
  const singleTxt = norm(single?.textContent || '');
  if (singleTxt) return singleTxt;
  const chips = Array.from(el.querySelectorAll(".select__multi-value__label, [class*='multiValue'], [class*='tag']"))
    .map(x => norm(x.textContent || ''))
    .filter(Boolean);
  if (chips.length) return chips.join(', ');
  const own = norm(el.textContent || '');
  if (own && own.length <= 80) return own;
  return '';
}
"""
        )
    except Exception:
        return ""
    return str(out or "").strip()


def _fill_comboboxes(page, section_name: str, draft: ListingDraft) -> int:
    _activate_section(page, section_name)
    filled = 0
    defaults = [
        draft.attributes.get("material", ""),
        draft.attributes.get("colour", ""),
        draft.attributes.get("size", ""),
    ]

    def combo_hint(loc) -> str:
        try:
            txt = loc.evaluate(
                """
(el) => {
  const wrap = el.closest('div');
  const label = el.closest('label')?.textContent || '';
  const aria = el.getAttribute('aria-label') || '';
  const ph = el.getAttribute('placeholder') || '';
  const hint = (wrap?.textContent || '').slice(0, 260);
  return [label, aria, ph, hint].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
}
"""
            )
        except Exception:
            txt = ""
        return str(txt or "")

    facts = _draft_facts_text(draft)
    warranty_months = _extract_warranty_months(draft)
    warranty_type_value = ""
    no_warranty = _text_has_any(facts, ["no warranty", "without warranty", "不保修", "无保修"])
    try:
        inputs = page.locator(f"section[data-sectionname='{section_name}'] [role='combobox']")
        count = inputs.count()
    except Exception:
        return 0
    for i in range(min(count, 60)):
        try:
            loc = inputs.nth(i)
            if not loc.is_visible(timeout=1000):
                continue
            value_now = _combo_current_value(loc)
            aria_invalid = str(loc.get_attribute("aria-invalid") or "").strip().lower() == "true"
            if value_now and (not aria_invalid) and (not _is_placeholder_combo_value(value_now)):
                continue
            hint = combo_hint(loc)
            optional = "optional" in hint or "可选" in hint
            aria_required = str(loc.get_attribute("aria-required") or "").strip().lower() == "true"
            required_like = aria_required or (("required" in hint or "必填" in hint) and (not optional))
            candidates: list[str] = []
            loc.click(timeout=1200)
            page.wait_for_timeout(120)
            options = _get_visible_dropdown_options(page)
            options_text = " ".join(o.lower() for o in options)
            has_yes_no = ("yes" in options_text) and ("no" in options_text)
            is_warranty = ("warranty" in hint) or ("保修" in hint) or ("warranty" in options_text)
            is_warranty_period = any(k in hint for k in ["period", "duration", "month", "year", "期限"])
            if (not is_warranty_period) and any(("month" in x.lower() or "year" in x.lower()) for x in options):
                is_warranty_period = True

            allow_fallback = False
            if (section_name == "Product Attributes") and (not optional) and _is_placeholder_combo_value(value_now):
                required_like = True
            if is_warranty:
                if is_warranty_period:
                    years = max(1, int(round(warranty_months / 12)))
                    if no_warranty or ("no warranty" in warranty_type_value.lower()):
                        candidates = ["Not Applicable", "N/A", "No Warranty", "0 Months", "0 Month"]
                    else:
                        candidates = [
                            f"{warranty_months} Months",
                            f"{warranty_months} Month",
                            f"{years} Year",
                            f"{years} Years",
                            "12 Months",
                            "6 Months",
                            "3 Months",
                            "Not Applicable",
                            "N/A",
                        ]
                else:
                    if no_warranty:
                        candidates = ["No Warranty", "No", "Not Applicable", "N/A", "Limited Warranty"]
                    else:
                        candidates = [
                            "Limited Warranty",
                            "Manufacturer Warranty",
                            "Supplier Warranty",
                            "Limited",
                            "No Warranty",
                        ]
                allow_fallback = True
            elif has_yes_no:
                yn = _infer_yes_no_for_hint(hint, draft)
                if yn == "Yes":
                    candidates = ["Yes", "No", "Not Applicable", "N/A"]
                else:
                    candidates = ["No", "Not Applicable", "N/A", "Yes"]
                if optional and yn is None:
                    page.keyboard.press("Escape")
                    continue
            elif any(k in hint for k in ["rechargeable", "water resistant", "integrated remote", "proudly south african"]):
                yn = _infer_yes_no_for_hint(hint, draft) or "No"
                candidates = [yn, "No", "Not Applicable", "N/A", "Yes"]
            elif "country of origin" in hint:
                candidates = ["China", "CN", "South Africa"]
            elif "main colour" in hint or "primary colour" in hint:
                c = str(draft.attributes.get("colour", "")).strip()
                if c:
                    candidates = [c]
            elif "secondary colour" in hint and optional:
                page.keyboard.press("Escape")
                continue
            elif ("peripheral connectivity" in hint) or ("connectivity" in hint):
                if _text_has_any(facts, ["bluetooth", "wireless", "wifi", "2.4g"]):
                    candidates = ["Wireless", "Bluetooth", "Wired"]
                else:
                    candidates = ["Wired", "Wireless", "Bluetooth"]
            elif any(k in hint for k in ["headsets form factor", "headphone style", "special feature"]):
                candidates = [
                    "In-Ear",
                    "On-Ear",
                    "Over-Ear",
                    "Sports",
                    "Noise Cancelling",
                    "Bluetooth",
                    "Wired",
                ]

            picked = _pick_visible_dropdown_option(page, candidates, allow_fallback=allow_fallback) if candidates else ""
            if (not picked) and required_like:
                picked = _pick_visible_dropdown_option(page, [], allow_fallback=True)
            page.wait_for_timeout(180)
            value_now = _combo_current_value(loc)
            if (_is_placeholder_combo_value(value_now)) and has_yes_no and (required_like or aria_invalid):
                picked = _pick_visible_dropdown_option(page, ["No", "Not Applicable", "N/A", "Yes"], allow_fallback=True)
                page.wait_for_timeout(180)
                value_now = _combo_current_value(loc)
            if (_is_placeholder_combo_value(value_now)) and (not picked) and (required_like or aria_invalid) and candidates:
                picked = _pick_visible_dropdown_option(page, candidates, allow_fallback=True)
                page.wait_for_timeout(180)
                value_now = _combo_current_value(loc)
            if (_is_placeholder_combo_value(value_now)) and (not picked) and (required_like or aria_invalid):
                # Last fallback for required comboboxes only.
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(220)
                value_now = _combo_current_value(loc)
            if _is_placeholder_combo_value(value_now):
                v = str(defaults[i % len(defaults)] or "").strip()
                if v and (required_like or aria_invalid):
                    try:
                        tag = str(loc.evaluate("(el) => (el.tagName || '').toLowerCase()") or "")
                    except Exception:
                        tag = ""
                    if tag == "input":
                        loc.fill(v, timeout=1500)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(150)
                        value_now = _combo_current_value(loc)
            if value_now and (not _is_placeholder_combo_value(value_now)):
                if is_warranty and (not is_warranty_period):
                    warranty_type_value = value_now
                filled += 1
        except Exception:
            continue
    return filled


def _section_has_none_summary(page, section_name: str) -> bool:
    try:
        txt = page.evaluate(
            """
(sec) => {
  const root = document.querySelector(`section[data-sectionname="${sec}"]`);
  if (!root) return '';
  return (root.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
}
""",
            section_name,
        )
    except Exception:
        return False
    t = str(txt or "")
    return (" none " in f" {t} ") or t.endswith(" none") or t.startswith("none ")


def _count_required_errors(page, section_name: str) -> int:
    try:
        n = page.evaluate(
            """
(sec) => {
  const root = document.querySelector(`section[data-sectionname="${sec}"]`);
  if (!root) return 0;
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const msgPatterns = [
    'this field is required',
    'please choose from the dropdown',
    'invalid value',
    'required attributes are incomplete'
  ];
  const msgErrs = Array.from(root.querySelectorAll('*')).filter(n => {
    const t = (n.textContent || '').trim().toLowerCase();
    if (!t) return false;
    if (!msgPatterns.some(p => t.includes(p))) return false;
    return isVisible(n);
  });
  const invalidFields = Array.from(
    root.querySelectorAll(
      "input[aria-invalid='true'], textarea[aria-invalid='true'], [role='combobox'][aria-invalid='true'], [aria-invalid='true'] [role='combobox'], .is-invalid, .has-error"
    )
  ).filter(isVisible);
  return msgErrs.length + invalidFields.length;
}
""",
            section_name,
        )
        return int(n or 0)
    except Exception:
        return 0


def _collect_required_debug(page, section_name: str) -> dict[str, Any]:
    try:
        out = page.evaluate(
            """
(sec) => {
  const root = document.querySelector(`section[data-sectionname="${sec}"]`);
  if (!root) return { errors: [], pending_fields: [] };
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const errPatterns = [
    'this field is required',
    'please choose from the dropdown',
    'invalid value',
    'required attributes are incomplete'
  ];
  const errors = [];
  const nodes = Array.from(root.querySelectorAll('*'));
  for (const n of nodes) {
    const t = norm(n.textContent || '').toLowerCase();
    if (!t || t.length > 180) continue;
    if (!errPatterns.some(p => t.includes(p))) continue;
    if (!errors.includes(t)) errors.push(t);
  }

  const pending = [];
  const fields = Array.from(root.querySelectorAll("input, textarea, [role='combobox']"));
  for (const el of fields) {
    if (el.disabled) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (['file', 'hidden', 'radio', 'checkbox'].includes(type) && role !== 'combobox') continue;
    let value = norm(el.value || '');
    if (!value && role === 'combobox') {
      const single = el.querySelector(".select__single-value, [class*='singleValue'], .Select-value-label, [class*='selected-value']");
      value = norm(single?.textContent || '') || norm(el.getAttribute('aria-valuetext') || '');
      if (!value) {
        const own = norm(el.textContent || '');
        if (own && own.length <= 80) value = own;
      }
    }
    const hint = norm([
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('placeholder') || '',
      (el.closest('label')?.textContent || ''),
      (el.closest('div')?.textContent || '').slice(0, 220),
    ].join(' ')).toLowerCase();
    const optional = hint.includes('optional');
    const required = !optional && (
      el.required ||
      el.getAttribute('aria-required') === 'true' ||
      hint.includes('required')
    );
    const invalid = el.getAttribute('aria-invalid') === 'true';
    const placeholderLike = !value || /^choose|select|optional|none|-|n\\/a$/i.test(value);
    if (!(invalid || (required && placeholderLike))) continue;
    pending.push({
      role: role || 'input',
      required,
      invalid,
      value,
      hint: hint.slice(0, 160),
    });
    if (pending.length >= 16) break;
  }
  return { errors: errors.slice(0, 20), pending_fields: pending };
}
""",
            section_name,
        )
    except Exception:
        return {"errors": [], "pending_fields": []}
    if isinstance(out, dict):
        return out
    return {"errors": [], "pending_fields": []}


def _reset_scroll_to_section_top(page, section_name: str) -> None:
    try:
        page.evaluate(
            """
(sec) => {
  const root = document.querySelector(`section[data-sectionname="${sec}"]`);
  if (root) root.scrollIntoView({ block: 'start' });
  window.scrollTo({ top: 0, behavior: 'instant' });
}
""",
            section_name,
        )
        page.wait_for_timeout(120)
    except Exception:
        pass


def _scroll_section_step(page, section_name: str, step: int = 650) -> None:
    try:
        page.evaluate(
            """
(args) => {
  const root = document.querySelector(`section[data-sectionname="${args.sec}"]`);
  if (root) root.scrollIntoView({ block: 'start' });
  window.scrollBy({ top: args.step, left: 0, behavior: 'instant' });
}
""",
            {"sec": section_name, "step": int(step)},
        )
        page.wait_for_timeout(120)
    except Exception:
        pass


def _ensure_required_sections_filled(page, draft: ListingDraft, evidence: dict[str, Any]) -> list[str]:
    missing: list[str] = []

    # Product Attributes
    attr_ok = False
    attr_err = 999
    for _ in range(5):
        _reset_scroll_to_section_top(page, "Product Attributes")
        for _scan in range(5):
            _fill_ai_suggested_values(page, "Product Attributes", draft, evidence)
            _fill_generic_text_fields(page, "Product Attributes", draft)
            _fill_packaged_dimensions_by_label(page, "Product Attributes", draft)
            _clear_brand_fields(page, "Product Attributes")
            _fill_comboboxes(page, "Product Attributes", draft)
            _fill_required_inputs(page, "Product Attributes", draft)
            _scroll_section_step(page, "Product Attributes", step=700)
        if _section_next_enabled(page, "Product Attributes"):
            _click_section_next(page, "Product Attributes")
            page.wait_for_timeout(600)
        attr_err = _count_required_errors(page, "Product Attributes")
        if (not _section_has_none_summary(page, "Product Attributes")) and attr_err == 0:
            attr_ok = True
            break
    evidence["filled"]["attributes"] = attr_ok
    evidence["filled"]["attributes_required_errors"] = attr_err
    evidence["attributes_debug"] = _collect_required_debug(page, "Product Attributes")
    if not attr_ok:
        missing.append("Product Attributes")

    # Product Details
    details_ok = False
    details_err = 999
    for _ in range(4):
        _fill_ai_suggested_values(page, "Product Details", draft, evidence)
        _fill_generic_text_fields(page, "Product Details", draft)
        _fill_comboboxes(page, "Product Details", draft)
        _fill_required_inputs(page, "Product Details", draft)
        if _section_next_enabled(page, "Product Details"):
            _click_section_next(page, "Product Details")
            page.wait_for_timeout(600)
        details_err = _count_required_errors(page, "Product Details")
        if (not _section_has_none_summary(page, "Product Details")) and details_err == 0:
            details_ok = True
            break
    evidence["filled"]["details"] = details_ok
    evidence["filled"]["details_required_errors"] = details_err
    evidence["details_debug"] = _collect_required_debug(page, "Product Details")
    if not details_ok:
        missing.append("Product Details")

    return missing


def _upload_images_in_section(page, image_paths: list[Path]) -> tuple[bool, str]:
    _activate_section(page, "Product Images")
    white_files = [str(p) for p in image_paths if p.exists() and "/images_white/" in str(p)]
    files = white_files if white_files else [str(p) for p in image_paths if p.exists()]
    # Business rule: 1 hero + up to 4 secondary images.
    files = files[:5]
    if not files:
        return False, "image_files_missing"
    try:
        inp = page.locator("#image-management__input").first
        if inp.count() == 0:
            return False, "image_input_not_found"
        page.set_input_files("#image-management__input", files, timeout=20000)
        # Wait for upload settle to avoid submitting while thumbnails are still processing.
        for _ in range(50):
            busy = False
            try:
                busy = bool(
                    page.evaluate(
                        """
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll("[class*='upload'], [class*='progress'], [class*='spinner'], *"));
  for (const n of nodes) {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!t) continue;
    if (!/upload|processing|loading/.test(t)) continue;
    if (isVisible(n)) return true;
  }
  return false;
}
"""
                    )
                )
            except Exception:
                busy = False
            if not busy:
                break
            page.wait_for_timeout(600)
        page.wait_for_timeout(800)
        return True, ""
    except Exception:
        return False, "image_upload_failed"


def _finalize_action(page, mode: str) -> tuple[bool, str]:
    def _has_submit_blocker() -> bool:
        try:
            return bool(
                page.evaluate(
                    """
() => {
  const pats = [
    'required attributes are incomplete',
    'this field is required',
    'please choose from the dropdown',
    'invalid value'
  ];
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll('[role="alert"], .alert, .error, .invalid-feedback, .text-danger, *'));
  for (const n of nodes) {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!t) continue;
    if (!pats.some(p => t.includes(p))) continue;
    if (isVisible(n)) return true;
  }
  return false;
}
"""
                )
            )
        except Exception:
            return False

    def _has_submit_success_hint() -> bool:
        try:
            return bool(
                page.evaluate(
                    """
() => {
  const pats = ['saved', 'successfully', 'draft saved', 'published', 'submitted'];
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll('[role="status"], [role="alert"], .toast, .notification, .alert-success, *'));
  for (const n of nodes) {
    const t = (n.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!t) continue;
    if (!pats.some(p => t.includes(p))) continue;
    if (isVisible(n)) return true;
  }
  return false;
}
"""
                )
            )
        except Exception:
            return False

    def _wait_submit_confirmed() -> bool:
        # Save/Publish is considered successful only if form exits or success hint appears,
        # and no visible validation blocker remains.
        for _ in range(16):
            if _has_submit_blocker():
                return False
            if "single-product" not in page.url:
                return True
            if _has_submit_success_hint():
                return True
            page.wait_for_timeout(500)
        return False

    save_btn = page.locator("button:has-text('Save and Close')").first
    preview_btn = page.locator("button:has-text('Continue to Preview')").first

    if mode == "draft":
        try:
            if save_btn.count() > 0 and save_btn.is_enabled(timeout=1200):
                save_btn.click(timeout=5000)
                if _wait_submit_confirmed():
                    return True, "save_draft"
                return False, "save_blocked_or_not_confirmed"
        except Exception:
            pass
        try:
            if preview_btn.count() > 0 and preview_btn.is_enabled(timeout=1200):
                preview_btn.click(timeout=5000)
                page.wait_for_timeout(2000)
                for sel in [
                    "button:has-text('Save and Close')",
                    "button:has-text('Save Draft')",
                    "button:has-text('Submit')",
                ]:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_enabled(timeout=1200):
                        btn.click(timeout=5000)
                        if _wait_submit_confirmed():
                            return True, "save_draft_after_preview"
                        return False, "save_after_preview_blocked_or_not_confirmed"
                return False, "ready_for_preview_but_no_submit"
        except Exception:
            pass
        return False, "save_button_disabled"

    # publish mode
    try:
        if preview_btn.count() > 0 and preview_btn.is_enabled(timeout=1200):
            preview_btn.click(timeout=5000)
            page.wait_for_timeout(2000)
            for sel in [
                "button:has-text('Publish')",
                "button:has-text('Submit')",
                "button:has-text('Confirm')",
            ]:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_enabled(timeout=1200):
                    btn.click(timeout=5000)
                    if _wait_submit_confirmed():
                        return True, "publish"
                    return False, "publish_blocked_or_not_confirmed"
    except Exception:
        pass
    return False, "publish_button_disabled"


# ─── Portal 类目字段探测（缓存到本地 JSON）────────────────────────────────────

_PROBE_CACHE_DIR = Path(__file__).parent.parent.parent / "input" / "portal_fields"


def _probe_cache_path(category_key: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", category_key.lower())[:80]
    return _PROBE_CACHE_DIR / f"{safe}.json"


def load_probed_fields(category_key: str) -> dict[str, Any] | None:
    """
    读取已缓存的类目字段定义。
    返回 None 表示尚未探测。
    """
    p = _probe_cache_path(category_key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_probed_fields(category_key: str, data: dict[str, Any]) -> None:
    _PROBE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _probe_cache_path(category_key)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _probe_fill_combobox_options(page, fields: list[dict[str, Any]]) -> None:
    """
    对已抓取的字段列表中 type=combobox 且 options 为空的字段，
    尝试点击展开下拉框，收集选项后关闭，将选项回填到 field['options']。
    """
    combo_fields = [f for f in fields if f.get("type") in ("combobox", "select") and not f.get("options")]
    if not combo_fields:
        return

    print(f"[probe] 开始抓取 {len(combo_fields)} 个 combobox 的选项...")
    for fdef in combo_fields:
        label = fdef.get("label", "")
        section = fdef.get("section", "")
        if not label:
            continue
        try:
            # 在所属 section 内按 label 文本找对应 combobox
            if section:
                base = page.locator(f"section[data-sectionname='{section}']")
            else:
                base = page

            # 找到包含该 label 文本的字段容器，然后取其内的 combobox input
            container = base.locator(
                f"[data-fieldid]:has-text('{label}')"
            ).first
            if container.count() == 0:
                continue
            combo_input = container.locator("input[role='combobox']").first
            if combo_input.count() == 0:
                continue

            # 点击打开下拉
            combo_input.click(timeout=3000)
            # 等待下拉选项真正出现在 DOM 里（最多 2.5s），而不是固定等待
            # 某些字段选项通过异步 API 加载，600ms 固定等待容易漏掉
            try:
                page.wait_for_selector(
                    '[role="option"], [role="listbox"] li, .Select__option, .dropdown-item',
                    timeout=2500,
                )
            except Exception:
                page.wait_for_timeout(800)  # fallback: 等不到选项时兜底

            # 收集选项（[role='option'] 或 li 在 dropdown 里）
            opts = page.evaluate("""() => {
                const items = document.querySelectorAll(
                    '[role="option"], [role="listbox"] li, .Select__option, .dropdown-item'
                );
                return Array.from(items)
                    .map(el => el.textContent.replace(/\\s+/g, ' ').trim())
                    .filter(t => t && t.length < 100);
            }""")
            # 若第一次读到空（选项仍在加载），再等 500ms 重试一次
            if not opts:
                page.wait_for_timeout(500)
                opts = page.evaluate("""() => {
                    const items = document.querySelectorAll(
                        '[role="option"], [role="listbox"] li, .Select__option, .dropdown-item'
                    );
                    return Array.from(items)
                        .map(el => el.textContent.replace(/\\s+/g, ' ').trim())
                        .filter(t => t && t.length < 100);
                }""")
            if isinstance(opts, list) and opts:
                fdef["options"] = opts
                print(f"[probe]   {label}: 获得 {len(opts)} 个选项")

            # 按 Escape 关闭下拉
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

        except Exception as _e:
            print(f"[probe]   {label} combobox 选项抓取失败: {_e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    # 某些字段在前端下拉里选项有限且结构特殊，实际抓取容易失败；
    # 这里做一次兜底：如果仍然没有 options，就用 loadsheet LOOKUP 里的合法值填充，
    # 保证 UI 下拉可用且值一定合法。
    _HARDCODED_OPTIONS: dict[str, list[str]] = {
        "Speaker Features": [
            "Hi - Fi Sound",
            "Dynamic RGB Lighting",
            "Multiple Ways to Connect",
            "Quiet Standby",
            "Energy Saving",
        ],
        "Voice Recorder Compatibility": [
            "Laptop",
            "Personal Computer",
            "Smartphone",
            "Tablet",
        ],
    }
    for fdef in fields:
        label = str(fdef.get("label", "")).strip()
        if not label or fdef.get("options"):
            continue
        fallback = _HARDCODED_OPTIONS.get(label)
        if fallback:
            fdef["options"] = fallback
            print(f"[probe]   {label}: 使用内置选项兜底（{len(fallback)} 个）")


def _scrape_attributes_fields(page) -> list[dict[str, Any]]:
    """
    在 Add-a-Product 页面上抓取 Product Attributes / Product Details 两个
    section 的所有字段定义（标签、类型、必填、下拉选项）。
    Takealot 用 "(Optional)" 标注可选字段，未标注的即为必填。
    """
    SECTIONS = ["Product Attributes", "Product Details"]
    results: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for section_name in SECTIONS:
        _activate_section(page, section_name)
        page.wait_for_timeout(600)

        # 滚动 section 内容区域，触发懒加载，确保所有字段渲染进 DOM
        try:
            page.evaluate("""
(sectionName) => {
  const sec = document.querySelector(`section[data-sectionname="${sectionName}"]`);
  if (!sec) return;
  const content = sec.querySelector('.ZorkSection__content') || sec;
  const step = content.scrollHeight / 6 || 300;
  let pos = 0;
  const scroll = () => {
    pos += step;
    content.scrollTop = pos;
    if (pos < content.scrollHeight) requestAnimationFrame(scroll);
  };
  scroll();
}
""", section_name)
            page.wait_for_timeout(800)
        except Exception:
            pass

        try:
            fields = page.evaluate(
                """
(sectionName) => {
  const sec = document.querySelector(`section[data-sectionname="${sectionName}"]`);
  if (!sec) return [];
  const out = [];
  const seen = new Set();

  // --- 建立"视觉必填组"映射 ---
  // Takealot 用 REQUIRED / RECOMMENDED / OPTIONAL 等标题行把字段分组。
  // 对位于 REQUIRED 标题之后（OPTIONAL/RECOMMENDED 之前）的字段强制标为 required。
  // 方法：遍历 section 内所有顶层节点，记录上一个分组标题。
  const requiredByPosition = new Set();  // 记录属于 REQUIRED 组的 [data-fieldid] 元素
  const allNodes = Array.from(sec.querySelectorAll('[data-fieldid], [class*="section-header"], [class*="SectionHeader"], [class*="group-header"], [class*="GroupHeader"]'));
  let inRequired = false;
  for (const node of allNodes) {
    const txt = (node.textContent || '').trim().toUpperCase();
    // 检测分组标题节点（小型节点且仅含 REQUIRED/OPTIONAL/RECOMMENDED 文字）
    if (!node.hasAttribute('data-fieldid') && txt.length < 40) {
      if (txt.includes('REQUIRED')) { inRequired = true; continue; }
      if (txt.includes('OPTIONAL') || txt.includes('RECOMMENDED')) { inRequired = false; continue; }
    }
    if (node.hasAttribute('data-fieldid') && inRequired) {
      requiredByPosition.add(node);
    }
  }

  // 用 [data-fieldid] 作为主选择器，覆盖所有字段容器类型（ZorkFieldContainer + 特殊字段如 Warranty）
  const containers = Array.from(sec.querySelectorAll('[data-fieldid]'));

  for (const c of containers) {
    // 跳过父级分组容器（有子 [data-fieldid] 的是分组，取其叶子字段即可）
    if (c.querySelector('[data-fieldid]')) continue;

    // data-isrequired 是权威来源，直接读取；
    // 也接受位于视觉 REQUIRED 分组里的字段（Takealot 对类目专属必填字段不设 data-isrequired）
    const isRequired = c.getAttribute('data-isrequired') === 'true' || requiredByPosition.has(c);
    const dataFieldType = c.getAttribute('data-fieldtype') || '';

    // 标签：优先从 .ZorkFieldContainer__title 取直接文本节点
    const titleEl = c.querySelector('.ZorkFieldContainer__title, .ZorkFieldContainer__label');
    let cleanLabel = '';
    if (titleEl) {
      const directText = Array.from(titleEl.childNodes)
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent)
        .join('').trim();
      cleanLabel = directText.replace(/\*/g, '').replace(/\s+/g, ' ').trim();
    }
    // fallback：取容器内第一个 label 元素的直接文本
    if (!cleanLabel) {
      const labelEl = c.querySelector('label');
      if (labelEl) {
        cleanLabel = Array.from(labelEl.childNodes)
          .filter(n => n.nodeType === 3)
          .map(n => n.textContent)
          .join('').replace(/\*/g, '').replace(/\s+/g, ' ').trim();
      }
    }
    if (!cleanLabel || cleanLabel.length < 2 || cleanLabel.length > 100) continue;

    // 去重（大小写不敏感）
    if (seen.has(cleanLabel.toLowerCase())) continue;
    seen.add(cleanLabel.toLowerCase());

    // 字段类型：优先用 data-fieldtype，再看实际控件
    const inp = c.querySelector('input:not([type=hidden]):not([type=radio]):not([type=checkbox])');
    const sel = c.querySelector('select');
    const combo = c.querySelector('[role=combobox], [role=listbox]');
    const textarea = c.querySelector('textarea');

    let type = 'text';
    let options = [];
    if (dataFieldType === 'Boolean') {
      type = 'select';
    } else if (dataFieldType === 'Float' || dataFieldType === 'Integer') {
      type = 'number';
    } else if (sel) {
      type = 'select';
      options = Array.from(sel.options).map(o => o.text.trim()).filter(Boolean);
    } else if (combo) {
      type = 'combobox';
    } else if (textarea) {
      type = 'textarea';
    } else if (inp) {
      type = inp.getAttribute('type') || 'text';
    }

    // 提示文字（description 或 placeholder）
    const hint = (
      c.querySelector('.ZorkFieldContainer__description')?.textContent ||
      (inp || textarea)?.getAttribute('placeholder') ||
      ''
    ).replace(/\\s+/g, ' ').trim().slice(0, 200);

    out.push({ label: cleanLabel, required: isRequired, type, options, hint });
  }
  return out;
}
""",
                section_name,
            )
            if isinstance(fields, list):
                for f in fields:
                    if not isinstance(f, dict):
                        continue
                    lbl = str(f.get("label", "")).strip()
                    if not lbl or lbl in seen_labels:
                        continue
                    seen_labels.add(lbl)
                    results.append({
                        "label":    lbl,
                        "required": bool(f.get("required", False)),
                        "type":     str(f.get("type", "text")),
                        "options":  f.get("options", []),
                        "hint":     str(f.get("hint", "")),
                        "section":  section_name,
                    })
        except Exception as exc:
            print(f"[probe] 抓取 {section_name} 失败: {exc}")

    return results


def _portal_path_from_loadsheet_ids(
    main_cat: str,
    low_cat: str,
    selectors_cfg_path: str | Path,
) -> list[str]:
    """
    给定 loadsheet 里读出的带 ID 类目字符串，例如：
        main_cat = 'Audio Devices (15425)'
        low_cat  = 'Speakers (15446)'
    从 takealot_categories.csv 按 lowest 字段 ID 精确定位该行，
    返回完整的 portal 导航路径（已处理 -> 多级结构）。
    找不到返回空列表。
    """
    # 提取 lowest 最末级的数字 ID
    last_part = str(low_cat or "").split("->")[-1]
    id_match = re.search(r"\((\d+)\)", last_part)
    if not id_match:
        return []
    low_id = id_match.group(1)

    try:
        cfg = load_selectors(selectors_cfg_path)
        csv_path = _resolve_catalog_csv_path(cfg, selectors_cfg_path)
    except Exception:
        return []
    if not csv_path.exists():
        return []

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header_found = False
            for row in reader:
                if not header_found:
                    if (
                        len(row) >= 4
                        and str(row[0]).strip().lower() == "division"
                        and str(row[3]).strip().lower().startswith("lowest")
                    ):
                        header_found = True
                    continue
                if len(row) < 4:
                    continue
                raw_lowest = str(row[3]).strip()
                last_seg = raw_lowest.split("->")[-1]
                m = re.search(r"\((\d+)\)", last_seg)
                if not (m and m.group(1) == low_id):
                    continue
                # 匹配成功，构建完整路径
                division = str(row[0]).strip()
                department = str(row[1]).strip()
                raw_main = str(row[2]).strip()
                path: list[str] = [division, department]
                for seg in raw_main.split("->"):
                    p = _strip_category_id(seg).strip()
                    if p and (_norm_text(path[-1]) if path else "") != _norm_text(p):
                        path.append(p)
                for seg in raw_lowest.split("->"):
                    p = _strip_category_id(seg).strip()
                    if p and (_norm_text(path[-1]) if path else "") != _norm_text(p):
                        path.append(p)
                return path
    except Exception:
        pass
    return []


def _find_full_portal_path(partial_path: list[str], selectors_cfg_path: str | Path) -> list[str]:
    """
    Given a short translated path like ['Speakers'], look up the full portal
    navigation path e.g. ['Consumer Electronics', 'TV & Audio', 'Audio Devices', 'Speakers']

    Priority:
    1. portal.category_keyword_paths in selectors.yaml (keyword → manual path)
    2. takealot_categories.csv exact match on lowest/main field
    Returns the original partial_path if no match found.
    """
    if not partial_path:
        return partial_path
    try:
        cfg = load_selectors(selectors_cfg_path)
    except Exception:
        return partial_path

    # --- Pass 1: category_keyword_paths ---
    leaf_lower = partial_path[-1].strip().lower()
    query = " ".join(str(x).strip().lower() for x in partial_path)
    rules = cfg.get("portal", {}).get("category_keyword_paths", [])
    best_path: list[str] = []
    best_score = 0
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            kws_raw = r.get("keywords", [])
            path_raw = r.get("path", [])
            kws = [str(k).strip().lower() for k in kws_raw if str(k).strip()] if isinstance(kws_raw, list) else []
            if not kws:
                continue
            if isinstance(path_raw, list):
                path = [str(x).strip() for x in path_raw if str(x).strip()]
            elif isinstance(path_raw, str):
                path = [p.strip() for p in path_raw.split(">") if p.strip()]
            else:
                path = []
            if not path:
                continue
            score = sum(1 for kw in kws if kw in query or kw == leaf_lower)
            if score > best_score:
                best_score = score
                best_path = path
    if best_score > 0 and best_path:
        return best_path

    # --- Pass 2 & 3: CSV exact match ---
    try:
        csv_path = _resolve_catalog_csv_path(cfg, selectors_cfg_path)
        catalog = _load_takealot_catalog(csv_path)
    except Exception:
        return partial_path
    if not catalog:
        return partial_path

    leaf_norm = _norm_text(_strip_category_id(partial_path[-1]))
    if not leaf_norm:
        return partial_path

    # Pass 2: exact match on lowest
    for row in catalog:
        if _norm_text(_strip_category_id(str(row.get("lowest", "")))) == leaf_norm:
            full = [_strip_category_id(p).strip() for p in row.get("path", []) if p]
            if full:
                return full

    # Pass 3: exact match on main
    for row in catalog:
        if _norm_text(_strip_category_id(str(row.get("main", "")))) == leaf_norm:
            full = [_strip_category_id(p).strip() for p in row.get("path", []) if p]
            if full:
                return full

    return partial_path


# 已知的非认证类 cookie 名称（GA / Cloudflare / 其他追踪 cookie），存在这些 cookie 不代表已登录
_TAKEALOT_NON_AUTH_COOKIE_NAMES: frozenset = frozenset({
    "__cf_bm", "_cfuvid", "wfx_unq", "_gid", "_fbp",
})


def _takealot_state_has_auth_cookie(state_path: str) -> bool:
    """
    快速检查 Playwright storage_state 文件中是否含有 Takealot 认证凭证。
    Takealot 把 auth JWT 存在 localStorage（usr_st_auth），不是 cookie，
    因此同时检查 origins.localStorage 和 cookie 两种来源。
    不打开浏览器，<1ms 完成。
    """
    try:
        p = Path(state_path)
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        # 1) 优先检查 localStorage（Takealot 主要认证方式）
        _AUTH_LS_KEYS = frozenset({"usr_st_auth", "usr_st_usr", "usr_st_slr"})
        for origin in data.get("origins", []):
            if "sellers.takealot.com" in str(origin.get("origin", "")):
                ls = origin.get("localStorage", [])
                if any(item.get("name") in _AUTH_LS_KEYS for item in ls):
                    return True
        # 2) 兼容：检查非追踪类 cookie
        cookies = data.get("cookies", [])
        for c in cookies:
            domain = str(c.get("domain", "")).lower()
            name = str(c.get("name", ""))
            if "takealot" not in domain:
                continue
            if name in _TAKEALOT_NON_AUTH_COOKIE_NAMES:
                continue
            if name.startswith("_ga"):
                continue
            return True
        return False
    except Exception:
        return False


def probe_category_fields(
    category_path: list[str],
    selectors_cfg_path: str | Path,
    headless: bool = True,
    browser_channel: str = "msedge",
    user_data_dir: str | None = None,
    storage_state_path: str | None = None,
    browser_profile_directory: str = "Default",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    打开 Takealot 卖家后台 → 选择类目 → 变体选 None → 点 Next
    → 抓取所有属性字段的名称/类型/必填/下拉选项
    → 以 JSON 缓存到 input/portal_fields/<category>.json

    同一类目下次直接从缓存读取，不再打开浏览器。

    返回:
        {
            "category_key": str,
            "category_path": [...],
            "fields": [
                {"label": ..., "required": bool, "type": ..., "options": [...], "hint": ..., "section": ...},
                ...
            ],
            "required_labels": [...],   # 仅必填字段的标签列表，方便快速查询
            "probed_at": "ISO timestamp",
        }
    """
    import datetime

    category_key = " > ".join(str(x).strip() for x in category_path if x)
    if not force_refresh:
        cached = load_probed_fields(category_key)
        if cached:
            print(f"[probe] 使用缓存：{category_key}")
            return cached

    # 路径过短时（只有叶子名），自动从 takealot_categories.csv 扩展为完整层级
    if len(category_path) <= 2:
        expanded = _find_full_portal_path(category_path, selectors_cfg_path)
        if expanded != category_path:
            print(f"[probe] 类目路径扩展: {category_path} → {expanded}")
            category_path = expanded
            category_key = " > ".join(str(x).strip() for x in category_path if x)
            if not force_refresh:
                cached = load_probed_fields(category_key)
                if cached:
                    print(f"[probe] 使用缓存（扩展后）：{category_key}")
                    return cached

    # 快速预检：如果 storage_state_path 指定但没有有效认证 cookie，立即返回 need_login，
    # 无需启动浏览器（避免 10-30 秒的浏览器启动 + 页面加载 + 等待时间）。
    if storage_state_path and not _takealot_state_has_auth_cookie(storage_state_path):
        print("[probe] ✗ 未登录 Takealot，无法探测，请在 UI 中点击登录 Takealot.")
        import datetime as _dt
        return {
            "category_key":    category_key,
            "category_path":   category_path,
            "fields":          [],
            "required_labels": [],
            "probed_at":       _dt.datetime.now().isoformat(timespec="seconds"),
            "error":           "need_login",
        }

    print(f"[probe] 开始探测类目字段：{category_key}")
    cfg = load_selectors(selectors_cfg_path)
    add_product_url = cfg.get("portal", {}).get(
        "add_product_url", "https://sellers.takealot.com/single-product"
    )
    # probe_headless 优先级：selectors.yaml > 调用方传入的 headless 参数
    probe_headless_cfg = cfg.get("portal", {}).get("probe_headless", None)
    if probe_headless_cfg is not None:
        headless = _to_bool(probe_headless_cfg, headless)
    probe_stay_open = int(cfg.get("portal", {}).get("probe_stay_open_seconds", 0) or 0)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        f"--profile-directory={browser_profile_directory}",
    ]

    fields: list[dict[str, Any]] = []
    error: str = ""

    browser: Any = None
    ctx: Any = None
    page: Any = None
    try:
        with sync_playwright() as play:
            if storage_state_path:
                browser = play.chromium.launch(
                    headless=headless,
                    channel=browser_channel if browser_channel else None,
                    ignore_default_args=["--enable-automation"],
                    args=launch_args,
                )
                ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 1700}}
                state_file = Path(storage_state_path)
                if state_file.exists():
                    ctx_kwargs["storage_state"] = str(state_file)
                try:
                    ctx = browser.new_context(**ctx_kwargs)
                except Exception as ctx_err:
                    print(f"[probe] ⚠️  加载 session 失败，以匿名模式继续：{ctx_err}")
                    ctx_kwargs.pop("storage_state", None)
                    ctx = browser.new_context(**ctx_kwargs)
            elif user_data_dir:
                ctx = play.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=headless,
                    channel=browser_channel if browser_channel else None,
                    ignore_default_args=["--enable-automation"],
                    args=launch_args,
                    viewport={"width": 1440, "height": 1700},
                )
                browser = None
            else:
                browser = play.chromium.launch(
                    headless=headless,
                    channel=browser_channel if browser_channel else None,
                    ignore_default_args=["--enable-automation"],
                    args=launch_args,
                )
                ctx = browser.new_context(viewport={"width": 1440, "height": 1700})

            page = ctx.new_page()
            page.goto(add_product_url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # 检测是否需要登录
            if _check_login_required(page, cfg):
                if not headless and probe_stay_open > 0:
                    print(f"[probe] ⚠️  未登录 Takealot！请在弹出窗口中登录，最多等待 {probe_stay_open}s...")
                    deadline = time.time() + probe_stay_open
                    while time.time() < deadline:
                        page.wait_for_timeout(2000)
                        if not _check_login_required(page, cfg):
                            print("[probe] ✅  登录成功，继续探测...")
                            # 保存新的 session
                            if storage_state_path:
                                try:
                                    state = ctx.storage_state()
                                    Path(storage_state_path).write_text(
                                        json.dumps(state, ensure_ascii=False), encoding="utf-8"
                                    )
                                    print(f"[probe] 已更新登录状态：{storage_state_path}")
                                except Exception:
                                    pass
                            page.goto(add_product_url, timeout=60000, wait_until="domcontentloaded")
                            page.wait_for_timeout(2500)
                            page.wait_for_timeout(1500)
                            break
                    else:
                        error = "need_login"
                        print("[probe] ❌  等待登录超时，放弃探测。请在 UI 中重新登录 Takealot。")
                else:
                    error = "need_login"
                    print("[probe] ❌  未登录 Takealot，无法探测。请在 UI 中点击登录 Takealot。")

            if not error:
                # 选类目
                ok = _complete_category_by_path(page, category_path)
                if not ok:
                    error = "category_selection_failed"
                    print(f"[probe] 类目选择失败：{category_path}")
                else:
                    # 等待 Product Category section 的 Next 出现并点击
                    page.wait_for_timeout(800)
                    _click_section_next(page, "Product Category")
                    page.wait_for_timeout(1000)

                    # 选变体 None
                    try:
                        none_opt = page.locator(
                            "section[data-sectionname='Product Variants'] "
                            ".ZorkMillerColumns__item:has-text('None')"
                        ).first
                        if none_opt.count() > 0:
                            none_opt.click(timeout=3000)
                            page.wait_for_timeout(600)
                    except Exception:
                        pass
                    _click_section_next(page, "Product Variants")
                    page.wait_for_timeout(1200)

                    # 抓取字段
                    fields = _scrape_attributes_fields(page)
                    print(f"[probe] 抓取到 {len(fields)} 个字段")

                    # 补充抓取 combobox 下拉选项（点击展开后收集）
                    _probe_fill_combobox_options(page, fields)

            # 失败时保持窗口让用户查看
            if error and not headless and probe_stay_open > 0:
                print(f"[probe] 探测失败，浏览器将保持 {probe_stay_open}s 供查看，之后自动关闭…")
                page.wait_for_timeout(probe_stay_open * 1000)

            page.close()
            if browser:
                browser.close()

    except Exception as exc:
        error = str(exc)
        print(f"[probe] 探测异常：{exc}")
        # 确保浏览器窗口被关闭，不残留空白窗口
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    # 针对特定类目做 required 字段补丁（例如 Nail Tools 的 Hand Foot And Nail Tool Type / Main Material）。
    _patch_required_fields_for_category(category_path, fields)

    result: dict[str, Any] = {
        "category_key":     category_key,
        "category_path":    category_path,
        "fields":           fields,
        "required_labels":  [f["label"] for f in fields if f.get("required")],
        "probed_at":        datetime.datetime.now().isoformat(timespec="seconds"),
        "error":            error,
    }

    if fields:   # 只有成功拿到字段才缓存
        _save_probed_fields(category_key, result)
        print(f"[probe] 已缓存到 {_probe_cache_path(category_key)}")

    return result


def automate_listing(
    draft: ListingDraft,
    image_paths: list[Path],
    selectors_cfg_path: str | Path,
    run_dir: Path,
    mode: str = "draft",
    headless: bool = True,
    browser_channel: str = "msedge",
    user_data_dir: str | None = None,
    storage_state_path: str | None = None,
    timeout_ms: int = 120000,
    login_wait_seconds: int = 0,
    browser_profile_directory: str = "Default",
    source_title: str = "",
    source_category_path: list[str] | None = None,
) -> dict[str, Any]:
    cfg = load_selectors(selectors_cfg_path)
    listing_url = cfg.get("portal", {}).get("listing_url", "https://sellers.takealot.com")
    add_product_url = cfg.get("portal", {}).get("add_product_url", "https://sellers.takealot.com/single-product")
    stay_open_on_error_headed = _to_bool(cfg.get("portal", {}).get("stay_open_on_error_headed", True), True)
    try:
        stay_open_seconds_on_error_headed = int(
            cfg.get("portal", {}).get("stay_open_seconds_on_error_headed", 35) or 35
        )
    except Exception:
        stay_open_seconds_on_error_headed = 35
    launch_selectors = cfg.get("portal", {}).get(
        "launch_add_product_selectors",
        ["text=Add a Product", "a:has-text('Add a Product')", "button:has-text('Add a Product')"],
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "listing_url": listing_url,
        "add_product_url": add_product_url,
        "mode": mode,
        "filled": {},
        "attribute_fill": {},
        "ai_fill": {},
        "ai_fill_error": {},
        "clicked": None,
        "warnings": [],
    }

    with sync_playwright() as play:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            f"--profile-directory={browser_profile_directory}",
        ]
        if storage_state_path:
            browser = play.chromium.launch(
                headless=headless,
                channel=browser_channel if browser_channel else None,
                ignore_default_args=["--enable-automation"],
                args=launch_args,
            )
            ctx_kwargs = {"viewport": {"width": 1440, "height": 1700}}
            state_file = Path(storage_state_path)
            if state_file.exists():
                ctx_kwargs["storage_state"] = str(state_file)
            context = browser.new_context(**ctx_kwargs)
        elif user_data_dir:
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
            context = play.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel=browser_channel if browser_channel else None,
                headless=headless,
                viewport={"width": 1440, "height": 1700},
                ignore_default_args=["--enable-automation"],
                args=launch_args,
            )
        else:
            browser = play.chromium.launch(
                headless=headless,
                channel=browser_channel if browser_channel else None,
                ignore_default_args=["--enable-automation"],
                args=launch_args,
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1700})

        try:
            page = context.new_page()
            page.goto(listing_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)

            if _check_login_required(page, cfg) and (not headless) and login_wait_seconds > 0:
                page.bring_to_front()
                deadline = time.time() + login_wait_seconds
                while time.time() < deadline:
                    if not _check_login_required(page, cfg):
                        break
                    page.wait_for_timeout(2000)

            if _check_login_required(page, cfg):
                page.screenshot(path=str(run_dir / "portal_need_login.png"), full_page=True)
                raise NeedLoginError(
                    "NEED_LOGIN: please complete Takealot seller login in this browser window, then rerun."
                )

            if "single-product" not in page.url:
                for sel in launch_selectors:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=5000)
                            page.wait_for_timeout(2500)
                            if "single-product" in page.url:
                                break
                    except Exception:
                        continue

            # Fallback: direct open Add Product form url when menu/button selectors are not present.
            if "single-product" not in page.url:
                try:
                    page.goto(str(add_product_url), wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(2200)
                except Exception:
                    pass

            if "single-product" not in page.url:
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                evidence["current_url"] = str(page.url or "")
                try:
                    evidence["page_title"] = str(page.title() or "")
                except Exception:
                    evidence["page_title"] = ""
                raise PortalFormNotReadyError(
                    "PORTAL_FORM_NOT_READY: 已登录，但未进入 Add a Product 表单页。已尝试菜单点击和直达链接。请检查账号权限或页面状态后重试。"
                )

            strategy = str(cfg.get("portal", {}).get("category_strategy", "path_first")).strip().lower()
            allow_fallback = bool(cfg.get("portal", {}).get("allow_category_heuristic_fallback", False))
            category_path, category_match = _resolve_category_path(
                cfg,
                draft,
                selectors_cfg_path=selectors_cfg_path,
                source_title=source_title,
                source_category_path=source_category_path or [],
            )
            evidence["category_path_used"] = category_path
            evidence["category_match"] = category_match

            category_ok = False
            if strategy in ("strict_path", "path_first"):
                if category_path:
                    category_ok = _complete_category_by_path(page, category_path)
                elif strategy == "strict_path":
                    category_ok = False
                if (not category_ok) and (strategy == "path_first") and allow_fallback:
                    category_ok = _complete_category_section_heuristic(page, draft)
            else:
                category_ok = _complete_category_section_heuristic(page, draft)

            if not category_ok:
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                missing = _detect_incomplete_sections(page)
                details = f" 缺失步骤: {', '.join(missing)}。" if missing else ""
                cols = _category_columns_snapshot(page)
                (run_dir / "category_options.json").write_text(
                    json.dumps(
                        {
                            "category_path_used": category_path,
                            "category_match": category_match,
                            "category_strategy": strategy,
                            "columns": cols,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                path_hint = ""
                if strategy == "strict_path":
                    path_hint = (
                        f" 当前类目路径配置: {' > '.join(category_path) if category_path else '(未配置)'}。"
                        " 请在 config/selectors.yaml 的 portal.category_path 配置正确路径，"
                        "或在 UI 里填写“类目路径(>)”。"
                    )
                raise PortalFormNotReadyError(
                    f"PORTAL_FORM_NOT_READY: 类目未自动选定成功。请在 Product Category 里先选类目。{details}{path_hint} 已导出 category_options.json。"
                )
            evidence["filled"]["category"] = True
            evidence["selected_category"] = _read_selected_category(page)
            if category_path and (not _selected_category_matches_path(evidence["selected_category"], category_path)):
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                raise PortalFormNotReadyError(
                    "PORTAL_FORM_NOT_READY: 类目未准确命中到配置路径末级，请检查三级类目映射。"
                )

            variant_choice, variant_debug = _fill_variants(page, draft)
            evidence["variant_choice"] = variant_choice
            evidence["variant_debug"] = variant_debug
            if draft.variants and _variant_choice_is_none(variant_choice):
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                raise PortalFormNotReadyError(
                    "PORTAL_FORM_NOT_READY: 检测到草稿包含变体，但 Product Variants 仍为 None。请检查变体选项是否正确提交。"
                )

            variant_rows_debug = _fill_variant_rows(page, draft, image_paths)
            evidence["variant_rows"] = variant_rows_debug
            variant_err = _count_required_errors(page, "Product Variants")
            evidence["filled"]["variants_required_errors"] = variant_err
            evidence["filled"]["variant_images_uploaded"] = int(variant_rows_debug.get("images_uploaded", 0) or 0)
            if draft.variants:
                rows_after_trim = int(variant_rows_debug.get("rows_after_trim", 0) or 0)
                unique_colours = {
                    _normalize_text(str(v.get("colour") or v.get("color") or ""))
                    for v in draft.variants
                    if isinstance(v, dict)
                }
                unique_colours = {x for x in unique_colours if x}
                expected_images = len(unique_colours) if unique_colours else len(draft.variants)
                expected_images = max(expected_images, rows_after_trim)
                if rows_after_trim > len(draft.variants):
                    evidence["warnings"].append("variant_rows_extra")
                if rows_after_trim < len(draft.variants):
                    page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                    raise PortalFormNotReadyError(
                        "PORTAL_FORM_NOT_READY: Product Variants 行数不足，未按草稿生成完整变体行。"
                    )
                if int(variant_rows_debug.get("images_uploaded", 0) or 0) < expected_images:
                    page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                    raise PortalFormNotReadyError(
                        "PORTAL_FORM_NOT_READY: Product Variants 变体图上传不足，未达到每个颜色变体至少一张图。"
                    )
                if variant_err > 0:
                    page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                    raise PortalFormNotReadyError(
                        "PORTAL_FORM_NOT_READY: Product Variants 仍有必填或下拉错误，未完成变体填写。"
                    )
                if _has_variants_missing_values(page):
                    page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                    raise PortalFormNotReadyError(
                        "PORTAL_FORM_NOT_READY: Product Variants 仍提示存在缺失值（红框校验未通过）。"
                    )
            if not _advance_variants_section(page):
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                raise PortalFormNotReadyError(
                    "PORTAL_FORM_NOT_READY: Product Variants 未能进入下一步（Next 未生效）。请检查 Variants 下拉值是否已确认。"
                )
            evidence["filled"]["variants"] = True

            missing_required = _ensure_required_sections_filled(page, draft, evidence)
            if missing_required:
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                raise PortalFormNotReadyError(
                    "PORTAL_FORM_NOT_READY: 必填模块未填完。缺失步骤: " + ", ".join(missing_required)
                )

            img_ok, img_warn = _upload_images_in_section(page, image_paths)
            evidence["filled"]["images"] = img_ok
            if img_warn:
                evidence["warnings"].append(img_warn)
            if _section_next_enabled(page, "Product Images"):
                _click_section_next(page, "Product Images")
                page.wait_for_timeout(500)

            # Business rule: Product Identifiers usually not required for this flow; avoid noisy auto-fill.
            if _section_next_enabled(page, "Product Identifiers"):
                _click_section_next(page, "Product Identifiers")
                page.wait_for_timeout(500)

            ok_action, action_tag = _finalize_action(page, mode=mode)
            if not ok_action:
                page.screenshot(path=str(run_dir / "portal_form_not_ready.png"), full_page=True)
                missing = _detect_incomplete_sections(page)
                details = f" 缺失步骤: {', '.join(missing)}。" if missing else ""
                raise PortalFormNotReadyError(
                    f"PORTAL_FORM_NOT_READY: 最终提交按钮不可用（{action_tag}）。{details}"
                )
            evidence["clicked"] = action_tag

            page.wait_for_timeout(3000)
            page.screenshot(path=str(run_dir / "portal_after_submit.png"), full_page=True)
            (run_dir / "portal_result.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return evidence
        except (PortalFormNotReadyError, NeedLoginError) as e:
            evidence["error"] = str(e)
            try:
                (run_dir / "portal_result.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            except Exception:
                pass
            if (not headless) and stay_open_on_error_headed and stay_open_seconds_on_error_headed > 0:
                try:
                    page.bring_to_front()
                    page.wait_for_timeout(stay_open_seconds_on_error_headed * 1000)
                except Exception:
                    pass
            raise
        finally:
            context.close()
