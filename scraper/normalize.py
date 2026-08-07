"""Normalization helpers for enums, prices, dates, locations."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

PROPERTY_TYPE_MAP = {
    "residential flats": "Apartment",
    "residential flat": "Apartment",
    "apartment": "Apartment",
    "apartments": "Apartment",
    "flat": "Apartment",
    "flats": "Apartment",
    "villa": "Villa",
    "villas": "Villa",
    "plot": "Plot",
    "plots": "Plot",
    "deen dayal plots": "Plot",
    "deen dayal plot": "Plot",
    "penthouse": "Penthouse",
    "studio": "Studio",
    "duplex": "Duplex",
    "independent floor": "Independent Floor",
    "independent floors": "Independent Floor",
    "floor": "Independent Floor",
    "commercial": "Commercial",
    "commercial property": "Commercial",
    "office": "Commercial",
    "shop": "Commercial",
    "sco": "SCO",
    "sco plot": "SCO",
    "sco plots": "SCO",
    "senior living": "Senior Living",
}

POSSESSION_MAP = {
    "underconstruction": "UNDER_CONSTRUCTION",
    "under_construction": "UNDER_CONSTRUCTION",
    "under construction": "UNDER_CONSTRUCTION",
    "new launch": "NEW_LAUNCH",
    "newlaunch": "NEW_LAUNCH",
    "new_launch": "NEW_LAUNCH",
    "upcoming": "NEW_LAUNCH",
    "ready": "READY_TO_MOVE",
    "ready to move": "READY_TO_MOVE",
    "readytomove": "READY_TO_MOVE",
    "ready_to_move": "READY_TO_MOVE",
    "completed": "READY_TO_MOVE",
    "resale": "READY_TO_MOVE",
}

RERA_STATE_HINTS = {
    "HARERA": "Haryana",
    "HRERA": "Haryana",
    "UPRERA": "Uttar Pradesh",
    "MAHARERA": "Maharashtra",
    "K-RERA": "Karnataka",
    "KRERA": "Karnataka",
    "TNRERA": "Tamil Nadu",
    "RERARAJ": "Rajasthan",
    "GERA": "Gujarat",
    "WBHIRA": "West Bengal",
    "ORERA": "Odisha",
}

CITY_ALIASES = {
    "gurugram": "Gurgaon",
    "gurgaon": "Gurgaon",
    "bengaluru": "Bangalore",
    "bangalore": "Bangalore",
    "bombay": "Mumbai",
    "new delhi": "Delhi",
    # NOTE: "ncr" is intentionally NOT mapped to "Delhi".
    # NCR (National Capital Region) encompasses Gurgaon, Noida, Faridabad etc.
    # Mapping it to Delhi would break cross-source match keys for those cities.
}

# Cities expected by the destination Admin → Cities catalog on export/copy.
EXPORT_CITY_ALIASES = {
    "delhi": "Delhi NCR",
    "new delhi": "Delhi NCR",
}

# UI scrapes often append "+21 More" style placeholders — never real amenities.
AMENITY_JUNK_RE = re.compile(r"^\+\s*\d+\s*more$", re.I)

SECTOR_RE = re.compile(r"\bSector\s*[\-]?\s*([0-9]+[A-Za-z]?)\b", re.I)
PINCODE_RE = re.compile(r"\b([1-9][0-9]{5})\b")
BHK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BHK", re.I)
CR_RE = re.compile(r"([\d,.]+)\s*(cr|crore)s?", re.I)
LAKH_RE = re.compile(r"([\d,.]+)\s*(l|lac|lakh)s?", re.I)
AREA_RANGE_RE = re.compile(r"[\d.]+\s*[-–—to]+\s*[\d.]+", re.I)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001FA00-\U0001FAFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)
PRICE_ON_REQUEST = {
    "on request",
    "price on request",
    "por",
    "call for price",
    "call for pricing",
    "price on call",
    "call for prices",
    "contact for price",
    "ask for price",
    "na",
    "n/a",
}

# Site-wide default pins reused by portals when a project has no real map coordinates.
# Add new entries here as discovered; duplicates are avoided via the 1e-6 tolerance check
# in sanitize_coords().
PLACEHOLDER_COORDS = {
    (28.4595, 77.0266),   # 100acress.com Gurgaon default
    (28.5355, 77.3910),   # 99acres.com Noida default
    (28.6139, 77.2090),   # Generic Delhi city-centre default (shared by several portals)
}


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def strip_decorative(value: Optional[str]) -> Optional[str]:
    """Remove emoji / decorative symbols from headings and titles."""
    text = clean_text(value)
    if not text:
        return None
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -\u2013\u2014")
    return text or None


def clean_rera_number(raw: Optional[str]) -> Optional[str]:
    """Normalize noisy RERA strings; return None for undefined/placeholder values."""
    text = clean_text(raw)
    if not text:
        return None
    lowered = text.lower().lstrip("$").strip()
    if lowered in {"undefined", "null", "none", "na", "n/a", "applied", "pending"}:
        return None
    cleaned = text
    cleaned = re.sub(r"(?i)^\$?undefined$", "", cleaned).strip()
    cleaned = re.sub(r"(?i)^rera\s*reg(?:istration)?\s*no\.?\s*", "", cleaned)
    cleaned = re.sub(r"(?i)^rera\s*(?:no\.?|number)\s*[:\-]?\s*", "", cleaned)
    cleaned = re.sub(r"(?i)\bdated\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;-")
    return cleaned or None


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def normalize_city(value: Optional[str]) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    key = text.lower()
    return CITY_ALIASES.get(key, text.title() if text.islower() else text)


def export_city(value: Optional[str]) -> Optional[str]:
    """Map stored city names to destination Admin → Cities labels on export."""
    text = clean_text(value)
    if not text:
        return None
    return EXPORT_CITY_ALIASES.get(text.lower(), text)


def clean_amenity_name(value: Any) -> Optional[str]:
    """Return a real amenity label, or None for junk / empty placeholders."""
    text = clean_text(str(value) if value is not None else None)
    if not text:
        return None
    if AMENITY_JUNK_RE.match(text):
        return None
    return text


def clean_amenities(values: Any) -> list[str]:
    """Dedupe and strip amenity junk like '+22 More'."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("title") or item.get("label")
        else:
            raw = item
        name = clean_amenity_name(raw)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def map_property_type(raw: Optional[str]) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    return PROPERTY_TYPE_MAP.get(text.lower(), text.title())


def map_possession_status(raw: Optional[str]) -> Optional[str]:
    text = clean_text(raw)
    if not text:
        return None
    key = text.lower().replace("-", " ").replace("_", " ")
    key = re.sub(r"\s+", " ", key).strip()
    mapped = POSSESSION_MAP.get(key)
    if mapped:
        return mapped
    # Also try underscored form used by some portals
    underscored = text.upper().replace(" ", "_").replace("-", "_")
    if underscored in {"READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH", "UPCOMING"}:
        return "NEW_LAUNCH" if underscored == "UPCOMING" else underscored
    return underscored


def parse_possession_date(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"on request", "na", "n/a", "applied"}:
        return None

    cleaned = text.replace("+00:00", "Z")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    if "." in cleaned and "T" in cleaned:
        cleaned = cleaned.split(".")[0]

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%b %Y",
        "%B %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    match = re.match(r"^([A-Za-z]{3,9})\s+(\d{4})$", text)
    if match:
        for fmt in ("%b %Y", "%B %Y"):
            try:
                return datetime.strptime(
                    f"{match.group(1)} {match.group(2)}", fmt
                ).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def parse_bhk(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw)
    match = BHK_RE.search(text)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return None
    match = re.search(r"\d+", text)
    if match:
        return int(match.group(0))
    return None


def price_to_inr(value: Any, unit: Optional[str] = None) -> Optional[int]:
    """Convert numeric price + unit (Cr/Lakh) to integer INR.

    Returns None for call-for-price / on-request strings — never invent a number.

    Input contract for the ``unit``-less heuristic path:
    - 100acress RSC payloads embed prices as small floats representing Crore
      (e.g. ``2.5`` meaning ₹2.5 Cr). Any bare number < 1000 is therefore
      assumed to be in Crore.
    - Bare integers ≥ 1000 are assumed to be already in full INR rupees.
    - When ``unit`` is provided it always takes precedence over this heuristic.
    """
    if value is None or value == "":
        return None
    unit_text = (unit or "").strip().lower()
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in PRICE_ON_REQUEST:
            return None
        # Phrases like "Call For Price" / "Price on Call" with extra words
        if re.search(r"(?i)\b(call\s+for\s+pric|price\s+on\s+(call|request)|on\s+request)\b", text):
            return None
        cr = CR_RE.search(text)
        if cr:
            return int(round(float(cr.group(1).replace(",", "")) * 10_000_000))
        lakh = LAKH_RE.search(text)
        if lakh:
            return int(round(float(lakh.group(1).replace(",", "")) * 100_000))
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None

    if unit_text in {"cr", "crore", "crores"}:
        return int(round(num * 10_000_000))
    if unit_text in {"l", "lac", "lakh", "lakhs"}:
        return int(round(num * 100_000))
    # Heuristic: small floats from RSC are usually Cr
    if num < 1000:
        return int(round(num * 10_000_000))
    return int(round(num))


def parse_area(raw: Any) -> Optional[int]:
    """Parse a single area value. Ambiguous ranges (e.g. 3710-3763) return None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return int(round(float(raw)))
    text = str(raw).replace(",", "").strip()
    if AREA_RANGE_RE.search(text):
        return None
    match = re.search(r"[\d.]+", text)
    if not match:
        return None
    try:
        return int(round(float(match.group(0))))
    except ValueError:
        return None


def sanitize_coords(
    lat: Optional[float],
    lng: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Drop known site-wide placeholder coordinates."""
    if lat is None or lng is None:
        return None, None
    for plat, plng in PLACEHOLDER_COORDS:
        if abs(lat - plat) < 1e-6 and abs(lng - plng) < 1e-6:
            return None, None
    return lat, lng


def extract_locality(address: Optional[str], title: Optional[str] = None) -> Optional[str]:
    for source in (address, title):
        if not source:
            continue
        match = SECTOR_RE.search(source)
        if match:
            return f"Sector {match.group(1).upper()}" if match.group(1)[-1:].isalpha() else f"Sector {match.group(1)}"
    return None


def extract_pincode(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    match = PINCODE_RE.search(address)
    return match.group(1) if match else None


def extract_city_from_text(*parts: Optional[str]) -> Optional[str]:
    blob = " ".join(p for p in parts if p)
    if not blob:
        return None
    # Common cities
    cities = [
        "Gurgaon",
        "Gurugram",
        "Noida",
        "Greater Noida",
        "Delhi",
        "New Delhi",
        "Mumbai",
        "Pune",
        "Bangalore",
        "Bengaluru",
        "Hyderabad",
        "Chennai",
        "Kolkata",
        "Ahmedabad",
        "Jaipur",
        "Chandigarh",
        "Faridabad",
        "Ghaziabad",
        "Dubai",
    ]
    lower = blob.lower()
    for city in cities:
        if city.lower() in lower:
            return normalize_city(city)
    return None


def derive_rera_state(rera_number: Optional[str]) -> Optional[str]:
    cleaned = clean_rera_number(rera_number) or clean_text(rera_number)
    if not cleaned:
        return None
    upper = cleaned.upper()
    for hint, state in RERA_STATE_HINTS.items():
        if hint in upper:
            return state
    if "HARYANA" in upper:
        return "Haryana"
    return None


def is_registered_rera(rera_number: Optional[str]) -> Optional[bool]:
    cleaned = clean_rera_number(rera_number) if rera_number else None
    # Preserve explicit "applied" as not registered before cleaning drops it
    raw = clean_text(rera_number)
    if raw and raw.lower().lstrip("$") in {"applied", "pending"}:
        return False
    if not cleaned:
        return None
    if cleaned.lower() in {"applied", "na", "n/a", "pending", "on request"}:
        return False
    return True


def build_address(
    project_name: Optional[str],
    locality: Optional[str],
    address: Optional[str],
    city: Optional[str],
    pincode: Optional[str],
) -> Optional[str]:
    parts: list[str] = []
    base = clean_text(address) or ""
    if locality and locality.lower() not in base.lower():
        parts.append(locality)
    if base:
        parts.append(base)
    city_n = normalize_city(city)
    if city_n and city_n.lower() not in " ".join(parts).lower():
        # Prefer Gurugram in address string if Gurgaon? Sample uses Gurugram in address.
        address_city = "Gurugram" if city_n == "Gurgaon" else city_n
        parts.append(address_city)
    if city_n == "Gurgaon" and "Haryana" not in " ".join(parts):
        parts.append("Haryana")
    if pincode and pincode not in " ".join(parts):
        parts.append(pincode)
    # Dedupe consecutive-ish
    text = ", ".join(dict.fromkeys(p.strip(" ,") for p in parts if p))
    return text or None
