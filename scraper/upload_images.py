"""Download scraped imageUrls and replace them with public S3 URLs.

Usage:
  python -m scraper.upload_images
  python -m scraper.upload_images --in output/properties.json --limit 5
  python -m scraper.upload_images --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from scraper.schema import coerce_images, ensure_schema
from scraper.store import load_records, match_key, write_records

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (does not override existing)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            f"Missing env var(s): {', '.join(missing)}. "
            f"Set them in {ROOT / '.env'} or the environment."
        )
    return {n: os.environ[n] for n in names}


def is_our_s3_url(url: str, bucket: str, region: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    bucket_l = bucket.lower()
    region_l = region.lower()
    return host in {
        f"{bucket_l}.s3.{region_l}.amazonaws.com",
        f"{bucket_l}.s3.amazonaws.com",
        f"s3.{region_l}.amazonaws.com",
        "s3.amazonaws.com",
    } and (bucket_l in host or f"/{bucket_l}/" in urlparse(url).path)


def sanitize_segment(value: str, *, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return (cleaned or "unknown")[:max_len]


def guess_extension(url: str, content_type: Optional[str]) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext == ".jpe":
            return ".jpg"
        if ext:
            return ext
    return ".jpg"


def content_type_for(ext: str, header_ct: Optional[str]) -> str:
    if header_ct and header_ct.startswith("image/"):
        return header_ct.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(f"file{ext}")
    return guessed or "application/octet-stream"


def record_folder(record: dict[str, Any]) -> str:
    key = match_key(record) or (record.get("slug") or "")
    if key:
        return sanitize_segment(key.replace("|", "-"), max_len=120)
    project = sanitize_segment(str(record.get("projectName") or "property"))
    return project


def object_key(record: dict[str, Any], source_url: str, ext: str) -> str:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    return f"properties/{record_folder(record)}/{digest}{ext}"


def public_s3_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def download_image(client: httpx.Client, url: str) -> tuple[bytes, Optional[str]]:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type")


def upload_bytes(
    s3_client: Any,
    *,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> None:
    # Prefer ACL when the bucket allows it; fall back if ACLs are disabled.
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ACL="public-read",
        )
    except Exception as exc:  # noqa: BLE001 — boto exception types vary by config
        msg = str(exc).lower()
        if "acl" in msg or "access control list" in msg or "blockpublicacl" in msg:
            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        else:
            raise


def process_url(
    *,
    url: str,
    record: dict[str, Any],
    http: httpx.Client,
    s3: Any,
    bucket: str,
    region: str,
    cache: dict[str, str],
    dry_run: bool,
) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if is_our_s3_url(url, bucket, region):
        return url
    if url in cache:
        return cache[url]

    if dry_run:
        ext = guess_extension(url, None)
        key = object_key(record, url, ext)
        s3_url = public_s3_url(bucket, region, key)
        cache[url] = s3_url
        logger.info("dry-run would upload %s -> %s", url[:80], s3_url)
        return s3_url

    body, header_ct = download_image(http, url)
    ext = guess_extension(url, header_ct)
    key = object_key(record, url, ext)
    ct = content_type_for(ext, header_ct)
    upload_bytes(s3, bucket=bucket, key=key, body=body, content_type=ct)
    s3_url = public_s3_url(bucket, region, key)
    cache[url] = s3_url
    logger.info("uploaded %s -> %s", url[:80], s3_url)
    return s3_url


def process_records(
    records: list[dict[str, Any]],
    *,
    http: httpx.Client,
    s3: Any,
    bucket: str,
    region: str,
    dry_run: bool,
    limit: Optional[int],
) -> dict[str, int]:
    cache: dict[str, str] = {}
    stats = {
        "records_touched": 0,
        "urls_seen": 0,
        "urls_uploaded": 0,
        "urls_skipped_s3": 0,
        "urls_reused": 0,
        "urls_failed": 0,
    }
    processed_records = 0

    for idx, record in enumerate(records):
        records[idx] = ensure_schema(record)
        record = records[idx]
        images = coerce_images(record.get("images"))
        # Also fold any leftover legacy imageUrls
        if record.get("imageUrls"):
            images = coerce_images(images + list(record.get("imageUrls") or []))
        if not images:
            continue
        if limit is not None and processed_records >= limit:
            break

        new_images: list[dict[str, Any]] = []
        changed = False
        for img in images:
            src = str(img.get("url") or "").strip()
            if not src:
                continue
            stats["urls_seen"] += 1
            meta = {
                "type": img.get("type") or "GALLERY",
                "is_primary": bool(img.get("is_primary")),
                "order": img.get("order"),
            }
            if is_our_s3_url(src, bucket, region):
                stats["urls_skipped_s3"] += 1
                new_images.append({**meta, "url": src})
                continue
            if src in cache:
                stats["urls_reused"] += 1
                mapped = cache[src]
                new_images.append({**meta, "url": mapped})
                if mapped != src:
                    changed = True
                continue
            try:
                mapped = process_url(
                    url=src,
                    record=record,
                    http=http,
                    s3=s3,
                    bucket=bucket,
                    region=region,
                    cache=cache,
                    dry_run=dry_run,
                )
                stats["urls_uploaded"] += 1
                new_images.append({**meta, "url": mapped})
                if mapped != src:
                    changed = True
            except Exception as exc:  # noqa: BLE001
                stats["urls_failed"] += 1
                logger.warning("failed %s: %s", src[:100], exc)
                new_images.append({**meta, "url": src})

        normalized = coerce_images(new_images)
        if changed or record.get("imageUrls") or record.get("images") != normalized:
            record["images"] = normalized
            record.pop("imageUrls", None)
            records[idx] = ensure_schema(record)
            stats["records_touched"] += 1
        processed_records += 1

    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Upload property images to S3 and rewrite JSON with public S3 image URLs.",
    )
    p.add_argument(
        "--in",
        dest="input_path",
        default="output/properties.json",
        help="Input archive JSON (default: output/properties.json)",
    )
    p.add_argument(
        "--out",
        dest="output_path",
        default=None,
        help="Output path (default: overwrite --in)",
    )
    p.add_argument(
        "--env",
        default=str(ROOT / ".env"),
        help="Path to .env with AWS credentials",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N records that have images",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Map URLs without downloading or uploading",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout seconds for image downloads",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    load_dotenv(Path(args.env))
    creds = require_env(
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_S3_BUCKET",
    )
    bucket = creds["AWS_S3_BUCKET"]
    region = creds["AWS_REGION"]

    in_path = Path(args.input_path)
    out_path = Path(args.output_path) if args.output_path else in_path
    records = load_records(in_path)
    if not records:
        logger.error("No records found in %s", in_path)
        return 1

    try:
        import boto3
    except ImportError as exc:
        raise SystemExit(
            "boto3 is required. Run: pip install -r requirements.txt"
        ) from exc

    s3 = None
    if not args.dry_run:
        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
        )

    with httpx.Client(
        headers={"User-Agent": DEFAULT_UA, "Accept": "image/*,*/*;q=0.8"},
        timeout=args.timeout,
        follow_redirects=True,
    ) as http:
        stats = process_records(
            records,
            http=http,
            s3=s3,
            bucket=bucket,
            region=region,
            dry_run=args.dry_run,
            limit=args.limit,
        )

    if not args.dry_run:
        write_records(out_path, records)
        logger.info("wrote %s (%d records)", out_path, len(records))
    else:
        logger.info("dry-run complete; JSON not written")

    logger.info(
        "done records_touched=%d urls_seen=%d uploaded=%d skipped_s3=%d reused=%d failed=%d",
        stats["records_touched"],
        stats["urls_seen"],
        stats["urls_uploaded"],
        stats["urls_skipped_s3"],
        stats["urls_reused"],
        stats["urls_failed"],
    )
    return 0 if stats["urls_failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
