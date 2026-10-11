"""Service 1: episode / download-link scraper for modpro pages  ->  GET /scrape?url="""
import asyncio
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


def fetch_soup(url, retries=3):
    for _ in range(retries):
        try:
            resp = session.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            if resp.status_code == 200 and "<html" in resp.text.lower():
                return BeautifulSoup(resp.text, "html.parser")
        except Exception:
            pass
    return None


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


def scrape_secondary_links(url):
    soup = fetch_soup(url)
    if soup is None:
        return []
    results = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and href.startswith(DOWNLOAD_PREFIX):
            results.append({"url": href, "text": a.get_text(" ", strip=True)})
    return results


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
                i["download_links"] = f.result()
            except Exception:
                i["download_links"] = []
    return {"regular": regular, "batch": batch}


async def scrape(url):
    """Async entry point: runs the blocking scraper in a worker thread."""
    return await asyncio.to_thread(scrape_main_page, url)
