"""Explicitly labeled synthetic multi-lead-time snapshot generator for pipeline validation.

IMPORTANT TRANSPARENCY NOTICE (Prompt Section 8):
Because unauthenticated slot-level scraping of Playtomic (`/api`, `/*?*date=`), Matchpoint
(`/Login.aspx` + `Disallow: /Booking/`), and Playbypoint (Cloudflare 403 + TDM opt-out)
is prohibited by platform robots.txt / Terms / access controls, this module generates an
EXPLICITLY LABELED (`data_origin='synthetic_validation'`, `source_url='synthetic://...'`)
multi-snapshot dataset anchored on the REAL live-verified courts, court environments
(indoor / covered_outdoor / outdoor), singles/doubles sizes, pricing tiers, and weekly
Europe/Dublin opening hours extracted from Playtomic's permitted SSR club pages.

Every record produced by this module carries:
- `data_origin = "synthetic_validation"`
- `source_url = "synthetic://pipeline-validation/<club_id>"`
so synthetic records can NEVER be confused with `live_public` or `live_authorized_api` data.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta
from typing import Iterable

from .models import (
    AvailabilityObservation,
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
)
from .scheduler import STANDARD_LEAD_BUCKETS
from .time_utils import (
    DUBLIN_TZ,
    UTC_TZ,
    WEEKDAY_NAMES,
    generate_atomic_slices,
    is_peak_dublin_hour,
    now_dublin_iso,
    resolve_operating_window,
)


def _target_booking_probability(
    *,
    court_env: str,
    court_size: str,
    is_weekend: bool,
    is_peak: bool,
    hour_local: int,
) -> float:
    """Realistic County Dublin padel demand curve for November weather conditions."""
    if court_env == "indoor":
        base = 0.84 if is_peak else 0.48
    elif court_env == "covered_outdoor":
        base = 0.74 if is_peak else 0.36
    else:  # outdoor uncovered in November Dublin
        base = 0.61 if is_peak else 0.24

    if is_weekend:
        base = min(0.92, base + 0.08)

    # Late night (after 22:30) and very early morning (06:00-07:00) taper off
    if hour_local in (0, 23):
        base *= 0.45
    elif hour_local == 6:
        base *= 0.55
    elif 18 <= hour_local <= 20 and not is_weekend:
        base = min(0.95, base + 0.08)

    if court_size == "single":
        base *= 0.85

    return max(0.05, min(0.95, base))


# Cumulative share of bookings already materialized by each lead-time bucket
LEAD_CAPTURE_CDF: dict[str, float] = {
    "7d": 0.22,   # Early-bird members & prime indoor regulars
    "3d": 0.54,   # Core Playtomic open-match creation window
    "24h": 0.81,  # Day-before confirmations
    "6h": 0.94,   # Same-day lunch bookings
    "1h": 1.00,   # Last-minute fill
}


def generate_synthetic_validation_dataset(
    *,
    clubs_by_id: dict[str, dict],
    courts: Iterable[CourtRecord],
    opening_hours: dict[tuple[str, str], OpeningHoursRecord],
    start_date: date = date(2026, 11, 9),  # Monday
    num_days: int = 7,
    seed: int = 20261106,
) -> CollectionRunResult:
    """Generate 5 lead-time snapshots (7d, 3d, 24h, 6h, 1h) with overlapping 60m & 90m slots."""
    rng = random.Random(seed)
    run_id = f"synth-validation-{seed}-{uuid.uuid4().hex[:6]}"
    started = now_dublin_iso()

    padel_courts = [
        c
        for c in courts
        if c.sport.upper() == "PADEL"
        and c.club_id in clubs_by_id
        and clubs_by_id[c.club_id].get("access_model") != "members_only"
        and int(clubs_by_id[c.club_id].get("in_county_dublin", 1)) == 1
    ]

    observations: list[AvailabilityObservation] = []
    buckets_ordered = [b[0] for b in STANDARD_LEAD_BUCKETS]  # ['7d', '3d', '24h', '6h', '1h']
    bucket_hours_map = {b[0]: b[1] for b in STANDARD_LEAD_BUCKETS}

    for day_offset in range(num_days):
        t_day = start_date + timedelta(days=day_offset)
        dow = WEEKDAY_NAMES[t_day.weekday()]
        is_wknd = t_day.weekday() >= 5

        for idx_c, court in enumerate(padel_courts):
            oh = opening_hours.get((court.club_id, dow))
            if oh is None:
                continue

            club_info = clubs_by_id[court.club_id]
            off_price = float(club_info.get("off_peak_price_eur") or 36.0)
            peak_price = float(club_info.get("peak_price_eur") or 44.0)
            if court.court_size == "single":
                off_price = round(off_price * 0.70, 2)
                peak_price = round(peak_price * 0.70, 2)

            win_start, win_end = resolve_operating_window(t_day, oh.opening_time, oh.closing_time)
            slices_30m = generate_atomic_slices(win_start, win_end, slice_minutes=30)
            if len(slices_30m) < 2:
                continue

            # Decide ground-truth physical occupancy in 60-min / 90-min non-overlapping blocks
            # for this court-day, and assign each booked block a capture lead-time bucket.
            slice_booked_at_bucket: list[str | None] = [None] * len(slices_30m)
            slice_block_reason: list[str | None] = [None] * len(slices_30m)

            pos = 0
            while pos < len(slices_30m):
                sl_start, _ = slices_30m[pos]
                hr = sl_start.hour
                peak_flag = is_peak_dublin_hour(sl_start)

                # Deterministic maintenance / academy coaching blocks (~4% of capacity)
                if idx_c == 0 and t_day.weekday() == 1 and hr == 10 and pos + 2 <= len(slices_30m):
                    for k in range(pos, pos + 2):
                        slice_block_reason[k] = "blocked_maintenance"
                    pos += 2
                    continue
                if idx_c == 1 and t_day.weekday() in (2, 4) and hr == 17 and pos + 3 <= len(slices_30m):
                    for k in range(pos, pos + 3):
                        slice_block_reason[k] = "blocked_coaching_or_event"
                    pos += 3
                    continue

                p_book = _target_booking_probability(
                    court_env=court.court_environment,
                    court_size=court.court_size,
                    is_weekend=is_wknd,
                    is_peak=peak_flag,
                    hour_local=hr,
                )
                if rng.random() < p_book and pos + 2 <= len(slices_30m):
                    # Choose 90-min booking (3 half-hours) 45% of the time if space allows, else 60-min (2 half-hours)
                    dur_slots = 3 if (pos + 3 <= len(slices_30m) and rng.random() < 0.45) else 2
                    u = rng.random()
                    chosen_bkt = "1h"
                    for b_name in buckets_ordered:
                        if u <= LEAD_CAPTURE_CDF[b_name]:
                            chosen_bkt = b_name
                            break
                    for k in range(pos, pos + dur_slots):
                        slice_booked_at_bucket[k] = chosen_bkt
                    pos += dur_slots
                else:
                    pos += 1

            # Now emit overlapping 60-minute and 90-minute offered slots on every 30-min start
            # across all 5 snapshot runs (anchoring observed_at relative to 18:00 local).
            anchor_dt = datetime.combine(t_day, time(18, 0), tzinfo=DUBLIN_TZ)
            src_url = f"synthetic://pipeline-validation/{court.club_id}"

            for b_idx, b_name in enumerate(buckets_ordered):
                obs_dt = (anchor_dt.astimezone(UTC_TZ) - timedelta(hours=bucket_hours_map[b_name])).astimezone(DUBLIN_TZ)

                # Simulate an HTTP timeout ('unknown') on 1 specific outdoor club-day at the '1h' bucket
                simulate_unknown_outage = (
                    court.club_id == "padel_pines" and day_offset == 5 and b_name == "1h"
                )

                for i in range(len(slices_30m)):
                    for span_half_hours, dur_min in ((2, 60), (3, 90)):
                        if i + span_half_hours > len(slices_30m):
                            continue
                        slot_s = slices_30m[i][0]
                        slot_e = slices_30m[i + span_half_hours - 1][1]
                        # Skip if observation timestamp would be after slot end
                        if obs_dt.astimezone(UTC_TZ) >= slot_s.astimezone(UTC_TZ):
                            continue

                        peak_slot = is_peak_dublin_hour(slot_s)
                        hr_rate = peak_price if peak_slot else off_price
                        offered_price = round(hr_rate * (dur_min / 60.0), 2)

                        persistent_unknown_window = (
                            court.club_id == "padel_pines"
                            and day_offset == 6
                            and 16 <= slot_s.hour < 18
                        )

                        if simulate_unknown_outage or persistent_unknown_window:
                            observations.append(
                                AvailabilityObservation.from_raw(
                                    club_id=court.club_id,
                                    court_id=court.court_id,
                                    booking_platform="playtomic",
                                    start_time=slot_s,
                                    end_time=slot_e,
                                    price_eur=None,
                                    availability_status="unknown",
                                    status_detail="missing_unknown",
                                    observed_at=obs_dt,
                                    snapshot_bucket=b_name,
                                    source_url=src_url,
                                    data_origin="synthetic_validation",
                                    run_id=run_id,
                                )
                            )
                            continue

                        sub_blocks = slice_block_reason[i : i + span_half_hours]
                        if any(sub_blocks):
                            b_detail = next(x for x in sub_blocks if x is not None)
                            observations.append(
                                AvailabilityObservation.from_raw(
                                    club_id=court.club_id,
                                    court_id=court.court_id,
                                    booking_platform="playtomic",
                                    start_time=slot_s,
                                    end_time=slot_e,
                                    price_eur=None,
                                    availability_status="blocked",
                                    status_detail=b_detail,
                                    observed_at=obs_dt,
                                    snapshot_bucket=b_name,
                                    source_url=src_url,
                                    data_origin="synthetic_validation",
                                    run_id=run_id,
                                )
                            )
                            continue

                        # Check if any constituent 30-min slice has already been booked at or before b_idx
                        booked_already = False
                        for k in range(i, i + span_half_hours):
                            cap_bkt = slice_booked_at_bucket[k]
                            if cap_bkt is not None and buckets_ordered.index(cap_bkt) <= b_idx:
                                booked_already = True
                                break

                        if booked_already:
                            # Mimics public calendar disappearance: status='unavailable', price_eur=None
                            detail = "inferred_unavailable" if b_idx > 0 else "observed_unavailable"
                            observations.append(
                                AvailabilityObservation.from_raw(
                                    club_id=court.club_id,
                                    court_id=court.court_id,
                                    booking_platform="playtomic",
                                    start_time=slot_s,
                                    end_time=slot_e,
                                    price_eur=None,
                                    availability_status="unavailable",
                                    status_detail=detail,
                                    observed_at=obs_dt,
                                    snapshot_bucket=b_name,
                                    source_url=src_url,
                                    data_origin="synthetic_validation",
                                    run_id=run_id,
                                )
                            )
                        else:
                            observations.append(
                                AvailabilityObservation.from_raw(
                                    club_id=court.club_id,
                                    court_id=court.court_id,
                                    booking_platform="playtomic",
                                    start_time=slot_s,
                                    end_time=slot_e,
                                    price_eur=offered_price,
                                    availability_status="available",
                                    status_detail="observed_available",
                                    observed_at=obs_dt,
                                    snapshot_bucket=b_name,
                                    source_url=src_url,
                                    data_origin="synthetic_validation",
                                    run_id=run_id,
                                )
                            )

    completed = now_dublin_iso()
    return CollectionRunResult(
        run_id=run_id,
        started_at=started,
        completed_at=completed,
        adapter_name="SyntheticValidationGenerator",
        platform_id="playtomic",
        club_id=None,
        target_url="synthetic://pipeline-validation/dublin-playtomic-9-clubs",
        http_status=None,
        robots_allowed=True,
        auth_required=False,
        run_status="success",
        data_origin="synthetic_validation",
        records_written=len(observations),
        error_message=(
            "EXPLICITLY SYNTHETIC DATA generated over real live-verified Playtomic courts & opening hours "
            "to validate multi-lead-time occupancy, 60m/90m overlap deduplication, and GBV carry-forward."
        ),
        observations=observations,
    )
