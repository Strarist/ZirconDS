"""Housing.com source adapter (httpx-first; may be bot-blocked)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scraper.fetch import Fetcher
from scraper.normalize import (
    clean_rera_number,
    clean_text,
    derive_rera_state,
    is_registered_rera,
    normalize_city,
    parse_area,
    parse_bhk,
    parse_possession_date,
    slugify,
)
from scraper.schema import ensure_schema, urls_to_images
from scraper.sources.base import SourceAdapter
from scraper.store import ensure_source_meta

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://housing.com/in/buy/projects/gurgaon",
    "https://housing.com/in/buy/new-projects/gurgaon",
    "https://housing.com/in/buy/projects/noida",
]

PROJECT_PATH_RE = re.compile(
    r"https?://(?:www\.)?housing\.com/in/buy/projects/page/[^\"'\s<>]+",
    re.I,
)


class HousingAdapter(SourceAdapter):
    site = "housing"

    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for listing in LISTING_URLS:
            if max_projects is not None and len(found) >= max_projects:
                break
            try:
                html = fetcher.get(listing)
            except Exception as exc:
                logger.warning("Housing listing failed %s: %s", listing, exc)
                continue
            if "Security Alert" in html or len(html) < 5000:
                logger.warning(
                    "Housing.com blocked or returned a challenge page for %s "
                    "(status body looks like anti-bot). Skipping this source for now.",
                    listing,
                )
                continue
            for match in PROJECT_PATH_RE.findall(html):
                url = match.split("?")[0]
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if max_projects is not None and len(found) >= max_projects:
                        break
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(listing, a["href"]).split("?")[0]
                if "/in/buy/projects/page/" in href and href not in seen:
                    seen.add(href)
                    found.append(href)
                    if max_projects is not None and len(found) >= max_projects:
                        break
        logger.info("Housing discovered %s project URLs", len(found))
        return found

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        if "Security Alert" in html:
            raise RuntimeError("Housing.com anti-bot challenge page")

        # Prefer __NEXT_DATA__ when present
        blob = _next_data_project(html)
        soup = BeautifulSoup(html, "lxml")
        project_name = clean_text((blob or {}).get("name") or (blob or {}).get("projectName"))
        if not project_name and soup.find("h1"):
            project_name = clean_text(soup.find("h1").get_text(" ", strip=True))
        project_name = project_name or "Unknown Project"

        developer = clean_text((blob or {}).get("developer_name") or (blob or {}).get("builderName"))
        city = normalize_city(clean_text((blob or {}).get("city") or (blob or {}).get("city_name")))
        locality = clean_text((blob or {}).get("locality") or (blob or {}).get("locality_name"))
        rera_number = clean_rera_number(clean_text((blob or {}).get("rera_id") or (blob or {}).get("rera")))
        if not rera_number:
            m = re.search(r"RERA[^\n<:]{0,10}[:\s]+([A-Z0-9/.\-]+)", html, re.I)
            if m:
                rera_number = clean_rera_number(m.group(1))

        configs = (blob or {}).get("inventory_configs") or (blob or {}).get("configs") or []
        unit_rows: list[tuple[Optional[int], Optional[int], Optional[int]]] = []
        if isinstance(configs, list):
            for cfg in configs:
                if not isinstance(cfg, dict):
                    continue
                bhk = parse_bhk(cfg.get("apartment_type") or cfg.get("bhk") or cfg.get("title"))
                area = parse_area(cfg.get("carpet_area") or cfg.get("area") or cfg.get("built_up_area"))
                price = cfg.get("price")
                try:
                    price_i = int(float(price)) if price is not None else None
                except (TypeError, ValueError):
                    price_i = None
                unit_rows.append((bhk, area, price_i))
        if not unit_rows:
            unit_rows = [(None, None, None)]

        slug_base = slugify(urlparse(url).path.strip("/") or project_name)
        records: list[dict[str, Any]] = []
        used: set[str] = set()
        for bhk, area, price in unit_rows:
            slug = slug_base
            if bhk:
                slug = f"{slug_base}-{bhk}bhk"
            if area:
                slug = f"{slug}-{area}sqft"
            candidate = slug
            n = 2
            while candidate in used:
                candidate = f"{slug}-{n}"
                n += 1
            used.add(candidate)
            title = " - ".join(x for x in (project_name, locality, city) if x)
            record = ensure_schema(
                {
                    "title": title,
                    "slug": candidate,
                    "propertyType": "Apartment",
                    "listingType": "BUY",
                    "status": "ACTIVE",
                    "projectName": project_name,
                    "developer": developer,
                    "city": city,
                    "locality": locality,
                    "address": ", ".join(x for x in (locality, city) if x) or None,
                    "bhk": bhk,
                    "carpetArea": area,
                    "superBuiltUpArea": area,
                    "price": price,
                    "reraNumber": rera_number,
                    "reraState": derive_rera_state(rera_number),
                    "reraRegistered": is_registered_rera(rera_number),
                    "reraVerified": is_registered_rera(rera_number),
                    "seoTitle": title,
                    "h1": title,
                    "possessionDate": parse_possession_date((blob or {}).get("possession_date")),
                    "images": urls_to_images([]),
                }
            )
            ensure_source_meta(record, self.site, url)
            records.append(record)
        return records


def _next_data_project(html: str) -> Optional[dict[str, Any]]:
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    # Walk for a dict that looks like a project
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "projectName" in cur or ("name" in cur and ("inventory_configs" in cur or "rera_id" in cur)):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def is_housing_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "housing.com" in host
