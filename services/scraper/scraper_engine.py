"""
scraper_engine.py — the ONE place a page is fetched or a browser launched.

Everything network-facing about scraping lives behind scrape(url, job_type):
robots.txt check -> per-domain rate limit -> headless-Chromium fetch
(Playwright, the spec's pick) -> BeautifulSoup extraction into the
structured shape store.py persists. The worker task calls scrape() and
nothing else, so tests monkeypatch this single seam (see conftest) and no
test ever launches a browser or touches the network; extract() is pure and
robots_allowed() takes its text through _fetch_robots_txt, so both are also
unit-testable offline.

Playwright is imported LAZILY inside fetch_page: the pip package installs
everywhere, but the browser binaries only exist in the Docker image
(`playwright install` in the Dockerfile) — importing this module must never
require them, or the CI suite and the build-time smoke test would.

Spec compliance notes:
  - "Respect robots.txt": fetched per target and evaluated for our user
    agent before every run; unreachable/absent robots.txt permits the fetch
    (the conventional permissive posture), a disallow raises
    RobotsDisallowedError and the run concludes as failed.
  - "reasonable rate limits per target domain": a per-process courtesy gap
    of SCRAPER_MIN_DELAY_SECONDS between fetches of the same host. With one
    worker process (compose default) that is a real per-domain limit; scale
    the worker out and it becomes per-process — a shared limiter would need
    Redis, deliberately not added yet.

Engine env (all read at call time so tests/compose can override):
  SCRAPER_USER_AGENT         identifies us to robots.txt and servers
  SCRAPER_TIMEOUT_SECONDS    navigation + robots fetch timeout (default 30)
  SCRAPER_MIN_DELAY_SECONDS  per-domain gap between fetches (default 2)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("scraper.engine")

MAX_LINKS = 200      # per document — enough for discovery, bounded storage
MAX_HEADINGS = 100


class ScrapeError(Exception):
    """A run-concluding scrape failure (fetch, navigation, robots denial)."""


class RobotsDisallowedError(ScrapeError):
    """robots.txt forbids fetching this URL for our user agent."""


def _user_agent() -> str:
    return os.getenv("SCRAPER_USER_AGENT", "CreditFlowScraper/0.1")


def _timeout() -> float:
    return float(os.getenv("SCRAPER_TIMEOUT_SECONDS", "30"))


def _min_delay() -> float:
    return float(os.getenv("SCRAPER_MIN_DELAY_SECONDS", "2"))


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

def _fetch_robots_txt(base_url: str) -> str | None:
    """The robots.txt body for a site, or None if there is none to honor
    (404/4xx or unreachable). Tests monkeypatch THIS to feed canned rules."""
    try:
        resp = httpx.get(
            f"{base_url}/robots.txt",
            headers={"User-Agent": _user_agent()},
            timeout=_timeout(),
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.warning("robots.txt fetch failed for %s (%s) — treating as absent",
                       base_url, exc)
        return None
    if resp.status_code >= 300:
        return None
    return resp.text


def robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    text = _fetch_robots_txt(f"{parts.scheme}://{parts.netloc}")
    if text is None:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    return parser.can_fetch(_user_agent(), url)


# --------------------------------------------------------------------------
# Per-domain rate limit (per-process — see module docstring)
# --------------------------------------------------------------------------

_last_fetch_at: dict[str, float] = {}
_rate_lock = threading.Lock()


def _rate_limit(host: str) -> None:
    """Sleep long enough that consecutive fetches of `host` are at least
    SCRAPER_MIN_DELAY_SECONDS apart. The lock only guards the bookkeeping;
    the sleep happens outside it."""
    delay = _min_delay()
    if delay <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        ready_at = max(_last_fetch_at.get(host, 0.0) + delay, now)
        _last_fetch_at[host] = ready_at
        wait = ready_at - now
    if wait > 0:
        time.sleep(wait)


# --------------------------------------------------------------------------
# Fetch + extract
# --------------------------------------------------------------------------

def fetch_page(url: str) -> dict:
    """{html, status_code, final_url} rendered by headless Chromium —
    JS-heavy pages come back as the DOM the user would see, not the empty
    shell curl would get."""
    from playwright.sync_api import sync_playwright  # lazy — see module docstring

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_user_agent())
                response = page.goto(url, timeout=_timeout() * 1000,
                                     wait_until="domcontentloaded")
                return {
                    "html": page.content(),
                    "status_code": response.status if response is not None else None,
                    "final_url": page.url,
                }
            finally:
                browser.close()
    except ScrapeError:
        raise
    except Exception as exc:  # noqa: BLE001 — any Playwright failure concludes the run
        raise ScrapeError(f"fetch failed: {exc}") from exc


def extract(html: str, url: str) -> dict:
    """Structured data out of raw HTML: title, meta description, headings,
    de-tagged visible text, and absolute http(s) links."""
    soup = BeautifulSoup(html or "", "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else None
    description = None
    meta = soup.find("meta", attrs={"name": "description"})
    if meta is not None:
        description = (meta.get("content") or "").strip() or None

    headings = [h.get_text(" ", strip=True)
                for h in soup.find_all(["h1", "h2", "h3"])][:MAX_HEADINGS]

    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        absolute = urljoin(url, a["href"])
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= MAX_LINKS:
                break

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())

    return {
        "title": title,
        "description": description,
        "headings": headings,
        "text": text,
        "links": links,
    }


def scrape(url: str, job_type: str = "page") -> dict:
    """THE seam the worker calls (and the only function tests need to mock):
    one validated URL in, one storable document dict out, ScrapeError out on
    any failure."""
    if not robots_allowed(url):
        raise RobotsDisallowedError(f"robots.txt disallows scraping {url}")
    _rate_limit(urlsplit(url).hostname or "")
    fetched = fetch_page(url)
    content = extract(fetched["html"], fetched.get("final_url") or url)
    return {
        **content,
        "html": fetched["html"],
        "status_code": fetched["status_code"],
        "final_url": fetched["final_url"],
    }
