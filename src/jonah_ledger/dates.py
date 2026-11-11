"""US/Eastern date handling. The entry rule hinges on the video's *Eastern calendar date*, not UTC."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import ET, UTC


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def et_date(published_utc: str) -> date:
    """Calendar date in America/New_York of a UTC timestamp (handles DST via zoneinfo)."""
    return parse_utc(published_utc).astimezone(ET).date()


def approx_next_session_open_utc(published_utc: str) -> datetime:
    """09:30 ET on the first weekday strictly after the ET calendar date. Ignores market holidays
    (a holiday only makes the real open later, so this is a conservative lower bound)."""
    d = et_date(published_utc) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, time(9, 30), tzinfo=ET).astimezone(UTC)
