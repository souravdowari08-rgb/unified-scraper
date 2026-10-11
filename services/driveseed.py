"""Service 3: DriveSeed file page -> Google Drive link  ->  GET|POST /api/link

Ported from the standalone sync-Playwright service to async so it runs on the shared
Chromium. The steps are identical: open the file page with a logged-in session, call the
page's own generate('original'), open the token page, read #gdlink.
"""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import time
from urllib.parse import urljoin, urlparse

from playwright.async_api import TimeoutError as PWTimeout

import browser
import config

log = logging.getLogger("driveseed")

os.makedirs(config.SESSION_DIR, exist_ok=True)
STATE_FILE = os.path.join(config.SESSION_DIR, "state.json")
META_FILE = os.path.join(config.SESSION_DIR, "meta.json")

BLOCK_TYPES = {"image", "media", "font", "stylesheet"}


class SessionInvalid(Exception):
    pass


class Busy(Exception):
    pass


# ----------------------------------------------------------------- session handling
def session_loaded():
    return os.path.exists(STATE_FILE)


def write_session(state, user_agent=None):
    if isinstance(state, str):
        state = json.loads(state)
    if not isinstance(state, dict) or "cookies" not in state:
        raise ValueError("state must be the contents of state.json (an object with a 'cookies' list)")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    if user_agent:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump({"user_agent": user_agent}, f)
    _cache.clear()


def load_session_from_files():
    """Copy state.json / meta.json that sit next to app.py into SESSION_DIR (if not already there)."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, dest in (("state.json", STATE_FILE), ("meta.json", META_FILE)):
        src = os.path.join(app_dir, name)
        if os.path.exists(src) and not os.path.exists(dest):
            shutil.copy(src, dest)
            log.info("Loaded %s from the app folder", name)


def load_session_from_env():
    import base64
    b64 = os.environ.get("STATE_JSON_B64", "").strip()
    if b64 and not os.path.exists(STATE_FILE):
        try:
            write_session(base64.b64decode(b64).decode("utf-8"), os.environ.get("USER_AGENT"))
            log.info("Session loaded from STATE_JSON_B64")
        except Exception as e:
            log.warning("Could not load STATE_JSON_B64: %s", e)


def load_session():
    load_session_from_files()
    load_session_from_env()


def url_allowed(url):
    p = urlparse(url or "")
    return p.scheme == "https" and (p.hostname or "").lower() in config.DRIVESEED_ALLOWED_HOSTS


# ----------------------------------------------------------------- browser job
async def _load_user_agent(b):
    if os.path.exists(META_FILE):
        with open(META_FILE, encoding="utf-8") as f:
            ua = json.load(f).get("user_agent")
            if ua:
                return ua
    tmp = await b.new_page()
    try:
        ua = (await tmp.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
    finally:
        await tmp.close()
    return ua


async def _route_filter(route):
    if route.request.resource_type in BLOCK_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def _save_debug(page, name):
    if not config.DEBUG_DIR:
        return
    try:
        os.makedirs(config.DEBUG_DIR, exist_ok=True)
        await page.screenshot(path=os.path.join(config.DEBUG_DIR, f"{name}.png"), full_page=True)
        with open(os.path.join(config.DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(await page.content())
    except Exception:
        pass


async def _run(b, url):
    if not os.path.exists(STATE_FILE):
        raise SessionInvalid(f"{STATE_FILE} not found")

    ua = await _load_user_agent(b)
    context = await b.new_context(storage_state=STATE_FILE, user_agent=ua)
    try:
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        await context.route("**/*", _route_filter)
        page = await context.new_page()
        page.set_default_timeout(30000)

        # 1. file page + session check
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector("#ddl", state="attached", timeout=15000)
        except PWTimeout:
            await _save_debug(page, "debug_session")
            raise SessionInvalid("no Download File button on the file page")
        if await page.locator("a[href='/logout']").count() == 0:
            await _save_debug(page, "debug_session")
            raise SessionInvalid("not logged in (no Logout link)")

        # 2. the page's own generate(): wait for scripts, retry a few times
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except PWTimeout:
            pass
        next_url, data = None, None
        for attempt in range(1, 4):
            try:
                data = await page.evaluate("generate('original')")
            except Exception as e:
                data = {"js_error": str(e)[:200]}
            if isinstance(data, dict):
                next_url = data.get("visit_url") or data.get("url")
            if next_url:
                break
            log.info("generate() attempt %d gave no url: %s", attempt, str(data)[:200])
            await page.wait_for_timeout(2000 * attempt)
        if not next_url:
            await _save_debug(page, "debug_generate")
            raise RuntimeError(f"generate() returned no url: {str(data)[:200]}")
        next_url = urljoin(url, next_url)

        # 3. token page -> #gdlink
        await page.goto(next_url, wait_until="commit", timeout=45000)
        try:
            await page.wait_for_selector("#gdlink", state="attached", timeout=30000)
        except PWTimeout:
            await _save_debug(page, "debug_token_page")
            raise RuntimeError("No #gdlink on the token page")

        gd = html.unescape(await page.get_attribute("#gdlink", "href") or "")
        try:
            direct = await page.evaluate("typeof worker_url !== 'undefined' ? worker_url : null")
        except Exception:
            direct = None
        m = re.search(r"[?&]id=([\w-]+)|/d/([\w-]+)", gd)
        return {"gdrive": gd,
                "file_id": (m.group(1) or m.group(2)) if m else None,
                "direct": direct}
    finally:
        await context.close()


# ----------------------------------------------------------------- public entry point
_cache = {}   # url -> (stored_at, result)


async def get_link(url):
    now = time.time()
    hit = _cache.get(url)
    if config.DRIVESEED_CACHE_TTL and hit and now - hit[0] < config.DRIVESEED_CACHE_TTL:
        return dict(hit[1], cached=True)

    if not session_loaded():
        raise SessionInvalid(f"{STATE_FILE} not found")

    try:
        await asyncio.wait_for(browser.sem.acquire(), timeout=config.QUEUE_WAIT)
    except asyncio.TimeoutError:
        raise Busy("browser busy, try again shortly")
    try:
        last = None
        for attempt in (1, 2):
            try:
                b = await browser.ensure_browser()
                result = await _run(b, url)
                last = None
                break
            except SessionInvalid:
                raise                      # retrying will not fix an expired session
            except BaseException as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                last = e
                log.warning("attempt %d failed: %s: %s", attempt, type(e).__name__, str(e)[:200])
        if last is not None:
            raise last
    finally:
        browser.sem.release()

    if config.DRIVESEED_CACHE_TTL and result.get("gdrive"):
        _cache[url] = (time.time(), result)
        if len(_cache) > 200:
            _cache.pop(next(iter(_cache)))
    return result
