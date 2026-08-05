"""Discover project URLs from 100acress listing pages."""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scraper.fetch import BASE_URL, Fetcher

logger = logging.getLogger(__name__)

DEFAULT_LISTING_URL = f"{BASE_URL}/projects/residential/"
COMMERCIAL_LISTING_URL = f"{BASE_URL}/projects/commercial/"
DEFAULT_LISTING_URLS = [DEFAULT_LISTING_URL, COMMERCIAL_LISTING_URL]

# Static / marketing / tool pages that look like /{slug}/
RESERVED_SLUGS = {
    "about",
    "about-us",
    "blog",
    "blogs",
    "branded-residences",
    "career",
    "career-with-us",
    "careers",
    "contact",
    "contact-us",
    "developers",
    "disclaimer",
    "emi-calculator",
    "faq",
    "faqs",
    "home",
    "login",
    "news",
    "nri",
    "post-property",
    "privacy",
    "privacy-policy",
    "projects",
    "rental",
    "resale",
    "search",
    "signup",
    "site-map",
    "sitemap",
    "terms",
    "terms-and-conditions",
    "testimonials",
    "top-luxury-projects",
    "luxury-projects",
}

# Slug substrings that indicate listing hubs / city aggregates, not project PDPs.
NON_PROJECT_SLUG_MARKERS = (
    "projects-in-",
    "new-launch-projects-in-",
    "-projects-in-",
    "new-projects-in-",
    "under-construction-projects-in-",
    "ready-to-move-projects-in-",
)

EXCLUDED_PREFIXES = (
    "/projects/",
    "/project/",
    "/blog/",
    "/blogs/",
    "/news/",
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/careers",
    "/api/",
    "/_next/",
    "/auth/",
    "/login",
    "/signup",
    "/post-property",
    "/property/",
    "/buy/",
    "/rent/",
    "/commercial/",
    "/dubai/",
    "/nri/",
    "/builders/",
    "/builder/",
    "/city/",
    "/cities/",
    "/search",
    "/sitemap",
    "/tags/",
    "/category/",
    "/projects-in-",
    "/rental/",
    "/developers/",
)

EXCLUDED_EXACT = {
    "/",
    "/projects",
    "/projects/residential",
    "/projects/residential/",
    "/projects/commercial",
    "/projects/commercial/",
}

PROJECT_PATH_RE = re.compile(r"^/[a-z0-9][a-z0-9\-]{1,120}/?$", re.I)
PUSH_RE = re.compile(r"self\.__next_f\.push\(\[(.*?)\]\)\s*</script>", re.S)
LONG_STRING_RE = re.compile(r'"((?:\\.|[^"\\]){100,})"')


def _unescape_js_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw.replace(r"\/", "/").replace(r"\"", '"')


def _is_non_project_slug(slug: str) -> bool:
    """True when slug is a listing hub, city aggregate, or marketing page."""
    if not slug:
        return True
    lowered = slug.lower()
    if lowered in RESERVED_SLUGS:
        return True
    if lowered.startswith("projects-in-"):
        return True
    for marker in NON_PROJECT_SLUG_MARKERS:
        if marker in lowered:
            return True
    return False


def normalize_project_url(href: str, base: str = BASE_URL) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
        return None

    # Bare slug from RSC
    if "/" not in href.strip("/"):
        href = "/" + href.strip("/") + "/"

    absolute = urljoin(base, href)
    parsed = urlparse(absolute)
    if parsed.netloc and "100acress.com" not in parsed.netloc:
        return None
    path = parsed.path or "/"
    path = path.split("?")[0].split("#")[0]
    if path in EXCLUDED_EXACT:
        return None
    lowered = path.lower()
    for prefix in EXCLUDED_PREFIXES:
        if lowered.startswith(prefix):
            return None
    if not PROJECT_PATH_RE.match(path):
        return None
    slug = path.strip("/").lower()
    if _is_non_project_slug(slug):
        return None
    if not path.endswith("/"):
        path = path + "/"
    return f"{BASE_URL}{path}"


def extract_project_urls_from_rsc(html: str) -> set[str]:
    """Prefer authoritative projectUrl values embedded in listing RSC data."""
    found: set[str] = set()
    for push in PUSH_RE.findall(html):
        for match in LONG_STRING_RE.finditer(push):
            blob = _unescape_js_string(match.group(1))
            if "projectUrl" not in blob:
                continue
            for m in re.finditer(r'"projectUrl"\s*:\s*"([^"]+)"', blob):
                url = normalize_project_url(m.group(1))
                if url:
                    found.add(url)
    return found


def extract_links_from_html(html: str, page_url: str) -> set[str]:
    found = extract_project_urls_from_rsc(html)
    if found:
        return found

    # Fallback: anchor tags after reserved-slug filtering
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        url = normalize_project_url(a["href"], base=page_url)
        if url:
            found.add(url)
    return found


def _listing_category(url: str) -> str | None:
    path = (urlparse(url).path or "").lower()
    if "/projects/commercial" in path:
        return "commercial"
    if "/projects/residential" in path:
        return "residential"
    return None


def _same_listing_category(candidate_url: str, page_url: str) -> bool:
    seed = _listing_category(page_url)
    if seed is None:
        return _listing_category(candidate_url) is not None
    return _listing_category(candidate_url) == seed


def extract_pagination_urls(html: str, page_url: str) -> set[str]:
    """Collect next listing pages for the same category as page_url."""
    soup = BeautifulSoup(html, "lxml")
    pages: set[str] = set()
    category = _listing_category(page_url) or "residential"
    marker = f"/projects/{category}"

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").strip().lower()
        abs_url = urljoin(page_url, href)
        parsed = urlparse(abs_url)
        host = parsed.netloc or "www.100acress.com"
        if "100acress.com" not in host:
            continue
        path = (parsed.path or "").lower()
        if marker in path or (
            "page=" in (parsed.query or "") and _same_listing_category(abs_url, page_url)
        ) or (
            text in {"next", "previous", "prev", "load more"}
            and _same_listing_category(abs_url, page_url)
        ):
            if normalize_project_url(href, base=page_url) is None and _same_listing_category(
                abs_url, page_url
            ):
                clean = f"https://{host}{parsed.path}"
                if parsed.query:
                    clean += f"?{parsed.query}"
                pages.add(clean)
    for match in re.finditer(
        rf'https?://(?:www\.)?100acress\.com{re.escape(marker)}/?\?[^"\'\s<>]*page=\d+',
        html,
        re.I,
    ):
        pages.add(match.group(0).rstrip("\\"))
    for match in re.finditer(
        rf'["\']({re.escape(marker)}/?\?[^"\']*page=\d+)["\']',
        html,
        re.I,
    ):
        pages.add(urljoin(BASE_URL, match.group(1)))
    return pages


def discover_project_urls(
    fetcher: Fetcher,
    listing_url: str = DEFAULT_LISTING_URL,
    max_projects: int | None = None,
) -> list[str]:
    """BFS listing pages and return unique project URLs."""
    to_visit = [listing_url]
    visited_listings: set[str] = set()
    projects: list[str] = []
    seen_projects: set[str] = set()

    while to_visit:
        current = to_visit.pop(0)
        if current in visited_listings:
            continue
        visited_listings.add(current)
        logger.info("Crawling listing page: %s", current)
        try:
            html = fetcher.get(current)
        except Exception as exc:
            logger.error("Failed listing page %s: %s", current, exc)
            continue

        for url in sorted(extract_links_from_html(html, current)):
            if url not in seen_projects:
                seen_projects.add(url)
                projects.append(url)
                if max_projects is not None and len(projects) >= max_projects:
                    logger.info("Reached max_projects=%s", max_projects)
                    return projects

        for page in sorted(extract_pagination_urls(html, current)):
            if page not in visited_listings and page not in to_visit:
                to_visit.append(page)

        if len(visited_listings) >= 30:
            logger.warning("Listing page visit cap reached (30)")
            break

    logger.info("Discovered %s project URLs", len(projects))
    return projects


def discover_all_project_urls(
    fetcher: Fetcher,
    listing_urls: Iterable[str] | None = None,
    max_projects: int | None = None,
) -> list[str]:
    """Discover project URLs from one or more listing seeds, deduped in order."""
    seeds = list(listing_urls) if listing_urls is not None else list(DEFAULT_LISTING_URLS)
    projects: list[str] = []
    for seed in seeds:
        seen = dedupe_preserve(projects)
        if max_projects is not None and len(seen) >= max_projects:
            break
        remaining = None if max_projects is None else max_projects - len(seen)
        batch = discover_project_urls(
            fetcher,
            listing_url=seed,
            max_projects=remaining,
        )
        projects.extend(batch)
    result = dedupe_preserve(projects)
    logger.info("Discovered %s unique project URLs across %s listing(s)", len(result), len(seeds))
    return result


def listing_urls_for_category(category: str) -> list[str]:
    """Map CLI category to listing seed URL(s)."""
    key = (category or "all").strip().lower()
    if key == "residential":
        return [DEFAULT_LISTING_URL]
    if key == "commercial":
        return [COMMERCIAL_LISTING_URL]
    if key == "all":
        return list(DEFAULT_LISTING_URLS)
    raise ValueError(f"Unknown category: {category!r} (expected residential|commercial|all)")


def load_urls_file(path: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = normalize_project_url(line)
            if url is None and line.startswith("http"):
                # Allow explicit URLs even if slug is unusual
                parsed = urlparse(line)
                path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
                url = f"{BASE_URL}{path}"
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def dedupe_preserve(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
