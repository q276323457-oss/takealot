from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

# macOS system Python (LibreSSL) triggers urllib3 NotOpenSSLWarning in requests stack.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+.*")


class LoginNotCompletedError(RuntimeError):
    pass


_A1688_AUTH_COOKIE_HINTS = {
    "_nk_",
    "cookie2",
    "_tb_token_",
    "havana_lgc2_0",
    "cookie1",
    "cookie17",
}

_TAKEALOT_AUTH_LS_KEYS = {"usr_st_auth", "usr_st_usr", "usr_st_slr"}


def _has_1688_auth_cookie(page: Page) -> bool:
    try:
        cookies = page.context.cookies(
            ["https://www.1688.com", "https://detail.1688.com", "https://work.1688.com"]
        )
    except Exception:
        return False
    names = {str(c.get("name", "")).strip().lower() for c in cookies if isinstance(c, dict)}
    return any(n in names for n in _A1688_AUTH_COOKIE_HINTS)


def _has_takealot_auth_localstorage(page: Page) -> bool:
    try:
        keys = page.evaluate("Object.keys(localStorage || {})") or []
    except Exception:
        return False
    key_set = {str(k).strip() for k in keys if str(k).strip()}
    return any(k in key_set for k in _TAKEALOT_AUTH_LS_KEYS)


def _looks_like_1688_login(page: Page) -> bool:
    try:
        title_now = str(page.title() or "").lower()
    except Exception:
        title_now = ""
    try:
        current_url = page.url.lower()
    except Exception:
        current_url = ""

    # 1688 often redirects login to taobao/alibaba passport domains.
    url_markers = (
        "login.1688.com",
        "login.taobao.com",
        "passport.alibaba.com",
        "member.1688.com/member/login",
    )
    if any(m in current_url for m in url_markers):
        return True

    title_markers = ("登录", "sign in", "login")
    if any(m in title_now for m in title_markers):
        return True

    # Fallback: login form signatures.
    try:
        has_pwd = page.locator("input[type='password']").count() > 0
        has_user = page.locator("input[type='text'],input[type='tel'],input[name*='user' i],input[name*='account' i]").count() > 0
        has_login_btn = (
            page.locator("button:has-text('登录'), button:has-text('Log in'), button:has-text('Login')").count() > 0
        )
        if has_pwd and (has_user or has_login_btn):
            return True
    except Exception:
        pass

    return False


def _looks_like_1688_authenticated(page: Page) -> bool:
    try:
        current_url = page.url.lower()
    except Exception:
        return False

    # Must be on 1688 domain, otherwise don't treat as logged in.
    if "1688.com" not in current_url:
        return False
    if _looks_like_1688_login(page):
        return False

    # 必须看到认证 cookie，避免在未登录详情页被误判为已登录。
    if not _has_1688_auth_cookie(page):
        return False

    # 登录后常见入口域名优先通过。
    if any(x in current_url for x in ("work.1688.com", "my.1688.com", "member.1688.com")):
        return True

    # 兜底：页面上存在明确“已登录态”标识。
    try:
        markers = [
            "text=我的阿里",
            "text=我的订单",
            "text=采购车",
            "a[href*='work.1688.com']",
        ]
        if any(page.locator(m).count() > 0 for m in markers):
            return True
    except Exception:
        pass

    return False


def _looks_like_takealot_login(page: Page) -> bool:
    url = page.url.lower()
    if ("auth.takealot" in url) or ("accounts.takealot" in url) or ("/login" in url and "takealot" in url):
        return True
    # Fallback for seller login pages that keep base URL without /login.
    try:
        has_pwd = page.locator("input[type='password']").count() > 0
        has_user = page.locator("input[type='email'], input[name*='email' i], input[placeholder*='email' i]").count() > 0
        has_login_btn = (
            page.locator("button:has-text('Log In'), button:has-text('Login'), button:has-text('Sign in')").count() > 0
        )
        if has_pwd and (has_user or has_login_btn):
            return True
    except Exception:
        pass
    return False


def _looks_like_takealot_authenticated(page: Page) -> bool:
    url = page.url.lower()
    if "sellers.takealot.com" not in url:
        return False
    if _looks_like_takealot_login(page):
        return False
    # 优先：localStorage 中出现 seller token，说明登录已完成。
    if _has_takealot_auth_localstorage(page):
        return True

    # 1) URL 路径匹配：登录后会跳到 /dashboard、/offers、/shipments 等明确路径
    _LOGGED_IN_PATHS = ("/dashboard", "/offers", "/shipments", "/accounting", "/sales", "/advertising")
    if any(p in url for p in _LOGGED_IN_PATHS):
        return True
    # 2) 左侧导航出现：登录后的 seller portal 特有标签
    try:
        markers = [
            "text=My Existing Offers",
            "text=Shipments",
            "text=Sales",
            "text=API Integrations",
            "text=Knowledge & Ticket Centre",
        ]
        if any(page.locator(m).count() > 0 for m in markers):
            return True
    except Exception:
        pass
    # 原来的 "return True" 兜底已移除：
    # 只要 URL 在 sellers.takealot.com 但路径/导航都还没出现（如页面跳转中间态），
    # 不能误判为"已登录"，否则会在用户操作前就关闭浏览器。
    return False


def _is_logged_in(page: Page, mode: str) -> bool:
    if mode == "1688":
        return _looks_like_1688_authenticated(page)
    if mode == "takealot":
        return _looks_like_takealot_authenticated(page)
    return False


def _wait_for_manual_login(
    context: BrowserContext,
    mode: str,
    wait_seconds: int,
    stable_hits_required: int = 2,
) -> bool:
    deadline = time.time() + wait_seconds
    stable_logged_in_hits = 0
    stable_hits_required = max(2, int(stable_hits_required))

    while time.time() < deadline:
        alive_pages = [p for p in context.pages if not p.is_closed()]
        if not alive_pages:
            break

        any_logged_in = False
        try:
            for p in alive_pages:
                if _is_logged_in(p, mode):
                    any_logged_in = True
                    break
        except Exception:
            any_logged_in = False

        if any_logged_in:
            stable_logged_in_hits += 1
            # consecutive hits reduce false positives while allowing fast completion
            if stable_logged_in_hits >= stable_hits_required:
                return True
        else:
            stable_logged_in_hits = 0

        time.sleep(1)

    return False


def _wait_for_short_final_settle(context: BrowserContext, mode: str, seconds: int = 8) -> bool:
    deadline = time.time() + max(1, seconds)
    while time.time() < deadline:
        alive_pages = [p for p in context.pages if not p.is_closed()]
        if not alive_pages:
            return False
        try:
            if any(_is_logged_in(p, mode) for p in alive_pages):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _validate_state(state_path: Path, mode: str, browser_channel: str, verify_url: str) -> bool:
    if not state_path.exists():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") if isinstance(payload, dict) else []
        if not isinstance(cookies, list):
            cookies = []
    except Exception:
        return False

    if mode == "1688":
        names = {str(c.get("name", "")).strip().lower() for c in cookies if isinstance(c, dict)}
        has_auth_cookie = any(n in names for n in _A1688_AUTH_COOKIE_HINTS)
        if not has_auth_cookie:
            return False
    elif mode == "takealot":
        # Takealot 对无头浏览器有反爬检测，无头验证会失败。
        # 只要有 takealot 域的 cookie 就视为有效，不再开浏览器验证。
        if not any("takealot" in str(c.get("domain", "")) for c in cookies):
            return False
        print("[validate] Takealot 状态已保存（跳过无头浏览器验证）。")
        return True

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=browser_channel if browser_channel else None,
            headless=True,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--disable-infobars"],
        )
        try:
            context = browser.new_context(storage_state=str(state_path), viewport={"width": 1280, "height": 900})
            try:
                page = context.new_page()
                page.goto(verify_url, wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(1500)
                return _is_logged_in(page, mode)
            finally:
                context.close()
        finally:
            browser.close()


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _load_storage_state_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        _ = json.loads(path.read_text(encoding="utf-8"))
        return str(path)
    except Exception:
        return None


def run_manual_login(
    *,
    url: str,
    state_path: str | Path,
    mode: str,
    browser_channel: str = "msedge",
    wait_seconds: int = 600,
    verify_url: str = "",
    stable_hits: int = 2,
) -> None:
    if mode not in {"1688", "takealot"}:
        raise ValueError(f"unsupported mode: {mode}")

    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_state_path = state_path.with_suffix(state_path.suffix + ".tmp")
    _safe_unlink(temp_state_path)

    verify_url = (verify_url or "").strip()
    if not verify_url:
        verify_url = "https://detail.1688.com" if mode == "1688" else "https://sellers.takealot.com"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=browser_channel if browser_channel else None,
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--disable-infobars", "--new-window"],
        )
        context: BrowserContext | None = None
        try:
            ctx_kwargs = {"viewport": {"width": 1440, "height": 900}}
            maybe_state = _load_storage_state_if_exists(state_path)
            if maybe_state:
                ctx_kwargs["storage_state"] = maybe_state
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.bring_to_front()

            _ = _wait_for_manual_login(
                context=context,
                mode=mode,
                wait_seconds=max(30, int(wait_seconds)),
                stable_hits_required=max(2, int(stable_hits)),
            )
            _ = _wait_for_short_final_settle(context=context, mode=mode, seconds=8)

            # Persist to temp first, only promote to official state on full validation success.
            context.storage_state(path=str(temp_state_path))
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            browser.close()

    # Validate state before returning success.
    ok = _validate_state(
        state_path=temp_state_path,
        mode=mode,
        browser_channel=browser_channel,
        verify_url=verify_url,
    )
    if not ok:
        _safe_unlink(temp_state_path)
        raise LoginNotCompletedError(f"LOGIN_NOT_COMPLETED: {mode} login not confirmed. Please login fully and retry.")

    if state_path.exists():
        _safe_unlink(state_path)
    temp_state_path.rename(state_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open browser for manual login and save storage state")
    parser.add_argument("--url", required=True, help="target login url")
    parser.add_argument("--state-path", required=True, help="output Playwright storage state path")
    parser.add_argument("--mode", required=True, choices=["1688", "takealot"], help="login mode")
    parser.add_argument("--browser-channel", default="msedge", help="browser channel (msedge/chrome/chromium)")
    parser.add_argument("--wait-seconds", type=int, default=600, help="manual login max wait seconds")
    parser.add_argument("--verify-url", default="", help="post-login verification url")
    parser.add_argument("--stable-hits", type=int, default=2, help="required consecutive logged-in checks before completion")
    args = parser.parse_args()

    try:
        run_manual_login(
            url=args.url,
            state_path=args.state_path,
            mode=args.mode,
            browser_channel=args.browser_channel,
            wait_seconds=args.wait_seconds,
            verify_url=args.verify_url,
            stable_hits=args.stable_hits,
        )
    except LoginNotCompletedError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
