"""100acress.com source adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from scraper.crawl import discover_all_project_urls, listing_urls_for_category
from scraper.fetch import BASE_URL, Fetcher
from scraper.map_property import map_unit_records
from scraper.parse_html import parse_html_extras
from scraper.parse_rsc import extract_project
from scraper.sources.base import SourceAdapter
from scraper.store import ensure_source_meta

EXTRA_LISTING_SEEDS = [
    f"{BASE_URL}/projects/residential/?page=2",
    f"{BASE_URL}/projects/residential/?page=3",
    f"{BASE_URL}/projects/commercial/?page=2",
    f"{BASE_URL}/projects/commercial/?page=3",
    f"{BASE_URL}/projects-in-gurugram/",
    f"{BASE_URL}/projects-in-gurgaon/",
    f"{BASE_URL}/new-launch-projects-in-gurgaon/",
]


class Acress100Adapter(SourceAdapter):
    site = "100acress"

    def __init__(self, category: str = "all") -> None:
        self.category = category

    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        seeds = list(listing_urls_for_category(self.category))
        if self.category in {"all", "residential"}:
            seeds.extend(u for u in EXTRA_LISTING_SEEDS if u not in seeds)
        elif self.category == "commercial":
            seeds.extend(
                [
                    f"{BASE_URL}/projects/commercial/?page=2",
                    f"{BASE_URL}/projects/commercial/?page=3",
                ]
            )
        return discover_all_project_urls(fetcher, listing_urls=seeds, max_projects=max_projects)

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        project = extract_project(html)
        if not project:
            raise RuntimeError("Could not extract project payload from page")
        extras = parse_html_extras(html, project_name=project.get("projectName"))
        records = map_unit_records(project, extras, source_url=url)
        for rec in records:
            ensure_source_meta(rec, self.site, url)
        return records


def is_acress_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "100acress.com" in host
