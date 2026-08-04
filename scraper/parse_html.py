"""Parse human-visible HTML fields: meta, FAQs, coords, overview."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag


@dataclass
class HtmlExtras:
    title: Optional[str] = None
    meta_description: Optional[str] = None
    canonical: Optional[str] = None
    overview: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    faqs: list[dict[str, str]] = field(default_factory=list)
    h1: Optional[str] = None


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def _first_meta(soup: BeautifulSoup, *selectors: tuple[str, str]) -> Optional[str]:
    for attr, value in selectors:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    return None


def extract_coords(html: str) -> tuple[Optional[float], Optional[float]]:
    patterns = [
        r"destination=(-?\d+\.?\d*),(-?\d+\.?\d*)",
        r"[?&]q=(-?\d+\.?\d*),(-?\d+\.?\d*)",
        r'"latitude"\s*:\s*(-?\d+\.?\d*).{0,80}"longitude"\s*:\s*(-?\d+\.?\d*)',
        r'"lat"\s*:\s*(-?\d+\.?\d*).{0,40}"lng"\s*:\s*(-?\d+\.?\d*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            try:
                return float(match.group(1)), float(match.group(2))
            except ValueError:
                continue
    return None, None


def extract_faqs(soup: BeautifulSoup, html: str) -> list[dict[str, str]]:
    faqs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(q: str, a: str) -> None:
        q = _clean_text(q)
        a = _clean_text(a)
        if not q or not a:
            return
        key = q.lower()
        if key in seen:
            return
        # Skip form / CTA noise
        if len(q) < 12 or len(a) < 12:
            return
        if "authorize" in a.lower() or "submit" in q.lower():
            return
        seen.add(key)
        faqs.append({"question": q, "answer": a})

    # details/summary
    for details in soup.find_all("details"):
        summary = details.find("summary")
        if not summary:
            continue
        q = summary.get_text(" ", strip=True)
        # answer = rest of details text without summary
        parts = []
        for child in details.children:
            if child is summary:
                continue
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                parts.append(child.get_text(" ", strip=True))
        add(q, " ".join(parts))

    # accordion-like headings followed by sibling paragraphs
    for heading in soup.find_all(["h2", "h3", "h4", "button"]):
        text = heading.get_text(" ", strip=True)
        if not text.endswith("?") and not text.lower().startswith(
            ("what ", "when ", "how ", "who ", "is ", "are ", "why ")
        ):
            continue
        if "?" not in text and not text.lower().startswith(
            ("what ", "when ", "how ", "who ")
        ):
            continue
        sibling = heading.find_next_sibling()
        answer = ""
        if sibling:
            answer = sibling.get_text(" ", strip=True)
        if not answer:
            parent = heading.parent
            if parent:
                answer = parent.get_text(" ", strip=True)
                answer = answer.replace(text, "", 1).strip()
        add(text if text.endswith("?") else text + "?", answer)

    # Regex fallback on FAQ-ish blocks in HTML text
    text = soup.get_text("\n", strip=True)
    qa_re = re.compile(
        r"(What [^?\n]{8,120}\?)\s*\n+([^\n]{20,500})",
        re.I,
    )
    for match in qa_re.finditer(text):
        add(match.group(1), match.group(2))

    # JSON-LD FAQPage
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if "FAQPage" not in raw and "Question" not in raw:
            continue
        try:
            import json

            data = json.loads(raw)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            main = node.get("mainEntity") or node.get("mainEntityOfPage") or []
            if isinstance(main, dict):
                main = [main]
            if not isinstance(main, list):
                continue
            for qnode in main:
                if not isinstance(qnode, dict):
                    continue
                q = qnode.get("name") or qnode.get("question")
                ans = qnode.get("acceptedAnswer") or qnode.get("answer")
                if isinstance(ans, dict):
                    ans = ans.get("text")
                if q and ans:
                    add(str(q), str(ans))

    return faqs


def extract_overview(soup: BeautifulSoup, project_name: Optional[str] = None) -> Optional[str]:
    # Prefer long paragraphs under overview-ish sections
    candidates: list[str] = []
    for p in soup.find_all("p"):
        text = _clean_text(p.get_text(" ", strip=True))
        if len(text) < 120:
            continue
        lowered = text.lower()
        if any(
            noise in lowered
            for noise in (
                "authorize company",
                "disclaimer",
                "copyright",
                "enter your details",
                "request callback",
                "get in touch",
            )
        ):
            continue
        candidates.append(text)

    if project_name:
        named = [c for c in candidates if project_name.lower() in c.lower()]
        if named:
            # longest named paragraph is usually the overview
            return max(named, key=len)
    if candidates:
        return max(candidates, key=len)
    return None


def parse_html_extras(html: str, project_name: Optional[str] = None) -> HtmlExtras:
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    title = _clean_text(title_tag.get_text()) if title_tag else None
    meta_description = _first_meta(
        soup,
        ("name", "description"),
        ("property", "og:description"),
    )
    canonical = None
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        canonical = link["href"].strip()

    h1_tag = soup.find("h1")
    h1 = _clean_text(h1_tag.get_text(" ", strip=True)) if h1_tag else None

    lat, lng = extract_coords(html)
    faqs = extract_faqs(soup, html)
    overview = extract_overview(soup, project_name=project_name)

    return HtmlExtras(
        title=title,
        meta_description=meta_description,
        canonical=canonical,
        overview=overview,
        latitude=lat,
        longitude=lng,
        faqs=faqs,
        h1=h1,
    )


def decode_possibly_encoded(text: str) -> str:
    try:
        return unquote(text)
    except Exception:
        return text
