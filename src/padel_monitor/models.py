"""Domain models, canonical enumerations, and validation rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .time_utils import DUBLIN_TZ, parse_iso_aware, to_dublin

VALID_AVAILABILITY_STATUSES = frozenset({"available", "unavailable", "blocked", "unknown"})

VALID_STATUS_DETAILS = frozenset(
    {
        "confirmed_booking",
        "observed_available",
        "inferred_unavailable",
        "observed_unavailable",
        "blocked_maintenance",
        "blocked_coaching_or_event",
        "missing_unknown",
        "unspecified",
    }
)

VALID_SNAPSHOT_BUCKETS = frozenset({"7d", "3d", "24h", "6h", "1h", "ad_hoc"})

VALID_DATA_ORIGINS = frozenset({"live_public", "live_authorized_api", "synthetic_validation"})


@dataclass(frozen=True)
class CourtRecord:
    club_id: str
    court_id: str
    court_name: str
    sport: str = "PADEL"
    court_environment: str = "unknown"  # indoor | covered_outdoor | outdoor | unknown
    court_size: str = "double"          # double | single | unknown
    wall_type: Optional[str] = None
    is_active: bool = True
    source_origin: str = "live_public"
    verified_at: str = ""


@dataclass(frozen=True)
class OpeningHoursRecord:
    club_id: str
    day_of_week: str  # MONDAY..SUNDAY, HOLIDAYS
    opening_time: str  # HH:MM local Europe/Dublin
    closing_time: str  # HH:MM local Europe/Dublin
    closes_next_day: bool = False
    source_origin: str = "live_public"
    updated_at: str = ""


@dataclass(frozen=True)
class PublicClubEvent:
    club_id: str
    event_id: str
    event_type: str  # competition | academy_course | public_class | other
    title: str
    start_time: Optional[str]
    price_eur: Optional[float]
    source_url: str
    observed_at: str
    run_id: Optional[str] = None


@dataclass(frozen=True)
class AvailabilityObservation:
    """Canonical normalized slot availability observation (Section 6 schema)."""

    club_id: str
    court_id: str
    booking_platform: str
    target_date: str
    start_time: str
    end_time: str
    duration_minutes: int
    price_eur: Optional[float]
    availability_status: str
    observed_at: str
    source_url: str
    status_detail: str = "unspecified"
    lead_time_hours: Optional[float] = None
    snapshot_bucket: str = "ad_hoc"
    data_origin: str = "live_public"
    run_id: Optional[str] = None

    @classmethod
    def from_raw(
        cls,
        *,
        club_id: str,
        court_id: str,
        booking_platform: str,
        start_time: str | datetime,
        end_time: str | datetime,
        availability_status: str,
        observed_at: str | datetime,
        source_url: str,
        price_eur: Optional[float | str] = None,
        status_detail: str = "unspecified",
        snapshot_bucket: Optional[str] = None,
        data_origin: str = "live_public",
        run_id: Optional[str] = None,
    ) -> "AvailabilityObservation":
        """Validate, normalize to Europe/Dublin ISO-8601, and compute duration & lead time."""
        from .scheduler import classify_lead_time_bucket

        if not club_id or not str(club_id).strip():
            raise ValueError("club_id must be non-empty")
        if not court_id or not str(court_id).strip():
            raise ValueError("court_id must be non-empty")
        if not booking_platform or not str(booking_platform).strip():
            raise ValueError("booking_platform must be non-empty")
        if not source_url or not str(source_url).strip():
            raise ValueError("source_url must be non-empty")

        status_norm = str(availability_status).strip().lower()
        if status_norm not in VALID_AVAILABILITY_STATUSES:
            raise ValueError(
                f"Invalid availability_status '{availability_status}'. "
                f"Expected one of {sorted(VALID_AVAILABILITY_STATUSES)}"
            )

        if data_origin not in VALID_DATA_ORIGINS:
            raise ValueError(
                f"Invalid data_origin '{data_origin}'. Expected one of {sorted(VALID_DATA_ORIGINS)}"
            )

        start_dt = to_dublin(parse_iso_aware(start_time, field_name="start_time"))
        end_dt = to_dublin(parse_iso_aware(end_time, field_name="end_time"))
        obs_dt = to_dublin(parse_iso_aware(observed_at, field_name="observed_at"))

        if end_dt <= start_dt:
            raise ValueError(f"end_time ({end_dt.isoformat()}) must be strictly after start_time ({start_dt.isoformat()})")

        duration_sec = (end_dt - start_dt).total_seconds()
        duration_minutes = int(round(duration_sec / 60.0))
        if duration_minutes <= 0:
            raise ValueError(f"Computed non-positive duration_minutes={duration_minutes}")

        parsed_price: Optional[float] = None
        if price_eur is not None and str(price_eur).strip() != "":
            cleaned = str(price_eur).strip().replace("€", "").replace("EUR", "").strip()
            parsed_price = round(float(cleaned), 2)
            if parsed_price < 0:
                raise ValueError(f"price_eur cannot be negative: {parsed_price}")

        if status_detail == "unspecified":
            if status_norm == "available":
                status_detail = "observed_available"
            elif status_norm == "unavailable":
                status_detail = "observed_unavailable"
            elif status_norm == "blocked":
                status_detail = "blocked_maintenance"
            elif status_norm == "unknown":
                status_detail = "missing_unknown"

        if status_detail not in VALID_STATUS_DETAILS:
            raise ValueError(f"Invalid status_detail '{status_detail}'")

        lead_hours = round((start_dt - obs_dt).total_seconds() / 3600.0, 3)
        bucket = snapshot_bucket or classify_lead_time_bucket(lead_hours)
        if bucket not in VALID_SNAPSHOT_BUCKETS:
            raise ValueError(f"Invalid snapshot_bucket '{bucket}'")

        target_date = start_dt.astimezone(DUBLIN_TZ).date().isoformat()

        return cls(
            club_id=str(club_id).strip(),
            court_id=str(court_id).strip(),
            booking_platform=str(booking_platform).strip().lower(),
            target_date=target_date,
            start_time=start_dt.isoformat(timespec="seconds"),
            end_time=end_dt.isoformat(timespec="seconds"),
            duration_minutes=duration_minutes,
            price_eur=parsed_price,
            availability_status=status_norm,
            status_detail=status_detail,
            observed_at=obs_dt.isoformat(timespec="seconds"),
            lead_time_hours=lead_hours,
            snapshot_bucket=bucket,
            source_url=str(source_url).strip(),
            data_origin=data_origin,
            run_id=run_id,
        )


@dataclass
class CollectionRunResult:
    run_id: str
    started_at: str
    completed_at: str
    adapter_name: str
    platform_id: str
    club_id: Optional[str]
    target_url: str
    http_status: Optional[int]
    robots_allowed: Optional[bool]
    auth_required: Optional[bool]
    run_status: str
    data_origin: str
    records_written: int = 0
    error_message: Optional[str] = None
    raw_artifact_path: Optional[str] = None
    observations: list[AvailabilityObservation] = field(default_factory=list)

