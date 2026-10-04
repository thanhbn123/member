"""Tiny in-process sliding-window rate limiter.

Single process only: for multi-worker deployments point ``RATE_LIMIT_BACKEND``
at a shared store (Redis) - the interface below stays the same.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_last_cleanup = 0.0
_CLEANUP_INTERVAL_SECONDS = 300.0


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Register a hit. Returns ``(allowed, retry_after_seconds)``."""
    if limit <= 0:
        return True, 0
    now = time.monotonic()
    with _lock:
        _maybe_cleanup(now, window_seconds)
        bucket = _hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
            return False, retry_after
        bucket.append(now)
        return True, 0


def _maybe_cleanup(now: float, window_seconds: int) -> None:
    global _last_cleanup
    if now - _last_cleanup < _CLEANUP_INTERVAL_SECONDS:
        return
    _last_cleanup = now
    max_window = max(window_seconds, 3600)
    for key in list(_hits.keys()):
        bucket = _hits[key]
        cutoff = now - max_window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            _hits.pop(key, None)


def reset() -> None:
    """Clear all counters (used by tests)."""
    with _lock:
        _hits.clear()
