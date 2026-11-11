"""Timezone-aware Europe/Dublin date/time utilities and atomic interval slicing.

Handles Irish Standard Time (IST, UTC+01:00 in summer) and Greenwich Mean Time
(GMT, UTC+00:00 in winter), including spring-forward and autumn-fallback DST
transitions and post-midnight club closing hours (e.g. 00:30 or 01:00).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DUBLIN_TZ = ZoneInfo("Europe/Dublin")
UTC_TZ = timezone.utc

WEEKDAY_NAMES = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)


def now_dublin_iso() -> str:
    """Return current wall-clock time in Europe/Dublin as ISO-8601 with offset."""
    return datetime.now(DUBLIN_TZ).isoformat(timespec="seconds")


def parse_iso_aware(value: str | datetime, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp or datetime, requiring explicit timezone awareness."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} datetime must be timezone-aware: {value!r}")
        return value
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{field_name} cannot be empty")
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone offset: {value!r}")
    return dt


def to_dublin(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to Europe/Dublin."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"Cannot convert naive datetime {dt!r} to Europe/Dublin")
    return dt.astimezone(DUBLIN_TZ)


def parse_hhmm(hhmm: str) -> time:
    """Parse 'HH:MM' or 'HH:MM:SS' into a datetime.time object."""
    parts = [int(p) for p in hhmm.strip().split(":")]
    if len(parts) == 2:
        if parts[0] == 24 and parts[1] == 0:
            return time(0, 0)
        return time(parts[0], parts[1])
    if len(parts) == 3:
        return time(parts[0], parts[1], parts[2])
    raise ValueError(f"Invalid HH:MM time string: {hhmm!r}")


def resolve_operating_window(
    target_day: date,
    opening_hhmm: str,
    closing_hhmm: str,
) -> tuple[datetime, datetime]:
    """Return DST-accurate (open_dt, close_dt) in Europe/Dublin for a club's operating day.

    If closing_hhmm <= opening_hhmm (for example opening '06:00' and closing '00:30' or
    '00:00'), closing_dt falls on target_day + 1 day in Europe/Dublin. Arithmetic is
    performed via UTC after resolving wall-clock endpoints so DST transitions produce
    exact physical durations.
    """
    open_t = parse_hhmm(opening_hhmm)
    close_t = parse_hhmm(closing_hhmm)
    open_dt = datetime.combine(target_day, open_t, tzinfo=DUBLIN_TZ)
    close_day = target_day + timedelta(days=1) if (close_t <= open_t or closing_hhmm.strip() == "24:00") else target_day
    close_dt = datetime.combine(close_day, close_t, tzinfo=DUBLIN_TZ)
    if close_dt.astimezone(UTC_TZ) <= open_dt.astimezone(UTC_TZ):
        close_dt = datetime.combine(target_day + timedelta(days=1), close_t, tzinfo=DUBLIN_TZ)
    return open_dt, close_dt


def generate_atomic_slices(
    window_start: datetime,
    window_end: datetime,
    slice_minutes: int = 15,
) -> list[tuple[datetime, datetime]]:
    """Partition [window_start, window_end) into non-overlapping slices in UTC, returned in Europe/Dublin.

    Stepping in UTC guarantees exact physical durations (no duplicated or skipped slices
    when Europe/Dublin springs forward or falls back across a DST boundary).
    """
    if slice_minutes <= 0 or 60 % slice_minutes != 0:
        raise ValueError("slice_minutes must be a positive divisor of 60 (e.g. 15 or 30)")
    cur_utc = window_start.astimezone(UTC_TZ)
    end_utc = window_end.astimezone(UTC_TZ)
    step = timedelta(minutes=slice_minutes)
    slices: list[tuple[datetime, datetime]] = []
    while cur_utc < end_utc:
        nxt_utc = min(cur_utc + step, end_utc)
        slices.append((cur_utc.astimezone(DUBLIN_TZ), nxt_utc.astimezone(DUBLIN_TZ)))
        cur_utc = nxt_utc
    return slices


def is_peak_dublin_hour(slot_start_dublin: datetime) -> bool:
    """Standard Dublin market peak classification for cross-club comparison:
    - Monday–Friday: 17:00–22:30 local start time = Peak; otherwise Off-Peak.
    - Saturday–Sunday: 09:00–20:00 local start time = Peak; early/late = Off-Peak.
    """
    local_dt = to_dublin(slot_start_dublin)
    wd = local_dt.weekday()  # 0=Mon .. 6=Sun
    minutes_of_day = local_dt.hour * 60 + local_dt.minute
    if wd < 5:
        return 17 * 60 <= minutes_of_day < 22 * 60 + 30
    return 9 * 60 <= minutes_of_day < 20 * 60

