"""Public-exposure guardrails: rate limits, daily cap, bounded concurrency.

Every public upload triggers paid model calls, so:

- per-client sliding-window limits on the upload endpoints (429 when hit);
- one global jobs-per-day cap as the budget kill-switch (503 when hit);
- grading/ingestion pipelines run through a bounded semaphore so a burst
  of uploads queues instead of hammering the model API concurrently.

All in-memory by design: single-process deployment is the current target,
and the limits are per-instance safety valves, not billing-grade metering.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request

from . import config


class SlidingWindow:
    """Per-key sliding-window counter. `limit` is a callable so env-tuned
    config values are read at check time (and tests can monkeypatch them)."""

    def __init__(self, limit: Callable[[], int], per_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.per_seconds = per_seconds
        self.clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        cap = self.limit()
        if cap <= 0:  # 0 = disabled
            return True
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.per_seconds:
                hits.popleft()
            if len(hits) >= cap:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


submissions_window = SlidingWindow(
    lambda: config.SUBMISSIONS_PER_HOUR_PER_IP, 3600)
contributions_window = SlidingWindow(
    lambda: config.CONTRIBUTIONS_PER_HOUR_PER_IP, 3600)
daily_window = SlidingWindow(lambda: config.GLOBAL_JOBS_PER_DAY, 86400)

_job_gate = threading.BoundedSemaphore(max(1, config.MAX_CONCURRENT_JOBS))


def reset() -> None:
    """Clear all counters (tests)."""
    submissions_window.reset()
    contributions_window.reset()
    daily_window.reset()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check(window: SlidingWindow, request: Request, what: str) -> None:
    if not daily_window.allow("global"):
        raise HTTPException(
            503, "Daily processing capacity reached — please try again "
                 "tomorrow. Nothing was uploaded.")
    if not window.allow(client_ip(request)):
        raise HTTPException(
            429, f"Too many {what} from this address — please wait an hour "
                 "and try again. Nothing was uploaded.")


def limit_submissions(request: Request) -> None:
    """Dependency for grading-submission uploads."""
    _check(submissions_window, request, "submissions")


def limit_contributions(request: Request) -> None:
    """Dependency for question-paper contributions."""
    _check(contributions_window, request, "contributions")


def run_gated(fn, *args) -> None:
    """Run a pipeline under the concurrency gate: bursts queue, they don't
    fan out into parallel model-call storms."""
    with _job_gate:
        fn(*args)
