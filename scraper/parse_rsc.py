"""Extract project objects from Next.js RSC flight data."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

PUSH_RE = re.compile(r"self\.__next_f\.push\(\[(.*?)\]\)\s*</script>", re.S)
LONG_STRING_RE = re.compile(r'"((?:\\.|[^"\\]){100,})"')


def _unescape_js_string(raw: str) -> str:
    """Decode a JS string literal without corrupting UTF-8 text."""
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        # Fallback for odd escape sequences: only decode \\uXXXX / \\xNN
        def repl_unicode(match: re.Match[str]) -> str:
            try:
                return chr(int(match.group(1), 16))
            except ValueError:
                return match.group(0)

        text = re.sub(r"\\u([0-9a-fA-F]{4})", repl_unicode, raw)
        text = text.replace(r"\/", "/").replace(r"\"", '"').replace(r"\n", "\n")
        return text


def extract_rsc_blobs(html: str) -> list[str]:
    blobs: list[str] = []
    for push in PUSH_RE.findall(html):
        for match in LONG_STRING_RE.finditer(push):
            blobs.append(_unescape_js_string(match.group(1)))
    return blobs


def _try_parse_object_at(blob: str, start: int) -> Optional[dict[str, Any]]:
    if start < 0 or start >= len(blob) or blob[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(blob)):
        ch = blob[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = blob[start : i + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                if isinstance(obj, dict):
                    return obj
                return None
    return None


def extract_project_from_blob(blob: str) -> Optional[dict[str, Any]]:
    markers = (
        '"projectName":',
        '"BhK_Details":',
        '"projectReraNo":',
        '"projectUrl":',
    )
    if not any(m in blob for m in markers):
        return None

    # Prefer objects that include BhK_Details or projectName
    search_from = 0
    best: Optional[dict[str, Any]] = None
    while True:
        idx = blob.find('"projectName":', search_from)
        if idx < 0:
            break
        # Walk back to nearest '{'
        start = blob.rfind("{", 0, idx)
        # Try progressively earlier braces within a window
        window_start = max(0, idx - 8000)
        for brace_at in range(idx, window_start - 1, -1):
            if blob[brace_at] != "{":
                continue
            obj = _try_parse_object_at(blob, brace_at)
            if not obj:
                continue
            if "projectName" in obj and (
                "BhK_Details" in obj
                or "bhkDetails" in obj
                or "Amenities" in obj
                or "projectReraNo" in obj
            ):
                # Prefer richer objects
                score = len(obj.keys())
                if "BhK_Details" in obj:
                    score += 50
                if best is None or score > len(best.keys()):
                    best = obj
                if "BhK_Details" in obj and "Amenities" in obj:
                    return obj
        search_from = idx + 1
    return best


def extract_project(html: str) -> Optional[dict[str, Any]]:
    """Return the richest project dict found in RSC payloads."""
    blobs = extract_rsc_blobs(html)
    best: Optional[dict[str, Any]] = None
    best_score = -1
    for blob in blobs:
        obj = extract_project_from_blob(blob)
        if not obj:
            continue
        score = len(obj)
        if isinstance(obj.get("BhK_Details"), list):
            score += 100 + len(obj["BhK_Details"]) * 10
        if isinstance(obj.get("Amenities"), list):
            score += 20
        if score > best_score:
            best = obj
            best_score = score
    if best is None:
        logger.warning("No project object found in RSC payloads")
    return best


def resolve_bhk_details(project: dict[str, Any]) -> list[dict[str, Any]]:
    details = project.get("BhK_Details")
    if isinstance(details, list):
        return [d for d in details if isinstance(d, dict)]
    # Sometimes only a reference string is present
    return []


def resolve_highlights(project: dict[str, Any]) -> list[str]:
    highlights: list[str] = []
    highlight = project.get("highlight")
    if isinstance(highlight, dict):
        items = highlight.get("highlights") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    point = item.get("highlight_Point") or item.get("highlight_point")
                    if point and isinstance(point, str):
                        highlights.append(point.strip())
                elif isinstance(item, str) and item.strip():
                    highlights.append(item.strip())
        # Connectivity blurbs can enrich locationAdvantages later
    # Flat list fallback
    flat = project.get("highlights")
    if isinstance(flat, list):
        for item in flat:
            if isinstance(item, dict):
                point = item.get("highlight_Point")
                if point:
                    highlights.append(str(point).strip())
            elif isinstance(item, str) and item.strip():
                highlights.append(item.strip())
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in highlights:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def resolve_amenities(project: dict[str, Any]) -> list[str]:
    amenities = project.get("Amenities") or project.get("amenities") or []
    if not isinstance(amenities, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in amenities:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
        else:
            continue
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def location_blurbs(project: dict[str, Any]) -> list[str]:
    highlight = project.get("highlight")
    if not isinstance(highlight, dict):
        return []
    parts: list[str] = []
    for key in (
        "connectivity",
        "business",
        "education",
        "entertainment",
        "hospital",
        "shopping",
        "nearbyPlaces",
    ):
        values = highlight.get(key) or []
        if isinstance(values, list):
            for v in values:
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                elif isinstance(v, dict):
                    name = v.get("name") or v.get("title")
                    if name:
                        parts.append(str(name).strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
