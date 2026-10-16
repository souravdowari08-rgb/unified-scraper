"""Service 2: multi-step shortener ("linkpilot") resolver  ->  GET /getlink?url=

Drives the shared headless Chromium through the shortener hops, then collects the
download links from the final file page. Logic is unchanged from the standalone service.
"""
import asyncio
import json
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
RETRY_BY_LABEL = {"Generate link": 6 if TIME_SPEEDUP == 1 else 1.5}  # clicking this again mid-countdown can restart it
SUBMIT_LP_FORM = """() => {
    const f = document.getElementById('lp-land') || document.querySelector("form[id^='lp-']");
    if (!f) return null;
    try { HTMLFormElement.prototype.submit.call(f); } catch (e) { return null; }
    return f.id || "(form)";
}"""
NUDGE_AFTER = 0.1  # seconds on the redirect page before submitting its form ourselves
POLL = 0.05      # seconds between page polls
CLICK_PAUSE = 0.03  # pause after a click
BLOCK_CSS = config.BLOCK_CSS
TRACE = config.TRACE
VARIANT_RETRY_SEC = int(os.environ.get("GETLINK_VARIANT_RETRY_SEC", 0))  # 0 = never auto-skip

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
        if req.resource_type in ("image", "media", "font", "manifest"):
            await route.abort()
            return
        if strict and BLOCK_CSS and req.resource_type == "stylesheet":
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



_cookie_cache = (None, [])


def _driveseed_cookies():
    """driveseed.org cookies from the same state.json /api/link uses (None-safe, cached by mtime)."""
    global _cookie_cache
    path = os.path.join(config.SESSION_DIR, "state.json")
    try:
        mtime = os.path.getmtime(path)
        if _cookie_cache[0] == mtime:
            return _cookie_cache[1]
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return []
    bases = {base_domain(h) for h in config.DRIVESEED_ALLOWED_HOSTS}
    keys = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    out = [{k: c[k] for k in keys if k in c} for c in state.get("cookies", [])
           if base_domain((c.get("domain") or "").lstrip(".")) in bases]
    _cookie_cache = (mtime, out)
    return out


def attach_trace(page):
    """GETLINK_TRACE=1: log every document/xhr/fetch response, to see what plain HTTP would need."""
    def on_response(r):
        try:
            req = r.request
            if req.resource_type in ("document", "xhr", "fetch"):
                body = ""
                if req.method != "GET":
                    try:
                        body = " body=" + (req.post_data or "")[:200]
                    except Exception:
                        body = " body=<binary>"
                log.info("trace %s %s %s %s%s", req.resource_type, req.method, r.status, r.url[:160], body)
        except Exception:
            pass
    page.on("response", on_response)

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
    # Don't click before the page's scripts have attached their handlers: with stylesheets
    # blocked the button exists almost immediately, and an early click is silently ignored
    # (that cost a 6s retry). DOMContentLoaded is the point where those handlers are ready.
    if hop > 0:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightError:
            pass
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
def page_summary(html, soup):
    """One-line description of a page that did not contain the wanted link (for the log)."""
    title = soup.title.get_text(strip=True)[:80] if soup.title else ""
    low = html.lower()
    flags = [k for k in ("turnstile", "captcha", "recaptcha", "cloudflare", "login", "logout") if k in low]
    return "title=%r forms=%d flags=%s text=%r" % (
        title, len(soup.find_all("form")), flags, soup.get_text(" ", strip=True)[:200])


def _session_headers():
    """Same User-Agent the driveseed login was captured with (meta.json), if we use that login."""
    if not config.USE_DRIVESEED_SESSION:
        return {}
    try:
        with open(os.path.join(config.SESSION_DIR, "meta.json"), encoding="utf-8") as f:
            ua = json.load(f).get("user_agent")
        return {"User-Agent": ua} if ua else {}
    except Exception:
        return {}


async def _session_check(ctx, file_url):
    """Is the driveseed login accepted?  (logs the answer; does not delay the variants)"""
    try:
        resp = await ctx.request.get(file_url, timeout=8000, headers=_session_headers())
        text = await resp.text()
        logged = 'href="/logout"' in text or "href='/logout'" in text
        log.info("variants: session check on %s -> status %s, logged_in=%s", file_url, resp.status, logged)
        return logged
    except Exception as e:
        log.info("variants: session check failed: %s", str(e).splitlines()[0])
        return None


async def fetch_variant_http(context, url, selector):
    """
    Fast path: plain HTTP GET through the browser context (same proxy, cookies and UA),
    then parse the HTML. No page load, no rendering. Returns a link or None.
    """
    try:
        resp = await context.request.get(url, timeout=8000, headers={"Referer": url, **_session_headers()})
        if not resp.ok:
            log.info("variant http %s -> status %s", url, resp.status)
            return None
        html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        a = soup.select_one(selector)
        href = a.get("href") if a else None
        if not href:
            log.info("variant http %s -> 200 but selector not in HTML (%d bytes, %d <a> tags) %s",
                     url, len(html), len(soup.find_all("a")), page_summary(html, soup))
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


async def _http_follow_file(ctx, url, referer):
    """Plain HTTP GET of the /r?key=... hop: if it is a server redirect we get /file/<id> at once."""
    try:
        resp = await ctx.request.get(url, headers={"Referer": referer}, timeout=6000)
        if "/file/" in resp.url:
            return resp.url
    except Exception as e:
        log.debug("http /r follow failed: %s", str(e).splitlines()[0])
    return None


async def _wait_file_url(page, ctx, referer):
    """Return the /file/<id> URL (or None): whichever is first, HTTP follow or the browser."""
    r_url = page.url
    browser_task = asyncio.ensure_future(wait_until_left(page, lambda u: "/file/" in u, timeout=12.0))
    http_task = asyncio.ensure_future(_http_follow_file(ctx, r_url, referer))
    pending = {browser_task, http_task}
    found = None
    try:
        while pending and not found:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    res = t.result()
                except Exception:
                    continue
                if t is http_task and res:
                    found = res
                elif t is browser_task and res:
                    found = page.url
    finally:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return found


_variants_blind_until = 0.0   # after an all-empty variant stage, skip it until this time


async def _resolve(start_url: str, strict: bool):
    global _variants_blind_until
    async with browser.sem:
        t_start = time.perf_counter()
        tm = {}

        def mark(name):
            nonlocal t_start
            now = time.perf_counter()
            tm[name] = int((now - t_start) * 1000)
            t_start = now

        start_host = urllib.parse.urlparse(start_url).hostname or ""
        ctx = await new_context(start_host, strict)
        page = await ctx.new_page()
        if TRACE:
            attach_trace(page)
        mark("context")
        try:
            # "commit": hop polling below copes with a page that is still loading
            await page.goto(start_url, wait_until="commit", timeout=NAV_TIMEOUT)
            mark("first_nav")

            final_url = await run_hops(page, start_host)
            if not final_url:
                return {
                    "error": "Hop chain did not finish",
                    "debug_url": page.url,
                    "debug_title": await page.title(),
                }, 504

            mark("hops")
            # Leaving the shortener can land on an intermediate "/r?key=..." hop of the file
            # host; get the real /file/<id> URL (HTTP follow and browser race each other).
            final_url = page.url
            if urllib.parse.urlparse(final_url).path.rstrip("/") == "/r":
                found = await _wait_file_url(page, ctx, f"https://{start_host}/")
                if not found:
                    await dump(page, "r_redirect_stuck")
                    return {
                        "error": "File redirect did not finish",
                        "debug_url": page.url,
                    }, 504
                final_url = found
            mark("file_redirect")
            parsed = urllib.parse.urlparse(final_url)
            segments = [s for s in parsed.path.split("/") if s]
            file_id = segments[-1] if segments else ""
            origin = f"{parsed.scheme}://{parsed.netloc}"

            # the file pages may have their own countdown: speed those timers up too
            if TIME_SPEEDUP > 1 and parsed.hostname:
                await ctx.add_init_script(speedup_script(base_domain(parsed.hostname)))

            if not file_id:
                return {"final_url": final_url, "file_id": None, "download_links": {}}, 200

            if config.SKIP_VARIANTS or time.time() < _variants_blind_until:
                log.info("getlink stage timings ms: %s (variants skipped)", tm)
                return {"final_url": final_url, "file_id": file_id, "download_links": {},
                        "variants_skipped": True, "timings_ms": tm}, 200

            # the file pages need the driveseed login (without it they only show a "Login" link)
            if config.USE_DRIVESEED_SESSION and base_domain(parsed.hostname or "") in \
                    {base_domain(h) for h in config.DRIVESEED_ALLOWED_HOSTS}:
                cookies = _driveseed_cookies()
                if cookies:
                    await ctx.add_cookies(cookies)
                    log.info("variants: using %d driveseed session cookies", len(cookies))
                else:
                    log.warning("variants: no driveseed session (state.json) -> file pages will show 'Login'")

            check_task = None
            if config.USE_DRIVESEED_SESSION and base_domain(parsed.hostname or "") in \
                    {base_domain(h) for h in config.DRIVESEED_ALLOWED_HOSTS}:
                check_task = asyncio.ensure_future(_session_check(ctx, final_url))

            tasks = [
                fetch_variant(ctx, key, origin + path.format(id=file_id), selector)
                for key, (path, selector) in VARIANTS.items()
                if not ENABLED_VARIANTS or key in ENABLED_VARIANTS
            ]
            results = dict(await asyncio.gather(*tasks))
            mark("variants")
            if check_task:
                try:
                    await asyncio.wait_for(check_task, timeout=3)
                except Exception:
                    check_task.cancel()
            if not any(results.values()) and VARIANT_RETRY_SEC > 0:
                _variants_blind_until = time.time() + VARIANT_RETRY_SEC
                log.warning("no variant returned a link; skipping the variant stage for the next %ds "
                            "(file pages probably need a valid login/captcha)", VARIANT_RETRY_SEC)
            log.info("getlink stage timings ms: %s", tm)
            return {"final_url": final_url, "file_id": file_id, "download_links": results,
                    "timings_ms": tm}, 200
        finally:
            await ctx.close()



async def _resolve_with_retry(start_url: str):
    body, status = await _resolve(start_url, BLOCK_THIRD_PARTY)
    if BLOCK_THIRD_PARTY and status == 504 and "Hop chain" in str(body.get("error", "")):
        log.warning("hop chain failed with third-party blocking/CSS skipping; retrying without it")
        body, status = await _resolve(start_url, False)
    return body, status


_inflight = {}   # url -> Task: the same link asked twice at once is resolved once


async def resolve(start_url: str):
    task = _inflight.get(start_url)
    if task is None:
        task = asyncio.ensure_future(_resolve_with_retry(start_url))
        _inflight[start_url] = task
        task.add_done_callback(lambda t, u=start_url: (_inflight.pop(u, None),
                                                       t.cancelled() or t.exception()))
    return await asyncio.shield(task)


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
    if CACHE_TTL > 0 and (any((body.get("download_links") or {}).values())
                          or (config.SKIP_VARIANTS and body.get("file_id"))):
        _cache[url] = (time.time() + CACHE_TTL, body)
        if len(_cache) > 200:   # keep it small
            _cache.pop(next(iter(_cache)))
