"""Validate / fix ZirconDS bulk export JSON for site import."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_KEYS = {
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
}

ALLOWED = {
    "listingType": {"BUY", "RENT", "NEW_PROJECT", "RESALE", "PG", "PLOT", "COMMERCIAL"},
    "status": {"ACTIVE", "INACTIVE", "SOLD", "RENTED"},
    "furnishing": {"UNFURNISHED", "SEMI_FURNISHED", "FULLY_FURNISHED", None},
    "possessionStatus": {"READY_TO_MOVE", "UNDER_CONSTRUCTION", "NEW_LAUNCH", None},
}

FACING_OK = {
    "NORTH",
    "SOUTH",
    "EAST",
    "WEST",
    "NORTH_EAST",
    "NORTH_WEST",
    "SOUTH_EAST",
    "SOUTH_WEST",
    "NORTH_SOUTH",
    "EAST_WEST",
    None,
}

CITY_ALIASES = {
    "delhi": "Delhi NCR",
    "new delhi": "Delhi NCR",
    "ncr": "Delhi NCR",
    "gurugram": "Gurgaon",
    "gurgaon": "Gurgaon",
}

AMENITY_JUNK_RE = re.compile(r"^\+\s*\d+\s*more$", re.I)


def unwrap(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("properties"), list):
            return data["properties"]
        if isinstance(data.get("items"), list):
            return data["items"]
        return [data]
    if isinstance(data, list):
        return data
    raise TypeError(f"unexpected root type: {type(data).__name__}")


def clean_amenity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or AMENITY_JUNK_RE.match(text):
        return None
    return text


def normalize_city(city: Any) -> Any:
    if not isinstance(city, str):
        return city
    mapped = CITY_ALIASES.get(city.strip().lower())
    return mapped or city.strip()


def coerce_number(value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            if "." in text:
                return float(text)
            return int(text)
        except ValueError:
            return value
    return value


def fix_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["city"] = normalize_city(out.get("city"))

    amenities = out.get("amenities")
    if isinstance(amenities, list):
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in amenities:
            text = clean_amenity(item)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        out["amenities"] = cleaned

    for key in (
        "bhk",
        "bathrooms",
        "carpetArea",
        "builtUpArea",
        "superBuiltUpArea",
        "price",
        "latitude",
        "longitude",
        "maintenancePerMonth",
        "stampDuty",
        "registrationCharges",
        "propertyAge",
        "floor",
        "totalFloors",
        "totalUnits",
        "parkingSpaces",
    ):
        if key in out:
            out[key] = coerce_number(out[key])

    images = out.get("images")
    if isinstance(images, list):
        fixed_images: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for item in images:
            if isinstance(item, str):
                url = item.strip()
                meta: dict[str, Any] = {}
            elif isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                meta = item
            else:
                continue
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            order = len(fixed_images)
            fixed_images.append(
                {
                    "url": url,
                    "type": meta.get("type") or ("EXTERIOR" if order == 0 else "GALLERY"),
                    "is_primary": bool(meta.get("is_primary")) if meta.get("is_primary") is not None else order == 0,
                    "order": order if meta.get("order") is None else meta.get("order"),
                }
            )
        for i, img in enumerate(fixed_images):
            img["order"] = i
            img["is_primary"] = i == 0
        out["images"] = fixed_images

    # Drop non-schema keys from export payload
    return {k: out.get(k) for k in out if k in SCHEMA_KEYS}


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cities: Counter[str] = Counter()
    prices_null = 0
    locality_null = 0
    amenity_junk: list[tuple[int, str, str]] = []
    enum_bad: list[tuple[int, str, Any, str]] = []
    missing_req: list[tuple[int, str]] = []
    type_issues: list[tuple[int, str, str, str, str]] = []
    extra_keys: Counter[str] = Counter()
    structure_issues: list[tuple[int, str]] = []

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            structure_issues.append((i, f"not_object:{type(row).__name__}"))
            continue
        title = str(row.get("title") or "")[:60]
        city = row.get("city")
        cities[str(city)] += 1
        if not row.get("title"):
            missing_req.append((i, "title"))
        if not city:
            missing_req.append((i, "city"))
        if row.get("price") in (None, 0, "0", ""):
            prices_null += 1
        if row.get("locality") in (None, ""):
            locality_null += 1

        amenities = row.get("amenities") or []
        if isinstance(amenities, list):
            for item in amenities:
                if isinstance(item, str) and AMENITY_JUNK_RE.match(item.strip()):
                    amenity_junk.append((i, item, title))

        for field, allowed in ALLOWED.items():
            value = row.get(field)
            if value not in allowed:
                enum_bad.append((i, field, value, title))
        facing = row.get("facing")
        if facing is not None and facing not in FACING_OK:
            enum_bad.append((i, "facing", facing, title))

        for numf in (
            "bhk",
            "bathrooms",
            "carpetArea",
            "builtUpArea",
            "superBuiltUpArea",
            "price",
            "latitude",
            "longitude",
        ):
            value = row.get(numf)
            if value is not None and not isinstance(value, (int, float)):
                type_issues.append((i, numf, type(value).__name__, repr(value)[:80], title[:40]))

        images = row.get("images")
        if images is not None and not isinstance(images, list):
            type_issues.append((i, "images", type(images).__name__, "", title[:40]))
        elif isinstance(images, list):
            for j, image in enumerate(images):
                if not isinstance(image, dict) or not image.get("url"):
                    type_issues.append((i, f"images[{j}]", "bad", repr(image)[:80], title[:40]))
                    break

        for key in row:
            if key not in SCHEMA_KEYS:
                extra_keys[key] += 1

    return {
        "row_count": len(rows),
        "cities": dict(cities),
        "prices_null_or_0": prices_null,
        "locality_null": locality_null,
        "amenity_junk": amenity_junk,
        "enum_bad": enum_bad,
        "missing_req": missing_req,
        "type_issues": type_issues,
        "extra_keys": dict(extra_keys),
        "structure_issues": structure_issues,
        "furnishing_vals": dict(Counter(str(r.get("furnishing")) for r in rows if isinstance(r, dict))),
        "possession_vals": dict(Counter(str(r.get("possessionStatus")) for r in rows if isinstance(r, dict))),
        "facing_vals": dict(Counter(str(r.get("facing")) for r in rows if isinstance(r, dict))),
        "listing_vals": dict(Counter(str(r.get("listingType")) for r in rows if isinstance(r, dict))),
        "status_vals": dict(Counter(str(r.get("status")) for r in rows if isinstance(r, dict))),
        "propertyType_vals": dict(Counter(str(r.get("propertyType")) for r in rows if isinstance(r, dict))),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    raw = args.path.read_text(encoding="utf-8")
    print(f"bytes={len(raw.encode('utf-8'))}")
    print(f"starts={raw[:60]!r}")
    print(f"ends={raw[-60:]!r}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"JSON_ERROR: {exc}")
        raise SystemExit(1) from exc

    rows = unwrap(data)
    report = analyze(rows)
    print(json.dumps(report, indent=2, default=str))

    if args.fix:
        fixed = [fix_row(row) if isinstance(row, dict) else row for row in rows]
        out_path = args.out or args.path.with_name(args.path.stem + ".fixed.json")
        out_path.write_text(json.dumps(fixed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote={out_path}")
        print(json.dumps(analyze(fixed), indent=2, default=str))


if __name__ == "__main__":
    main()
