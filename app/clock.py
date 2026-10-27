"""Single time source so tests can move time forward (OTP expiry, session TTL)."""

import time
from datetime import datetime, timezone


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    """UTC ISO-8601 timestamp with millisecond precision."""
    ts = now() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def cirl_datetime(ts: float) -> str:
    """Date format used in mock CIRL responses."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
