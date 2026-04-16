"""
Authentication for dev.brya.com.

Two paths:
  1. Magic link via brya-server's test endpoint (PLAYWRIGHT_LOGIN_URL + PLAYWRIGHT_AUTH_KEY).
     Mirrors brya-web/e2e/fixtures/login.ts. Preferred.
  2. Interactive email+password against /login. Fallback for environments without the
     test endpoint.
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

from .config import Config, UserCreds


def ensure_logged_in(
    context: BrowserContext,
    page: Page,
    cfg: Config,
    user: UserCreds | None = None,
    browser: str = "",
) -> None:
    """Log in as `user` (or the configured Brya admin if not specified)."""
    user = user or cfg.brya_user

    # Probe /login — an authed session redirects away from it.
    page.goto(f"{cfg.base_url}/login")
    page.wait_for_load_state("domcontentloaded")
    _dismiss_cookie_banner(page)

    if "/login" not in page.url and "/email-confirmation" not in page.url:
        print(f"  [auth] already authenticated as {user.email} (landed at {page.url})")
        return

    if cfg.playwright_login_url and cfg.playwright_auth_key and user.email:
        _login_magic_link(page, cfg, user)
    elif user.email and user.password:
        _login_password(page, cfg, user)
    else:
        raise RuntimeError(
            f"No login credentials configured for {user.email!r}. Set either "
            "PLAYWRIGHT_LOGIN_URL+PLAYWRIGHT_AUTH_KEY+<email> (magic link) "
            "or <email>+LOGIN_PASSWORD (password)."
        )

    # Persist storage state so subsequent runs skip auth.
    state_path = cfg.storage_path_for(user, browser)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(state_path))
    print(f"  [auth] saved storage state to {state_path}")


def _dismiss_cookie_banner(page: Page) -> None:
    try:
        btn = page.wait_for_selector(
            "[role='region'][aria-label='Cookie consent'] button:has-text('Accept All')",
            state="visible",
            timeout=3000,
        )
        if btn:
            btn.click()
            page.wait_for_selector(
                "[role='region'][aria-label='Cookie consent']",
                state="hidden",
                timeout=5000,
            )
    except Exception:
        pass


def _login_password(page: Page, cfg: Config, user: UserCreds) -> None:
    print(f"  [auth] password login as {user.email}")
    page.goto(f"{cfg.base_url}/login")
    page.wait_for_load_state("load")
    if "/login" not in page.url:
        return
    page.fill("[placeholder='Enter email address']", user.email)
    page.click("text=\"Continue\"")
    page.wait_for_selector("[placeholder='Enter password']", state="visible", timeout=10000)
    page.fill("[placeholder='Enter password']", user.password)
    page.press("[placeholder='Enter password']", "Enter")
    page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    print(f"  [auth] logged in, now at {page.url}")


def _login_magic_link(page: Page, cfg: Config, user: UserCreds) -> None:
    """
    Port of brya-web/e2e/fixtures/login.ts `Login.login`.
    Fetches a magic link from the test endpoint and navigates to it.
    """
    print(f"  [auth] magic-link login as {user.email}")
    page.goto(f"{cfg.base_url}/login")
    page.wait_for_load_state("domcontentloaded")
    page.fill("[placeholder='Enter email address']", user.email)
    page.click("text=\"Continue\"")

    try:
        page.wait_for_url("**/email-confirmation", timeout=15000)
    except PWTimeout:
        pass

    magic_link = _poll_magic_link(cfg, user)
    if not magic_link:
        raise RuntimeError(
            f"Could not retrieve magic link for {user.email} from PLAYWRIGHT_LOGIN_URL after 15 attempts"
        )

    page.goto(magic_link)
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_url(
            lambda url: "/account/postLogin" not in url and "/email-confirmation" not in url,
            timeout=20000,
        )
    except PWTimeout:
        pass

    for _ in range(20):
        cookies = page.context.cookies()
        if any(c["name"] == "authorization" for c in cookies):
            print("  [auth] authorization cookie set")
            return
        time.sleep(1)
    print("  [auth] warning: authorization cookie never appeared")


_MAGIC_HREF_RE = re.compile(r'href=["\']([^"\']*code=[^"\']*)["\']', re.IGNORECASE)


def _poll_magic_link(cfg: Config, user: UserCreds) -> str | None:
    url = f"{cfg.playwright_login_url}?email={user.email}&auth={cfg.playwright_auth_key}"
    with httpx.Client(timeout=10.0) as client:
        for _ in range(15):
            try:
                r = client.get(url)
                if r.status_code == 200:
                    m = _MAGIC_HREF_RE.search(r.text)
                    if m:
                        return m.group(1).replace("&amp;", "&")
            except httpx.HTTPError:
                pass
            time.sleep(1)
    return None
