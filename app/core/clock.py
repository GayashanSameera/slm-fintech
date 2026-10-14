"""Time helpers.

All timestamps are stored and returned in UTC, ISO-8601 format with a "Z"
suffix, e.g. "2026-10-13T10:15:02Z". Keeping one format everywhere avoids
time-zone bugs. Epoch seconds are used only for DynamoDB TTL attributes,
because that is the format DynamoDB TTL requires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def now() -> datetime:
    """The current time in UTC (timezone-aware)."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """A datetime as "2026-10-14T10:15:09Z" (UTC, whole seconds): the API's time format."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """The current UTC time in the API's time format."""
    return iso(now())


def iso_in(seconds: int) -> str:
    """ISO timestamp `seconds` from now (e.g. when an upload URL expires)."""
    return iso(now() + timedelta(seconds=seconds))


def epoch_in(seconds: int) -> int:
    """Epoch seconds `seconds` from now. Used for DynamoDB TTL (`expires_at`)."""
    return int((now() + timedelta(seconds=seconds)).timestamp())


def epoch_now() -> int:
    """The current time in whole epoch seconds (DynamoDB TTL and deadlines use these)."""
    return int(now().timestamp())
