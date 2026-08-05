"""100acress.com source adapter."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from scraper.crawl import discover_all_project_urls, listing_urls_for_category, normalize_project_url
from scraper.fetch import BASE_URL, Fetcher
from scraper.map_property import map_unit_records
from scraper.parse_html import parse_html_extras
from scraper.parse_rsc import extract_project
from scraper.sources.base import SourceAdapter
from scraper.store import ensure_source_meta

logger = logging.getLogger(__name__)

EXTRA_LISTING_SEEDS = [
    f"{BASE_URL}/projects/residential/?page=2",
    f"{BASE_URL}/projects/residential/?page=3",
    f"{BASE_URL}/projects/commercial/?page=2",
    f"{BASE_URL}/projects/commercial/?page=3",
    f"{BASE_URL}/projects-in-gurugram/",
    f"{BASE_URL}/projects-in-gurgaon/",
    f"{BASE_URL}/new-launch-projects-in-gurgaon/",
]


class NonProjectPageError(RuntimeError):
    """Raised when a URL is not a project PDP (listing hub, marketing, empty RSC)."""


def _build_listing_seeds(category: str) -> list[str]:
    seeds = list(listing_urls_for_category(category))
    if category in {"all", "residential"}:
        seeds.extend(u for u in EXTRA_LISTING_SEEDS if u not in seeds)
    elif category == "commercial":
        seeds.extend(
            [
                f"{BASE_URL}/projects/commercial/?page=2",
                f"{BASE_URL}/projects/commercial/?page=3",
            ]
        )
    return seeds


def listing_seed_urls(category: str) -> set[str]:
    """Normalized listing seed URLs — must never enter the scrape queue."""
    out: set[str] = set()
    for seed in _build_listing_seeds(category):
        parsed = urlparse(seed)
        bare = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        out.add(bare)
        out.add(bare + "/")
        if parsed.query:
            out.add(f"{bare}?{parsed.query}")
            out.add(f"{bare}/?{parsed.query}")
    return out


def filter_listing_seeds(urls: list[str], seeds: set[str]) -> list[str]:
    """Drop URLs that are listing seeds (crawl-only, not project PDPs)."""
    filtered: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        key_slash = key + "/"
        if key in seeds or key_slash in seeds:
            logger.debug("Dropping listing seed from project queue: %s", url)
            continue
        # Also reject if normalize would not treat path as project (safety)
        if normalize_project_url(url) is None:
            logger.debug("Dropping non-project URL from queue: %s", url)
            continue
        filtered.append(url)
    return filtered


class Acress100Adapter(SourceAdapter):
    site = "100acress"

    def __init__(self, category: str = "all") -> None:
        self.category = category

    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        seed_list = _build_listing_seeds(self.category)
        seeds = listing_seed_urls(self.category)
        urls = discover_all_project_urls(fetcher, listing_urls=seed_list, max_projects=max_projects)
        before = len(urls)
        urls = filter_listing_seeds(urls, seeds)
        dropped = before - len(urls)
        if dropped:
            logger.info("100acress filtered %s listing/non-project URL(s) from discovery", dropped)
        return urls

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        project = extract_project(html)
        if not project:
            raise NonProjectPageError(f"No project payload at {url}")
        extras = parse_html_extras(html, project_name=project.get("projectName"))
        records = map_unit_records(project, extras, source_url=url)
        for rec in records:
            ensure_source_meta(rec, self.site, url)
        return records


def is_acress_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "100acress.com" in host
