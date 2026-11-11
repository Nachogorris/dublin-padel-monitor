"""Multi-lead-time snapshot scheduling and bucket classification (Section 6).

Supports scheduled observation snapshots at:
- 7 days (168 hours) before target slot
- 3 days (72 hours) before target slot
- 24 hours before target slot
- 6 hours before target slot
- 1 hour before target slot
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Sequence

from .time_utils import DUBLIN_TZ, to_dublin

STANDARD_LEAD_BUCKETS: tuple[tuple[str, float, float, float], ...] = (
    # (bucket_name, nominal_hours, min_hours_inclusive, max_hours_exclusive)
    ("7d", 168.0, 120.0, 216.0),
    ("3d", 72.0, 48.0, 120.0),
    ("24h", 24.0, 12.0, 48.0),
    ("6h", 6.0, 3.0, 12.0),
    ("1h", 1.0, 0.0, 3.0),
)


@dataclass(frozen=True)
class ScheduledCollectionJob:
    club_id: str
    target_date: str
    snapshot_bucket: str
    nominal_lead_hours: float
    scheduled_run_at: str  # ISO-8601 in Europe/Dublin


def classify_lead_time_bucket(lead_time_hours: float) -> str:
    """Map observed lead time (in hours prior to slot start) into a canonical snapshot bucket."""
    for name, _, low, high in STANDARD_LEAD_BUCKETS:
        if low <= lead_time_hours < high:
            return name
    return "ad_hoc"


def build_snapshot_plan_for_date(
    club_id: str,
    target_day: date,
    anchor_hour_local: time = time(18, 0),
    lead_buckets: Sequence[tuple[str, float, float, float]] = STANDARD_LEAD_BUCKETS,
) -> list[ScheduledCollectionJob]:
    """Generate the 5 recommended collection run timestamps in Europe/Dublin for a target date.

    Anchors lead times against peak evening start (default 18:00 Europe/Dublin) so a single
    polite daily/sub-daily collection per date captures the full day's calendar at ~7d, 3d,
    24h, 6h, and 1h prior to prime play.
    """
    anchor_dt = datetime.combine(target_day, anchor_hour_local, tzinfo=DUBLIN_TZ)
    jobs: list[ScheduledCollectionJob] = []
    for bucket_name, nominal_hours, _, _ in lead_buckets:
        run_dt = to_dublin(anchor_dt - timedelta(hours=nominal_hours))
        jobs.append(
            ScheduledCollectionJob(
                club_id=club_id,
                target_date=target_day.isoformat(),
                snapshot_bucket=bucket_name,
                nominal_lead_hours=nominal_hours,
                scheduled_run_at=run_dt.isoformat(timespec="seconds"),
            )
        )
    jobs.sort(key=lambda j: j.scheduled_run_at)
    return jobs

