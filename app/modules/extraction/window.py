"""Processing window (requirement FR-5).

Default "always": documents are processed as soon as they arrive (FR-5.2).

Optionally, a window like "Mon-Fri 09:00-19:00 Asia/Kolkata" can be configured
(SLM_PROCESSING_WINDOW). Then:
  * the API answers 202 at once outside the window (FR-2.15), and says when
    processing starts (`processing_starts_at`);
  * in AWS, EventBridge Scheduler disables the worker's queue triggers at
    closing time and enables them at opening (infra/cloudformation/app.yaml);
    queued jobs wait safely in SQS.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.clock import iso

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WINDOW_RE = re.compile(r"^(\w{3})-(\w{3}) (\d{2}):(\d{2})-(\d{2}):(\d{2}) ([\w/]+)$")


def _parse(spec: str):
    """"Mon-Fri 09:00-19:00 Asia/Kolkata" -> (first day, last day, start, end, time zone)."""
    m = _WINDOW_RE.match(spec.strip())
    if not m:
        raise ValueError("invalid processing window")
    d1, d2, h1, m1, h2, m2, tz = m.groups()
    return _DAYS.index(d1), _DAYS.index(d2), (int(h1), int(m1)), (int(h2), int(m2)), ZoneInfo(tz)


def processing_open(at: datetime | None = None) -> bool:
    """True if documents are processed now: always with "always", else inside the window."""
    if settings.processing_window == "always":
        return True
    first, last, start, end, tz = _parse(settings.processing_window)
    local = (at or datetime.now(tz)).astimezone(tz)
    in_days = first <= local.weekday() <= last
    minutes = local.hour * 60 + local.minute
    return in_days and start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]


def next_opening_iso(at: datetime | None = None) -> str | None:
    """When the window next opens, as ISO time (for `processing_starts_at`); None with "always"."""
    if settings.processing_window == "always":
        return None
    first, last, start, _end, tz = _parse(settings.processing_window)
    local = (at or datetime.now(tz)).astimezone(tz)
    candidate = local.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    for _ in range(8):   # search the next 8 days for an opening
        if candidate > local and first <= candidate.weekday() <= last:
            return iso(candidate)
        candidate += timedelta(days=1)
    return None
