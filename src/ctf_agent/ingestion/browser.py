"""Optional Playwright login, session acquisition, and scoped page capture."""

from __future__ import annotations

import asyncio
from pathlib import Path


class BrowserUnavailable(RuntimeError):
    pass


class BrowserAuthenticationTimeout(TimeoutError):
    pass


async def acquire_authenticated_storage(
    login_url: str,
    storage_state: Path,
    *,
    success_selector: str = 'a[href*="logout"], [data-authenticated="true"]',
    timeout_seconds: float = 300,
) -> Path:
    """Wait for browser-visible authentication without terminal confirmation prompts."""
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise BrowserUnavailable(
            "Playwright is required for interactive authentication; install .[browser]"
        ) from exc

    storage_state.parent.mkdir(parents=True, exist_ok=True)
    existing = storage_state if storage_state.is_file() else None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=existing is not None)
        context = await browser.new_context(storage_state=str(existing) if existing else None)
        page = await context.new_page()
        await page.goto(login_url, wait_until="domcontentloaded")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            selector_found = await page.locator(success_selector).count() > 0
            left_login_page = "/login" not in page.url.rstrip("/").lower()
            has_session_cookie = any(
                cookie.get("name", "").lower() in {"session", "sessionid", "auth", "token"}
                for cookie in await context.cookies()
            )
            if selector_found or (left_login_page and has_session_cookie):
                await context.storage_state(path=str(storage_state))
                await browser.close()
                storage_state.chmod(0o600)
                return storage_state
            await page.wait_for_timeout(500)
        await browser.close()
    raise BrowserAuthenticationTimeout(
        f"authentication was not detected within {timeout_seconds:g} seconds"
    )


async def capture_scoped_page(
    url: str,
    output: Path,
    *,
    storage_state: Path,
    selector: str = "main",
    timeout_seconds: float = 30,
) -> Path:
    """Capture only the challenge content region, never the full desktop."""
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise BrowserUnavailable("Playwright is required for evidence screenshots") from exc
    if not storage_state.is_file():
        raise BrowserUnavailable("authenticated Playwright storage state is missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(storage_state))
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
        region = page.locator(selector).first
        if await region.count() > 0:
            await region.screenshot(path=str(output))
        else:
            await page.screenshot(path=str(output), full_page=True)
        await browser.close()
    return output
