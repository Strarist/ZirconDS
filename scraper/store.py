"""Archive load/merge helpers for multi-source retained scrape output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from scraper.normalize import slugify
from scraper.schema import ARRAY_KEYS, SCHEMA_KEYS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_identity(record: dict[str, Any]) -> str:
    """Stable unit identity — prefers cross-source match key."""
    return match_key(record) or legacy_source_identity(record)


def legacy_source_identity(record: dict[str, Any]) -> str:
    source = (record.get("sourceUrl") or "").strip().rstrip("/").lower()
    if source:
        bhk = record.get("bhk")
        area = record.get("superBuiltUpArea")
        return f"url:{source}|{bhk}|{area}"
    slug = (record.get("slug") or "").strip().lower()
    return f"slug:{slug}" if slug else json.dumps(record, sort_keys=True, default=str)


def match_key(record: dict[str, Any], *, loose: bool = False) -> str:
    """Cross-site identity: project|city|locality|bhk|area (area omitted when loose)."""
    project = slugify(str(record.get("projectName") or ""))
    city = slugify(str(record.get("city") or ""))
    locality = slugify(str(record.get("locality") or ""))
    bhk = record.get("bhk")
    area = record.get("superBuiltUpArea")
    if not project:
        return ""
    base = f"{project}|{city}|{locality}|{bhk}"
    if loose:
        return base
    # Strict always includes an area token so null-area rows stay distinct from sized ones.
    area_tok = "" if area is None else str(area)
    return f"{base}|{area_tok}"


def known_source_urls(records: Iterable[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for rec in records:
        url = (rec.get("sourceUrl") or "").strip()
        if url:
            urls.add(url.rstrip("/") + "/")
            urls.add(url.rstrip("/"))
        for src in rec.get("sources") or []:
            if isinstance(src, dict):
                u = (src.get("url") or "").strip()
                if u:
                    urls.add(u.rstrip("/") + "/")
                    urls.add(u.rstrip("/"))
    return urls


def normalize_project_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    return url if url.endswith("/") else url + "/"


def filter_new_urls(urls: list[str], known: set[str]) -> list[str]:
    known_norm = {normalize_project_url(u) for u in known} | set(known)
    out: list[str] = []
    for url in urls:
        norm = normalize_project_url(url)
        bare = url.rstrip("/")
        if norm in known_norm or url in known_norm or bare in known_norm:
            continue
        # MagicBricks URLs often have no trailing slash
        if any(url == k or bare == k.rstrip("/") for k in known_norm):
            continue
        out.append(url)
    return out


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def latest_path_for(out_path: Path) -> Path:
    return out_path.parent / "latest.json"


def run_stats_path_for(out_path: Path) -> Path:
    return out_path.parent / "scrape-run.json"


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _union_lists(existing: Any, incoming: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(incoming or []):
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def merge_sources(existing: Any, incoming: Any) -> list[dict[str, str]]:
    merged = _union_lists(existing or [], incoming or [])
    cleaned: list[dict[str, str]] = []
    for item in merged:
        if isinstance(item, dict) and item.get("url"):
            cleaned.append(
                {
                    "site": str(item.get("site") or "unknown"),
                    "url": str(item["url"]),
                }
            )
    return cleaned


def merge_record_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fill nulls from incoming; union arrays. Returns (merged, changed)."""
    out = dict(existing)
    changed = False

    for key in SCHEMA_KEYS:
        old = out.get(key)
        new = incoming.get(key)
        if key in ARRAY_KEYS:
            combined = _union_lists(old, new)
            if combined != (old or []):
                out[key] = combined
                changed = True
            continue
        if _is_empty(old) and not _is_empty(new):
            out[key] = new
            changed = True

    # Verification extras
    for key in ("imageUrls",):
        combined = _union_lists(out.get(key), incoming.get(key))
        if combined != (out.get(key) or []):
            out[key] = combined
            changed = True

    sources = merge_sources(out.get("sources"), incoming.get("sources"))
    # Also fold legacy sourceUrl into sources
    for rec in (existing, incoming):
        url = rec.get("sourceUrl")
        site = rec.get("sourceSite") or "unknown"
        if url:
            sources = merge_sources(sources, [{"site": site, "url": url}])
    if sources != (out.get("sources") or []):
        out["sources"] = sources
        changed = True

    if not out.get("sourceUrl") and incoming.get("sourceUrl"):
        out["sourceUrl"] = incoming["sourceUrl"]
        changed = True
    if not out.get("sourceSite") and incoming.get("sourceSite"):
        out["sourceSite"] = incoming["sourceSite"]
        changed = True

    if incoming.get("scrapedAt"):
        out["scrapedAt"] = incoming["scrapedAt"]

    return out, changed


def infer_source_site(record: dict[str, Any]) -> str | None:
    site = record.get("sourceSite")
    if isinstance(site, str) and site.strip():
        return site.strip().lower()
    for src in record.get("sources") or []:
        if isinstance(src, dict) and src.get("site"):
            return str(src["site"]).strip().lower()
    url = (record.get("sourceUrl") or "").lower()
    if "100acress.com" in url:
        return "100acress"
    if "99acres.com" in url:
        return "99acres"
    if "magicbricks.com" in url:
        return "magicbricks"
    if "housing.com" in url:
        return "housing"
    if "squareyards.com" in url:
        return "squareyards"
    return None


def backfill_source_meta(records: list[dict[str, Any]]) -> None:
    """Ensure legacy archive rows have sourceSite / sources for UI + skip logic."""
    for rec in records:
        site = infer_source_site(rec)
        url = (rec.get("sourceUrl") or "").strip()
        if site and not rec.get("sourceSite"):
            rec["sourceSite"] = site
        if site and url:
            rec["sources"] = merge_sources(rec.get("sources"), [{"site": site, "url": url}])


def find_match_index(archive: list[dict[str, Any]], incoming: dict[str, Any]) -> Optional[int]:
    strict = match_key(incoming, loose=False)
    if strict:
        hits = [i for i, rec in enumerate(archive) if match_key(rec, loose=False) == strict]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            leg = legacy_source_identity(incoming)
            for i in hits:
                if legacy_source_identity(archive[i]) == leg:
                    return i
            return hits[0]

    loose = match_key(incoming, loose=True)
    incoming_area = incoming.get("superBuiltUpArea")
    if loose:
        candidates: list[int] = []
        for i, rec in enumerate(archive):
            if match_key(rec, loose=True) != loose:
                continue
            existing_area = rec.get("superBuiltUpArea")
            # Loose only when at least one side lacks area
            if existing_area is None or incoming_area is None:
                candidates.append(i)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1 and incoming_area is not None:
            null_only = [i for i in candidates if archive[i].get("superBuiltUpArea") is None]
            if len(null_only) == 1:
                return null_only[0]

    leg = legacy_source_identity(incoming)
    for i, rec in enumerate(archive):
        if legacy_source_identity(rec) == leg:
            return i
    return None


def merge_archive(
    archive: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge incoming into archive with cross-source fill.

    Returns (merged, newly_added, updated).
    """
    merged = [dict(r) for r in archive]
    added: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []

    for rec in incoming:
        idx = find_match_index(merged, rec)
        if idx is None:
            merged.append(rec)
            added.append(rec)
            continue
        combined, changed = merge_record_fields(merged[idx], rec)
        merged[idx] = combined
        if changed:
            updated.append(combined)

    return merged, added, updated


def stamp_scraped_at(records: list[dict[str, Any]], scraped_at: str | None = None) -> str:
    ts = scraped_at or utc_now_iso()
    for rec in records:
        rec["scrapedAt"] = ts
    return ts


def ensure_source_meta(record: dict[str, Any], site: str, url: str) -> None:
    record["sourceSite"] = record.get("sourceSite") or site
    record["sourceUrl"] = record.get("sourceUrl") or url
    sources = list(record.get("sources") or [])
    sources = merge_sources(sources, [{"site": site, "url": url}])
    record["sources"] = sources
