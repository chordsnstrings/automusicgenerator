"""Shared HTTP behaviour: retry with backoff, and a single place that decides
what "retryable" means.

Every provider here returns HTTP 200 on at least some failures, so status code
alone is never enough — each client checks its own envelope and raises
ProviderError with an explicit ``retryable`` flag.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from .errors import ProviderError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def request(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    json: Any = None,
    params: dict | None = None,
    timeout: float = 60.0,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """One HTTP call with bounded retries on transport and 5xx failures.

    Deliberately does not retry 4xx other than 408/425/429: a malformed request
    retried three times is three identical failures and a slower error message.

    Redirects are followed. Several of the free feeds live behind a permanent
    redirect to a different host, and an unfollowed 301 arrives as a non-JSON
    body rather than an error status — which looks like a parse bug at the call
    site instead of a routing one.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.request(method, url, headers=headers, json=json,
                                 params=params, timeout=timeout,
                                 follow_redirects=True)
        except httpx.HTTPError as exc:
            last = exc
            if attempt == attempts:
                raise ProviderError(provider, f"transport failure: {exc}", retryable=True) from exc
            _backoff(attempt, base_delay, sleep, provider, str(exc))
            continue

        if resp.status_code in RETRYABLE_STATUS and attempt < attempts:
            _backoff(attempt, base_delay, sleep, provider,
                     f"HTTP {resp.status_code}")
            continue
        return resp

    raise ProviderError(provider, f"exhausted {attempts} attempts: {last}", retryable=True)


def _backoff(attempt: int, base: float, sleep: Callable[[float], None],
             provider: str, why: str) -> None:
    # Jitter so a whole fan-out does not retry in lockstep.
    delay = base * (2 ** (attempt - 1)) * (0.75 + random.random() * 0.5)
    log.warning("%s: %s — retrying in %.1fs (attempt %d)", provider, why, delay, attempt)
    sleep(delay)


def download(url: str, dest, *, provider: str = "download", timeout: float = 300.0,
             attempts: int = 3) -> int:
    """Stream a URL to disk. Returns bytes written.

    Writes to a ``.part`` file and renames on completion, so a truncated
    download can never be mistaken for a finished asset by a later run.
    """
    from pathlib import Path
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        written = 0
        try:
            with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise ProviderError(provider, f"HTTP {r.status_code} fetching asset",
                                        retryable=r.status_code in RETRYABLE_STATUS,
                                        status=r.status_code)
                with part.open("wb") as fh:
                    for chunk in r.iter_bytes(65536):
                        fh.write(chunk)
                        written += len(chunk)
            if written == 0:
                raise ProviderError(provider, "empty response body", retryable=True)
            part.rename(dest)
            return written
        except (httpx.HTTPError, ProviderError) as exc:
            last = exc
            part.unlink(missing_ok=True)
            retryable = getattr(exc, "retryable", True)
            if attempt == attempts or not retryable:
                raise
            _backoff(attempt, 2.0, time.sleep, provider, str(exc))

    raise ProviderError(provider, f"download failed: {last}", retryable=True)
