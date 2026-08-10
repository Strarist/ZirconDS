"""Archive load/merge helpers for multi-source retained scrape output."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

from scraper.normalize import slugify
from scraper.schema import ARRAY_KEYS, SCHEMA_KEYS, coerce_images


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


_PROJECT_MARKERS = {
    "east",
    "west",
    "north",
    "south",
    "phase",
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
}


def _digit_tokens(slug: str) -> tuple[str, ...]:
    return tuple(tok for tok in slug.split("-") if any(ch.isdigit() for ch in tok))


def _marker_tokens(slug: str) -> tuple[str, ...]:
    return tuple(tok for tok in slug.split("-") if tok in _PROJECT_MARKERS)


def projects_soft_match(a: Any, b: Any) -> bool:
    """True when project names look like the same listing with a typo/spelling drift.

    Rejects phase / avenue-number / direction differences (e.g. 1st vs 4th Avenue).
    Requires a shared leading brand token so unrelated short names do not collide.
    """
    sa = slugify(str(a or ""))
    sb = slugify(str(b or ""))
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    # Very short slugs are too ambiguous for fuzzy matching.
    if min(len(sa), len(sb)) < 8:
        return False
    ta = sa.split("-")
    tb = sb.split("-")
    if ta[0] != tb[0]:
        return False
    if _digit_tokens(sa) != _digit_tokens(sb):
        return False
    if _marker_tokens(sa) != _marker_tokens(sb):
        return False
    if abs(len(sa) - len(sb)) > 3:
        return False
    return SequenceMatcher(None, sa, sb).ratio() >= 0.92


def _has_locality(record: dict[str, Any]) -> bool:
    return bool(slugify(str(record.get("locality") or "")))


def unit_signature(record: dict[str, Any], *, loose: bool = False) -> str:
    """Identity without project name — used for soft project typo matching."""
    city = slugify(str(record.get("city") or ""))
    locality = slugify(str(record.get("locality") or ""))
    bhk = record.get("bhk")
    if loose:
        return f"{city}|{locality}|{bhk}"
    area = record.get("superBuiltUpArea")
    area_tok = "" if area is None else str(area)
    return f"{city}|{locality}|{bhk}|{area_tok}"


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
    """Atomically write records to path (write-to-tmp then os.replace).

    This guarantees that a crash or KeyboardInterrupt during write never leaves
    a partially-written / corrupt JSON file. Readers always see either the
    complete previous file or the complete new file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
        if key == "images":
            combined = coerce_images(list(old or []) + list(new or []))
            if combined != coerce_images(old or []):
                out[key] = combined
                changed = True
            continue
        if key in ARRAY_KEYS:
            combined = _union_lists(old, new)
            if combined != (old or []):
                out[key] = combined
                changed = True
            continue
        if _is_empty(old) and not _is_empty(new):
            out[key] = new
            changed = True

    # Legacy imageUrls → fold into images
    legacy = []
    for rec in (existing, incoming):
        for u in rec.get("imageUrls") or []:
            if isinstance(u, str) and u.strip():
                legacy.append(u.strip())
    if legacy:
        combined = coerce_images(list(out.get("images") or []) + legacy)
        if combined != coerce_images(out.get("images") or []):
            out["images"] = combined
            changed = True
    out.pop("imageUrls", None)

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


def record_fullness(record: dict[str, Any]) -> int:
    """Rough richness score — prefer keeping denser rows as merge base."""
    skip = {
        "sources",
        "sourceUrl",
        "sourceSite",
        "scrapedAt",
        "slug",
        "title",
        "listingType",
        "status",
        "seoTitle",
        "seoDescription",
        "seoKeywords",
        "h1",
    }
    score = 0
    for key, value in record.items():
        if key in skip:
            continue
        if _is_empty(value):
            continue
        if isinstance(value, list):
            score += min(len(value), 8)
        elif isinstance(value, str):
            score += 1 + min(len(value), 200) // 50
        else:
            score += 1
    return score


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
            # Prefer the densest existing row when several share the same key.
            return max(hits, key=lambda i: record_fullness(archive[i]))

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

    # Soft project-name match: same city/locality/bhk/area, near-identical project spelling.
    # Require a real locality — empty locality is too ambiguous across portals.
    incoming_project = incoming.get("projectName")
    if incoming_project and _has_locality(incoming):
        soft_hits: list[int] = []
        sig = unit_signature(incoming, loose=False)
        for i, rec in enumerate(archive):
            if not _has_locality(rec):
                continue
            if unit_signature(rec, loose=False) != sig:
                continue
            if projects_soft_match(incoming_project, rec.get("projectName")):
                soft_hits.append(i)
        if len(soft_hits) == 1:
            return soft_hits[0]
        if len(soft_hits) > 1:
            return max(soft_hits, key=lambda i: record_fullness(archive[i]))

        # Soft + loose area (exactly one side missing SBA)
        soft_loose: list[int] = []
        loose_sig = unit_signature(incoming, loose=True)
        for i, rec in enumerate(archive):
            if not _has_locality(rec):
                continue
            if unit_signature(rec, loose=True) != loose_sig:
                continue
            existing_area = rec.get("superBuiltUpArea")
            # Require complementary null/sized — never soft-merge two null-area rows.
            if not (
                (existing_area is None and incoming_area is not None)
                or (existing_area is not None and incoming_area is None)
            ):
                continue
            if projects_soft_match(incoming_project, rec.get("projectName")):
                soft_loose.append(i)
        if len(soft_loose) == 1:
            return soft_loose[0]
        if len(soft_loose) > 1 and incoming_area is not None:
            null_only = [i for i in soft_loose if archive[i].get("superBuiltUpArea") is None]
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


def dedupe_archive(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Collapse duplicate units already in the archive via null-fill merge.

    Exact match-key groups collapse in linear time first; remaining near-dupes
    (loose area / soft project typos) use find_match_index.

    Returns (deduped, merge_count, updated_rows).
    """
    if not records:
        return [], 0, []

    # Pass 1: exact match_key groups
    by_key: dict[str, list[dict[str, Any]]] = {}
    no_key: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = match_key(rec, loose=False)
        if not key:
            no_key.append(dict(rec))
            continue
        by_key.setdefault(key, []).append(dict(rec))

    collapsed: list[dict[str, Any]] = []
    merge_count = 0
    updated: list[dict[str, Any]] = []

    for group in by_key.values():
        group.sort(key=record_fullness, reverse=True)
        base = group[0]
        for extra in group[1:]:
            base, changed = merge_record_fields(base, extra)
            merge_count += 1
            if changed:
                updated.append(base)
        collapsed.append(base)

    # Pass 2: only soft/loose cross-group merges (exact keys already unique).
    # Index by unit signature so we don't O(n^2)-scan the whole archive.
    by_sig: dict[str, list[int]] = {}
    final = list(collapsed) + no_key
    for i, rec in enumerate(final):
        by_sig.setdefault(unit_signature(rec, loose=False), []).append(i)

    remove: set[int] = set()
    for sig, idxs in list(by_sig.items()):
        live = [i for i in idxs if i not in remove]
        if len(live) < 2:
            continue
        # Soft project typos sharing city|locality|bhk|area.
        # Skip empty-locality signatures — too ambiguous.
        sample = final[live[0]]
        if not _has_locality(sample):
            continue
        live.sort(key=lambda i: record_fullness(final[i]), reverse=True)
        for seed_pos, kept in enumerate(live):
            if kept in remove:
                continue
            for other in live[seed_pos + 1 :]:
                if other in remove:
                    continue
                if not projects_soft_match(final[kept].get("projectName"), final[other].get("projectName")):
                    continue
                combined, changed = merge_record_fields(final[kept], final[other])
                final[kept] = combined
                merge_count += 1
                if changed:
                    updated.append(combined)
                remove.add(other)

    if remove:
        final = [r for i, r in enumerate(final) if i not in remove]

    # Pass 2b: loose area via find_match_index among remaining (much smaller set of null-area rows)
    null_area = [r for r in final if r.get("superBuiltUpArea") is None]
    sized = [r for r in final if r.get("superBuiltUpArea") is not None]
    if null_area and sized:
        rebuilt: list[dict[str, Any]] = list(sized)
        for rec in sorted(null_area, key=record_fullness, reverse=True):
            idx = find_match_index(rebuilt, rec)
            if idx is None:
                rebuilt.append(rec)
                continue
            combined, changed = merge_record_fields(rebuilt[idx], rec)
            rebuilt[idx] = combined
            merge_count += 1
            if changed:
                updated.append(combined)
        final = rebuilt

    # Pass 3: sole null-area row onto a sized sibling under the same loose key
    changed = True
    while changed:
        changed = False
        by_loose: dict[str, list[int]] = {}
        for i, rec in enumerate(final):
            loose = match_key(rec, loose=True)
            if not loose:
                continue
            by_loose.setdefault(loose, []).append(i)
        remove_idx: int | None = None
        for idxs in by_loose.values():
            if len(idxs) < 2:
                continue
            nulls = [i for i in idxs if final[i].get("superBuiltUpArea") is None]
            sized_idxs = [i for i in idxs if final[i].get("superBuiltUpArea") is not None]
            if len(nulls) != 1 or not sized_idxs:
                continue
            src = nulls[0]
            target = max(sized_idxs, key=lambda i: record_fullness(final[i]))
            combined, did_change = merge_record_fields(final[target], final[src])
            final[target] = combined
            merge_count += 1
            if did_change:
                updated.append(combined)
            remove_idx = src
            changed = True
            break
        if remove_idx is not None:
            final = [r for i, r in enumerate(final) if i != remove_idx]

    return final, merge_count, updated


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
