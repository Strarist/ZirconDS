#!/usr/bin/env python3
"""Serve the verification UI, scraped properties JSON, and scrape controls."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent
DATA_FILE = ROOT / "output" / "properties.json"
LATEST_FILE = ROOT / "output" / "latest.json"
RUN_FILE = ROOT / "output" / "scrape-run.json"
STATUS_FILE = ROOT / "output" / "scrape-status.json"
DEFAULT_PORT = 8765
LOG_TAIL_MAX = 80
ALLOWED_IMAGE_HOSTS = (
    "cdn.100acress.com",
    "dqtkvjsm31k64.cloudfront.net",
    "100acress.com",
    "www.100acress.com",
    "img.staticmb.com",
    "cdn.staticmb.com",
    "property.magicbricks.com",
    "www.magicbricks.com",
    "is1-2.housingcdn.com",
    "is1-3.housingcdn.com",
    "housing.com",
    "www.housing.com",
)

_job_lock = threading.Lock()
_process: subprocess.Popen[str] | None = None
_log_lines: deque[str] = deque(maxlen=LOG_TAIL_MAX)
_status: dict[str, Any] = {
    "state": "idle",
    "startedAt": None,
    "finishedAt": None,
    "exitCode": None,
    "message": "No scrape run yet",
    "logTail": [],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _persist_status() -> None:
    payload = {
        **_status,
        "logTail": list(_log_lines),
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _set_status(**kwargs: Any) -> None:
    _status.update(kwargs)
    _status["logTail"] = list(_log_lines)
    _persist_status()


def _append_log(line: str) -> None:
    text = line.rstrip("\n")
    if not text:
        return
    _log_lines.append(text)
    _status["logTail"] = list(_log_lines)
    _persist_status()


def _load_run_stats() -> dict[str, Any] | None:
    if not RUN_FILE.is_file():
        return None
    try:
        data = json.loads(RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _reader_thread(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _append_log(line)
    finally:
        code = proc.wait()
        global _process
        with _job_lock:
            finished = _utc_now()
            run = _load_run_stats() or {}
            msg = run.get("message")
            if code == 0:
                _set_status(
                    state="ok",
                    finishedAt=finished,
                    exitCode=code,
                    message=msg or "Scrape finished successfully",
                    runStats=run,
                )
            else:
                _set_status(
                    state="error",
                    finishedAt=finished,
                    exitCode=code,
                    message=msg or f"Scrape failed with exit code {code}",
                    runStats=run,
                )
            _process = None


def start_scrape() -> tuple[int, dict[str, Any]]:
    """Start scraper subprocess. Returns (http_status, body)."""
    global _process
    with _job_lock:
        if _process is not None and _process.poll() is None:
            return 409, {
                **_status,
                "logTail": list(_log_lines),
                "message": "A scrape is already running",
            }

        _log_lines.clear()
        cmd = [
            sys.executable,
            "-m",
            "scraper",
            "--sites",
            "100acress,housing,magicbricks",
            "--category",
            "all",
            "--out",
            str(DATA_FILE),
        ]
        _append_log(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            _set_status(
                state="error",
                startedAt=_utc_now(),
                finishedAt=_utc_now(),
                exitCode=None,
                message=f"Failed to start scraper: {exc}",
            )
            return 500, {**_status, "logTail": list(_log_lines)}

        _process = proc
        _set_status(
            state="running",
            startedAt=_utc_now(),
            finishedAt=None,
            exitCode=None,
            message="Scrape in progress (100acress + housing + magicbricks)",
            runStats=None,
        )
        threading.Thread(target=_reader_thread, args=(proc,), daemon=True).start()
        return 202, {**_status, "logTail": list(_log_lines)}


def get_status() -> dict[str, Any]:
    with _job_lock:
        running = _process is not None and _process.poll() is None
        if running and _status.get("state") != "running":
            _status["state"] = "running"
        return {**_status, "logTail": list(_log_lines)}


def _count_json_array(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def _latest_scraped_at() -> str | None:
    if not LATEST_FILE.is_file():
        return None
    try:
        data = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if isinstance(first, dict):
        value = first.get("scrapedAt")
        return value if isinstance(value, str) else None
    return None


def get_meta() -> dict[str, Any]:
    meta: dict[str, Any] = {
        "dataFile": str(DATA_FILE),
        "latestFile": str(LATEST_FILE),
        "exists": DATA_FILE.is_file(),
        "mtime": None,
        "mtimeIso": None,
        "sizeBytes": None,
        "archiveCount": _count_json_array(DATA_FILE),
        "latestCount": _count_json_array(LATEST_FILE),
        "latestScrapedAt": _latest_scraped_at(),
        "lastRun": _load_run_stats(),
        "scrape": get_status(),
    }
    if DATA_FILE.is_file():
        stat = DATA_FILE.stat()
        meta["mtime"] = stat.st_mtime
        meta["mtimeIso"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        meta["sizeBytes"] = stat.st_size
    return meta


class VerificationHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/data/properties.json":
            self._serve_json_file(DATA_FILE)
            return
        if path == "/data/latest.json":
            self._serve_json_file(LATEST_FILE)
            return
        if path == "/proxy-image":
            self._proxy_image(parsed.query)
            return
        if path == "/api/scrape/status":
            self._json_response(200, get_status())
            return
        if path == "/api/meta":
            self._json_response(200, get_meta())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/scrape":
            # Drain body if present
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            code, body = start_scrape()
            self._json_response(code, body)
            return
        self.send_error(404, "Not found")

    def _json_response(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_json_file(self, path: Path) -> None:
        if not path.is_file():
            body = b"[]"
        else:
            body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_image(self, query: str) -> None:
        params = parse_qs(query)
        raw_url = (params.get("url") or [""])[0]
        filename = (params.get("filename") or ["image.jpg"])[0]
        url = unquote(raw_url)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not any(
            host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_IMAGE_HOSTS
        ):
            self.send_error(400, "URL host not allowed")
            return
        filename = re.sub(r"[^\w.\-]+", "_", filename) or "image.jpg"
        try:
            req = Request(url, headers={"User-Agent": "ZirconDS-verification-ui/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                content_type = (
                    resp.headers.get("Content-Type")
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self.send_error(502, f"Failed to fetch image: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        print("[%s] %s" % (self.log_date_time_string(), format % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the property verification UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open a browser tab automatically",
    )
    args = parser.parse_args()

    if STATUS_FILE.is_file():
        try:
            saved = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("state") != "running":
                _status.update(
                    {
                        "state": saved.get("state") or "idle",
                        "startedAt": saved.get("startedAt"),
                        "finishedAt": saved.get("finishedAt"),
                        "exitCode": saved.get("exitCode"),
                        "message": saved.get("message") or _status["message"],
                    }
                )
                for line in saved.get("logTail") or []:
                    if isinstance(line, str):
                        _log_lines.append(line)
        except (OSError, json.JSONDecodeError):
            pass

    handler = partial(VerificationHandler, directory=str(UI_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Verification UI: {url}")
    print(f"Data file: {DATA_FILE}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
