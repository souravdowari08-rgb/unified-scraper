"""Service 1: episode / download-link scraper for modpro pages  ->  GET /scrape?url="""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

EPISODE_PREFIX = (
    "https://episodes.modpro.blog/",
    "https://links.modpro.blog/",
)
DOWNLOAD_PREFIX = "https://en.thenaukriadda.in/"
BATCH_KEYWORD = "batch/zip file"


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def fetch_html(url, retries=3):
    """Return (soup_or_None, reason). reason explains the last failure."""
    reason = "unknown"
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20, headers=HEADERS)
            if resp.status_code == 200 and "<html" in resp.text.lower():
                return BeautifulSoup(resp.text, "html.parser"), None
            reason = f"http {resp.status_code}" if resp.status_code != 200 else "200 but no <html> (blocked?)"
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(0.5 * (attempt + 1))
    return None, reason


def fetch_soup(url, retries=3):
    return fetch_html(url, retries)[0]


def starts_with_prefix(href, prefixes):
    href_lower = href.lower().rstrip("/")
    return any(href_lower.startswith(p.lower().rstrip("/")) for p in prefixes)


def find_heading_before(link_tag):
    wrapper = link_tag
    while wrapper.parent and wrapper.parent.name in ("a", "span", "strong", "em"):
        wrapper = wrapper.parent
    wrapper = wrapper.parent
    if wrapper is None:
        return None
    prev = wrapper.previous_sibling
    while prev:
        if hasattr(prev, "get_text"):
            tx = prev.get_text(" ", strip=True)
            if tx and len(tx) > 3:
                return tx
        if isinstance(prev, str):
            tx = prev.strip()
            if len(tx) > 3:
                return tx
        prev = prev.previous_sibling
    return None


def is_batch_tag(tag):
    combined = (tag.get_text(" ", strip=True) + " " + tag.parent.get_text(" ", strip=True)).lower()
    return BATCH_KEYWORD in combined


def extract_links(soup):
    results = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and href.startswith(DOWNLOAD_PREFIX):
            results.append({"url": href, "text": a.get_text(" ", strip=True)})
    return results


def scrape_secondary_links(url):
    soup, reason = fetch_html(url)
    if soup is None:
        return [], reason
    return extract_links(soup), None


def scrape_main_page(url):
    soup = fetch_soup(url)
    if soup is None:
        return {"regular": [], "batch": [], "error": "Unable to load main page (protection/timeout)."}
    regular, batch = [], []
    for tag in soup.find_all("a"):
        href = tag.get("href")
        if href and starts_with_prefix(href, EPISODE_PREFIX):
            entry = {
                "heading": find_heading_before(tag),
                "episode_link": href,
                "episode_text": tag.get_text(" ", strip=True),
                "download_links": [],
            }
            (batch if is_batch_tag(tag) else regular).append(entry)

    all_items = regular + batch
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(scrape_secondary_links, i["episode_link"]): i for i in all_items}
        for f, i in futures.items():
            try:
                links, reason = f.result()
                i["download_links"] = links
                if reason:
                    i["fetch_error"] = reason
            except Exception as e:
                i["download_links"] = []
                i["fetch_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return {"regular": regular, "batch": batch}


async def _browser_links(ctx, url):
    """Fallback: open the page in the shared Chromium (a real browser fingerprint)."""
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_selector(f"a[href^='{DOWNLOAD_PREFIX}']", timeout=10000)
        except Exception:
            pass  # maybe still on a challenge page / no links: parse what we have
        return extract_links(BeautifulSoup(await page.content(), "html.parser"))
    finally:
        await page.close()


async def _browser_fallback(items):
    import browser  # local import: keeps this module usable without playwright
    b = await browser.ensure_browser()
    ctx = await b.new_context(
        user_agent=BROWSER_UA, viewport={"width": 1280, "height": 720}, locale="en-US")

    async def block_heavy(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_heavy)
    gate = asyncio.Semaphore(3)

    async def one(item):
        async with gate:
            try:
                links = await _browser_links(ctx, item["episode_link"])
            except Exception as e:
                item["fetch_error"] = f"{item.get('fetch_error', '')} | browser: {type(e).__name__}: {str(e)[:60]}"
                return
            if links:
                item["download_links"] = links
                item.pop("fetch_error", None)
                item["fetched_via"] = "browser"

    try:
        await asyncio.gather(*(one(i) for i in items))
    finally:
        await ctx.close()


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


async def scrape(url):
    """Async entry point: plain-HTTP scrape in a worker thread, then a browser retry for blocked pages."""
    data = await asyncio.to_thread(scrape_main_page, url)
    failed = [i for i in data.get("regular", []) + data.get("batch", []) if not i["download_links"]]
    if failed:
        try:
            await _browser_fallback(failed)
        except Exception as e:
            data["browser_fallback_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return data
