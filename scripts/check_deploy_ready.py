"""Local readiness checks for Render + Vercel deployment."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = 0
FAIL = 0


def load_dotenv(path: Path) -> None:
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


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"[{mark}] {name}{suffix}")


def http_ok(url: str, timeout: float = 30.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    load_dotenv(ROOT / ".env")
    print("=== ZirconDS deployment readiness ===\n")

    # Files
    for rel in (
        "requirements.txt",
        "Procfile",
        "runtime.txt",
        "render.yaml",
        "vercel.json",
        "ui/serve.py",
        "ui/index.html",
        "output/properties.json",
        ".env",
    ):
        path = ROOT / rel
        check(f"exists {rel}", path.is_file(), f"{path.stat().st_size} bytes" if path.is_file() else "missing")

    # Python imports
    try:
        import boto3  # noqa: F401
        import bs4  # noqa: F401
        import httpx  # noqa: F401
        import lxml  # noqa: F401

        check("Python deps importable", True, "httpx, bs4, lxml, boto3")
    except ImportError as exc:
        check("Python deps importable", False, str(exc))

    # Env
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_S3_BUCKET"):
        check(f"env {key}", bool(os.environ.get(key)))

    # Archive quality
    props = ROOT / "output" / "properties.json"
    if props.is_file():
        data = json.loads(props.read_text(encoding="utf-8"))
        check("properties.json is array", isinstance(data, list), f"{len(data)} records")
        with_imgs = sum(1 for r in data if isinstance(r, dict) and r.get("imageUrls"))
        s3_refs = sum(
            1
            for r in data
            if isinstance(r, dict)
            for u in (r.get("imageUrls") or [])
            if isinstance(u, str) and "zircondsphotos.s3" in u
        )
        check("records with images", with_imgs > 0, str(with_imgs))
        check("S3 image URL refs", s3_refs > 0, str(s3_refs))

    # serve.py production bits
    serve_src = (ROOT / "ui" / "serve.py").read_text(encoding="utf-8")
    check("serve.py reads PORT env", "os.environ.get(\"PORT\")" in serve_src or "environ.get('PORT')" in serve_src)
    check("serve.py has /healthz", "/healthz" in serve_src)
    check("serve.py has CORS", "Access-Control-Allow-Origin" in serve_src)
    check("serve.py bootstraps S3 data", "ensure_data_from_s3" in serve_src)
    check("serve.py allows S3 image host", "zircondsphotos.s3" in serve_src)

    # Procfile / vercel placeholders
    proc = (ROOT / "Procfile").read_text(encoding="utf-8")
    check("Procfile binds 0.0.0.0", "0.0.0.0" in proc and "$PORT" in proc)
    vercel = (ROOT / "vercel.json").read_text(encoding="utf-8")
    check(
        "vercel.json points at Render",
        "zirconds.onrender.com" in vercel and "REPLACE_WITH_RENDER_URL" not in vercel,
        "https://zirconds.onrender.com",
    )

    # Live local UI (optional)
    ok, detail = http_ok("http://127.0.0.1:8765/healthz", timeout=5)
    check("local UI /healthz", ok, detail if ok else "not running (start: python ui/serve.py)")

    # Public S3
    bucket = os.environ.get("AWS_S3_BUCKET", "zircondsphotos")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    data_url = f"https://{bucket}.s3.{region}.amazonaws.com/data/properties.json"
    ok, detail = http_ok(data_url, timeout=60)
    check("public S3 data/properties.json", ok, detail)

    sample_img = None
    if props.is_file():
        data = json.loads(props.read_text(encoding="utf-8"))
        for rec in data:
            for url in rec.get("imageUrls") or []:
                if isinstance(url, str) and "zircondsphotos.s3" in url:
                    sample_img = url
                    break
            if sample_img:
                break
    if sample_img:
        ok, detail = http_ok(sample_img, timeout=30)
        check("public S3 sample image", ok, detail)
    else:
        check("public S3 sample image", False, "no S3 imageUrls found")

    print(f"\n=== Result: {PASS} passed, {FAIL} failed ===")
    if FAIL:
        print("Fix FAIL items before deploying.")
        return 1
    print("Ready for Render, then update vercel.json and deploy Vercel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
