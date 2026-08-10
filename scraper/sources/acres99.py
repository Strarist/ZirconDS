"""99acres.com source adapter (httpx + embedded floorPlans JSON)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scraper.fetch import Fetcher, is_challenge_body
from scraper.normalize import (
    clean_amenities,
    clean_rera_number,
    clean_text,
    derive_rera_state,
    is_registered_rera,
    map_possession_status,
    normalize_city,
    parse_area,
    parse_bathrooms,
    parse_bhk,
    parse_possession_date,
    price_to_inr,
    slugify,
)
from scraper.schema import ensure_schema, urls_to_images
from scraper.sources.base import SourceAdapter
from scraper.store import ensure_source_meta

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://www.99acres.com/new-projects-in-gurgaon-ffid",
    "https://www.99acres.com/new-projects-in-noida-ffid",
    "https://www.99acres.com/new-projects-in-delhi-ncr-ffid",
    "https://www.99acres.com/new-projects-in-greater-noida-ffid",
]

PDP_RE = re.compile(
    r"https?://(?:www\.)?99acres\.com/[a-z0-9\-]+-npxid-[a-z0-9]+",
    re.I,
)
RERA_RE = re.compile(r"\b((?:GGM|UPRERAPRJ|HRERA)[A-Z0-9/.\-]{6,})\b", re.I)


def _parse_json_at(text: str, start: int) -> Optional[Any]:
    if start < 0 or start >= len(text) or text[start] not in "{[":
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, min(len(text), start + 500_000)):
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
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _value_after_key(html: str, key: str) -> Optional[Any]:
    token = f'"{key}"'
    idx = html.find(token)
    if idx < 0:
        return None
    colon = html.find(":", idx + len(token))
    if colon < 0:
        return None
    j = colon + 1
    while j < len(html) and html[j] in " \n\r\t":
        j += 1
    return _parse_json_at(html, j)


def _string_after_key(html: str, key: str) -> Optional[str]:
    m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"')
    except json.JSONDecodeError:
        return clean_text(m.group(1))


def extract_unit_rows(
    html: str,
) -> list[tuple[Optional[int], Optional[int], Optional[int], Optional[int], Optional[int]]]:
    """Return (bhk, carpet, super, price, bathrooms) rows from floorPlans JSON."""
    plans = _value_after_key(html, "floorPlans")
    rows: list[tuple[Optional[int], Optional[int], Optional[int], Optional[int], Optional[int]]] = []
    seen: set[tuple[Optional[int], Optional[int], Optional[int], Optional[int]]] = set()
    configs: list[Any] = []
    if isinstance(plans, dict):
        data = plans.get("data") or {}
        if isinstance(data, dict):
            configs = data.get("configurations") or []
    if not isinstance(configs, list):
        return rows

    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        bhk = parse_bhk(cfg.get("bedroom") or cfg.get("label") or cfg.get("configLabel"))
        cfg_baths = parse_bathrooms(
            cfg.get("bathroom")
            or cfg.get("bathrooms")
            or cfg.get("washroom")
            or cfg.get("washrooms")
        )
        for tup in cfg.get("tuples") or []:
            if not isinstance(tup, dict):
                continue
            carpet = None
            super_area = None
            for area_type in tup.get("areaType") or []:
                if not isinstance(area_type, dict):
                    continue
                area_obj = area_type.get("area") or {}
                val = area_obj.get("min") if isinstance(area_obj, dict) else None
                parsed = parse_area(val)
                aid = str(area_type.get("id") or "").upper()
                if aid == "CARPET":
                    carpet = parsed
                elif aid in {"SUPER", "SUPER_BUILTUP", "SBA", "BUILTUP", "BUILT_UP"}:
                    super_area = parsed
            price = None
            price_obj = tup.get("price") or tup.get("minPrice")
            if isinstance(price_obj, dict):
                price = price_to_inr(str(price_obj.get("value") or price_obj.get("min") or ""))
            elif price_obj is not None:
                try:
                    price = int(float(price_obj))
                except (TypeError, ValueError):
                    price = price_to_inr(str(price_obj))
            bathrooms = parse_bathrooms(
                tup.get("bathroom")
                or tup.get("bathrooms")
                or tup.get("washroom")
                or tup.get("washrooms")
                or cfg_baths
            )
            key = (bhk, carpet, super_area, bathrooms)
            if key in seen:
                continue
            seen.add(key)
            rows.append((bhk, carpet, super_area, price, bathrooms))
    return rows


def extract_product_ld(html: str) -> dict[str, Any]:
    for m in re.finditer(
        r"<script[^>]+type=.application/ld\+json.[^>]*>(.*?)</script>",
        html,
        re.S | re.I,
    ):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return {}


class Acres99Adapter(SourceAdapter):
    site = "99acres"

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
                logger.warning("99acres listing failed %s: %s", listing, exc)
                blocked += 1
                continue
            if len(html) < 5000 or "Access Denied" in html or is_challenge_body(html):
                logger.warning("99acres blocked/challenge for %s", listing)
                blocked += 1
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
                if "-npxid-" in href.lower() and "99acres.com" in href and href not in seen:
                    seen.add(href)
                    found.append(href)
                    if max_projects is not None and len(found) >= max_projects:
                        break
        if not found and blocked:
            logger.warning(
                "99acres discovered 0 URLs (%s listing(s) blocked). Soft-failing this source.",
                blocked,
            )
        logger.info("99acres discovered %s project URLs", len(found))
        return found

    def fetch_project(self, fetcher: Fetcher, url: str) -> list[dict[str, Any]]:
        html = fetcher.get(url)
        if len(html) < 5000 or "Access Denied" in html:
            raise RuntimeError("99acres anti-bot / empty page")

        product = extract_product_ld(html)
        soup = BeautifulSoup(html, "lxml")
        project_name = clean_text(product.get("name")) or _string_after_key(html, "projectName")
        if not project_name and soup.find("h1"):
            project_name = clean_text(soup.find("h1").get_text(" ", strip=True))
        project_name = project_name or "Unknown Project"

        brand = product.get("brand") if isinstance(product.get("brand"), dict) else {}
        developer = clean_text(brand.get("name")) or _string_after_key(html, "builderName")

        # City/locality from URL / h1 / crumbs
        city = None
        locality = None
        path = urlparse(url).path.lower()
        for token, label in (
            ("gurgaon", "Gurgaon"),
            ("gurugram", "Gurgaon"),
            ("noida", "Noida"),
            ("greater-noida", "Greater Noida"),
            ("new-delhi", "New Delhi"),
            ("delhi", "Delhi"),
        ):
            if token in path:
                city = label
                break
        city = normalize_city(city)
        sec = re.search(r"sector[-\s]?(\d+[a-z]?)", path, re.I)
        if sec:
            locality = f"Sector {sec.group(1).upper()}"

        rera_number = None
        rera_hits = RERA_RE.findall(html)
        if rera_hits:
            # Prefer GGM / official-looking first hit near project body
            rera_number = clean_rera_number(rera_hits[0])

        possession_date = None
        poss = re.search(
            r"Possession[^A-Za-z0-9]{0,20}((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}|\d{4})",
            html,
            re.I,
        )
        if poss:
            possession_date = parse_possession_date(poss.group(1))
        possession_status = map_possession_status("under construction") if possession_date else None

        unit_price = None
        offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
        if offers.get("lowPrice") is not None:
            try:
                unit_price = int(float(offers["lowPrice"]))
            except (TypeError, ValueError):
                unit_price = price_to_inr(str(offers.get("lowPrice")))

        image_urls: list[str] = []
        img = product.get("image")
        if isinstance(img, str) and img.startswith("http"):
            image_urls.append(img)
        elif isinstance(img, list):
            for item in img:
                if isinstance(item, str) and item.startswith("http"):
                    image_urls.append(item)
        for tag in soup.select("img[src]"):
            src = tag.get("src") or ""
            if "99acres.com" in src or "imagecdn.99acres" in src:
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http") and src not in image_urls:
                    image_urls.append(src)
            if len(image_urls) >= 12:
                break

        unit_rows = extract_unit_rows(html)
        if not unit_rows:
            unit_rows = [(None, None, None, None, None)]

        amenities_raw: list[str] = []
        for m in re.finditer(r'"amenit(?:y|ies)Name"\s*:\s*"([^"]+)"', html, re.I):
            name = clean_text(m.group(1))
            if name and name not in amenities_raw:
                amenities_raw.append(name)
            if len(amenities_raw) >= 40:
                break
        amenities = clean_amenities(amenities_raw)

        slug_base = slugify(urlparse(url).path.strip("/") or project_name)
        records: list[dict[str, Any]] = []
        used: set[str] = set()
        for bhk, carpet, super_area, price, bathrooms in unit_rows:
            # Keep carpet out of superBuiltUpArea — match key is keyed on SBA only.
            area_for_slug = super_area if super_area is not None else carpet
            slug = slug_base
            if bhk:
                slug = f"{slug_base}-{bhk}bhk"
            if area_for_slug:
                slug = f"{slug}-{area_for_slug}sqft"
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
                    "bathrooms": bathrooms,
                    "carpetArea": carpet,
                    "superBuiltUpArea": super_area,  # None when only carpet is known
                    "possessionStatus": possession_status,
                    "possessionDate": possession_date,
                    "price": price if price is not None else unit_price,
                    "reraNumber": rera_number,
                    "reraState": derive_rera_state(rera_number),
                    "reraRegistered": is_registered_rera(rera_number),
                    "reraVerified": is_registered_rera(rera_number),
                    "amenities": amenities,
                    "images": urls_to_images(image_urls),
                    "seoTitle": title,
                    "h1": clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else title,
                }
            )
            ensure_source_meta(record, self.site, url)
            records.append(record)
        return records


def is_99acres_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "99acres.com" in host
