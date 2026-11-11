"""Stratified Field / Telephone / Visual Spot-Check Adapter (Zero-API Court Measurement).

Enables rapid, statistically rigorous occupancy measurement for clubs behind login walls,
Cloudflare challenges, or offline phone-enquiry desks (Padel Dome, Bushy Park, Portmarnock
PSLC, dlr Leisure Meadowbrook & Loughlinstown, Shankill TC, Rockbrook):
- Classifies every spot check into one of 4 canonical Dublin demand strata:
    1. `weekday_offpeak` (Mon–Fri opening–17:00 & >22:30)
    2. `weekday_peak`    (Mon–Fri 17:00–22:30)
    3. `weekend_prime`   (Sat–Sun 09:00–20:00)
    4. `weekend_offpeak` (Sat–Sun <09:00 & >=20:00)
- Computes Stratified Horvitz-Thompson / Ratio Occupancy Estimates with 95% confidence
  bounds from a handful of visual, telephone, or on-site court headcounts.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from ..time_utils import is_peak_dublin_hour, now_dublin_iso, parse_iso_aware, to_dublin

STRATA_DEFAULT_WEEKLY_WEIGHTS: dict[str, float] = {
    # Share of a standard 98-hour Dublin padel club week
    "weekday_offpeak": 0.45,
    "weekday_peak": 0.28,
    "weekend_prime": 0.22,
    "weekend_offpeak": 0.05,
}


@dataclass(frozen=True)
class StratifiedSpotEstimate:
    club_id: str
    samples_count: int
    strata_covered: int
    point_occupancy_pct: float
    ci95_low_pct: float
    ci95_high_pct: float


def classify_spot_stratum(slot_start: str | datetime) -> str:
    dt = to_dublin(parse_iso_aware(slot_start))
    is_wknd = dt.weekday() >= 5
    is_pk = is_peak_dublin_hour(dt)
    if not is_wknd:
        return "weekday_peak" if is_pk else "weekday_offpeak"
    return "weekend_prime" if is_pk else "weekend_offpeak"


def build_spot_check_record(
    *,
    club_id: str,
    target_slot_start: str,
    target_slot_end: str,
    total_courts_checked: int,
    occupied_courts: int,
    blocked_courts: int = 0,
    displayed_price_eur: Optional[float] = None,
    collection_method: str = "browser_visual_check",
    observed_at: Optional[str] = None,
    observer_notes: str = "",
) -> dict[str, Any]:
    if total_courts_checked <= 0:
        raise ValueError("total_courts_checked must be > 0")
    if occupied_courts < 0 or blocked_courts < 0 or occupied_courts + blocked_courts > total_courts_checked:
        raise ValueError(
            f"Invalid court counts: occupied={occupied_courts}, blocked={blocked_courts}, total={total_courts_checked}"
        )
    s_dt = to_dublin(parse_iso_aware(target_slot_start, "target_slot_start"))
    e_dt = to_dublin(parse_iso_aware(target_slot_end, "target_slot_end"))
    if e_dt <= s_dt:
        raise ValueError("target_slot_end must be after target_slot_start")
    obs_dt = to_dublin(parse_iso_aware(observed_at, "observed_at")) if observed_at else to_dublin(parse_iso_aware(now_dublin_iso()))
    avail = total_courts_checked - occupied_courts - blocked_courts

    return {
        "club_id": club_id.strip(),
        "observed_at": obs_dt.isoformat(timespec="seconds"),
        "target_slot_start": s_dt.isoformat(timespec="seconds"),
        "target_slot_end": e_dt.isoformat(timespec="seconds"),
        "time_stratum": classify_spot_stratum(s_dt),
        "total_courts_checked": total_courts_checked,
        "occupied_courts": occupied_courts,
        "blocked_courts": blocked_courts,
        "available_courts": avail,
        "displayed_price_eur": displayed_price_eur,
        "collection_method": collection_method,
        "observer_notes": observer_notes,
    }


def estimate_stratified_occupancy(
    spot_checks: Iterable[dict[str, Any]],
    stratum_weights: Optional[dict[str, float]] = None,
) -> dict[str, StratifiedSpotEstimate]:
    """Compute stratified ratio occupancy & 95% CI per club from spot-check observations."""
    weights = stratum_weights or STRATA_DEFAULT_WEEKLY_WEIGHTS
    by_club_stratum: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for sc in spot_checks:
        bookable = int(sc["total_courts_checked"]) - int(sc.get("blocked_courts") or 0)
        if bookable <= 0:
            continue
        ratio = float(sc["occupied_courts"]) / float(bookable)
        by_club_stratum[sc["club_id"]][sc["time_stratum"]].append(ratio)

    out: dict[str, StratifiedSpotEstimate] = {}
    for club_id, strata_map in sorted(by_club_stratum.items()):
        total_w = 0.0
        weighted_mean = 0.0
        weighted_var = 0.0
        n_total = 0

        for st_name, ratios in strata_map.items():
            w = weights.get(st_name, 0.25)
            n_h = len(ratios)
            n_total += n_h
            mean_h = sum(ratios) / n_h
            if n_h > 1:
                var_h = sum((x - mean_h) ** 2 for x in ratios) / (n_h - 1) / n_h
            else:
                # Binomial worst-case single-observation variance p(1-p) <= 0.25
                var_h = max(mean_h * (1.0 - mean_h), 0.09)
            total_w += w
            weighted_mean += w * mean_h
            weighted_var += (w ** 2) * var_h

        est = weighted_mean / max(total_w, 1e-6)
        se = math.sqrt(weighted_var) / max(total_w, 1e-6)
        low = max(0.0, est - 1.96 * se)
        high = min(1.0, est + 1.96 * se)
        out[club_id] = StratifiedSpotEstimate(
            club_id=club_id,
            samples_count=n_total,
            strata_covered=len(strata_map),
            point_occupancy_pct=round(100.0 * est, 1),
            ci95_low_pct=round(100.0 * low, 1),
            ci95_high_pct=round(100.0 * high, 1),
        )
    return out

