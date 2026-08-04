"""HTTP client with retries and polite rate limiting."""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Some portals (e.g. 99acres) block desktop UA but accept mobile Safari.
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

BASE_URL = "https://www.100acress.com"


class Fetcher:
    def __init__(
        self,
        delay: float = 1.2,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._last_request_at = 0.0
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get(self, url: str) -> str:
        last_error: Optional[Exception] = None
        tried_mobile = self.user_agent == MOBILE_UA
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                self._last_request_at = time.monotonic()
                response = self.client.get(url)
                # Anti-bot / hard client errors — try mobile UA once, else stop
                if response.status_code in (403, 406, 451):
                    if not tried_mobile and response.status_code == 403:
                        logger.info("HTTP %s for %s — retrying with mobile User-Agent", response.status_code, url)
                        self.client.headers["User-Agent"] = MOBILE_UA
                        tried_mobile = True
                        continue
                    response.raise_for_status()
                if response.status_code == 404:
                    response.raise_for_status()
                if response.status_code in (429, 500, 502, 503, 504):
                    wait = min(2 ** attempt, 20)
                    logger.warning(
                        "HTTP %s for %s (attempt %s/%s), sleeping %ss",
                        response.status_code,
                        url,
                        attempt,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as exc:
                last_error = exc
                code = exc.response.status_code if exc.response is not None else None
                if code in (403, 406, 451):
                    if not tried_mobile and code == 403:
                        logger.info("HTTP %s for %s — retrying with mobile User-Agent", code, url)
                        self.client.headers["User-Agent"] = MOBILE_UA
                        tried_mobile = True
                        continue
                    raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
                if code == 404:
                    raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
                wait = min(2 ** attempt, 20)
                logger.warning(
                    "Request failed for %s (attempt %s/%s): %s",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                )
                time.sleep(wait)
            except httpx.HTTPError as exc:
                last_error = exc
                wait = min(2 ** attempt, 20)
                logger.warning(
                    "Request failed for %s (attempt %s/%s): %s",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                )
                time.sleep(wait)
        raise RuntimeError(f"Failed to fetch {url}: {last_error}")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
