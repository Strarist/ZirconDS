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
    "parkingSpaces",
    "furnishing",
    "possessionStatus",
    "possessionDate",
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


def ensure_schema(record: dict[str, Any]) -> dict[str, Any]:
    """Ensure output has exactly SCHEMA_KEYS in order."""
    base = empty_record()
    base.update(record)
    return {key: base.get(key) for key in SCHEMA_KEYS}


def validate_record(record: dict[str, Any]) -> list[str]:
    """Return list of validation problems (empty if ok)."""
    problems: list[str] = []
    missing = [k for k in SCHEMA_KEYS if k not in record]
    if missing:
        problems.append(f"missing keys: {missing}")
    extra = [k for k in record if k not in SCHEMA_KEYS]
    if extra:
        problems.append(f"extra keys: {extra}")
    for key in ARRAY_KEYS:
        if key in record and not isinstance(record[key], list):
            problems.append(f"{key} must be a list")
    return problems
