"""Target property schema keys and empty defaults."""

from __future__ import annotations

from typing import Any

SCHEMA_KEYS: list[str] = [
    "title",
    "slug",
    "propertyType",
    "listingType",
    "status",
    "projectName",
    "developer",
    "city",
    "locality",
    "pincode",
    "address",
    "description",
    "featured",
    "hotDeal",
    "exclusive",
    "vastuCompliant",
    "bhk",
    "bathrooms",
    "carpetArea",
    "builtUpArea",
    "superBuiltUpArea",
    "propertyAge",
    "floor",
    "totalFloors",
    "totalUnits",
    "parkingSpaces",
    "parkingType",
    "furnishing",
    "possessionStatus",
    "possessionDate",
    "launchDate",
    "facing",
    "latitude",
    "longitude",
    "price",
    "maintenancePerMonth",
    "stampDuty",
    "registrationCharges",
    "reraNumber",
    "reraState",
    "reraRegistered",
    "reraVerified",
    "bankApprovals",
    "amenities",
    "overlooking",
    "images",
    "seoTitle",
    "seoDescription",
    "seoKeywords",
    "h1",
    "aboutSection",
    "highlights",
    "locationAdvantages",
    "whyInvest",
    "whyConsider",
    "addressSection",
    "faqs",
]

ARRAY_KEYS = {
    "bankApprovals",
    "amenities",
    "overlooking",
    "images",
    "seoKeywords",
    "highlights",
    "faqs",
}

NULLABLE_BOOL_KEYS = {
    "featured",
    "hotDeal",
    "exclusive",
    "vastuCompliant",
    "reraRegistered",
    "reraVerified",
}

# Kept on archive rows for the verification UI; not part of the public schema.
VERIFICATION_KEYS = (
    "sourceUrl",
    "sourceSite",
    "sources",
    "scrapedAt",
)


def empty_record() -> dict[str, Any]:
    """Return a record with every schema key present and safe defaults."""
    record: dict[str, Any] = {}
    for key in SCHEMA_KEYS:
        if key in ARRAY_KEYS:
            record[key] = []
        else:
            record[key] = None
    record["listingType"] = "BUY"
    record["status"] = "ACTIVE"
    return record


def normalize_image_entry(item: Any, *, order: int) -> dict[str, Any] | None:
    """Normalize one image to {url, type, is_primary, order}."""
    if isinstance(item, str):
        url = item.strip()
        if not url:
            return None
        return {
            "url": url,
            "type": "EXTERIOR" if order == 0 else "GALLERY",
            "is_primary": order == 0,
            "order": order,
        }
    if isinstance(item, dict):
        url = str(item.get("url") or "").strip()
        if not url:
            return None
        img_type = str(item.get("type") or ("EXTERIOR" if order == 0 else "GALLERY")).strip() or "GALLERY"
        is_primary = item.get("is_primary")
        if is_primary is None:
            is_primary = order == 0
        try:
            ord_val = int(item.get("order")) if item.get("order") is not None else order
        except (TypeError, ValueError):
            ord_val = order
        return {
            "url": url,
            "type": img_type,
            "is_primary": bool(is_primary),
            "order": ord_val,
        }
    return None


def coerce_images(value: Any) -> list[dict[str, Any]]:
    """Build ordered unique images list from images[] and/or legacy imageUrls[]."""
    raw: list[Any] = []
    if isinstance(value, list):
        raw.extend(value)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        entry = normalize_image_entry(item, order=len(out))
        if not entry:
            continue
        url = entry["url"]
        if url in seen:
            continue
        seen.add(url)
        entry["order"] = len(out)
        entry["is_primary"] = len(out) == 0
        if len(out) == 0 and entry["type"] == "GALLERY":
            entry["type"] = "EXTERIOR"
        out.append(entry)
    return out


def urls_to_images(urls: list[str] | None) -> list[dict[str, Any]]:
    return coerce_images(list(urls or []))


def image_urls_of(record: dict[str, Any]) -> list[str]:
    """Flatten image URLs from images[] (and legacy imageUrls)."""
    urls: list[str] = []
    seen: set[str] = set()
    for item in coerce_images(record.get("images")):
        url = item["url"]
        if url not in seen:
            seen.add(url)
            urls.append(url)
    for item in record.get("imageUrls") or []:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.add(item.strip())
            urls.append(item.strip())
    return urls


def ensure_schema(record: dict[str, Any]) -> dict[str, Any]:
    """Ensure output has exactly SCHEMA_KEYS in order (plus optional verification extras)."""
    base = empty_record()
    incoming = dict(record)

    # Migrate legacy imageUrls → images
    images = coerce_images(incoming.get("images"))
    if not images and incoming.get("imageUrls"):
        images = urls_to_images(
            [u for u in (incoming.get("imageUrls") or []) if isinstance(u, str)]
        )
    incoming["images"] = images

    base.update(incoming)
    out = {key: base.get(key) for key in SCHEMA_KEYS}
    out["images"] = coerce_images(out.get("images"))

    for key in VERIFICATION_KEYS:
        if key in record and record[key] is not None:
            out[key] = record[key]
    return out


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Schema-only record for integrations / export (no verification extras)."""
    ensured = ensure_schema(record)
    return {key: ensured.get(key) for key in SCHEMA_KEYS}


def validate_record(record: dict[str, Any]) -> list[str]:
    """Return list of validation problems (empty if ok)."""
    problems: list[str] = []
    missing = [k for k in SCHEMA_KEYS if k not in record]
    if missing:
        problems.append(f"missing keys: {missing}")
    allowed = set(SCHEMA_KEYS) | set(VERIFICATION_KEYS)
    extra = [k for k in record if k not in allowed and k != "imageUrls"]
    if extra:
        problems.append(f"extra keys: {extra}")
    for key in ARRAY_KEYS:
        if key in record and not isinstance(record[key], list):
            problems.append(f"{key} must be a list")
    if "images" in record:
        for i, img in enumerate(record.get("images") or []):
            if not isinstance(img, dict) or not img.get("url"):
                problems.append(f"images[{i}] must be object with url")
                break
    return problems
