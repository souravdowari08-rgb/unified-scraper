"""Service 2: multi-step shortener ("linkpilot") resolver  ->  GET /getlink?url=

Drives the shared headless Chromium through the shortener hops, then collects the
download links from the final file page. Logic is unchanged from the standalone service.
"""
import asyncio
import logging
import os
import re
import time
import urllib.parse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError

import browser
import config
from common import base_domain

log = logging.getLogger("getlink")

TIME_SPEEDUP = config.TIME_SPEEDUP
BLOCK_THIRD_PARTY = config.BLOCK_THIRD_PARTY
ENABLED_VARIANTS = config.ENABLED_VARIANTS
DEBUG_DIR = config.DEBUG_DIR
PROXY_URL = config.PROXY_URL
CACHE_TTL = config.CACHE_TTL

MAX_HOPS = 8            # safety cap on shortener steps
HOP_TIMEOUT = 35        # seconds to wait for one step (countdown included)
NAV_TIMEOUT = 20_000    # ms
DOWNLOAD_HINT = re.compile(r"download|instant|direct|resume|cloud|gdrive|mirror", re.I)
VARIANT_WAIT_MS = 1500  # browser fallback: how long to wait for the download link

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# The shortener's "continue" button / form (hop pages use ids like lp-s1-form, lp-land)
BTN_SELECTOR = "form[id^='lp-'] button[type='submit'], button.lp-btn"
STEP_LABELS = ["Go to download", "Click here to continue", "Generate link"]
RETRY_AFTER = 8 if TIME_SPEEDUP == 1 else 2  # seconds before retrying a button that may be countdown-gated
RETRY_BY_LABEL = {"Generate link": 6}  # clicking this again mid-countdown can restart it
SUBMIT_LP_FORM = """() => {
    const f = document.getElementById('lp-land') || document.querySelector("form[id^='lp-']");
    if (!f) return null;
    try { HTMLFormElement.prototype.submit.call(f); } catch (e) { return null; }
    return f.id || "(form)";
}"""
NUDGE_AFTER = 0.3  # seconds on the redirect page before submitting its form ourselves
POLL = 0.05      # seconds between page polls
CLICK_PAUSE = 0.1  # pause after a click

# Variants of the final file page: key -> (path template, CSS selector for the download link)
VARIANTS = {
    "zfile": ("/zfile/{id}", "#cf_captcha > div.card-body > div > a"),
    "wfile_type1": ("/wfile/{id}?type=1", "body > div > div > div.card-body > div > div > a"),
    "wfile_type2": ("/wfile/{id}?type=2", "body > div > div > div.card-body > div > div > a"),
}


async def dump(page, name: str):
    if not DEBUG_DIR:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{name}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(await page.content())
        log.info("debug: saved %s (url=%s)", path, page.url)
    except Exception as e:
        log.warning("debug dump failed: %s", e)


AD_HOSTS = (
    "vidverto.io", "doubleclick.net", "googlesyndication.com", "googletagmanager.com",
    "google-analytics.com", "adservice.google.com", "adnxs.com", "taboola.com",
    "outbrain.com", "amazon-adsystem.com", "popads.net", "propellerads.com",
)


# third-party hosts that are fine to load even in strict mode (libraries the flow may need)
CDN_OK = ("cdnjs.cloudflare.com", "ajax.googleapis.com", "code.jquery.com",
          "cdn.jsdelivr.net", "unpkg.com", "challenges.cloudflare.com")
THIRD_PARTY_TYPES = {"script", "iframe", "xhr", "fetch", "ping", "websocket", "eventsource", "texttrack"}


def make_router(start_base: str, strict: bool):
    async def route_handler(route):
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            await route.abort()
            return
        host = (urllib.parse.urlparse(req.url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in AD_HOSTS):
            await route.abort()
            return
        if strict and req.resource_type in THIRD_PARTY_TYPES and host:
            try:
                frame_host = urllib.parse.urlparse(req.frame.url).hostname or ""
            except Exception:
                frame_host = ""
            base = base_domain(host)
            same_site = base == start_base or (frame_host and base == base_domain(frame_host))
            if not same_site and not any(host == h or host.endswith("." + h) for h in CDN_OK):
                await route.abort()
                return
        await route.continue_()
    return route_handler


def speedup_script(host: str) -> str:
    """
    JS injected before any page script runs. On the shortener's own pages it makes
    timers fire TIME_SPEEDUP x sooner and the Date/performance clocks run faster,
    so client-side countdowns finish almost immediately.
    """
    return """
(() => {
  const K = %d, BASE = %r;
  if (K <= 1 || !location.hostname.endsWith(BASE)) return;
  const st = window.setTimeout.bind(window), si = window.setInterval.bind(window);
  window.setTimeout  = (f, d, ...a) => st(f, Math.max(0, (+d || 0) / K), ...a);
  window.setInterval = (f, d, ...a) => si(f, Math.max(1, (+d || 0) / K), ...a);
  const rn = Date.now.bind(Date), rp = performance.now.bind(performance);
  const t0 = rn(), p0 = rp();
  Date.now = () => t0 + (rn() - t0) * K;
  performance.now = () => p0 + (rp() - p0) * K;
})();
""" % (TIME_SPEEDUP, host)


async def new_context(shortener_host: str = "", strict: bool = False):
    b = await browser.ensure_browser()
    kwargs = {"user_agent": UA, "viewport": {"width": 1280, "height": 720}, "locale": "en-US"}
    if PROXY_URL:
        kwargs["proxy"] = {"server": PROXY_URL}      # per-context: only /getlink uses the proxy
    ctx = await b.new_context(**kwargs)
    if shortener_host and TIME_SPEEDUP > 1:
        await ctx.add_init_script(speedup_script(base_domain(shortener_host)))
    await ctx.route("**/*", make_router(base_domain(shortener_host), strict))
    return ctx


# --------------------------------------------------------------------------
# Shortener hops
# --------------------------------------------------------------------------
def step_locator(page, label):
    return page.locator("button, a, [role='button']").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    )


async def click_step(page, loc):
    """Click like a visitor; fall back to a DOM click if an overlay blocks it."""
    try:
        await loc.click(timeout=5000)
    except PlaywrightError:
        await loc.evaluate("el => el.click()")


async def advance_one_hop(page, hop: int = 0) -> bool:
    """
    Work through one shortener page: click whichever known button is visible
    (Generate link -> Click here to continue -> Go to download ...) until the
    browser URL changes. Returns True once the page navigated.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + HOP_TIMEOUT
    start_url = page.url
    clicked = {}      # label -> time of last click
    idle_since = None
    last_nudge = None

    while loop.time() < deadline:
        try:
            if page.url != start_url:
                return True

            acted = False
            for label in STEP_LABELS:
                last = clicked.get(label)
                if last is not None and loop.time() - last < RETRY_BY_LABEL.get(label, RETRY_AFTER):
                    continue
                loc = step_locator(page, label).first
                if await loc.is_visible():
                    log.info("clicking %r on %s", label, page.url)
                    await click_step(page, loc)
                    clicked[label] = loop.time()
                    acted = True
                    await asyncio.sleep(CLICK_PAUSE)
                    break

            if not acted:
                # fallback: the original lp- form button
                btn = page.locator(BTN_SELECTOR).first
                last = clicked.get("__form__")
                if (last is None or loop.time() - last >= RETRY_AFTER) and await btn.is_visible():
                    log.info("clicking form button on %s", page.url)
                    await click_step(page, btn)
                    clicked["__form__"] = loop.time()
                    acted = True
                    await asyncio.sleep(CLICK_PAUSE)

            if not acted:
                # Auto-submitting pages (the first hop, the "Please wait" page): don't wait
                # for their own timers, submit their form ourselves after a short moment.
                now = loop.time()
                if hop == 0 or await page.locator("#lp-land").count():
                    idle_since = idle_since or now
                    if now - idle_since >= NUDGE_AFTER and (last_nudge is None or now - last_nudge > 2.0):
                        fid = await page.evaluate(SUBMIT_LP_FORM)
                        if fid:
                            log.info("submitted auto form %r after %.1fs", fid, now - idle_since)
                            last_nudge = now
                else:
                    idle_since = None
        except PlaywrightError as e:
            # page is navigating / context destroyed: poll again
            log.debug("poll error: %s", str(e).splitlines()[0])
        await asyncio.sleep(POLL)

    return page.url != start_url


async def wait_until_left(page, left_shortener, timeout=18.0) -> bool:
    """
    Wait on the "Please wait, redirecting" page. Instead of idling until the page's own
    redirect fires, submit its auto-redirect form as soon as it exists (it does the same
    thing the page's script would do after its delay).
    """
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    last_nudge = None
    diagnosed = False
    while loop.time() - t0 < timeout:
        if left_shortener(page.url):
            return True
        try:
            now = loop.time()
            if now - t0 >= NUDGE_AFTER and (last_nudge is None or now - last_nudge > 2.0):
                form_id = await page.evaluate(
                    """() => {
                        const f = document.getElementById('lp-land') || document.querySelector("form[id^='lp-']");
                        if (!f) return null;
                        try { HTMLFormElement.prototype.submit.call(f); } catch (e) { return null; }
                        return f.id || "(form)";
                    }"""
                )
                if form_id:
                    log.info("redirect page: submitted form %r after %.1fs", form_id, now - t0)
                    last_nudge = now
            if not diagnosed and now - t0 > 1.5:
                diagnosed = True
                info = await page.evaluate(
                    """() => ({
                        forms: [...document.forms].map(f => f.id || f.action),
                        meta: (document.querySelector("meta[http-equiv='refresh' i]") || {}).content || null,
                        title: document.title
                    })"""
                )
                log.info("still on redirect page after 1.5s: %s", info)
        except PlaywrightError:
            pass  # navigating: poll again
        await asyncio.sleep(POLL)
    return left_shortener(page.url)


async def run_hops(page, start_host: str):
    """Follow shortener pages until the browser leaves the shortener's domain."""
    start_base = base_domain(start_host)

    def left_shortener(url: str) -> bool:
        return base_domain(urllib.parse.urlparse(url).hostname or "") != start_base

    for hop in range(MAX_HOPS):
        if left_shortener(page.url):
            log.info("left shortener after %d hop(s): %s", hop, page.url)
            return page.url

        log.info("hop %d on %s", hop, page.url)
        if not await advance_one_hop(page, hop):
            await dump(page, f"hop{hop}_stuck")
            return None

        # "/?lp_go=..." is the final "Please wait, redirecting" page: wait for it to leave
        if "lp_go=" in page.url and not left_shortener(page.url):
            if not await wait_until_left(page, left_shortener):
                await dump(page, f"hop{hop}_redirect_stuck")
                return None

    if left_shortener(page.url):
        return page.url
    await dump(page, "max_hops")
    return None


# --------------------------------------------------------------------------
# Final file page
# --------------------------------------------------------------------------
async def fetch_variant_http(context, url, selector):
    """
    Fast path: plain HTTP GET through the browser context (same proxy, cookies and UA),
    then parse the HTML. No page load, no rendering. Returns a link or None.
    """
    try:
        resp = await context.request.get(url, timeout=8000, headers={"Referer": url})
        if not resp.ok:
            log.info("variant http %s -> status %s", url, resp.status)
            return None
        html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        a = soup.select_one(selector)
        href = a.get("href") if a else None
        if not href:
            log.info("variant http %s -> 200 but selector not in HTML (%d bytes, %d <a> tags)",
                     url, len(html), len(soup.find_all("a")))
            if DEBUG_DIR:
                os.makedirs(DEBUG_DIR, exist_ok=True)
                name = re.sub(r"[^A-Za-z0-9]+", "_", urllib.parse.urlparse(url).path + "_" + urllib.parse.urlparse(url).query)
                with open(os.path.join(DEBUG_DIR, f"variant{name}.html"), "w", encoding="utf-8") as f:
                    f.write(html)
        return urllib.parse.urljoin(url, href) if href else None
    except Exception as e:
        log.debug("http fast path failed: %s", str(e).splitlines()[0])
        return None


async def fetch_variant_browser(context, url, selector):
    """
    Slow path: render the page (for links injected by JavaScript).
    Returns (exact_link, generic_link): the selector match, and the first <a> as a last resort.
    """
    pg = await context.new_page()
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=15000)
        try:
            await pg.wait_for_selector(selector, timeout=VARIANT_WAIT_MS)
        except PlaywrightError:
            pass  # fall back to scanning the page below
        el = await pg.query_selector(selector)
        if el:
            href = await el.get_attribute("href")
            if href:
                return urllib.parse.urljoin(url, href), None
        # selector did not match: look at every link on the rendered page
        html = await pg.content()
        soup = BeautifulSoup(html, "html.parser")
        anchors = [(a.get_text(" ", strip=True)[:40], a["href"]) for a in soup.find_all("a", href=True)]
        log.info("selector %r not found on %s; links on page: %s", selector, url,
                 [(t, h[:90]) for t, h in anchors[:15]])
        if DEBUG_DIR:
            await dump(pg, "variant_" + re.sub(r"[^A-Za-z0-9]+", "_", urllib.parse.urlparse(url).path + urllib.parse.urlparse(url).query))
        # only accept a link that actually looks like a download; never fall back to
        # nav/footer links (they would be returned as if they were the file)
        pick = next((h for t, h in anchors if DOWNLOAD_HINT.search(t)), None)
        return None, (urllib.parse.urljoin(url, pick) if pick else None)
    finally:
        await pg.close()


async def fetch_variant(context, key, url, selector):
    """
    Race the cheap HTTP fetch against the browser render and take whichever finds the
    link first (the HTTP path usually wins when the link is in the raw HTML; the browser
    path covers JavaScript-injected links without waiting for the HTTP attempt to fail).
    """
    t0 = time.time()
    http_task = asyncio.create_task(fetch_variant_http(context, url, selector))
    browser_task = asyncio.create_task(fetch_variant_browser(context, url, selector))
    pending = {http_task, browser_task}
    generic = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    result = task.result()
                except Exception as e:
                    log.debug("variant %s task failed: %s", key, str(e).splitlines()[0])
                    continue
                if task is browser_task:
                    result, generic = result
                if result:
                    log.info("variant %s: %s path, %.1fs", key,
                             "http" if task is http_task else "browser", time.time() - t0)
                    return key, result
        if generic:
            log.info("variant %s: generic link fallback, %.1fs", key, time.time() - t0)
        else:
            log.warning("variant %s: nothing found, %.1fs", key, time.time() - t0)
        return key, generic
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _resolve(start_url: str, strict: bool):
    async with browser.sem:
        start_host = urllib.parse.urlparse(start_url).hostname or ""
        ctx = await new_context(start_host, strict)
        page = await ctx.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)

            final_url = await run_hops(page, start_host)
            if not final_url:
                return {
                    "error": "Hop chain did not finish",
                    "debug_url": page.url,
                    "debug_title": await page.title(),
                }, 504

            # Leaving the shortener can land on an intermediate "/r?key=..." hop of the file
            # host; wait for it to finish redirecting to the real /file/<id> page.
            if urllib.parse.urlparse(page.url).path.rstrip("/") == "/r":
                if not await wait_until_left(page, lambda u: "/file/" in u, timeout=12.0):
                    await dump(page, "r_redirect_stuck")
                    return {
                        "error": "File redirect did not finish",
                        "debug_url": page.url,
                    }, 504

            final_url = page.url
            parsed = urllib.parse.urlparse(final_url)
            segments = [s for s in parsed.path.split("/") if s]
            file_id = segments[-1] if segments else ""
            origin = f"{parsed.scheme}://{parsed.netloc}"

            # the file pages may have their own countdown: speed those timers up too
            if TIME_SPEEDUP > 1 and parsed.hostname:
                await ctx.add_init_script(speedup_script(base_domain(parsed.hostname)))

            if not file_id:
                return {"final_url": final_url, "file_id": None, "download_links": {}}, 200

            tasks = [
                fetch_variant(ctx, key, origin + path.format(id=file_id), selector)
                for key, (path, selector) in VARIANTS.items()
                if not ENABLED_VARIANTS or key in ENABLED_VARIANTS
            ]
            results = dict(await asyncio.gather(*tasks))
            return {"final_url": final_url, "file_id": file_id, "download_links": results}, 200
        finally:
            await ctx.close()



async def resolve(start_url: str):
    body, status = await _resolve(start_url, BLOCK_THIRD_PARTY)
    if BLOCK_THIRD_PARTY and status == 504 and "Hop chain" in str(body.get("error", "")):
        log.warning("hop chain failed with third-party blocking; retrying without it")
        body, status = await _resolve(start_url, False)
    return body, status


async def startup_check():
    """Optional: log the exit IP when a proxy is configured."""
    if not PROXY_URL:
        return
    try:
        ctx = await new_context()
        page = await ctx.new_page()
        await page.goto("https://check.torproject.org/api/ip", timeout=60000)
        log.info("proxy check: %s", (await page.inner_text("body")).strip()[:200])
        await ctx.close()
    except Exception as e:
        log.warning("proxy check failed: %s", e)


_cache = {}   # url -> (expires_at, body)


def cache_get(url):
    hit = _cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    _cache.pop(url, None)
    return None


def cache_put(url, body):
    if CACHE_TTL > 0 and any((body.get("download_links") or {}).values()):
        _cache[url] = (time.time() + CACHE_TTL, body)
        if len(_cache) > 200:   # keep it small
            _cache.pop(next(iter(_cache)))
