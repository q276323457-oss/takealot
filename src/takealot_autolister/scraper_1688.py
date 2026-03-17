from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from .types import ProductSource


class Need1688LoginError(RuntimeError):
    pass


class Need1688VerificationError(RuntimeError):
    pass


class Need1688RetryError(RuntimeError):
    pass


def _is_transient_page_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "execution context was destroyed",
        "cannot find context with specified id",
        "context was destroyed",
        "most likely because of a navigation",
        "target page, context or browser has been closed",
    )
    return any(x in msg for x in markers)


def _is_transient_network_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "err_connection_reset",
        "err_timed_out",
        "err_network_changed",
        "err_connection_closed",
        "err_name_not_resolved",
        "err_internet_disconnected",
    )
    return any(x in msg for x in markers)


def _evaluate_with_retry(page, script: str, attempts: int = 3, wait_ms: int = 800):
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            return page.evaluate(script)
        except Exception as exc:
            last_exc = exc
            if (not _is_transient_page_error(exc)) or i == attempts - 1:
                break
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                time.sleep(wait_ms / 1000.0)
    if last_exc:
        raise last_exc
    raise RuntimeError("page evaluate failed")


def _goto_with_retry(context, url: str, attempts: int = 3):
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return page
        except Exception as exc:
            last_exc = exc
            try:
                page.close()
            except Exception:
                pass
            if _is_transient_network_error(exc) and i < attempts - 1:
                time.sleep(2.0 + i)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("page goto failed")


def _collect_text(page, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            txt = page.locator(selector).first.inner_text(timeout=2000).strip()
            if txt:
                return re.sub(r"\s+", " ", txt)
        except Exception:
            continue
    return ""


def _collect_title(page) -> str:
    try:
        title = _evaluate_with_retry(
            page,
            """
(() => {
  const h1 = document.querySelector('h1');
  if (h1 && h1.textContent && h1.textContent.trim()) return h1.textContent.trim();
  const og = document.querySelector("meta[property='og:title']");
  if (og && og.content) return og.content.trim();
  return (document.title || '').trim();
})()
""",
        )
        return re.sub(r"\s+", " ", str(title or "")).strip()
    except Exception:
        return ""


def _collect_subject_from_html(page) -> str:
    try:
        html = page.content()
    except Exception:
        return ""
    m = re.search(r'"subject":"((?:\\.|[^"\\])*)"', html)
    if not m:
        return ""
    raw = m.group(1)
    try:
        txt = json.loads(f"\"{raw}\"")
    except Exception:
        txt = raw
    return re.sub(r"\s+", " ", str(txt or "")).strip()


def _collect_images(page) -> list[str]:
    script = """
(() => {
  const out = [];
  const seen = new Set();
  const norm = (u) => {
    try {
      const x = new URL(u);
      x.hash = '';
      return x.toString();
    } catch (e) {
      return String(u || '');
    }
  };
  const nodes = Array.from(document.querySelectorAll('img'));
  for (const img of nodes) {
    const cands = [img.src, img.getAttribute('data-src'), img.getAttribute('data-lazy-src')]
      .filter(Boolean);
    for (const u of cands) {
      if (typeof u !== 'string' || !/^https?:\/\//.test(u)) continue;
      const key = norm(u);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      const w = Number(img.naturalWidth || img.width || 0);
      const h = Number(img.naturalHeight || img.height || 0);
      const cls = (img.className || '').toString();
      const alt = (img.alt || '').toString();
      out.push({ url: key, w, h, cls, alt });
    }
  }
  out.sort((a, b) => (b.w * b.h) - (a.w * a.h));
  return out;
})()
"""
    try:
        rows = _evaluate_with_retry(page, script, attempts=4, wait_ms=900)
    except Exception:
        return []
    cleaned: list[str] = []
    if not isinstance(rows, list):
        return cleaned
    for item in rows:
        if isinstance(item, dict):
            s = str(item.get("url", ""))
            w = int(item.get("w", 0) or 0)
            h = int(item.get("h", 0) or 0)
            cls = str(item.get("cls", ""))
            alt = str(item.get("alt", ""))
        else:
            s = str(item)
            w = h = 0
            cls = alt = ""
        if "alicdn.com/imgextra" not in s and "alicdn.com" not in s:
            continue
        low = s.lower()
        txt = f"{cls} {alt}".lower()
        if any(x in low for x in ["logo", "avatar", "icon", "qrcode", "sprite", "arrow", "btn", "loading"]):
            continue
        if any(x in txt for x in ["logo", "avatar", "icon", "qrcode", "arrow", "loading"]):
            continue
        if any(x in s for x in ["-2-tps-64-64", "-2-tps-87-32", "-2-tps-400-156"]):
            continue
        # Exclude tiny page icons; keep product gallery-like images only.
        if w and h and min(w, h) < 120:
            continue
        cleaned.append(s)
    return cleaned[:30]


def _collect_sku_texts(page) -> list[str]:
    script = """
(() => {
  const out = new Set();
  const cands = Array.from(document.querySelectorAll('button,li,span,div'));
  for (const el of cands) {
    const txt = (el.textContent || '').trim();
    if (!txt) continue;
    if (txt.length > 40) continue;
    if (/color|colour|size|尺寸|颜色|规格|型号/i.test(txt)) out.add(txt);
  }
  return Array.from(out).slice(0, 80);
})()
"""
    try:
        out = _evaluate_with_retry(page, script, attempts=3, wait_ms=700)
        if isinstance(out, list):
            return [str(x) for x in out]
        return []
    except Exception:
        return []


def _collect_category_path(page) -> list[str]:
    script = """
(() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  const sels = [
    "[class*='breadcrumb'] a",
    "[class*='crumb'] a",
    "[class*='nav'] a[title]",
    "a[data-spm-anchor-id*='breadcrumb']",
  ];
  const push = (t) => {
    const x = norm(t);
    if (!x) return;
    if (x.length > 60) return;
    if (/^home$/i.test(x)) return;
    if (!out.includes(x)) out.push(x);
  };
  for (const sel of sels) {
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const n of nodes) push(n.textContent || n.getAttribute('title') || '');
    if (out.length >= 2) break;
  }
  return out.slice(0, 8);
})()
"""
    try:
        out = _evaluate_with_retry(page, script, attempts=3, wait_ms=700)
        if isinstance(out, list):
            cleaned = [str(x).strip() for x in out if str(x).strip()]
            if cleaned:
                return cleaned
    except Exception:
        pass
    # Fallback: parse embedded offer JSON in page html.
    try:
        html = page.content()
        m_leaf = re.search(r'"leafCategoryName":"([^"]+)"', html)
        m_top = re.search(r'"topCategoryName":"([^"]+)"', html)
        out2: list[str] = []
        if m_top:
            out2.append(m_top.group(1).strip())
        if m_leaf:
            leaf = m_leaf.group(1).strip()
            if leaf and leaf not in out2:
                out2.append(leaf)
        if out2:
            return out2
    except Exception:
        pass
    return []


def _is_1688_verification_page(page) -> bool:
    try:
        html = page.content().lower()
    except Exception:
        html = ""
    markers = (
        "fourier.alibaba.com/fb",
        "x5secdata",
        "x-bx-resend",
        "window.themis",
    )
    if any(m in html for m in markers):
        return True
    try:
        u = page.url.lower()
    except Exception:
        u = ""
    if "verify" in u or "captcha" in u:
        return True
    return False




def _collect_packaging_info(page) -> list[dict[str, str]]:
    """抓取 1688 商品详情页「包装信息」表格，返回每行数据列表。

    返回示例：
        [{"variant": "6TB", "length_cm": "12.5", "width_cm": "9",
          "height_cm": "2", "weight_g": "80"},  ...]
    """
    script = r"""
(() => {
  // 找包含 长/宽/高/重量 表头的表格
  const KEY_MAP = {
    '长': 'length_cm', '长(cm)': 'length_cm',
    '宽': 'width_cm',  '宽(cm)': 'width_cm',
    '高': 'height_cm', '高(cm)': 'height_cm',
    '重量': 'weight_g', '重量(g)': 'weight_g',
    '体积': 'volume_cm3', '体积(cm³)': 'volume_cm3',
  };

  const tables = Array.from(document.querySelectorAll('table'));
  for (const table of tables) {
    const rows = Array.from(table.querySelectorAll('tr'));
    if (rows.length < 2) continue;

    // 尝试读表头
    const headerCells = Array.from(rows[0].querySelectorAll('th, td'));
    const headers = headerCells.map(c => (c.textContent || '').trim());

    // 必须包含至少一个尺寸/重量关键词
    const hasSpec = headers.some(h => h.includes('长') || h.includes('宽') || h.includes('重量'));
    if (!hasSpec) continue;

    // 归一化表头
    const normHeaders = headers.map(h => {
      for (const [zh, en] of Object.entries(KEY_MAP)) {
        if (h.includes(zh)) return en;
      }
      return h; // 保留原文（如 容量/规格 等变体列）
    });

    const result = [];
    for (let i = 1; i < rows.length; i++) {
      const cells = Array.from(rows[i].querySelectorAll('td'));
      if (cells.length < 2) continue;
      const row = {};
      cells.forEach((cell, idx) => {
        const val = (cell.textContent || '').trim();
        const key = normHeaders[idx] || `col${idx}`;
        if (val) row[key] = val;
      });
      // 统一变体列名
      for (const k of Object.keys(row)) {
        if (!Object.values(KEY_MAP).includes(k) && k !== 'variant') {
          row['variant'] = row['variant'] ? row['variant'] + '/' + row[k] : row[k];
          if (k !== 'variant') delete row[k];
        }
      }
      if (Object.keys(row).length > 1) result.push(row);
    }
    if (result.length > 0) return result;
  }
  return [];
})()
"""
    try:
        result = _evaluate_with_retry(page, script, attempts=3, wait_ms=500)
        if isinstance(result, list):
            return [
                {str(k).strip(): str(v).strip() for k, v in row.items() if v}
                for row in result if isinstance(row, dict)
            ]
    except Exception:
        pass
    return []


def _collect_product_attrs(page) -> dict[str, str]:
    """抓取 1688 商品详情页的「商品属性」表格，返回 {属性名: 属性值} 字典。"""
    script = r"""
(() => {
  const out = {};
  // 1688 商品属性表格有多种 DOM 结构，逐一尝试
  // 方案A：table 里 td 两两配对（奇数列=key, 偶数列=value）
  const tables = Array.from(document.querySelectorAll(
    '.detail-prop-item, .product-prop-item, ' +
    'table.attributes-list, table.detail-attributes, ' +
    '.offer-attr-item, .prop-item'
  ));
  if (tables.length === 0) {
    // 方案B：更宽泛的 table 选取，找包含商品属性的表
    const allTables = Array.from(document.querySelectorAll('table'));
    for (const t of allTables) {
      const rows = Array.from(t.querySelectorAll('tr'));
      for (const row of rows) {
        const cells = Array.from(row.querySelectorAll('td, th'));
        for (let i = 0; i + 1 < cells.length; i += 2) {
          const k = (cells[i].textContent || '').trim().replace(/:$/, '');
          const v = (cells[i+1].textContent || '').trim();
          if (k && v && k.length < 30 && v.length < 200) out[k] = v;
        }
      }
    }
  } else {
    for (const item of tables) {
      const label = item.querySelector('.label, .prop-name, dt, .key');
      const value = item.querySelector('.value, .prop-value, dd, .val');
      if (label && value) {
        const k = (label.textContent || '').trim().replace(/:$/, '');
        const v = (value.textContent || '').trim();
        if (k && v) out[k] = v;
      }
    }
  }
  // 方案C：从 JSON 嵌入数据提取属性（部分页面）
  if (Object.keys(out).length === 0) {
    const scripts = Array.from(document.querySelectorAll('script'));
    for (const s of scripts) {
      const txt = s.textContent || '';
      const m = txt.match(/"attributes"\s*:\s*(\[[\s\S]{0,5000}?\])/);
      if (m) {
        try {
          const attrs = JSON.parse(m[1]);
          for (const a of attrs) {
            const k = a.attrName || a.name || a.attributeName || '';
            const v = a.attrValue || a.value || a.attributeValue || '';
            if (k && v) out[String(k).trim()] = String(v).trim();
          }
        } catch(e) {}
        if (Object.keys(out).length > 0) break;
      }
    }
  }
  return out;
})()
"""
    try:
        result = _evaluate_with_retry(page, script, attempts=3, wait_ms=500)
        if isinstance(result, dict):
            return {str(k).strip(): str(v).strip() for k, v in result.items()
                    if str(k).strip() and str(v).strip()}
    except Exception:
        pass
    return {}


def _prepare_context(
    play,
    headless: bool,
    channel: str,
    user_data_dir: str | None,
    storage_state_path: str | None = None,
    browser_profile_directory: str = "Default",
) -> BrowserContext:
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        f"--profile-directory={browser_profile_directory}",
    ]
    if storage_state_path:
        browser = play.chromium.launch(
            channel=channel if channel else None,
            headless=headless,
            ignore_default_args=["--enable-automation"],
            args=launch_args,
        )
        ctx_kwargs = {"viewport": {"width": 1440, "height": 1800}}
        state_file = Path(storage_state_path)
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        return browser.new_context(**ctx_kwargs)

    if user_data_dir:
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        return play.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel=channel if channel else None,
            headless=headless,
            viewport={"width": 1440, "height": 1800},
            ignore_default_args=["--enable-automation"],
            args=launch_args,
        )

    browser = play.chromium.launch(
        channel=channel if channel else None,
        headless=headless,
        ignore_default_args=["--enable-automation"],
        args=launch_args,
    )
    return browser.new_context(viewport={"width": 1440, "height": 1800})


def scrape_1688_product(
    url: str,
    run_dir: Path,
    headless: bool = True,
    browser_channel: str = "msedge",
    user_data_dir: str | None = None,
    storage_state_path: str | None = None,
    login_wait_seconds: int = 0,
    browser_profile_directory: str = "Default",
) -> ProductSource:
    run_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        context = _prepare_context(
            play,
            headless=headless,
            channel=browser_channel,
            user_data_dir=user_data_dir,
            storage_state_path=storage_state_path,
            browser_profile_directory=browser_profile_directory,
        )
        try:
            try:
                page = _goto_with_retry(context, url, attempts=3)
            except Exception as exc:
                if _is_transient_network_error(exc):
                    raise Need1688RetryError(
                        "NEED_RETRY_1688: 1688 connection unstable (ERR_CONNECTION_RESET/timeout). Please retry in 1-2 minutes."
                    ) from exc
                raise

            def is_login_required() -> bool:
                title_now = _collect_title(page).lower()
                current_url = page.url.lower()
                login_markers = ["保持登录状态", "请登录", "login", "账号登录", "验证码登录"]
                return "login.1688.com" in current_url or any(m.lower() in title_now for m in login_markers)

            if is_login_required() and (not headless) and login_wait_seconds > 0:
                page.bring_to_front()
                deadline = time.time() + login_wait_seconds
                while time.time() < deadline:
                    if not is_login_required():
                        break
                    page.wait_for_timeout(2000)

            if is_login_required():
                page.screenshot(path=str(run_dir / "1688_need_login.png"), full_page=True)
                raise Need1688LoginError(
                    "NEED_LOGIN_1688: please complete 1688 login in this browser window, then rerun."
                )

            if _is_1688_verification_page(page) and (not headless) and login_wait_seconds > 0:
                page.bring_to_front()
                deadline = time.time() + login_wait_seconds
                while time.time() < deadline:
                    if not _is_1688_verification_page(page):
                        break
                    page.wait_for_timeout(2000)

            if _is_1688_verification_page(page):
                page.screenshot(path=str(run_dir / "1688_need_verify.png"), full_page=True)
                raise Need1688VerificationError(
                    "NEED_VERIFY_1688: 1688 risk verification page detected. Please complete slider/captcha in headed mode, then rerun."
                )

            title = _collect_title(page)
            subject = _collect_subject_from_html(page)
            if subject and (not title or "有限公司" in title or "供应链" in title):
                title = subject
            subtitle = _collect_text(page, [
                "[class*='subtitle']",
                "[class*='sub-title']",
            ])
            price_text = _collect_text(page, [
                "[class*='price']",
                "span:has-text('¥')",
            ])
            description = _collect_text(page, [
                "[class*='detail']",
                "[class*='desc']",
            ])
            category_path = _collect_category_path(page)

            image_urls = _collect_images(page)
            sku_options = _collect_sku_texts(page)
            product_attrs = _collect_product_attrs(page)
            packaging_info = _collect_packaging_info(page)

            screenshot_path = run_dir / "1688_page.png"
            html_path = run_dir / "1688_page.html"
            page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(page.content(), encoding="utf-8")

            source = ProductSource(
                source_url=url,
                title=title,
                category_path=category_path,
                subtitle=subtitle,
                description=description,
                price_text=price_text,
                image_urls=image_urls,
                sku_options=sku_options,
                product_attrs=product_attrs,
                packaging_info=packaging_info,
                raw={
                    "captured_title": title,
                    "captured_subject": subject,
                    "captured_category_path": category_path,
                    "captured_subtitle": subtitle,
                    "captured_price_text": price_text,
                    "screenshot": str(screenshot_path),
                    "html": str(html_path),
                },
            )

            (run_dir / "source.json").write_text(
                json.dumps(source.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return source
        finally:
            context.close()
