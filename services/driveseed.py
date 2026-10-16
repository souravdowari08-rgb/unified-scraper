"""Service 3: DriveSeed file page -> Google Drive link  ->  GET|POST /api/link

FAST design (target: well under 1 second per call)
--------------------------------------------------
1. HTTP fast path (no browser at all)
   * a persistent keep-alive HTTP client (TLS/HTTP2 handshake paid once, cookies from state.json)
   * replays the *exact* request that the page's own generate('original') makes
     (the "recipe"), then GETs the token page and reads #gdlink from the raw HTML.
2. Browser fallback (only when the fast path cannot work or breaks)
   * ONE long-lived browser context (no new context per call), strict request filter:
     no images / fonts / css / media / third-party scripts / iframes / trackers
   * no fixed sleeps, no wait for the "load" event
   * while it runs, it LEARNS the recipe (the network request generate() fires), so the next
     call is served by the HTTP fast path.
3. In-flight de-duplication + result cache (same URL asked twice = one job).
"""
import asyncio
import html as htmllib
import json
import logging
import os
import re
import shutil
import time
from urllib.parse import urljoin, urlparse

from playwright.async_api import TimeoutError as PWTimeout

import browser
import common
import config

try:
    import httpx
except ImportError:          # the service still works (browser only) without httpx
    httpx = None

log = logging.getLogger("driveseed")

os.makedirs(config.SESSION_DIR, exist_ok=True)
STATE_FILE = os.path.join(config.SESSION_DIR, "state.json")
META_FILE = os.path.join(config.SESSION_DIR, "meta.json")
RECIPE_FILE = os.path.join(config.SESSION_DIR, "recipe.json")

# resource types the browser fallback never downloads
BLOCK_TYPES = {"image", "media", "font", "stylesheet", "texttrack", "manifest",
               "websocket", "eventsource", "ping", "other"}
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# headers worth copying from the browser's own generate() request into the recipe
KEEP_HEADERS = {"accept", "content-type", "x-requested-with", "x-csrf-token", "x-xsrf-token", "origin"}

_FAST_TIMEOUT = float(os.environ.get("DRIVESEED_FAST_TIMEOUT", 8))
_FAST_ENABLED = os.environ.get("DRIVESEED_FAST", "1") != "0"


class SessionInvalid(Exception):
    pass


class Busy(Exception):
    pass


class FastFail(Exception):
    """The HTTP fast path cannot answer this one -> use the browser."""


# ================================================================= session handling
def session_loaded():
    return os.path.exists(STATE_FILE)


def write_session(state, user_agent=None):
    global _ctx_version, _ua
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
    _ctx_version += 1          # browser context + http client are rebuilt with the new login
    _ua = None
    _drop_http_client()


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


# ================================================================= recipe (learned generate() request)
_recipe = None
_recipe_loaded = False


def _load_recipe():
    global _recipe, _recipe_loaded
    if _recipe_loaded:
        return _recipe
    _recipe_loaded = True
    try:
        with open(RECIPE_FILE, encoding="utf-8") as f:
            _recipe = json.load(f)
    except Exception:
        _recipe = None
    return _recipe


def _save_recipe(recipe):
    global _recipe, _recipe_loaded
    _recipe, _recipe_loaded = recipe, True
    try:
        if recipe is None:
            if os.path.exists(RECIPE_FILE):
                os.remove(RECIPE_FILE)
        else:
            with open(RECIPE_FILE, "w", encoding="utf-8") as f:
                json.dump(recipe, f)
    except Exception as e:
        log.warning("could not persist recipe: %s", e)


def file_id_of(url):
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _meta_csrf(page_html):
    m = (re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', page_html or "", re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token', page_html or "", re.I))
    return m.group(1) if m else None


def build_recipe(captured, page_url, page_html):
    """captured = {'method','url','headers','post_data'} of the request generate('original') made."""
    fid = file_id_of(page_url)
    csrf = _meta_csrf(page_html)

    def templ(s):
        if not isinstance(s, str):
            return s
        if fid:
            s = s.replace(fid, "{id}")
        if csrf:
            s = s.replace(csrf, "{csrf}")
        return s

    headers = {k.lower(): templ(v) for k, v in (captured.get("headers") or {}).items()
               if k.lower() in KEEP_HEADERS}
    r = {"method": captured["method"], "url": templ(captured["url"]),
         "headers": headers, "body": templ(captured.get("post_data")),
         "learned_at": int(time.time())}
    r["needs_page"] = "{csrf}" in json.dumps(r)
    return r


def _fill(s, fid, csrf):
    if not isinstance(s, str):
        return s
    return s.replace("{id}", fid).replace("{csrf}", csrf or "")


# ================================================================= HTTP fast path
_client = None
_client_ver = -1


def _drop_http_client():
    global _client
    c, _client = _client, None
    if c is not None:
        try:
            asyncio.get_running_loop().create_task(c.aclose())
        except RuntimeError:
            pass


def _user_agent():
    global _ua
    if _ua:
        return _ua
    try:
        with open(META_FILE, encoding="utf-8") as f:
            _ua = json.load(f).get("user_agent")
    except Exception:
        _ua = None
    return _ua or DEFAULT_UA


def _make_client():
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    jar = httpx.Cookies()
    for c in state.get("cookies", []):
        try:
            jar.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."), path=c.get("path", "/"))
        except Exception:
            pass
    try:
        import h2  # noqa: F401
        http2 = True
    except ImportError:
        http2 = False
    return httpx.AsyncClient(
        cookies=jar, http2=http2, follow_redirects=True,
        timeout=httpx.Timeout(_FAST_TIMEOUT, connect=4.0),
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120),
        headers={"User-Agent": _user_agent(), "Accept-Language": "en-US,en;q=0.9",
                 "Accept-Encoding": "gzip, deflate"})


def _http():
    global _client, _client_ver
    if _client is None or _client_ver != _ctx_version:
        _client = _make_client()
        _client_ver = _ctx_version
    return _client


_GDLINK_RES = (
    re.compile(r'<a\b[^>]*\bid=["\']gdlink["\'][^>]*\bhref=["\']([^"\']+)', re.I),
    re.compile(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\bid=["\']gdlink["\']', re.I),
)
_WORKER_RE = re.compile(r'\bworker_url\s*=\s*["\']([^"\']+)["\']')


def parse_token_page(text):
    gd = None
    for rx in _GDLINK_RES:
        m = rx.search(text)
        if m:
            gd = htmllib.unescape(m.group(1))
            break
    if not gd:
        return None
    w = _WORKER_RE.search(text)
    m = re.search(r"[?&]id=([\w-]+)|/d/([\w-]+)", gd)
    return {"gdrive": gd,
            "file_id": (m.group(1) or m.group(2)) if m else None,
            "direct": w.group(1) if w else None}


def _next_url_from(data):
    if isinstance(data, dict):
        return data.get("visit_url") or data.get("url")
    return None


async def _fast_generate_url(url, recipe, client):
    """Step 2 over HTTP: replay the learned generate() request -> token page URL."""
    fid = file_id_of(url)
    csrf = None
    if recipe.get("needs_page"):
        r = await client.get(url, headers={"Accept": "text/html"})
        if r.status_code != 200 or 'href="/logout"' not in r.text and "href='/logout'" not in r.text:
            raise SessionInvalid("not logged in (fast path)")
        csrf = _meta_csrf(r.text)
        if not csrf:
            raise FastFail("csrf token not found on page")

    headers = {k: _fill(v, fid, csrf) for k, v in recipe["headers"].items()}
    headers["Referer"] = url
    headers.setdefault("Origin", f"{urlparse(url).scheme}://{urlparse(url).netloc}")
    target = urljoin(url, _fill(recipe["url"], fid, csrf))
    body = _fill(recipe.get("body"), fid, csrf)
    r = await client.request(recipe["method"], target, headers=headers,
                             content=body.encode() if isinstance(body, str) else None)
    if r.status_code in (401, 403, 419):
        raise FastFail(f"generate replay rejected ({r.status_code})")
    if "/login" in str(r.url) and "/login" not in target:
        raise SessionInvalid("redirected to login (fast path)")
    try:
        nxt = _next_url_from(r.json())
    except Exception:
        nxt = None
    if not nxt:
        raise FastFail(f"generate replay gave no url (status {r.status_code})")
    return urljoin(url, nxt)


async def _fast_token_page(next_url, client, referer):
    r = await client.get(next_url, headers={"Referer": referer, "Accept": "text/html"})
    res = parse_token_page(r.text)
    if not res:
        raise FastFail("#gdlink not in the raw HTML of the token page (needs JS)")
    return res


async def _fast(url):
    if not (_FAST_ENABLED and httpx):
        raise FastFail("fast path disabled / httpx missing")
    recipe = _load_recipe()
    if not recipe:
        raise FastFail("no recipe learned yet")
    try:
        client = _http()
        t0 = time.perf_counter()
        nxt = await _fast_generate_url(url, recipe, client)
        t1 = time.perf_counter()
        res = await _fast_token_page(nxt, client, url)
        res["timings_ms"] = {"generate_http": int((t1 - t0) * 1000),
                             "token_page_http": int((time.perf_counter() - t1) * 1000)}
        return res, nxt
    except (FastFail, SessionInvalid):
        raise
    except Exception as e:                      # network error, timeout, bad json ...
        raise FastFail(f"{type(e).__name__}: {str(e)[:120]}")


# ================================================================= browser fallback
_ua = None
_ctx = None
_ctx_browser = None
_ctx_version = 0
_ctx_built_ver = -1
_ctx_lock = None


async def _get_context(b):
    """One long-lived context with the login; rebuilt only if the browser or the session changed."""
    global _ctx, _ctx_browser, _ctx_built_ver, _ctx_lock
    if _ctx_lock is None:
        _ctx_lock = asyncio.Lock()
    async with _ctx_lock:
        if _ctx is not None and _ctx_browser is b and _ctx_built_ver == _ctx_version and b.is_connected():
            return _ctx
        if _ctx is not None:
            try:
                await _ctx.close()
            except Exception:
                pass
        ua = _user_agent()
        _ctx = await b.new_context(storage_state=STATE_FILE, user_agent=ua)
        await _ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        _ctx_browser, _ctx_built_ver = b, _ctx_version
        return _ctx


def _make_route_filter(allowed_bases, strict):
    async def handler(route):
        req = route.request
        rt = req.resource_type
        if rt in BLOCK_TYPES:
            return await route.abort()
        if strict:
            host = urlparse(req.url).hostname or ""
            same_site = common.base_domain(host) in allowed_bases
            if rt in ("script", "xhr", "fetch") and not same_site:
                return await route.abort()          # ads, analytics, recaptcha, trackers...
            if rt == "document" and req.frame.parent_frame is not None:
                return await route.abort()          # no iframes
        return await route.continue_()
    return handler


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


_PROBE_JS = "() => ({gen: typeof generate === 'function', out: !!document.querySelector(\"a[href='/logout']\")})"


async def _browser_run(b, url, next_url=None, strict=True, learn=True):
    if not os.path.exists(STATE_FILE):
        raise SessionInvalid(f"{STATE_FILE} not found")

    context = await _get_context(b)
    allowed = {common.base_domain(urlparse(url).hostname)} | {common.base_domain(h) for h in
                                                               config.DRIVESEED_ALLOWED_HOSTS}
    page = await context.new_page()
    handler = _make_route_filter(allowed, strict)
    await page.route("**/*", handler)
    page.set_default_timeout(30000)
    captured = []
    tm = {}
    _t = time.perf_counter()

    def mark(name):
        nonlocal _t
        now = time.perf_counter()
        tm[name] = int((now - _t) * 1000)
        _t = now

    try:
        if not next_url:
            if learn:
                page.on("request", lambda r: captured.append(r)
                        if r.resource_type in ("xhr", "fetch") else None)

            # 1. file page: only wait until generate() exists (no "load", no fixed sleeps)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            probe = None
            try:
                await page.wait_for_function("() => typeof generate === 'function'", timeout=10000)
            except PWTimeout:
                pass
            probe = await page.evaluate(_PROBE_JS)
            mark("file_page")
            if not probe["out"]:
                await _save_debug(page, "debug_session")
                raise SessionInvalid("not logged in (no Logout link)")
            if not probe["gen"]:
                await _save_debug(page, "debug_session")
                raise RuntimeError("generate() is not defined on the file page")
            page_html = await page.content() if learn else ""

            # 2. the page's own generate() -- short back-off only if it returns nothing
            data = None
            for attempt in range(1, 4):
                captured.clear()
                try:
                    data = await page.evaluate("generate('original')")
                except Exception as e:
                    data = {"js_error": str(e)[:200]}
                next_url = _next_url_from(data)
                if next_url:
                    break
                log.info("generate() attempt %d gave no url: %s", attempt, str(data)[:200])
                await page.wait_for_timeout(300 * attempt)
            if not next_url:
                await _save_debug(page, "debug_generate")
                raise RuntimeError(f"generate() returned no url: {str(data)[:200]}")
            next_url = urljoin(url, next_url)
            mark("generate")

            if learn:
                await _learn_from(captured, next_url, url, page_html)
                mark("learn")

        # 3. token page -> #gdlink
        allowed.add(common.base_domain(urlparse(next_url).hostname))
        await page.goto(next_url, wait_until="commit", timeout=30000)
        try:
            await page.wait_for_selector("#gdlink", state="attached", timeout=30000)
        except PWTimeout:
            await _save_debug(page, "debug_token_page")
            raise RuntimeError("No #gdlink on the token page")

        mark("token_page")
        gd = htmllib.unescape(await page.get_attribute("#gdlink", "href") or "")
        try:
            direct = await page.evaluate("typeof worker_url !== 'undefined' ? worker_url : null")
        except Exception:
            direct = None
        m = re.search(r"[?&]id=([\w-]+)|/d/([\w-]+)", gd)
        log.info("browser timings ms: %s", tm)
        return {"gdrive": gd,
                "file_id": (m.group(1) or m.group(2)) if m else None,
                "direct": direct, "timings_ms": tm}
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _learn_from(captured, next_url, page_url, page_html):
    """Find which request generate() fired (the one whose response holds the token-page URL)."""
    for req in list(captured):
        try:
            resp = await req.response()
            text = await resp.text() if resp else ""
            if not _next_url_from(json.loads(text)):
                continue
            recipe = build_recipe({"method": req.method, "url": req.url, "headers": req.headers,
                                   "post_data": req.post_data}, page_url, page_html)
            _save_recipe(recipe)
            log.info("learned generate() recipe: %s %s", recipe["method"], recipe["url"])
            return
        except Exception:
            continue
    log.info("generate() made no replayable XHR/fetch request; browser path will keep being used")


# ================================================================= public entry point
_cache = {}       # url -> (stored_at, result)
_inflight = {}    # url -> Task


async def _resolve(url):
    t0 = time.perf_counter()

    # ---- fast path
    fast_reason = None
    try:
        res, _ = await _fast(url)
        res = dict(res, via="http", took_ms=int((time.perf_counter() - t0) * 1000))
        return res
    except SessionInvalid:
        raise
    except FastFail as e:
        fast_reason = str(e)
        if "rejected" in fast_reason or "no url" in fast_reason:
            _save_recipe(None)                  # recipe is stale -> relearn on this browser run
        log.info("fast path unavailable after %.0f ms (%s) -> browser",
                 (time.perf_counter() - t0) * 1000, fast_reason)

    # ---- browser fallback
    try:
        await asyncio.wait_for(browser.sem.acquire(), timeout=config.QUEUE_WAIT)
    except asyncio.TimeoutError:
        raise Busy("browser busy, try again shortly")
    try:
        last = None
        for attempt, strict in ((1, True), (2, False)):     # 2nd try: let third-party scripts load
            try:
                b = await browser.ensure_browser()
                result = await _browser_run(b, url, strict=strict, learn=_load_recipe() is None)
                last = None
                break
            except SessionInvalid:
                raise
            except BaseException as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                last = e
                log.warning("browser attempt %d failed: %s: %s", attempt, type(e).__name__, str(e)[:200])
        if last is not None:
            raise last
    finally:
        browser.sem.release()
    return dict(result, via="browser", fast_failed=fast_reason,
                took_ms=int((time.perf_counter() - t0) * 1000))


async def get_link(url):
    now = time.time()
    hit = _cache.get(url)
    if config.DRIVESEED_CACHE_TTL and hit and now - hit[0] < config.DRIVESEED_CACHE_TTL:
        return dict(hit[1], cached=True, via="cache", took_ms=0)

    if not session_loaded():
        raise SessionInvalid(f"{STATE_FILE} not found")

    task = _inflight.get(url)               # same URL already being resolved -> share the job
    if task is None:
        task = asyncio.ensure_future(_resolve(url))
        _inflight[url] = task
        task.add_done_callback(lambda _t, u=url: _inflight.pop(u, None))
    result = await asyncio.shield(task)

    if config.DRIVESEED_CACHE_TTL and result.get("gdrive"):
        _cache[url] = (time.time(), result)
        if len(_cache) > 200:
            _cache.pop(next(iter(_cache)))
    return result


async def warmup():
    """At startup: open the HTTP connection and (optionally) learn the recipe from a sample URL."""
    if not session_loaded():
        return
    if httpx and _FAST_ENABLED:
        try:
            await _http().head("https://" + sorted(config.DRIVESEED_ALLOWED_HOSTS)[0] + "/")
        except Exception:
            pass
    sample = os.environ.get("DRIVESEED_WARM_URL", "").strip()
    if sample and url_allowed(sample) and _load_recipe() is None:
        try:
            log.info("warm-up: learning recipe from DRIVESEED_WARM_URL")
            await get_link(sample)
        except Exception as e:
            log.warning("warm-up failed: %s", e)
