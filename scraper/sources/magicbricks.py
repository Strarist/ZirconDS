"""MagicBricks.com source adapter (httpx + HTML/JSON extract)."""

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
    map_possession_status,
    normalize_city,
    parse_area,
    parse_bhk,
    parse_possession_date,
    price_to_inr,
    slugify,
)
from scraper.schema import ensure_schema
from scraper.sources.base import SourceAdapter
from scraper.store import ensure_source_meta

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://www.magicbricks.com/new-projects-Gurgaon",
    "https://www.magicbricks.com/new-projects-Gurgaon/page-2",
    "https://www.magicbricks.com/new-projects-Gurgaon/page-3",
    "https://www.magicbricks.com/new-projects-Noida",
    "https://www.magicbricks.com/new-projects-New-Delhi",
]

PDP_RE = re.compile(r"https?://(?:www\.)?magicbricks\.com/[a-z0-9\-]+-pdpid-[a-z0-9]+", re.I)
BHK_FLAT_RE = re.compile(r"(\d+)\s*BHK\s*(?:Flat|Apartment|Villa)?\s*([\d,]+)\s*sq\.?\s*ft", re.I)
RERA_RE = re.compile(r"RERA\s*ID\s*:\s*([A-Z0-9/.\-]+(?:\s+DATED\s+[0-9.]+)?)", re.I)


def _parse_json_object_at(text: str, start: int) -> Optional[dict[str, Any]]:
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, min(len(text), start + 250_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def extract_project_blob(html: str) -> dict[str, Any]:
    idx = html.find('"projectName"')
    if idx < 0:
        return {}
    start = html.rfind("{", 0, idx)
    obj = _parse_json_object_at(html, start)
    return obj or {}


def extract_unit_rows(html: str) -> list[tuple[Optional[int], Optional[int]]]:
    rows: list[tuple[Optional[int], Optional[int]]] = []
    seen: set[tuple[Optional[int], Optional[int]]] = set()
    for m in BHK_FLAT_RE.finditer(html):
        bhk = parse_bhk(m.group(1))
        area = parse_area(m.group(2))
        key = (bhk, area)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
    return rows


class MagicBricksAdapter(SourceAdapter):
    site = "magicbricks"

    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for listing in LISTING_URLS:
            if max_projects is not None and len(found) >= max_projects:
                break
            try:
                html = fetcher.get(listing)
            except Exception as exc:
                logger.warning("MagicBricks listing failed %s: %s", listing, exc)
                continue
            for match in PDP_RE.findall(html):
                url = match.split("?")[0]
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if max_projects is not None and len(found) >= max_projects:
                        break
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(listing, a["href"]).split("?")[0]
                if "-pdpid-" in href.lower() and "magicbricks.com" in href and href not in seen:
                    seen.add(href)
                    found.append(href)
                    if max_projects is not None and len(found) >= max_projects:
                        break
        logger.info("MagicBricks discovered %s project URLs", len(found))
        return found

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        blob = extract_project_blob(html)
        soup = BeautifulSoup(html, "lxml")
        project_name = clean_text(blob.get("projectName")) or (
            clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else None
        ) or "Unknown Project"
        developer = clean_text(blob.get("devName"))
        city = normalize_city(clean_text(blob.get("cityName")))
        locality = clean_text(blob.get("localityName") or blob.get("lmtDName"))
        lat = blob.get("psmLatitude")
        lng = blob.get("psmLongitude")
        try:
            latitude = float(lat) if lat not in (None, "") else None
        except (TypeError, ValueError):
            latitude = None
        try:
            longitude = float(lng) if lng not in (None, "") else None
        except (TypeError, ValueError):
            longitude = None

        rera_match = RERA_RE.search(html)
        rera_raw = rera_match.group(1) if rera_match else None
        rera_number = clean_rera_number(rera_raw)
        rera_state = derive_rera_state(rera_number or rera_raw)
        rera_registered = is_registered_rera(rera_raw)

        possession_raw = None
        poss = re.search(r"Possession\s+([A-Za-z]{3}'?\d{2})", html, re.I)
        if poss:
            possession_raw = poss.group(1).replace("'", " 20")
        possession_date = parse_possession_date(possession_raw)
        possession_status = map_possession_status("under construction") if possession_date else None

        min_price = blob.get("minPrice") or blob.get("schemaPriceMin")
        unit_price = None
        if min_price is not None:
            try:
                unit_price = int(float(min_price))
            except (TypeError, ValueError):
                unit_price = price_to_inr(str(min_price))

        image_urls: list[str] = []
        img = blob.get("image")
        if isinstance(img, str) and img.startswith("http"):
            image_urls.append(img)
        for tag in soup.select("img[src]"):
            src = tag.get("src") or ""
            if "mbimages" in src or "staticmb" in src:
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http") and src not in image_urls:
                    image_urls.append(src)
            if len(image_urls) >= 12:
                break

        unit_rows = extract_unit_rows(html)
        if not unit_rows:
            unit_rows = [(None, None)]

        slug_base = slugify(urlparse(url).path.strip("/") or project_name)
        records: list[dict[str, Any]] = []
        used: set[str] = set()
        for bhk, area in unit_rows:
            slug = slug_base
            if bhk:
                slug = f"{slug_base}-{bhk}bhk"
            if area:
                slug = f"{slug}-{area}sqft"
            n = 2
            candidate = slug
            while candidate in used:
                candidate = f"{slug}-{n}"
                n += 1
            used.add(candidate)

            title_bits = [project_name]
            if locality:
                title_bits.append(locality)
            if city:
                title_bits.append(city)
            title = " - ".join(title_bits)

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
                    "superBuiltUpArea": area,
                    "possessionStatus": possession_status,
                    "possessionDate": possession_date,
                    "latitude": latitude,
                    "longitude": longitude,
                    "price": unit_price,
                    "reraNumber": rera_number,
                    "reraState": rera_state,
                    "reraRegistered": rera_registered,
                    "reraVerified": rera_registered,
                    "seoTitle": title,
                    "h1": clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else title,
                }
            )
            record["imageUrls"] = list(image_urls)
            ensure_source_meta(record, self.site, url)
            records.append(record)
        return records


def is_magicbricks_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "magicbricks.com" in host
