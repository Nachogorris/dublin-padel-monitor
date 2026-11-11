"""Occupancy & Gross Booking Value estimation via atomic 15-minute court-time slices (Section 7).

Why atomic slices?
Platforms such as Playtomic simultaneously advertise overlapping booking durations on the
same court (e.g. 18:00–19:00 [60 min], 18:00–19:30 [90 min], and 18:30–19:30 [60 min]).
Counting raw slots or summing raw slot durations double-counts physical court capacity.

Methodology:
1. Decompose every court's operating window (or the union of observed intervals when opening
   hours are absent) into non-overlapping 15-minute physical UTC slices mapped to Europe/Dublin.
2. For each snapshot timestamp T_obs, resolve the state of each 15-minute slice [s, e):
   - 'confirmed_booking' if any confirmed booking covers [s, e)
   - 'blocked' if any maintenance / coaching / tournament block covers [s, e)
   - 'available' if AT LEAST ONE available slot option covers [s, e) (the court is free!)
   - 'unavailable' if explicit unavailable slots cover [s, e) and no available slot covers it
   - 'unknown' otherwise (HTTP failure, missing coverage, or outside observed range)
3. Across snapshots ordered by observed_at (prior to slice start):
   - Track whether a slice transitioned from 'available' -> 'unavailable' ('inferred_unavailable_transition')
     versus being 'unavailable' from the very first snapshot ('static_unavailable').
   - Carry forward the last known hourly price from earlier 'available' snapshots when a slot
     later becomes 'unavailable' with NULL price, enabling accurate Gross Booking Value (GBV)
     proxy estimation without inventing prices.
4. Compute Estimated Occupancy = unavailable bookable court-hours / total offered court-hours * 100
   alongside Confidence Levels ('high', 'medium', 'low' + 0–100 score).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from .models import AvailabilityObservation, CourtRecord, OpeningHoursRecord
from .time_utils import (
    DUBLIN_TZ,
    UTC_TZ,
    WEEKDAY_NAMES,
    generate_atomic_slices,
    is_peak_dublin_hour,
    parse_iso_aware,
    resolve_operating_window,
    to_dublin,
)

SLICE_MINUTES = 15
SLICE_HOURS = SLICE_MINUTES / 60.0


@dataclass(frozen=True)
class AtomicSliceResolution:
    club_id: str
    court_id: str
    court_environment: str
    court_size: str
    target_date: str
    day_of_week: str
    is_weekend: bool
    is_peak: bool
    hour_of_day: int
    slice_start: str
    slice_end: str
    final_state: str  # available | confirmed_booking | inferred_unavailable_transition | static_unavailable | blocked | unknown
    had_avail_to_unavail_transition: bool
    first_unavail_bucket: Optional[str]
    snapshots_count: int
    distinct_buckets: int
    hourly_rate_eur: Optional[float]
    est_gbv_eur: float


@dataclass(frozen=True)
class OccupancySummaryRow:
    dimension_type: str
    dimension_key: str
    total_capacity_hours: float
    offered_bookable_hours: float
    available_hours: float
    confirmed_booked_hours: float
    inferred_transition_unavail_hours: float
    static_unavail_hours: float
    total_unavailable_bookable_hours: float
    blocked_hours: float
    unknown_hours: float
    estimated_occupancy_pct: Optional[float]
    transition_backed_occupancy_pct: Optional[float]
    est_gross_booking_value_eur: float
    est_gbv_member_adjusted_low_eur: float
    avg_hourly_price_eur: Optional[float]
    confidence_level: str
    confidence_score: int
    confidence_notes: str


def _canonical_hourly_rate(obs_list: list[AvailabilityObservation]) -> Optional[float]:
    """Derive per-court-hour EUR rate, preferring 60-minute slots over 90/120-minute slots."""
    priced = [o for o in obs_list if o.price_eur is not None and o.duration_minutes > 0]
    if not priced:
        return None
    sixty = [o for o in priced if o.duration_minutes == 60]
    chosen = sixty if sixty else sorted(priced, key=lambda o: abs(o.duration_minutes - 60))
    rates = [o.price_eur / (o.duration_minutes / 60.0) for o in chosen if o.price_eur is not None]
    return round(sum(rates) / len(rates), 2) if rates else None


def _resolve_snapshot_slice_state(
    covering: list[AvailabilityObservation],
) -> tuple[str, Optional[float]]:
    """Resolve physical state & hourly rate of one 15-min slice within a single snapshot."""
    if not covering:
        return "unknown", None

    hourly_rate = _canonical_hourly_rate(covering)

    if any(o.status_detail == "confirmed_booking" for o in covering):
        return "confirmed_booking", hourly_rate

    if any(o.availability_status == "blocked" for o in covering):
        return "blocked", hourly_rate

    # Key overlap rule: if ANY offered duration covering this 15-min slice is bookable ('available'),
    # then this physical 15-minute court slice is genuinely free!
    avail = [o for o in covering if o.availability_status == "available"]
    if avail:
        return "available", _canonical_hourly_rate(avail) or hourly_rate

    if any(o.availability_status == "unavailable" for o in covering):
        return "unavailable", hourly_rate

    return "unknown", hourly_rate


def resolve_atomic_slices(
    observations: Iterable[AvailabilityObservation],
    courts: Optional[dict[tuple[str, str], CourtRecord]] = None,
    opening_hours: Optional[dict[tuple[str, str], OpeningHoursRecord]] = None,
    slice_minutes: int = SLICE_MINUTES,
) -> list[AtomicSliceResolution]:
    """Decompose observations into deduplicated atomic physical slices across snapshots."""
    obs_list = list(observations)
    if not obs_list:
        return []

    courts = courts or {}
    opening_hours = opening_hours or {}

    # Pre-parse UTC bounds for fast interval containment checks
    parsed_obs: dict[tuple[str, str, str], list[tuple[datetime, datetime, datetime, AvailabilityObservation]]] = defaultdict(list)
    for o in obs_list:
        s_utc = parse_iso_aware(o.start_time).astimezone(UTC_TZ)
        e_utc = parse_iso_aware(o.end_time).astimezone(UTC_TZ)
        obs_utc = parse_iso_aware(o.observed_at).astimezone(UTC_TZ)
        parsed_obs[(o.club_id, o.court_id, o.target_date)].append((obs_utc, s_utc, e_utc, o))

    resolutions: list[AtomicSliceResolution] = []

    for (club_id, court_id, target_date_str), items in sorted(parsed_obs.items()):
        t_day = date.fromisoformat(target_date_str)
        dow = WEEKDAY_NAMES[t_day.weekday()]
        court_meta = courts.get((club_id, court_id))
        # Skip non-padel courts if present
        if court_meta and court_meta.sport.upper() != "PADEL":
            continue

        oh = opening_hours.get((club_id, dow))
        if oh is not None:
            win_start, win_end = resolve_operating_window(t_day, oh.opening_time, oh.closing_time)
        else:
            min_s = min(s for _, s, _, _ in items).astimezone(DUBLIN_TZ)
            max_e = max(e for _, _, e, _ in items).astimezone(DUBLIN_TZ)
            win_start, win_end = min_s, max_e

        slices = generate_atomic_slices(win_start, win_end, slice_minutes=slice_minutes)
        # Group observations by snapshot timestamp
        by_snap: dict[datetime, list[tuple[datetime, datetime, AvailabilityObservation]]] = defaultdict(list)
        for obs_utc, s_utc, e_utc, o in items:
            by_snap[obs_utc].append((s_utc, e_utc, o))
        ordered_snaps = sorted(by_snap.keys())

        court_env = court_meta.court_environment if court_meta else "unknown"
        court_sz = court_meta.court_size if court_meta else "unknown"

        for sl_start_dub, sl_end_dub in slices:
            sl_s_utc = sl_start_dub.astimezone(UTC_TZ)
            sl_e_utc = sl_end_dub.astimezone(UTC_TZ)

            snap_states: list[tuple[datetime, str, Optional[float], str]] = []
            last_known_rate: Optional[float] = None

            for snap_utc in ordered_snaps:
                # Only consider snapshots taken on or before slice end
                if snap_utc > sl_e_utc:
                    continue
                covering = [
                    o
                    for s_u, e_u, o in by_snap[snap_utc]
                    if s_u <= sl_s_utc and e_u >= sl_e_utc
                ]
                if not covering:
                    continue
                st, rate = _resolve_snapshot_slice_state(covering)
                if rate is not None:
                    last_known_rate = rate
                bucket = covering[0].snapshot_bucket
                snap_states.append((snap_utc, st, rate, bucket))

            distinct_buckets = len({b for _, _, _, b in snap_states if b != "ad_hoc"})
            had_transition = False
            first_unavail_bucket: Optional[str] = None
            seen_available = False

            for _, st, _, bkt in snap_states:
                if st == "available":
                    seen_available = True
                elif st in ("unavailable", "confirmed_booking") and seen_available:
                    had_transition = True
                    if first_unavail_bucket is None:
                        first_unavail_bucket = bkt

            had_confirmed = any(st == "confirmed_booking" for _, st, _, _ in snap_states)
            # Determine final state using latest non-unknown snapshot where possible
            non_unknown = [x for x in snap_states if x[1] != "unknown"]
            if not snap_states or not non_unknown:
                final_state = "unknown"
            else:
                latest_st = non_unknown[-1][1]
                if latest_st == "confirmed_booking" or (latest_st == "unavailable" and had_confirmed):
                    final_state = "confirmed_booking"
                elif latest_st == "blocked":
                    final_state = "blocked"
                elif latest_st == "available":
                    final_state = "available"
                elif latest_st == "unavailable":
                    final_state = (
                        "inferred_unavailable_transition"
                        if had_transition
                        else "static_unavailable"
                    )
                    if first_unavail_bucket is None:
                        first_unavail_bucket = next(
                            (b for _, st, _, b in non_unknown if st in ("unavailable", "confirmed_booking")),
                            non_unknown[-1][3],
                        )
                else:
                    final_state = "unknown"

            slice_hrs = slice_minutes / 60.0
            is_unavail_bookable = final_state in (
                "confirmed_booking",
                "inferred_unavailable_transition",
                "static_unavailable",
            )
            est_gbv = round((last_known_rate or 0.0) * slice_hrs, 4) if is_unavail_bookable else 0.0

            resolutions.append(
                AtomicSliceResolution(
                    club_id=club_id,
                    court_id=court_id,
                    court_environment=court_env,
                    court_size=court_sz,
                    target_date=target_date_str,
                    day_of_week=dow,
                    is_weekend=t_day.weekday() >= 5,
                    is_peak=is_peak_dublin_hour(sl_start_dub),
                    hour_of_day=sl_start_dub.hour,
                    slice_start=sl_start_dub.isoformat(timespec="seconds"),
                    slice_end=sl_end_dub.isoformat(timespec="seconds"),
                    final_state=final_state,
                    had_avail_to_unavail_transition=had_transition,
                    first_unavail_bucket=first_unavail_bucket,
                    snapshots_count=len(snap_states),
                    distinct_buckets=distinct_buckets,
                    hourly_rate_eur=last_known_rate,
                    est_gbv_eur=est_gbv,
                )
            )
    return resolutions


def _score_confidence(slices: list[AtomicSliceResolution]) -> tuple[str, int, str]:
    if not slices:
        return "low", 0, "No slices observed"
    n = len(slices)
    unknown_share = sum(1 for s in slices if s.final_state == "unknown") / n
    avg_buckets = sum(s.distinct_buckets for s in slices) / n
    unavail = [
        s
        for s in slices
        if s.final_state in ("confirmed_booking", "inferred_unavailable_transition", "static_unavailable")
    ]
    transition_or_confirmed_share = (
        sum(
            1
            for s in unavail
            if s.final_state in ("confirmed_booking", "inferred_unavailable_transition")
        )
        / len(unavail)
        if unavail
        else 1.0
    )
    known_env_share = sum(1 for s in slices if s.court_environment != "unknown") / n

    score = int(
        round(
            35 * min(avg_buckets / 4.0, 1.0)
            + 30 * transition_or_confirmed_share
            + 25 * (1.0 - unknown_share)
            + 10 * known_env_share
        )
    )
    score = max(0, min(100, score))
    if score >= 75 and unknown_share <= 0.10:
        level = "high"
    elif score >= 45 and unknown_share <= 0.30:
        level = "medium"
    else:
        level = "low"

    notes = (
        f"avg_lead_buckets={avg_buckets:.1f}, "
        f"transition_or_confirmed_share={transition_or_confirmed_share*100:.0f}%, "
        f"unknown_share={unknown_share*100:.1f}%"
    )
    return level, score, notes


def summarize_occupancy(
    slices: list[AtomicSliceResolution],
    dimension_type: str,
    key_fn,
    slice_minutes: int = SLICE_MINUTES,
    member_discount_factor: float = 0.82,
) -> list[OccupancySummaryRow]:
    """Aggregate atomic slices along any analytical dimension (club, court, hour, day, peak, etc.)."""
    grouped: dict[str, list[AtomicSliceResolution]] = defaultdict(list)
    for s in slices:
        grouped[str(key_fn(s))].append(s)

    sh = slice_minutes / 60.0
    rows: list[OccupancySummaryRow] = []
    for key in sorted(grouped.keys()):
        grp = grouped[key]
        avail_h = sum(1 for s in grp if s.final_state == "available") * sh
        conf_h = sum(1 for s in grp if s.final_state == "confirmed_booking") * sh
        trans_h = sum(1 for s in grp if s.final_state == "inferred_unavailable_transition") * sh
        stat_h = sum(1 for s in grp if s.final_state == "static_unavailable") * sh
        blk_h = sum(1 for s in grp if s.final_state == "blocked") * sh
        unk_h = sum(1 for s in grp if s.final_state == "unknown") * sh

        unavail_bookable_h = conf_h + trans_h + stat_h
        offered_h = avail_h + unavail_bookable_h
        total_h = offered_h + blk_h + unk_h

        occ_pct = round(100.0 * unavail_bookable_h / offered_h, 2) if offered_h > 0 else None
        trans_occ_pct = round(100.0 * (conf_h + trans_h) / offered_h, 2) if offered_h > 0 else None

        gbv = round(sum(s.est_gbv_eur for s in grp), 2)
        gbv_low = round(gbv * member_discount_factor, 2)
        priced_rates = [s.hourly_rate_eur for s in grp if s.hourly_rate_eur is not None]
        avg_price = round(sum(priced_rates) / len(priced_rates), 2) if priced_rates else None

        conf_lvl, conf_score, conf_notes = _score_confidence(grp)
        rows.append(
            OccupancySummaryRow(
                dimension_type=dimension_type,
                dimension_key=key,
                total_capacity_hours=round(total_h, 2),
                offered_bookable_hours=round(offered_h, 2),
                available_hours=round(avail_h, 2),
                confirmed_booked_hours=round(conf_h, 2),
                inferred_transition_unavail_hours=round(trans_h, 2),
                static_unavail_hours=round(stat_h, 2),
                total_unavailable_bookable_hours=round(unavail_bookable_h, 2),
                blocked_hours=round(blk_h, 2),
                unknown_hours=round(unk_h, 2),
                estimated_occupancy_pct=occ_pct,
                transition_backed_occupancy_pct=trans_occ_pct,
                est_gross_booking_value_eur=gbv,
                est_gbv_member_adjusted_low_eur=gbv_low,
                avg_hourly_price_eur=avg_price,
                confidence_level=conf_lvl,
                confidence_score=conf_score,
                confidence_notes=conf_notes,
            )
        )
    return rows


def build_all_occupancy_breakdowns(slices: list[AtomicSliceResolution]) -> dict[str, list[OccupancySummaryRow]]:
    """Compute all 8 required occupancy breakdowns from Section 7."""
    return {
        "by_club": summarize_occupancy(slices, "club", lambda s: s.club_id),
        "by_court": summarize_occupancy(slices, "court", lambda s: f"{s.club_id}::{s.court_id}"),
        "by_hour": summarize_occupancy(slices, "hour_of_day", lambda s: f"{s.hour_of_day:02d}:00"),
        "by_day_of_week": summarize_occupancy(slices, "day_of_week", lambda s: s.day_of_week),
        "by_weekday_weekend": summarize_occupancy(
            slices, "weekday_vs_weekend", lambda s: "Weekend (Sat-Sun)" if s.is_weekend else "Weekday (Mon-Fri)"
        ),
        "by_peak_offpeak": summarize_occupancy(
            slices, "peak_vs_offpeak", lambda s: "Peak" if s.is_peak else "Off-Peak"
        ),
        "by_indoor_outdoor": summarize_occupancy(slices, "indoor_vs_outdoor", lambda s: s.court_environment),
        "by_lead_time_capture": summarize_occupancy(
            slices,
            "lead_time_capture_bucket",
            lambda s: s.first_unavail_bucket or ("still_available" if s.final_state == "available" else s.final_state),
        ),
    }
