"""CLI entrypoint — multi-source scrape with archive merge."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scraper.fetch import Fetcher
from scraper.schema import SCHEMA_KEYS, ensure_schema, validate_record
from scraper.sources.acres99 import Acres99Adapter
from scraper.sources.acress100 import Acress100Adapter, NonProjectPageError
from scraper.sources.housing import HousingAdapter
from scraper.sources.magicbricks import MagicBricksAdapter
from scraper.sources.squareyards import SquareYardsAdapter
from scraper.store import (
    backfill_source_meta,
    dedupe_archive,
    filter_new_urls,
    known_source_urls,
    latest_path_for,
    load_records,
    match_key,
    merge_archive,
    run_stats_path_for,
    stamp_scraped_at,
    write_records,
)

logger = logging.getLogger(__name__)

ADAPTERS: dict[str, type] = {
    "100acress": Acress100Adapter,
    "99acres": Acres99Adapter,
    "housing": HousingAdapter,
    "magicbricks": MagicBricksAdapter,
    "squareyards": SquareYardsAdapter,
}

DEFAULT_SITES = ",".join(ADAPTERS.keys())
# Safety cap when --refresh is used without --max-projects (avoids re-fetching the whole web).
REFRESH_DEFAULT_CAP = 50


def default_sites_csv() -> str:
    return DEFAULT_SITES


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _unique_latest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve order while dropping duplicate match keys from latest.json."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in records:
        key = match_key(rec) or json.dumps(rec.get("slug") or id(rec), default=str)
        if key in seen:
            # Prefer the later (usually more enriched) copy
            for i, existing in enumerate(out):
                existing_key = match_key(existing) or json.dumps(existing.get("slug") or id(existing), default=str)
                if existing_key == key:
                    out[i] = rec
                    break
            continue
        seen.add(key)
        out.append(rec)
    return out


def append_error(path: Path, url: str, error: str, run_id: str = "") -> None:
    """Append an error entry to the error log (never truncates — append-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "url": url,
        "error": error,
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if run_id:
        entry["runId"] = run_id
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_sites(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return list(ADAPTERS.keys())
    unknown = [p for p in parts if p not in ADAPTERS]
    if unknown:
        raise SystemExit(f"Unknown site(s): {unknown}. Choose from {sorted(ADAPTERS)}")
    return parts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape real-estate sites into a merged property JSON archive.",
    )
    parser.add_argument(
        "--out",
        default="output/properties.json",
        help="Archive JSON path (merged across runs)",
    )
    parser.add_argument(
        "--jsonl",
        default=None,
        help="Optional JSONL of newly added/updated records",
    )
    parser.add_argument(
        "--sites",
        default=DEFAULT_SITES,
        help=f"Comma-separated sources (default: {DEFAULT_SITES})",
    )
    parser.add_argument(
        "--category",
        choices=("residential", "commercial", "all"),
        default="all",
        help="Listing category for 100acress (default: all)",
    )
    parser.add_argument(
        "--max-projects",
        type=int,
        default=None,
        help="Max *new* project pages per source",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="Delay between HTTP requests in seconds",
    )
    parser.add_argument(
        "--errors",
        default="output/errors.jsonl",
        help="Append-only error log path",
    )
    parser.add_argument(
        "--dedupe-only",
        action="store_true",
        help="Collapse duplicate units already in the archive (no network scrape)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch already-archived project URLs so new fields can null-fill merge",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    run_id = str(uuid.uuid4())[:8]  # short 8-char prefix is enough for log correlation
    logger.info("Run ID: %s", run_id)

    out_path = Path(args.out)
    latest_path = latest_path_for(out_path)
    stats_path = run_stats_path_for(out_path)
    archive = load_records(out_path)
    backfill_source_meta(archive)
    logger.info("Loaded archive with %s record(s) from %s", len(archive), out_path)

    if args.dedupe_only:
        before = len(archive)
        merged, collapsed, updated = dedupe_archive(archive)
        write_records(out_path, merged)
        write_records(latest_path, updated)
        stats = {
            "sites": [],
            "discovered": 0,
            "skippedKnown": 0,
            "skippedNonProject": 0,
            "scraped": 0,
            "failed": 0,
            "added": 0,
            "updated": len(updated),
            "collapsed": collapsed,
            "runId": run_id,
            "message": (
                f"Dedupe-only: {before} -> {len(merged)} "
                f"({collapsed} merges, {len(updated)} enriched)"
            ),
            "perSite": {},
        }
        keys = [match_key(r) for r in merged]
        key_counts: dict[str, int] = {}
        for k in keys:
            if not k:
                continue
            key_counts[k] = key_counts.get(k, 0) + 1
        stats["duplicateMatchKeys"] = sum(1 for v in key_counts.values() if v > 1)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        logger.info(stats["message"])
        if stats["duplicateMatchKeys"]:
            logger.warning("Archive still has %s duplicate match-key group(s)", stats["duplicateMatchKeys"])
        return 0

    sites = parse_sites(args.sites)
    errors_path = Path(args.errors)
    # errors.jsonl is append-only across runs; each line carries a timestamp + runId.
    # Do NOT delete it here — history is preserved intentionally.

    known = known_source_urls(archive)
    stats: dict[str, Any] = {
        "sites": sites,
        "discovered": 0,
        "skippedKnown": 0,
        "skippedNonProject": 0,
        "scraped": 0,
        "failed": 0,
        "added": 0,
        "updated": 0,
        "collapsed": 0,
        "message": "",
        "perSite": {},
    }

    batch: list[dict[str, Any]] = []
    with Fetcher(delay=args.delay) as fetcher:
        for site in sites:
            adapter_cls = ADAPTERS[site]
            # All adapters accept category= (base class guarantees it); only 100acress uses it.
            adapter = adapter_cls(category=args.category)
            if args.category != "all" and site != "100acress":
                logger.warning(
                    "[%s] --category=%s is ignored for this source (only 100acress uses it)",
                    site,
                    args.category,
                )
            site_stats = {
                "discovered": 0,
                "skippedKnown": 0,
                "skippedNonProject": 0,
                "scraped": 0,
                "failed": 0,
                "units": 0,
                "blocked": False,
            }
            try:
                # FIX: pass max_projects so discovery is capped early (saves HTTP requests)
                urls = adapter.discover(fetcher, max_projects=args.max_projects)
            except Exception as exc:
                logger.exception("Discovery failed for %s: %s", site, exc)
                stats["perSite"][site] = {**site_stats, "error": str(exc), "blocked": True}
                continue

            site_stats["discovered"] = len(urls)
            stats["discovered"] += len(urls)
            if len(urls) == 0:
                site_stats["blocked"] = True
            if args.refresh:
                skipped = 0
                logger.info("[%s] --refresh: re-fetching known URLs for enrichment merge", site)
            else:
                urls = filter_new_urls(urls, known)
                skipped = site_stats["discovered"] - len(urls)
            site_stats["skippedKnown"] = skipped
            stats["skippedKnown"] += skipped
            if skipped:
                logger.info("[%s] Skipping %s already-archived URL(s)", site, skipped)

            new_candidates = len(urls)
            refresh_cap = args.max_projects
            if args.refresh and refresh_cap is None:
                refresh_cap = REFRESH_DEFAULT_CAP
                logger.warning(
                    "[%s] --refresh without --max-projects; capping to %s URLs (pass --max-projects to override)",
                    site,
                    refresh_cap,
                )
            if refresh_cap is not None:
                urls = urls[:refresh_cap]
            if new_candidates and not urls:
                logger.info(
                    "[%s] %s URL(s) found but cap=%s reduced scrape to 0",
                    site,
                    new_candidates,
                    refresh_cap,
                )

            if not urls:
                logger.info("[%s] Nothing new to scrape", site)
                stats["perSite"][site] = site_stats
                continue

            logger.info("[%s] Scraping %s project(s)", site, len(urls))
            for i, url in enumerate(urls, start=1):
                logger.info("[%s %s/%s] %s", site, i, len(urls), url)
                try:
                    unit_records = adapter.fetch_project(fetcher, url)
                    normalized: list[dict[str, Any]] = []
                    for rec in unit_records:
                        rec = ensure_schema(rec)
                        core = {k: rec.get(k) for k in SCHEMA_KEYS}
                        problems = validate_record(core)
                        if problems:
                            logger.warning("Schema issues for %s: %s", url, problems)
                        missing = [k for k in SCHEMA_KEYS if k not in rec]
                        if missing:
                            raise RuntimeError(f"missing schema keys: {missing}")
                        normalized.append(rec)
                    batch.extend(normalized)
                    known.add(url)
                    known.add(url.rstrip("/"))
                    known.add(url if url.endswith("/") else url + "/")
                    site_stats["scraped"] += 1
                    site_stats["units"] += len(unit_records)
                    stats["scraped"] += 1
                    logger.info("  -> %s unit record(s)", len(unit_records))
                except NonProjectPageError as exc:
                    logger.warning("Skipping non-project page %s: %s", url, exc)
                    append_error(errors_path, url, str(exc), run_id=run_id)
                    site_stats["skippedNonProject"] += 1
                    stats["skippedNonProject"] += 1
                except Exception as exc:
                    msg = str(exc)
                    soft = any(
                        tok in msg
                        for tok in (
                            "417",
                            "403",
                            "406",
                            "451",
                            "Access Denied",
                            "Challenge",
                            "anti-bot",
                        )
                    )
                    if soft:
                        logger.warning("Blocked/failed %s: %s", url, exc)
                    else:
                        logger.exception("Failed %s", url)
                    append_error(errors_path, url, msg, run_id=run_id)
                    site_stats["failed"] += 1
                    stats["failed"] += 1
            stats["perSite"][site] = site_stats

    if batch:
        stamp_scraped_at(batch)

    merged, added, updated = merge_archive(archive, batch)

    # Fast sanity: collapse leftover exact/soft duplicates when any remain.
    keys_probe = [match_key(r) for r in merged if match_key(r)]
    key_counts_probe: dict[str, int] = {}
    for k in keys_probe:
        key_counts_probe[k] = key_counts_probe.get(k, 0) + 1
    needs_dedupe = any(v > 1 for v in key_counts_probe.values())

    collapsed = 0
    if needs_dedupe:
        before_dedupe = len(merged)
        merged, collapsed, dedupe_updated = dedupe_archive(merged)
        if collapsed:
            logger.info(
                "Collapsed %s duplicate unit(s) in archive (%s -> %s)",
                collapsed,
                before_dedupe,
                len(merged),
            )
            updated = updated + dedupe_updated

    write_records(out_path, merged)
    # Latest = newly added + updated enrichments (dedupe identical row objects by match key)
    latest = _unique_latest(added + updated)
    write_records(latest_path, latest)

    stats["added"] = len(added)
    stats["updated"] = len(updated)
    stats["collapsed"] = collapsed
    stats["runId"] = run_id
    # Sanity: duplicate match keys in archive
    keys = [match_key(r) for r in merged]
    key_counts: dict[str, int] = {}
    for k in keys:
        if not k:
            continue
        key_counts[k] = key_counts.get(k, 0) + 1
    dup_keys = sum(1 for v in key_counts.values() if v > 1)
    stats["duplicateMatchKeys"] = dup_keys
    if dup_keys:
        logger.warning("Archive has %s duplicate match-key group(s)", dup_keys)

    if len(added) == 0 and len(updated) == 0 and collapsed == 0:
        detail_parts = [
            f"{stats['skippedKnown']} known skipped",
            f"{stats['discovered']} discovered",
        ]
        if stats["skippedNonProject"]:
            detail_parts.append(f"{stats['skippedNonProject']} non-project skipped")
        if stats["failed"]:
            detail_parts.append(f"{stats['failed']} failed")
        stats["message"] = (
            f"No new projects found ({', '.join(detail_parts)} across {', '.join(sites)})"
        )
    else:
        stats["message"] = (
            f"Added {len(added)}, updated {len(updated)}, collapsed {collapsed} "
            f"(discovered {stats['discovered']}, skipped {stats['skippedKnown']}, "
            f"scraped {stats['scraped']})"
        )
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")

    logger.info(stats["message"])
    logger.info(
        "Archive now %s record(s) (+%s new, ~%s updated, -%s dups) -> %s; latest -> %s",
        len(merged),
        len(added),
        len(updated),
        collapsed,
        out_path,
        latest_path,
    )

    if args.jsonl:
        write_jsonl(Path(args.jsonl), latest)
        logger.info("Wrote JSONL to %s", args.jsonl)

    if errors_path.exists():
        logger.warning("Some projects failed; see %s", errors_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
