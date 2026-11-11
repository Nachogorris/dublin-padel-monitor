"""Multi-Snapshot Next-Week Booking Tracker & Empirical Peak / Off-Peak Hour Classifier.

Coordinates:
1. 5-Pass Multi-Snapshot Gathering Plans (`7d`, `3d`, `24h`, `morning`/`6h`, `prepeak`/`1h`)
   and Dropzone Bundle Scaffolding (`data/inbox/week_<YYYY-MM-DD>/`).
2. Club x Hour (`06:00`–`23:00`) Occupancy Heatmaps (All-Week & Weekday-vs-Weekend split).
3. Empirical Peak / Shoulder / Off-Peak Hour Discovery per club (identifying hidden pre-work
   `07:00` and lunchtime `12:00–14:00` peaks alongside evening prime windows).
4. Cumulative 5-Snapshot Booking Progression Curves (`7d -> 3d -> 24h -> 6h -> 1h`) showing
   when Peak vs Off-Peak courts actually fill up before play.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import AvailabilityObservation, CourtRecord, OpeningHoursRecord
from .occupancy import AtomicSliceResolution, SLICE_MINUTES, resolve_atomic_slices
from .time_utils import DUBLIN_TZ, WEEKDAY_NAMES, parse_iso_aware

HOURS_GRID: tuple[int, ...] = tuple(range(6, 24))  # 06:00 through 23:00 local Europe/Dublin

SNAPSHOT_PASS_SPECS: tuple[tuple[str, str, float, str], ...] = (
    # (pass_tag, canonical_bucket, nominal_lead_hours, purpose)
    (
        "7d",
        "7d",
        168.0,
        "Week-ahead baseline: captures 14d/28d priority member bookings & standing club leagues",
    ),
    (
        "3d",
        "3d",
        72.0,
        "Mid-lead check: captures core Playtomic 4-player open-match formation window",
    ),
    (
        "24h",
        "24h",
        24.0,
        "Day-before lock-in: captures 24h cancellation deadline releases & evening-before bookings",
    ),
    (
        "morning",
        "6h",
        6.0,
        "Day-of 06:00–08:30 check: captures final realized morning & lunchtime off-peak occupancy",
    ),
    (
        "prepeak",
        "1h",
        1.5,
        "Day-of 16:30 check: captures final realized evening peak (17:00–23:00) & weather-driven fill",
    ),
)

PRIORITY_SCAFFOLD_CLUBS: tuple[str, ...] = (
    "house_of_padel",
    "padel_dome",
    "druid_kimmage",
    "druid_coolmine",
    "star_padel",
    "ob_padel",
    "portmarnock",
    "bushy_park",
    "halpenny",
    "leopardstown_golf",
    "padel100_ring_commons",
    "fitzpatrick_castle",
)


def compress_hour_spans(hours: Sequence[int]) -> str:
    """Format `[7, 12, 13, 17, 18, 19, 20, 21]` -> `'07:00–08:00, 12:00–14:00, 17:00–22:00'`."""
    if not hours:
        return "None"
    ordered = sorted(set(hours))
    spans: list[tuple[int, int]] = []
    start = ordered[0]
    prev = ordered[0]
    for h in ordered[1:]:
        if h == prev + 1:
            prev = h
        else:
            spans.append((start, prev + 1))
            start = prev = h
    spans.append((start, prev + 1))
    return ", ".join(f"{s:02d}:00–{e % 24:02d}:00" for s, e in spans)


def build_weekly_snapshot_schedule(
    clubs: Iterable[dict[str, Any]],
    week_start: date = date(2026, 11, 9),
) -> list[dict[str, Any]]:
    """Build the 5-snapshot collection schedule across a 7-day target week for public/hybrid clubs."""
    rows: list[dict[str, Any]] = []
    public_clubs = [
        c
        for c in clubs
        if c.get("access_model") in ("public_pay_and_play", "hybrid_member_visitor")
        and int(c.get("verified_padel_courts") or 0) > 0
    ]
    public_clubs.sort(
        key=lambda c: (
            -int(c.get("in_county_dublin") or 0),
            -int(c.get("verified_padel_courts") or 0),
            c.get("club_name") or "",
        )
    )

    for club in public_clubs:
        cid = club["club_id"]
        is_priority = cid in PRIORITY_SCAFFOLD_CLUBS
        for day_offset in range(7):
            t_day = week_start + timedelta(days=day_offset)
            dow = WEEKDAY_NAMES[t_day.weekday()]
            for pass_tag, bucket, nom_hrs, purpose in SNAPSHOT_PASS_SPECS:
                if pass_tag == "7d":
                    cap_dt = datetime.combine(t_day - timedelta(days=7), time(18, 0), tzinfo=DUBLIN_TZ)
                elif pass_tag == "3d":
                    cap_dt = datetime.combine(t_day - timedelta(days=3), time(18, 0), tzinfo=DUBLIN_TZ)
                elif pass_tag == "24h":
                    cap_dt = datetime.combine(t_day - timedelta(days=1), time(18, 0), tzinfo=DUBLIN_TZ)
                elif pass_tag == "morning":
                    cap_dt = datetime.combine(t_day, time(8, 0), tzinfo=DUBLIN_TZ)
                else:
                    cap_dt = datetime.combine(t_day, time(16, 30), tzinfo=DUBLIN_TZ)

                rows.append(
                    {
                        "capture_at_dublin": cap_dt.isoformat(timespec="seconds"),
                        "capture_day": cap_dt.strftime("%a %Y-%m-%d %H:%M"),
                        "priority_group": "P0_Core_12" if is_priority else "P1_Extended_10",
                        "club_id": cid,
                        "club_name": club.get("club_name") or cid,
                        "verified_courts": int(club.get("verified_padel_courts") or 0),
                        "platform_id": club.get("platform_id") or "",
                        "target_date": t_day.isoformat(),
                        "target_day_of_week": dow,
                        "snapshot_pass_tag": pass_tag,
                        "canonical_bucket": bucket,
                        "nominal_lead_hours": nom_hrs,
                        "bundle_dropzone_file": f"data/inbox/week_{week_start.isoformat()}/{cid}__week__{week_start.isoformat()}__{pass_tag}.txt",
                        "single_day_dropzone_file": f"data/inbox/{cid}__{t_day.isoformat()}__{pass_tag}.txt",
                        "public_booking_url": club.get("public_booking_url") or "",
                        "what_it_measures": purpose,
                    }
                )

    rows.sort(key=lambda r: (r["capture_at_dublin"], r["priority_group"], r["club_id"], r["target_date"]))
    return rows


def scaffold_weekly_inbox_pack(
    clubs: Iterable[dict[str, Any]],
    *,
    week_start: date = date(2026, 11, 9),
    inbox_dir: Path,
    club_filter: str | None = None,
) -> dict[str, Any]:
    """Create ready-to-fill 5-pass (`7d`, `3d`, `24h`, `morning`, `prepeak`) weekly bundle templates."""
    pack_dir = inbox_dir / f"week_{week_start.isoformat()}"
    pack_dir.mkdir(parents=True, exist_ok=True)

    clubs_by_id = {c["club_id"]: c for c in clubs}
    if club_filter:
        if club_filter not in clubs_by_id:
            raise ValueError(f"Unknown club_id={club_filter!r}")
        target_ids = [club_filter]
    else:
        target_ids = [cid for cid in PRIORITY_SCAFFOLD_CLUBS if cid in clubs_by_id]

    created_files: list[Path] = []
    skipped_existing = 0

    for cid in target_ids:
        c = clubs_by_id[cid]
        n_courts = int(c.get("verified_padel_courts") or 0)
        url = c.get("public_booking_url") or ""
        oh_summary = c.get("opening_hours_summary") or "08:00–22:00"
        off_p = c.get("off_peak_price_eur")
        pk_p = c.get("peak_price_eur")

        for pass_tag, bucket, _, purpose in SNAPSHOT_PASS_SPECS:
            fpath = pack_dir / f"{cid}__week__{week_start.isoformat()}__{pass_tag}.txt"
            if fpath.exists():
                skipped_existing += 1
                continue

            lines = [
                f"# Club          : {c.get('club_name')} ({cid}) — {n_courts} verified padel courts",
                f"# Booking URL   : {url}",
                f"# Opening Hours : {oh_summary} | Prices: Off-Peak EUR {off_p} / Peak EUR {pk_p}",
                f"# Snapshot Pass : {pass_tag} (bucket={bucket}) — {purpose}",
                "# Optional exact timestamp override (uncomment if desired):",
                "# observed_at: 2026-11-08T20:00:00+00:00",
                "#",
                "# HOW TO FILL ANY DAY BELOW (leave unfilled days blank — blank days are safely ignored!):",
                "#   Option A (10-sec shorthand):  09:00-17:00 = 3 free €30",
                "#                                 17:00-21:30 = 0 free €45",
                "#   Option B (Copy-paste text) :  Court 1",
                "#                                 09:00 60 min €30",
                "#                                 Court 2",
                "#                                 No slots available",
                "",
            ]
            for d_off in range(7):
                t_day = week_start + timedelta(days=d_off)
                dow = WEEKDAY_NAMES[t_day.weekday()].capitalize()
                lines.append(f"=== {t_day.isoformat()} ({dow}) ===")
                lines.append("")

            fpath.write_text("\n".join(lines), encoding="utf-8")
            created_files.append(fpath)

    return {
        "pack_dir": pack_dir,
        "clubs_scaffolded": len(target_ids),
        "files_created": len(created_files),
        "files_skipped_existing": skipped_existing,
    }


def compute_club_hour_heatmap_and_empirical_peaks(
    slices: Sequence[AtomicSliceResolution],
    clubs_by_id: dict[str, dict[str, Any]],
    hours_grid: Sequence[int] = HOURS_GRID,
) -> dict[str, list[dict[str, Any]]]:
    """Compute Club x Hour (`06:00`–`23:00`) occupancy matrices and empirical Peak/Shoulder/Off-Peak bands."""
    sh = SLICE_MINUTES / 60.0

    by_club: dict[str, list[AtomicSliceResolution]] = defaultdict(list)
    by_club_hour: dict[tuple[str, int], list[AtomicSliceResolution]] = defaultdict(list)
    by_club_daytype_hour: dict[tuple[str, str, int], list[AtomicSliceResolution]] = defaultdict(list)

    for s in slices:
        by_club[s.club_id].append(s)
        by_club_hour[(s.club_id, s.hour_of_day)].append(s)
        dtype = "Weekend (Sat-Sun)" if s.is_weekend else "Weekday (Mon-Fri)"
        by_club_daytype_hour[(s.club_id, dtype, s.hour_of_day)].append(s)

    def _occ_and_rate(grp: list[AtomicSliceResolution]) -> tuple[float | None, float, float | None]:
        avail_h = sum(1 for x in grp if x.final_state == "available") * sh
        unavail_h = (
            sum(
                1
                for x in grp
                if x.final_state in ("confirmed_booking", "inferred_unavailable_transition", "static_unavailable")
            )
            * sh
        )
        offered_h = avail_h + unavail_h
        if offered_h <= 0:
            return None, 0.0, None
        pct = round(100.0 * unavail_h / offered_h, 1)
        rates = [x.hourly_rate_eur for x in grp if x.hourly_rate_eur is not None]
        avg_r = round(sum(rates) / len(rates), 2) if rates else None
        return pct, round(offered_h, 2), avg_r

    club_hour_rows: list[dict[str, Any]] = []
    daytype_rows: list[dict[str, Any]] = []
    empirical_rows: list[dict[str, Any]] = []

    ordered_clubs = sorted(
        by_club.keys(),
        key=lambda cid: (
            -int(clubs_by_id.get(cid, {}).get("in_county_dublin") or 0),
            -int(clubs_by_id.get(cid, {}).get("verified_padel_courts") or 0),
            cid,
        ),
    )

    for cid in ordered_clubs:
        c_info = clubs_by_id.get(cid, {})
        c_name = c_info.get("club_name") or cid
        c_env = c_info.get("court_environment") or "unknown"
        n_courts = int(c_info.get("verified_padel_courts") or 0)

        all_slices = by_club[cid]
        overall_pct, overall_off_h, _ = _occ_and_rate(all_slices)
        std_pk_pct, _, _ = _occ_and_rate([x for x in all_slices if x.is_peak])
        std_off_pct, _, _ = _occ_and_rate([x for x in all_slices if not x.is_peak])

        row_mat: dict[str, Any] = {
            "club_id": cid,
            "club_name": c_name,
            "environment": c_env,
            "padel_courts": n_courts,
            "offered_hours": overall_off_h,
            "overall_occ_pct": overall_pct,
            "std_peak_occ_pct": std_pk_pct,
            "std_offpeak_occ_pct": std_off_pct,
        }

        hour_stats: dict[int, tuple[float, float, float | None]] = {}
        for h in hours_grid:
            col = f"{h:02d}:00"
            pct, off_h, avg_r = _occ_and_rate(by_club_hour.get((cid, h), []))
            row_mat[col] = pct
            if pct is not None and off_h > 0:
                hour_stats[h] = (pct, off_h, avg_r)
        club_hour_rows.append(row_mat)

        # Weekday vs Weekend rows
        for dtype in ("Weekday (Mon-Fri)", "Weekend (Sat-Sun)"):
            dt_slices = [
                x for x in all_slices if ("Weekend (Sat-Sun)" if x.is_weekend else "Weekday (Mon-Fri)") == dtype
            ]
            dt_pct, dt_off_h, _ = _occ_and_rate(dt_slices)
            dt_row: dict[str, Any] = {
                "club_id": cid,
                "club_name": c_name,
                "day_segment": dtype,
                "offered_hours": dt_off_h,
                "segment_occ_pct": dt_pct,
            }
            for h in hours_grid:
                pct, _, _ = _occ_and_rate(by_club_daytype_hour.get((cid, dtype, h), []))
                dt_row[f"{h:02d}:00"] = pct
            daytype_rows.append(dt_row)

        # Empirical Peak / Shoulder / Off-Peak Band Discovery
        if hour_stats:
            max_occ = max(v[0] for v in hour_stats.values())
            min_occ = min(v[0] for v in hour_stats.values())
            busiest_h = max(hour_stats.keys(), key=lambda hh: hour_stats[hh][0])
            quietest_h = min(hour_stats.keys(), key=lambda hh: hour_stats[hh][0])

            # Dynamic + absolute threshold: >= 75% OR within 10pp of club's max (if >= 55%)
            peak_thresh = min(75.0, max(55.0, max_occ - 10.0))
            shoulder_thresh = max(45.0, min(peak_thresh - 12.0, min_occ + 10.0))

            emp_peak_hrs = [h for h, (p, _, _) in hour_stats.items() if p >= peak_thresh]
            emp_shoulder_hrs = [h for h, (p, _, _) in hour_stats.items() if shoulder_thresh <= p < peak_thresh]
            emp_off_hrs = [h for h, (p, _, _) in hour_stats.items() if p < shoulder_thresh]

            def _band_weighted_occ(h_list: list[int]) -> float | None:
                if not h_list:
                    return None
                tot_w = sum(hour_stats[hh][1] for hh in h_list)
                if tot_w <= 0:
                    return None
                return round(sum(hour_stats[hh][0] * hour_stats[hh][1] for hh in h_list) / tot_w, 1)

            def _band_avg_price(h_list: list[int]) -> float | None:
                vals = [hour_stats[hh][2] for hh in h_list if hour_stats[hh][2] is not None]
                return round(sum(vals) / len(vals), 2) if vals else None

            emp_pk_occ = _band_weighted_occ(emp_peak_hrs)
            emp_off_occ = _band_weighted_occ(emp_off_hrs)
            spread = (
                round(emp_pk_occ - emp_off_occ, 1)
                if (emp_pk_occ is not None and emp_off_occ is not None)
                else None
            )

            empirical_rows.append(
                {
                    "club_id": cid,
                    "club_name": c_name,
                    "environment": c_env,
                    "padel_courts": n_courts,
                    "empirical_peak_windows": compress_hour_spans(emp_peak_hrs),
                    "empirical_shoulder_windows": compress_hour_spans(emp_shoulder_hrs),
                    "empirical_offpeak_windows": compress_hour_spans(emp_off_hrs),
                    "busiest_hour": f"{busiest_h:02d}:00 ({max_occ:.1f}%)",
                    "quietest_hour": f"{quietest_h:02d}:00 ({min_occ:.1f}%)",
                    "empirical_peak_occ_pct": emp_pk_occ,
                    "empirical_offpeak_occ_pct": emp_off_occ,
                    "peak_vs_offpeak_spread_pp": spread,
                    "avg_peak_price_eur": _band_avg_price(emp_peak_hrs),
                    "avg_offpeak_price_eur": _band_avg_price(emp_off_hrs),
                }
            )

    return {
        "club_hour_matrix": club_hour_rows,
        "club_daytype_hour_matrix": daytype_rows,
        "empirical_bands": empirical_rows,
    }


def compute_snapshot_progression_curve(
    observations: Sequence[AvailabilityObservation],
    courts: dict[tuple[str, str], CourtRecord],
    opening_hours: dict[tuple[str, str], OpeningHoursRecord],
    clubs_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Measure how Peak & Off-Peak occupancy accumulates across the 5 snapshot passes (`7d -> 3d -> 24h -> 6h -> 1h`)."""
    if not observations:
        return []

    stages: tuple[tuple[str, set[str], float], ...] = (
        ("Pass 1: 7d Ahead", {"7d"}, 120.0),
        ("Pass 2: 3d Ahead", {"7d", "3d"}, 48.0),
        ("Pass 3: 24h Ahead", {"7d", "3d", "24h"}, 12.0),
        ("Pass 4: 6h / Morning", {"7d", "3d", "24h", "6h"}, 3.0),
        ("Pass 5: 1h / Final Pre-Play", {"7d", "3d", "24h", "6h", "1h", "ad_hoc"}, -24.0),
    )

    sh = SLICE_MINUTES / 60.0
    stage_club_metrics: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {}

    for stage_label, allowed_buckets, min_lead_for_adhoc in stages:
        filtered = [
            o
            for o in observations
            if o.snapshot_bucket in allowed_buckets
            or (o.snapshot_bucket == "ad_hoc" and o.lead_time_hours >= min_lead_for_adhoc)
        ]
        # Require at least one full-day calendar observation (not just a single event card) at this stage
        has_calendar_obs = any(
            o.booking_platform in ("browser_dropzone", "csv_import", "playtomic_official_api", "playtomic")
            or o.data_origin in ("synthetic_validation", "live_public")
            for o in filtered
        )
        if not filtered or not has_calendar_obs:
            continue
        slices = resolve_atomic_slices(filtered, courts=courts, opening_hours=opening_hours)

        by_club: dict[str, list[AtomicSliceResolution]] = defaultdict(list)
        for s in slices:
            by_club[s.club_id].append(s)
            by_club["__ALL__"].append(s)

        for cid, grp in by_club.items():
            def _calc(sub: list[AtomicSliceResolution]) -> float | None:
                av = sum(1 for x in sub if x.final_state == "available") * sh
                un = sum(
                    1
                    for x in sub
                    if x.final_state in ("confirmed_booking", "inferred_unavailable_transition", "static_unavailable")
                ) * sh
                return round(100.0 * un / (av + un), 1) if (av + un) > 0 else None

            stage_club_metrics[(cid, stage_label)] = (
                _calc(grp),
                _calc([x for x in grp if x.is_peak]),
                _calc([x for x in grp if not x.is_peak]),
            )

    all_cids = sorted({cid for (cid, _) in stage_club_metrics.keys() if cid != "__ALL__"})
    ordered_cids = ["__ALL__"] + all_cids if ("__ALL__", stages[-1][0]) in stage_club_metrics else all_cids

    out: list[dict[str, Any]] = []
    for cid in ordered_cids:
        c_name = "ALL MONITORED CLUBS (Aggregate)" if cid == "__ALL__" else (clubs_by_id.get(cid, {}).get("club_name") or cid)
        prev_all: float | None = None
        for stage_label, _, _ in stages:
            vals = stage_club_metrics.get((cid, stage_label))
            if vals is None:
                continue
            occ_all, occ_pk, occ_off = vals
            delta_pp = round(occ_all - prev_all, 1) if (occ_all is not None and prev_all is not None) else None
            if occ_all is not None:
                prev_all = occ_all
            out.append(
                {
                    "club_id": "ALL_CLUBS" if cid == "__ALL__" else cid,
                    "club_name": c_name,
                    "snapshot_stage": stage_label,
                    "overall_occupancy_pct": occ_all,
                    "incremental_gain_pp": delta_pp,
                    "peak_occupancy_pct": occ_pk,
                    "offpeak_occupancy_pct": occ_off,
                }
            )
    return out


def build_live_next_week_organised_grid(
    telemetry_rows: Iterable[dict[str, Any]],
    clubs_by_id: dict[str, dict[str, Any]],
    week_start: date = date(2026, 11, 9),
    hours_grid: Sequence[int] = HOURS_GRID,
) -> list[dict[str, Any]]:
    """Map real harvested live tournaments & classes in `[week_start, week_start+7d)` onto a Club x Hour court-hours grid."""
    week_end = week_start + timedelta(days=7)
    latest_map: dict[tuple[str, str], dict[str, Any]] = {}
    for r in telemetry_rows:
        latest_map[(r["club_id"], r["activity_id"])] = r

    court_hrs_by_club_hour: dict[tuple[str, int], float] = defaultdict(float)
    players_by_club: dict[str, int] = defaultdict(int)
    events_by_club: dict[str, int] = defaultdict(int)

    for act in latest_map.values():
        if not act.get("start_time") or not act.get("end_time"):
            continue
        s_dt = parse_iso_aware(act["start_time"]).astimezone(DUBLIN_TZ)
        e_dt = parse_iso_aware(act["end_time"]).astimezone(DUBLIN_TZ)
        if not (week_start <= s_dt.date() < week_end):
            continue
        cid = act["club_id"]
        n_courts = int(act.get("courts_occupied") or 1)
        events_by_club[cid] += 1
        players_by_club[cid] += int(act.get("registered_players") or 0)

        cur = s_dt
        while cur < e_dt:
            nxt = min(e_dt, cur + timedelta(minutes=30))
            hrs_slice = (nxt - cur).total_seconds() / 3600.0
            court_hrs_by_club_hour[(cid, cur.hour)] += n_courts * hrs_slice
            cur = nxt

    out: list[dict[str, Any]] = []
    for cid in sorted(events_by_club.keys()):
        c_info = clubs_by_id.get(cid, {})
        row: dict[str, Any] = {
            "club_id": cid,
            "club_name": c_info.get("club_name") or cid,
            "padel_courts": int(c_info.get("verified_padel_courts") or 0),
            "next_week_live_events": events_by_club[cid],
            "registered_players_so_far": players_by_club[cid],
            "total_organised_court_hours": round(
                sum(court_hrs_by_club_hour.get((cid, h), 0.0) for h in hours_grid), 2
            ),
        }
        for h in hours_grid:
            val = round(court_hrs_by_club_hour.get((cid, h), 0.0), 2)
            row[f"{h:02d}:00"] = val if val > 0 else None
        out.append(row)

    out.sort(key=lambda r: r["total_organised_court_hours"], reverse=True)
    return out
