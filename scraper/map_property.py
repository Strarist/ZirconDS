"""Map 100acress project + BHK row into the website property schema."""

from __future__ import annotations

import re
from typing import Any, Optional

from scraper.normalize import (
    build_address,
    clean_rera_number,
    clean_text,
    derive_rera_state,
    extract_city_from_text,
    extract_locality,
    extract_pincode,
    is_registered_rera,
    map_possession_status,
    map_property_type,
    normalize_city,
    parse_area,
    parse_bhk,
    parse_possession_date,
    price_to_inr,
    sanitize_coords,
    slugify,
    strip_decorative,
)
from scraper.parse_html import HtmlExtras
from scraper.parse_rsc import location_blurbs, resolve_amenities, resolve_bhk_details, resolve_highlights
from scraper.schema import ensure_schema, urls_to_images


def _seo_keywords(
    project_name: str,
    locality: Optional[str],
    city: Optional[str],
    bhk: Optional[int],
    property_type: Optional[str],
) -> list[str]:
    keywords: list[str] = []
    if project_name and city:
        keywords.append(f"{project_name} {city}")
    if project_name and locality:
        keywords.append(f"{project_name} {locality}")
    if property_type and city:
        keywords.append(f"Luxury {property_type}s {city}" if property_type == "Apartment" else f"{property_type} {city}")
    if bhk and city:
        keywords.append(f"{bhk} BHK {city}")
    # Unique preserve
    seen: set[str] = set()
    out: list[str] = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:8]


def _build_title(
    html_title: Optional[str],
    project_name: str,
    locality: Optional[str],
    city: Optional[str],
    property_type: Optional[str],
) -> str:
    if html_title:
        # Strip site suffix noise
        title = html_title.split("|")[0].strip() if "100acress" in html_title.lower() else html_title.strip()
        title = title.replace(" | 100acress.com", "").strip()
        title = strip_decorative(title) or title
        if project_name.lower() in title.lower():
            return title
    bits = [project_name]
    if locality:
        bits.append(locality)
    if city:
        bits.append(city)
    head = " ".join(bits)
    suffix = property_type or "Apartments"
    return f"{head} | Luxury {suffix}"


def _build_h1(
    html_h1: Optional[str],
    project_name: str,
    locality: Optional[str],
    city: Optional[str],
    bhk: Optional[int],
    bhk_types: list[int],
) -> str:
    loc_bits = [x for x in (locality, city) if x]
    loc = " ".join(loc_bits)
    if len(bhk_types) > 1:
        bhk_label = " & ".join(str(b) for b in sorted(set(bhk_types)))
        return f"{project_name} {loc} — Luxury {bhk_label} BHK Residences".strip()
    if bhk:
        return f"{project_name} {loc} — Luxury {bhk} BHK Residences".strip()
    cleaned_h1 = strip_decorative(html_h1)
    if cleaned_h1 and len(cleaned_h1) > 5:
        cleaned = cleaned_h1
        # Insert space if "NameSector" glued together
        if project_name and cleaned.startswith(project_name):
            rest = cleaned[len(project_name) :].lstrip(" ,-")
            if rest:
                return f"{project_name} {rest}".strip()
        return cleaned
    return f"{project_name} {loc}".strip()


def _about_section(overview: Optional[str], project_name: str, max_len: int = 700) -> Optional[str]:
    text = clean_text(overview)
    if not text:
        return None
    if len(text) <= max_len:
        return text
    # Cut on sentence boundary when possible
    cut = text[:max_len]
    if "." in cut:
        cut = cut[: cut.rfind(".") + 1]
    return cut.strip()


def _location_advantages(blurbs: list[str], overview: Optional[str]) -> Optional[str]:
    if blurbs:
        return clean_text(" ".join(blurbs))
    if overview:
        # Use a middle sentence mentioning connectivity if present
        for sentence in overview.split("."):
            low = sentence.lower()
            if any(k in low for k in ("connect", "located", "proximity", "metro", "airport", "expressway")):
                return clean_text(sentence + ".")
    return None


def _address_section(
    project_name: str,
    address: Optional[str],
    blurbs: list[str],
) -> Optional[str]:
    parts: list[str] = []
    if address:
        if project_name.lower() not in address.lower():
            parts.append(f"{project_name}, {address}.")
        else:
            parts.append(address if address.endswith(".") else address + ".")
    metro = next((b for b in blurbs if "metro" in b.lower()), None)
    if metro:
        label = metro if metro.endswith(".") else metro + "."
        if "nearest" not in label.lower():
            label = f"Nearest transit: {label}"
        parts.append(label)
    return clean_text(" ".join(parts)) if parts else None


def _image_url_from_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        url = value.strip()
        if url.startswith("http"):
            return url
        return None
    if isinstance(value, dict):
        for key in ("cdn_url", "url", "secure_url", "src"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip().startswith("http"):
                return candidate.strip()
    return None


def collect_project_image_urls(project: dict[str, Any], row: dict[str, Any] | None = None) -> list[str]:
    """Gather unique project gallery + unit floorplan image URLs."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        url = _image_url_from_value(value)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    for key in ("frontImage", "heroBanner", "thumbnailImage", "highlightImage"):
        add(project.get(key))

    gallery = project.get("projectGallery") or project.get("gallery") or []
    if isinstance(gallery, list):
        for item in gallery:
            add(item)
    elif isinstance(gallery, dict):
        add(gallery)

    if row:
        for key in ("floorplan_Image", "floorplan_image", "floorPlanImage", "floor_plan"):
            add(row.get(key))

    return urls


def map_unit_records(
    project: dict[str, Any],
    html: HtmlExtras,
    source_url: str,
) -> list[dict[str, Any]]:
    project_name = clean_text(project.get("projectName")) or "Unknown Project"
    project_url = clean_text(project.get("projectUrl")) or slugify(project_name)
    developer = clean_text(project.get("builderName")) or clean_text(
        (project.get("builder") or {}).get("name") if isinstance(project.get("builder"), dict) else None
    )
    raw_address = clean_text(project.get("projectAddress"))
    city = normalize_city(clean_text(project.get("city"))) or extract_city_from_text(
        html.title, raw_address, html.meta_description, html.overview, source_url
    )
    locality = extract_locality(raw_address, html.title) or extract_locality(html.overview)
    pincode = extract_pincode(raw_address) or extract_pincode(html.overview or "")
    address = build_address(project_name, locality, raw_address, city, pincode)

    raw_type = project.get("propertyTypeName") or project.get("projectType") or project.get("type")
    if isinstance(project.get("propertyType"), dict):
        raw_type = raw_type or project["propertyType"].get("typeName")
    property_type = map_property_type(raw_type) or "Apartment"

    possession_status = map_possession_status(project.get("project_Status"))
    possession_date = parse_possession_date(project.get("possessionDate"))
    rera_number = clean_rera_number(project.get("projectReraNo"))
    rera_state = derive_rera_state(rera_number or project.get("projectReraNo"))
    rera_registered = is_registered_rera(project.get("projectReraNo"))
    rera_verified = rera_registered

    amenities = resolve_amenities(project)
    highlights = resolve_highlights(project)
    blurbs = location_blurbs(project)
    overview = html.overview
    # Description ref placeholders like "$35" are useless
    desc_raw = project.get("projectDescription")
    if isinstance(desc_raw, str) and not desc_raw.startswith("$") and len(desc_raw) > 40:
        overview = overview or desc_raw
    description = clean_text(overview)
    about = _about_section(overview, project_name)

    faqs = []
    for faq in html.faqs:
        answer = faq.get("answer", "")
        # Normalize ISO timestamps inside FAQ answers to YYYY-MM-DD
        answer = re.sub(
            r"(\d{4}-\d{2}-\d{2})T[\d:.]+Z?",
            r"\1",
            answer,
        )
        faqs.append({"question": faq["question"], "answer": clean_text(answer) or answer})
    lat, lng = sanitize_coords(html.latitude, html.longitude)
    bhk_rows = resolve_bhk_details(project)
    if not bhk_rows:
        # Still emit one project-level record with null unit fields
        bhk_rows = [{}]

    all_bhks = [b for b in (parse_bhk(r.get("bhk_type")) for r in bhk_rows) if b]

    records: list[dict[str, Any]] = []
    used_slugs: set[str] = set()
    for row in bhk_rows:
        bhk = parse_bhk(row.get("bhk_type"))
        area = parse_area(row.get("bhk_Area") or row.get("bhk_area") or row.get("area"))
        # Unit price only — never invent from project minPrice (would duplicate across BHKs)
        unit_price = price_to_inr(row.get("price"), row.get("priceUnit") or row.get("price_unit"))
        image_urls = collect_project_image_urls(project, row)

        slug_base = slugify(project_url)
        candidates: list[str] = []
        if bhk and area:
            candidates.append(f"{slug_base}-{bhk}bhk")
            candidates.append(f"{slug_base}-{bhk}bhk-{area}sqft")
        elif bhk:
            candidates.append(f"{slug_base}-{bhk}bhk")
        else:
            candidates.append(slug_base)
        slug = candidates[0]
        for candidate in candidates:
            if candidate not in used_slugs:
                slug = candidate
                break
        else:
            n = 2
            while True:
                suffix_base = candidates[-1]
                candidate = f"{suffix_base}-{n}"
                if candidate not in used_slugs:
                    slug = candidate
                    break
                n += 1
        used_slugs.add(slug)

        title = _build_title(html.title, project_name, locality, city, property_type)
        h1 = _build_h1(html.h1, project_name, locality, city, bhk, all_bhks)

        seo_title = html.title or title
        if seo_title and "100acress" in seo_title.lower():
            seo_title = title
        seo_title = strip_decorative(seo_title) or seo_title
        seo_description = html.meta_description
        if not seo_description:
            bits = [f"Explore {project_name}"]
            if locality and city:
                bits.append(f"in {locality} {city}")
            bits.append("Check latest price, floor plans, amenities, location, possession date and RERA details.")
            seo_description = " ".join(bits)

        record = {
            "title": title,
            "slug": slug,
            "propertyType": property_type,
            "listingType": "BUY",
            "status": "ACTIVE",
            "projectName": project_name,
            "developer": developer,
            "city": city,
            "locality": locality,
            "pincode": pincode,
            "address": address,
            "description": description,
            "featured": None,
            "hotDeal": None,
            "exclusive": None,
            "vastuCompliant": None,
            "bhk": bhk,
            "bathrooms": None,
            "carpetArea": None,
            "builtUpArea": None,
            "superBuiltUpArea": area,
            "propertyAge": None,
            "floor": None,
            "totalFloors": None,
            "totalUnits": None,
            "parkingSpaces": None,
            "parkingType": None,
            "furnishing": None,
            "possessionStatus": possession_status,
            "possessionDate": possession_date,
            "launchDate": None,
            "facing": None,
            "latitude": lat,
            "longitude": lng,
            "price": unit_price,
            "maintenancePerMonth": None,
            "stampDuty": None,
            "registrationCharges": None,
            "reraNumber": rera_number,
            "reraState": rera_state,
            "reraRegistered": rera_registered,
            "reraVerified": rera_verified,
            "bankApprovals": [],
            "amenities": amenities,
            "overlooking": [],
            "images": urls_to_images(image_urls),
            "seoTitle": seo_title,
            "seoDescription": seo_description,
            "seoKeywords": _seo_keywords(project_name, locality, city, bhk, property_type),
            "h1": h1,
            "aboutSection": about,
            "highlights": highlights,
            "locationAdvantages": _location_advantages(blurbs, overview),
            "whyInvest": None,
            "whyConsider": None,
            "addressSection": _address_section(project_name, address, blurbs),
            "faqs": faqs,
        }
        cleaned = ensure_schema(record)
        records.append(cleaned)
    return records
