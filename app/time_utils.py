from __future__ import annotations

from datetime import datetime, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def format_ist(dt: datetime) -> str:
    ist_dt = to_ist(dt)
    return ist_dt.strftime("%d %b %Y, %I:%M %p IST").replace(" 0", " ")
