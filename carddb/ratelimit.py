"""Polite HTTP pacing for the openCaselist sync. Spec §0.2.

Two pieces, both with injectable clock/sleep so tests can prove the pacing
math without ever really sleeping:

- ``RateLimiter``: a min-interval limiter enforcing the spec's hard cap
  (max 1 request/second by default, from config.toml [sync] rate_limit_rps).
- ``request_with_backoff``: exponential backoff with jitter on 429/5xx and
  transport errors, honoring ``Retry-After``, raising ``SyncError`` once
  ``max_retries`` is exhausted.

openCaselist is a community-run nonprofit; its rate limiters exist on
purpose. Never widen these defaults to "go faster".
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import httpx

__all__ = ["RateLimiter", "SyncError", "parse_retry_after",
           "request_with_backoff", "BACKOFF_BASE", "BACKOFF_CAP"]

BACKOFF_BASE = 1.0   # seconds; first retry delay before jitter
BACKOFF_CAP = 60.0   # seconds; exponential growth stops here


class SyncError(Exception):
    """The sync cannot proceed (retries exhausted, bad config, missing
    credentials). The message says what happened and what to fix."""


class RateLimiter:
    """Cap sustained request rate at ``rps`` requests/second.

    ``wait()`` blocks until at least ``1/rps`` seconds have passed since the
    previous ``wait()`` returned. ``rps <= 0`` disables limiting. ``sleep``
    and ``clock`` are injectable (tests pass a fake pair; production uses
    ``time.sleep`` / ``time.monotonic``).
    """

    def __init__(self, rps: float,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.rps = float(rps)
        self.min_interval = (1.0 / self.rps) if self.rps > 0 else 0.0
        self.sleep = sleep
        self.clock = clock
        self._last: Optional[float] = None

    def wait(self) -> None:
        """Block until the next request is allowed, then claim its slot."""
        now = self.clock()
        if self._last is not None and self.min_interval > 0:
            due = self._last + self.min_interval
            if now < due:
                self.sleep(due - now)
                # trust the schedule even if the injected sleep/clock pair
                # does not advance (a non-advancing fake must not let the
                # limiter burst)
                now = max(self.clock(), due)
        self._last = now


def parse_retry_after(value: Optional[str],
                      now: Optional[datetime] = None) -> Optional[float]:
    """Parse a Retry-After header: delta-seconds or an HTTP-date.

    Returns seconds to wait (>= 0), or None when absent/unparseable.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now if now is not None else datetime.now(timezone.utc)
    return max(0.0, (dt - ref).total_seconds())


def _retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def request_with_backoff(client: httpx.Client, method: str, url: str, *,
                         limiter: RateLimiter, max_retries: int,
                         backoff_base: float = BACKOFF_BASE,
                         backoff_cap: float = BACKOFF_CAP,
                         rng: Callable[[], float] = random.random,
                         **kw) -> httpx.Response:
    """One rate-limited request with exponential backoff on 429/5xx.

    - Every attempt (including the first) goes through ``limiter.wait()``.
    - 429 and 5xx responses and transport errors are retried up to
      ``max_retries`` times; any other response returns immediately
      (404/401 are the caller's problem, not a reason to hammer).
    - A parseable ``Retry-After`` header is honored verbatim; otherwise the
      delay is ``min(cap, base * 2**(attempt-1))`` scaled by jitter into
      [0.5x, 1.0x] (``rng`` is injectable for deterministic tests).
    - Backoff sleeps go through ``limiter.sleep`` so tests never really wait.
    - Exhaustion raises ``SyncError`` with the URL, the attempt count, and
      the last status.
    """
    attempt = 0
    while True:
        limiter.wait()
        response: Optional[httpx.Response] = None
        error: Optional[Exception] = None
        try:
            response = client.request(method, url, **kw)
        except httpx.TransportError as e:   # DNS, timeouts, resets...
            error = e
        if response is not None and not _retryable(response.status_code):
            return response

        attempt += 1
        if attempt > max_retries:
            if response is not None:
                raise SyncError(
                    "%s %s still failing with HTTP %d after %d retries; "
                    "giving up (spec §0.2: back off, never hammer). Wait and "
                    "retry later, or lower [sync] rate_limit_rps." % (
                        method, url, response.status_code, max_retries))
            raise SyncError(
                "%s %s failed after %d retries with a transport error: %r. "
                "Check connectivity and retry." % (
                    method, url, max_retries, error)) from error

        retry_after = None
        if response is not None:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(backoff_cap, backoff_base * (2.0 ** (attempt - 1)))
            delay *= 0.5 + 0.5 * rng()
        if delay > 0:
            limiter.sleep(delay)
