"""ONE shared Chromium for every service that needs a browser."""
import asyncio
import logging

from playwright.async_api import async_playwright

import config

log = logging.getLogger("browser")

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
    "--no-zygote", "--disable-extensions", "--disable-background-networking", "--mute-audio",
]

sem = None            # limits concurrent browser jobs (created at startup, inside the event loop)
_playwright = None
_browser = None
_lock = None


def init():
    global sem, _lock
    sem = asyncio.Semaphore(config.MAX_CONCURRENT)
    _lock = asyncio.Lock()


def is_up():
    return _browser is not None and _browser.is_connected()


async def ensure_browser():
    """Start (or transparently restart) the shared Chromium."""
    global _playwright, _browser
    async with _lock:
        if is_up():
            return _browser
        if _playwright is None:
            _playwright = await async_playwright().start()
        try:  # "new" headless mode: closer to a real Chrome
            _browser = await _playwright.chromium.launch(headless=True, channel="chromium", args=CHROME_ARGS)
        except Exception:
            _browser = await _playwright.chromium.launch(headless=True, args=CHROME_ARGS)
        log.info("browser started")
        return _browser


async def shutdown():
    global _playwright, _browser
    try:
        if _browser:
            await _browser.close()
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    _browser = _playwright = None
