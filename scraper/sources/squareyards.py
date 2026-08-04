"""SquareYards.com source adapter (httpx + HTML extract)."""

from __future__ import annotations

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
    "https://www.squareyards.com/new-projects-in-gurgaon",
    "https://www.squareyards.com/new-projects-in-noida",
    "https://www.squareyards.com/new-projects-in-delhi",
    "https://www.squareyards.com/new-projects-in-greater-noida",
]

PDP_RE = re.compile(
    r"https?://(?:www\.)?squareyards\.com/[a-z\-]+-(?:residential|commercial)-property/[a-z0-9\-]+/\d+/project",
    re.I,
)
REL_PDP_RE = re.compile(
    r"/[a-z\-]+-(?:residential|commercial)-property/[a-z0-9\-]+/\d+/project",
    re.I,
)
BHK_AREA_RE = re.compile(
    r"(\d+)\s*BHK.{0,80}?([\d,\.]{3,6})\s*(?:Sq\.?\s*Ft|sq\.?\s*ft|sqft)",
    re.I | re.S,
)
RERA_RE = re.compile(r"\b((?:GGM|UPRERAPRJ|HRERA)[A-Z0-9/.\-]{6,})\b", re.I)


def extract_unit_rows(html: str) -> list[tuple[Optional[int], Optional[int]]]:
    rows: list[tuple[Optional[int], Optional[int]]] = []
    seen: set[tuple[Optional[int], Optional[int]]] = set()
    for m in BHK_AREA_RE.finditer(html):
        bhk = parse_bhk(m.group(1))
        area = parse_area(m.group(2))
        key = (bhk, area)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
        if len(rows) >= 30:
            break
    return rows


class SquareYardsAdapter(SourceAdapter):
    site = "squareyards"

    def discover(self, fetcher: Fetcher, max_projects: int | None = None) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        blocked = 0
        for listing in LISTING_URLS:
            if max_projects is not None and len(found) >= max_projects:
                break
            try:
                html = fetcher.get(listing)
            except Exception as exc:
                logger.warning("SquareYards listing failed %s: %s", listing, exc)
                blocked += 1
                continue
            if len(html) < 5000:
                logger.warning("SquareYards empty/challenge for %s", listing)
                blocked += 1
                continue
            for match in PDP_RE.findall(html):
                url = match.split("?")[0]
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if max_projects is not None and len(found) >= max_projects:
                        break
            for match in REL_PDP_RE.findall(html):
                url = urljoin("https://www.squareyards.com", match).split("?")[0]
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if max_projects is not None and len(found) >= max_projects:
                        break
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(listing, a["href"]).split("?")[0]
                if REL_PDP_RE.search(urlparse(href).path or "") and href not in seen:
                    seen.add(href)
                    found.append(href)
                    if max_projects is not None and len(found) >= max_projects:
                        break
        if not found and blocked:
            logger.warning(
                "SquareYards discovered 0 URLs (%s listing(s) blocked). Soft-failing this source.",
                blocked,
            )
        logger.info("SquareYards discovered %s project URLs", len(found))
        return found

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        if len(html) < 5000:
            raise RuntimeError("SquareYards empty/challenge page")

        soup = BeautifulSoup(html, "lxml")
        h1 = clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else None
        project_name = h1 or "Unknown Project"
        # Strip trailing location from h1 when present: "Name Sector 86, Gurgaon"
        m_name = re.match(r"^(.+?)\s+Sector\s+\d+[A-Za-z]?\s*,", project_name, re.I)
        if m_name:
            project_name = clean_text(m_name.group(1)) or project_name

        city = None
        locality = None
        path = urlparse(url).path.lower()
        for token, label in (
            ("gurgaon", "Gurgaon"),
            ("noida", "Noida"),
            ("greater-noida", "Greater Noida"),
            ("new-delhi", "New Delhi"),
            ("delhi", "Delhi"),
            ("mumbai", "Mumbai"),
            ("bangalore", "Bangalore"),
            ("bengaluru", "Bangalore"),
        ):
            if token in path or (h1 and token.replace("-", " ") in h1.lower()):
                city = label
                break
        city = normalize_city(city)
        sec = re.search(r"Sector\s+(\d+[A-Za-z]?)", h1 or "", re.I) or re.search(
            r"sector[-\s]?(\d+[a-z]?)", path, re.I
        )
        if sec:
            locality = f"Sector {sec.group(1).upper()}"

        developer = None
        # Prefer "About <Builder>" style blocks without inventing
        about = soup.find(string=re.compile(r"^About\s+", re.I))
        if about and about.parent:
            text = clean_text(about.parent.get_text(" ", strip=True))
            am = re.match(r"About\s+([A-Za-z0-9 &\.]{2,60})", text or "")
            if am:
                developer = clean_text(am.group(1))

        rera_number = None
        rera_hits = RERA_RE.findall(html)
        if rera_hits:
            rera_number = clean_rera_number(rera_hits[0])

        possession_date = None
        poss = re.search(
            r"Possession[^A-Za-z0-9]{0,40}((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
            html,
            re.I,
        )
        if poss:
            possession_date = parse_possession_date(poss.group(1))
        possession_status = map_possession_status("under construction") if possession_date else None

        unit_price = None
        price_m = re.search(
            r"(?:Prices?\s+begin\s+at|Starting\s+(?:price|from)|Entry\s+price)\s*(?:is\s*)?(?:₹|Rs\.?)\s*([\d\.,]+)\s*(Cr|Crore|Lakh|Lac)?",
            html,
            re.I,
        )
        if price_m:
            raw = price_m.group(1)
            unit = (price_m.group(2) or "").lower()
            try:
                num = float(raw.replace(",", ""))
                if unit.startswith("cr"):
                    unit_price = int(num * 10_000_000)
                elif unit.startswith("l"):
                    unit_price = int(num * 100_000)
                else:
                    unit_price = int(num)
            except ValueError:
                unit_price = price_to_inr(f"{raw} {unit}".strip())

        image_urls: list[str] = []
        for tag in soup.select("img[src]"):
            src = tag.get("src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if "squareyards.com" in src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)
            if len(image_urls) >= 12:
                break

        amenities: list[str] = []
        for li in soup.select("[class*=amenit] li, [class*=Amenit] li"):
            name = clean_text(li.get_text(" ", strip=True))
            if name and name not in amenities:
                amenities.append(name)
            if len(amenities) >= 40:
                break

        unit_rows = extract_unit_rows(html)
        if not unit_rows:
            unit_rows = [(None, None)]

        prop_type = "Apartment"
        if "commercial-property" in path:
            prop_type = "Commercial"

        slug_base = slugify(urlparse(url).path.strip("/") or project_name)
        records: list[dict[str, Any]] = []
        used: set[str] = set()
        for bhk, area in unit_rows:
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
                    "propertyType": prop_type,
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
                    "price": unit_price,
                    "reraNumber": rera_number,
                    "reraState": derive_rera_state(rera_number),
                    "reraRegistered": is_registered_rera(rera_number),
                    "reraVerified": is_registered_rera(rera_number),
                    "amenities": amenities,
                    "seoTitle": title,
                    "h1": h1 or title,
                }
            )
            record["imageUrls"] = list(image_urls)
            ensure_source_meta(record, self.site, url)
            records.append(record)
        return records


def is_squareyards_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "squareyards.com" in host
