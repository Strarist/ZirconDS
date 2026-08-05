"""HTTP client with retries and polite rate limiting."""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

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

# Challenge / WAF style responses. 99acres often returns 417 instead of 403.
ANTIBOT_CODES = frozenset({403, 406, 417, 451})
RATE_LIMIT_CODES = frozenset({429})
HARD_FAIL_CODES = frozenset({404})
RETRYABLE_SERVER = frozenset({500, 502, 503, 504})


def _browser_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
    }


def is_challenge_body(html: str, status_code: Optional[int] = None) -> bool:
    """True when the response looks like a WAF/challenge page, not a listing/PDP."""
    if status_code in ANTIBOT_CODES:
        return True
    text = (html or "").strip()
    if len(text) < 800:
        low = text.lower()
        if any(tok in low for tok in ("access denied", "captcha", "cf-browser", "attention required")):
            return True
    low = text[:2000].lower()
    return "access denied" in low and "permission to access" in low


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
            headers=_browser_headers(user_agent),
            timeout=timeout,
            follow_redirects=True,
            http2=False,
        )

        def _strip_expect(request: httpx.Request) -> None:
            # Avoid HTTP 417 from servers that reject Expect: 100-continue.
            if "Expect" in request.headers:
                del request.headers["Expect"]

        self.client.event_hooks["request"] = [_strip_expect]

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _set_ua(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self.client.headers.update(_browser_headers(user_agent))

    def _site_referer(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return BASE_URL
        return f"{parsed.scheme}://{parsed.netloc}/"

    def get(self, url: str, *, referer: Optional[str] = None) -> str:
        last_error: Optional[Exception] = None
        tried_mobile = self.user_agent == MOBILE_UA
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            headers = {
                "Referer": referer or self._site_referer(url),
            }
            try:
                self._last_request_at = time.monotonic()
                response = self.client.get(url, headers=headers)
                code = response.status_code

                if code in HARD_FAIL_CODES:
                    response.raise_for_status()

                if code in RATE_LIMIT_CODES or code in RETRYABLE_SERVER:
                    wait = min(2 ** attempt, 20)
                    logger.warning(
                        "HTTP %s for %s (attempt %s/%s), sleeping %ss",
                        code,
                        url,
                        attempt,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if code in ANTIBOT_CODES:
                    last_error = httpx.HTTPStatusError(
                        f"Client error {code}",
                        request=response.request,
                        response=response,
                    )
                    # 403 sometimes unlocks with mobile UA; 417 from 99acres often worsens on mobile.
                    if code == 403 and not tried_mobile:
                        logger.info("HTTP 403 for %s — retrying with mobile User-Agent", url)
                        self._set_ua(MOBILE_UA)
                        tried_mobile = True
                        continue
                    wait = min(2 ** attempt, 12)
                    logger.warning(
                        "HTTP %s (anti-bot) for %s (attempt %s/%s), sleeping %ss",
                        code,
                        url,
                        attempt,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                text = response.text
                if is_challenge_body(text, code):
                    last_error = RuntimeError(f"Challenge/Access Denied page for {url}")
                    wait = min(2 ** attempt, 12)
                    logger.warning(
                        "Challenge page for %s (attempt %s/%s), sleeping %ss",
                        url,
                        attempt,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    if not tried_mobile:
                        self._set_ua(MOBILE_UA)
                        tried_mobile = True
                    continue
                return text
            except httpx.HTTPStatusError as exc:
                last_error = exc
                code = exc.response.status_code if exc.response is not None else None
                if code in HARD_FAIL_CODES:
                    raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
                if code in ANTIBOT_CODES:
                    if code == 403 and not tried_mobile:
                        logger.info("HTTP 403 for %s — retrying with mobile User-Agent", url)
                        self._set_ua(MOBILE_UA)
                        tried_mobile = True
                        continue
                    wait = min(2 ** attempt, 12)
                    logger.warning(
                        "Request failed for %s (attempt %s/%s): %s",
                        url,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    time.sleep(wait)
                    continue
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
