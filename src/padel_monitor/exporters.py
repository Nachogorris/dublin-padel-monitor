"""Excel (.xlsx) and CSV deliverable exporters (Section 5, Section 9, Section 10)."""
from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .db import PadelDatabase
from .inventory import build_platform_coverage_matrix
from .models import AvailabilityObservation
from .occupancy import OccupancySummaryRow

HEADER_FILL = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
SUBHEADER_FILL = PatternFill(start_color="D9E2EC", end_color="D9E2EC", fill_type="solid")
ZEBRA_FILL = PatternFill(start_color="F7FAFC", end_color="F7FAFC", fill_type="solid")
WARN_FILL = PatternFill(start_color="FFFBEB", end_color="FFFBEB", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="1B365D")
BOLD_FONT = Font(name="Calibri", size=10, bold=True)
BODY_FONT = Font(name="Calibri", size=10)
THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E0"),
    right=Side(style="thin", color="CBD5E0"),
    top=Side(style="thin", color="CBD5E0"),
    bottom=Side(style="thin", color="CBD5E0"),
)


DEVELOPMENT_ROADMAP_ROWS: list[dict[str, Any]] = [
    {
        "phase": "Phase 1 (Immediate - Week 1–2)",
        "priority": "P0 — Highest ROI",
        "target_platform_or_clubs": "Playtomic Official Third-Party API (12 County Dublin venues, 37 courts + 3 commuter venues, 16 courts)",
        "clubs_covered": "House of Padel, Star Padel, OB Padel, Druid Kimmage, Druid Coolmine, Halpenny, Leopardstown Golf, Fitzpatrick Castle, Padel & Pines, Padel 100 (x2), Druid SPORTSCO",
        "technical_action": (
            "Onboard via Playtomic Connect (https://playtomic.com/connect) or partner with Champion/Master-tier Dublin club managers "
            "(Playtomic Manager -> Settings -> Developer Tools) to provision PLAYTOMIC_CLIENT_ID & PLAYTOMIC_CLIENT_SECRET into PlaytomicOfficialApiAdapter."
        ),
        "compliance_status": "100% sanctioned read-only OAuth2 API; PII stripped at ingestion.",
        "expected_coverage_gain": "Unlocks live confirmed slot-level occupancy across 40.7% of all County Dublin courts (56.5% of public/hybrid courts).",
    },
    {
        "phase": "Phase 2 (Short Term - Week 3–4)",
        "priority": "P1 — Single Largest Venue",
        "target_platform_or_clubs": "Padel Dome / Dublin Sports Dome (TPC Matchpoint — 9 indoor courts)",
        "clubs_covered": "Padel Dome Santry (8 indoor doubles + 1 indoor singles)",
        "technical_action": (
            "Establish B2B market-intelligence benchmark exchange with Padel Dome management:club exports weekly anonymized booking sheet "
            "from Matchpoint admin directly into CsvObservationAdapter (`monitor.py ingest-csv`)."
        ),
        "compliance_status": "Avoids Matchpoint /Login.aspx wall and robots.txt Disallow: /Booking/.",
        "expected_coverage_gain": "+9 indoor courts (+9.9% of County Dublin total capacity; +36% of County Dublin indoor courts).",
    },
    {
        "phase": "Phase 3 (Medium Term - Month 2)",
        "priority": "P2 — Municipal & Leisure Operators",
        "target_platform_or_clubs": "GladstoneGo (Portmarnock PSLC — 5 courts) & dlr Leisure (Meadowbrook + Loughlinstown — 5 courts)",
        "clubs_covered": "Portmarnock PSLC, dlr Leisure Meadowbrook, dlr Leisure Loughlinstown",
        "technical_action": (
            "Request Gladstone operator report export from PSLC and submit a research / FOI aggregate court-utilization request "
            "to Dun Laoghaire-Rathdown Leisure Services."
        ),
        "compliance_status": "Fully compliant operator/municipal data sharing.",
        "expected_coverage_gain": "+10 public outdoor courts (+11.0% of County Dublin supply).",
    },
    {
        "phase": "Phase 4 (Medium Term - Month 2–3)",
        "priority": "P3 — Cloudflare / Hybrid Tennis-Padel Venues",
        "target_platform_or_clubs": "Playbypoint (Bushy Park — 4 courts), SmartClubCloud (Shankill TC — 2 courts), Rockbrook Padel (2 courts)",
        "clubs_covered": "Bushy Park, Shankill TC, Rockbrook Padel",
        "technical_action": (
            "Offer reciprocal Dublin Padel Benchmark Report to club committees in exchange for monthly court-hour utilization summaries; "
            "monitor padel-ireland.ie/bookings/ (Crawl-delay >= 10s) in case broken [rncbc_calendar] shortcode is restored."
        ),
        "compliance_status": "Respects Playbypoint Cloudflare Turnstile + EU TDM reservation and SmartClubCloud login walls.",
        "expected_coverage_gain": "+8 public/hybrid outdoor courts (+8.8% of County Dublin supply).",
    },
    {
        "phase": "Phase 5 (Ongoing Quarterly)",
        "priority": "P4 — Automated Supply & Dashboard Expansion",
        "target_platform_or_clubs": "Playtomic Sitemap Diffing, Fingal/DCC/DLR/SDCC Planning Registers & BI Dashboard",
        "clubs_covered": "Swords Pavilions Padel Hub (+10 planned courts, F26A/0624E) & new Irish Playtomic openings",
        "technical_action": (
            "Schedule weekly Playtomic sitemap scan + club profile refresh (`PlaytomicPublicClubAdapter`) to detect newly opened courts, "
            "price shifts, and tournament schedules automatically; connect SQLite/CSV outputs to Power BI / Streamlit dashboard."
        ),
        "compliance_status": "Uses robots.txt-permitted /sitemap.xml and /clubs/<slug> endpoints at <= 1 req / 1.5s.",
        "expected_coverage_gain": "Continuous supply tracking across all 91+ County Dublin courts and 20+ commuter-belt courts.",
    },
]


def _style_worksheet_table(ws, headers: Sequence[str], rows: Sequence[Sequence[Any]], start_row: int = 1) -> None:
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=col_idx, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER

    for r_offset, row_vals in enumerate(rows, start=1):
        r_idx = start_row + r_offset
        is_even = r_offset % 2 == 0
        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r_idx, column=col_idx, value=val)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            if is_even:
                cell.fill = ZEBRA_FILL
            if isinstance(val, str) and len(val) > 55:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            else:
                cell.alignment = Alignment(vertical="top")

    ws.freeze_panes = f"A{start_row + 1}"
    if rows:
        end_col = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A{start_row}:{end_col}{start_row + len(rows)}"

    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        sample_vals = [str(headers[col_idx - 1])]
        for r in rows[:200]:
            if col_idx - 1 < len(r) and r[col_idx - 1] is not None:
                sample_vals.append(str(r[col_idx - 1]))
        max_len = max((len(s) for s in sample_vals), default=12)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 48)


def write_dicts_to_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        path.write_text("", encoding="utf-8")
        return path
    cols = list(fieldnames) if fieldnames else list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


def export_backward_compatible_venues_csv(db: PadelDatabase, venues_csv_path: Path) -> Path:
    """Update starter `venues.csv` with verified court counts, slugs, and platforms while preserving its exact 7 columns."""
    status_map = {
        "live_metadata_monitored_slots_need_api": "live_metadata_verified_slots_need_official_api",
        "live_slots_monitored": "live_slots_monitored",
        "technically_accessible_not_integrated": "playtomic_ssr_available_commuter_belt",
        "awaiting_permission_or_api_access": "awaiting_operator_or_api_permission",
        "not_accessible_auth_or_antibot": "blocked_login_or_antibot",
        "not_accessible_members_only": "members_only",
        "not_accessible_no_online_calendar": "enquiry_or_offline_booking_only",
        "planned_venue": "planned_not_yet_open",
    }
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT club_id, club_name, county, verified_padel_courts, platform_id,
                   public_booking_url, automated_collection_status
            FROM clubs
            WHERE inventory_cohort != 'pipeline_planning_dublin'
            ORDER BY in_county_dublin DESC, verified_padel_courts DESC, club_name ASC
            """
        ).fetchall()

    out_rows = [
        {
            "venue_id": r["club_id"],
            "venue": r["club_name"],
            "county": r["county"],
            "courts_listed": r["verified_padel_courts"],
            "platform": r["platform_id"] or "unknown",
            "public_booking_url": r["public_booking_url"] or "",
            "access_status": status_map.get(r["automated_collection_status"], r["automated_collection_status"]),
        }
        for r in rows
    ]
    return write_dicts_to_csv(
        venues_csv_path,
        out_rows,
        fieldnames=["venue_id", "venue", "county", "courts_listed", "platform", "public_booking_url", "access_status"],
    )


def export_inventory_and_coverage_excel(db: PadelDatabase, output_dir: Path, data_dir: Path) -> dict[str, Path]:
    """Produce Deliverable 1, 2, 7 (normalized metadata), and 10 in Excel + CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    norm_dir = data_dir / "normalized"
    norm_dir.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        clubs = [dict(r) for r in conn.execute("SELECT * FROM clubs ORDER BY in_county_dublin DESC, verified_padel_courts DESC, club_name").fetchall()]
        platforms = [dict(r) for r in conn.execute("SELECT * FROM booking_platforms ORDER BY priority_rank").fetchall()]
        courts = [dict(r) for r in conn.execute("SELECT * FROM courts ORDER BY club_id, sport, court_name").fetchall()]
        hours = [dict(r) for r in conn.execute("SELECT * FROM club_opening_hours ORDER BY club_id, day_of_week").fetchall()]
        events = [dict(r) for r in conn.execute("SELECT * FROM public_club_events ORDER BY club_id, start_time").fetchall()]
        runs = [dict(r) for r in conn.execute("SELECT * FROM collection_runs ORDER BY started_at").fetchall()]

    matrix = build_platform_coverage_matrix(clubs, platforms)

    # Write normalized & summary CSVs
    paths: dict[str, Path] = {}
    paths["platform_coverage_csv"] = write_dicts_to_csv(output_dir / "platform_coverage_matrix.csv", matrix)
    paths["verified_clubs_csv"] = write_dicts_to_csv(output_dir / "verified_clubs_all.csv", clubs)
    paths["live_courts_csv"] = write_dicts_to_csv(norm_dir / "live_playtomic_courts.csv", courts)
    paths["live_hours_csv"] = write_dicts_to_csv(norm_dir / "live_playtomic_opening_hours.csv", hours)
    paths["live_events_csv"] = write_dicts_to_csv(norm_dir / "live_playtomic_events.csv", events)
    paths["collection_runs_csv"] = write_dicts_to_csv(norm_dir / "collection_runs_audit.csv", runs)

    wb = openpyxl.Workbook()

    # --- Sheet 1: Executive Summary ---
    ws_sum = wb.active
    ws_sum.title = "Executive Summary"
    ws_sum["A1"] = "Dublin Padel Market Intelligence — Verified Facility & Platform Audit (Nov 2026)"
    ws_sum["A1"].font = TITLE_FONT

    dub_base = [c for c in clubs if c["inventory_cohort"] == "baseline_24_dublin"]
    dub_new = [c for c in clubs if c["inventory_cohort"] == "newly_verified_dublin"]
    dub_active = dub_base + dub_new
    dub_planned = [c for c in clubs if c["inventory_cohort"] == "pipeline_planning_dublin"]
    border_ref = [c for c in clubs if c["inventory_cohort"] == "greater_dublin_border"]

    indoor_courts = sum(c["verified_padel_courts"] or 0 for c in dub_active if c["court_environment"] == "indoor")
    covered_courts = sum(c["verified_padel_courts"] or 0 for c in dub_active if c["court_environment"] == "covered_outdoor")
    outdoor_courts = sum(c["verified_padel_courts"] or 0 for c in dub_active if c["court_environment"] == "outdoor")
    pub_courts = sum(
        c["verified_padel_courts"] or 0
        for c in dub_active
        if c["access_model"] in ("public_pay_and_play", "hybrid_member_visitor")
    )
    mem_courts = sum(c["verified_padel_courts"] or 0 for c in dub_active if c["access_model"] == "members_only")
    live_meta_courts = sum(
        c["verified_padel_courts"] or 0
        for c in dub_active
        if c["automated_collection_status"] == "live_metadata_monitored_slots_need_api"
    )

    summary_kpis = [
        ("Starting Spreadsheet Baseline (County Dublin)", f"{len(dub_base)} clubs", f"{sum(c['preliminary_courts'] or 0 for c in dub_base)} preliminary courts", "Source: padel clubs.xlsx"),
        ("Verified Baseline Active Count (County Dublin)", f"{len(dub_base)} clubs", f"{sum(c['verified_padel_courts'] or 0 for c in dub_base)} verified padel courts", "76 doubles + 3 singles (Bective = 1 active + 1 planned; non-padel Pickleball/Squash filtered out)"),
        ("Newly Discovered County Dublin Venues", f"+{len(dub_new)} clubs", f"+{sum(c['verified_padel_courts'] or 0 for c in dub_new)} verified padel courts", "dlr Loughlinstown (3), Padel 100 Ring Commons (3), Padel 100 Balbriggan (2), Westmanstown (2), Castleknock LTC (2)"),
        ("Total Active County Dublin Supply", f"{len(dub_active)} clubs", f"{sum(c['verified_padel_courts'] or 0 for c in dub_active)} active padel courts", "88 doubles + 3 singles across County Dublin"),
        ("Environment Split (County Dublin Active)", f"{indoor_courts} indoor ({indoor_courts/91*100:.1f}%)", f"{covered_courts} covered-outdoor | {outdoor_courts} outdoor ({outdoor_courts/91*100:.1f}%)", "5 indoor venues (28 courts), 2 covered-outdoor venues (Druid Coolmine 4 + Druid Kimmage 3 = 7 courts), 22 outdoor venues (56 courts)"),
        ("Public / Hybrid vs Members-Only Supply", f"{pub_courts} public/hybrid courts ({pub_courts/91*100:.1f}%)", f"{mem_courts} members-only courts ({mem_courts/91*100:.1f}%)", "18 public/hybrid venues vs 11 private members-only venues"),
        ("Live API-Free Court & Activity Telemetry", "12 Playtomic clubs + 5 club websites", f"{live_meta_courts} County Dublin + 14 commuter courts", "Live-verified via robots.txt-permitted SSR (/clubs/<slug>, /competitions/tournaments, /academy/courses) with zero vendor API keys"),
        ("County Dublin Planning Pipeline", f"{len(dub_planned)} project", "10 planned outdoor courts", "Swords Pavilions Padel Hub (Fingal ref F26A/0624E)"),
        ("Greater Dublin Commuter-Belt Reference", f"{len(border_ref)} clubs", f"{sum(c['verified_padel_courts'] or 0 for c in border_ref)} active padel courts", "PadelZone Celbridge (8 indoor), Indoor Padel Navan (6 indoor), K Club (2), Naas LTC (2), Shoreline Greystones (2)"),
    ]
    _style_worksheet_table(
        ws_sum,
        ["Metric / Market Segment", "Club / Share Metric", "Court Capacity", "Verification Notes & Implications"],
        summary_kpis,
        start_row=3,
    )

    # --- Sheet 2: County Dublin Active (29) ---
    ws_dub = wb.create_sheet("County Dublin Active (29)")
    club_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("inventory_cohort", "Cohort"),
        ("area", "Area / District"),
        ("eircode", "Eircode"),
        ("preliminary_courts", "Prelim Courts"),
        ("verified_padel_courts", "Verified Padel Courts"),
        ("doubles_courts", "Doubles"),
        ("singles_courts", "Singles"),
        ("other_sport_courts", "Non-Padel Courts"),
        ("court_environment", "Environment"),
        ("platform_id", "Booking Platform"),
        ("platform_tenant_id", "Platform Tenant UUID"),
        ("public_booking_url", "Public Booking URL"),
        ("access_model", "Access Model"),
        ("unauthenticated_calendar_visible", "Public Web Listing (0/1)"),
        ("automated_collection_status", "Automated Collection Status"),
        ("off_peak_price_eur", "Off-Peak EUR/hr"),
        ("peak_price_eur", "Peak EUR/hr"),
        ("price_notes", "Pricing Details"),
        ("opening_hours_summary", "Opening Hours (Europe/Dublin)"),
        ("verification_confidence", "Confidence"),
        ("verification_sources", "Sources"),
        ("verification_notes", "Verification Notes"),
    ]
    _style_worksheet_table(
        ws_dub,
        [label for _, label in club_cols],
        [[c.get(k) for k, _ in club_cols] for c in dub_active],
    )

    # --- Sheet 3: Pipeline & Commuter Belt (6) ---
    ws_ext = wb.create_sheet("Pipeline & Commuter Belt (6)")
    ext_cols = [("county", "County")] + club_cols
    _style_worksheet_table(
        ws_ext,
        [label for _, label in ext_cols],
        [[c.get(k) for k, _ in ext_cols] for c in (dub_planned + border_ref)],
    )

    # --- Sheet 4: Live Verified Courts ---
    ws_crt = wb.create_sheet("Live Verified Courts")
    crt_cols = [
        ("club_id", "Club ID"),
        ("court_id", "Resource UUID / ID"),
        ("court_name", "Court Name"),
        ("sport", "Sport (PADEL vs Excluded)"),
        ("court_environment", "Environment"),
        ("court_size", "Size"),
        ("wall_type", "Wall Type"),
        ("source_origin", "Data Origin"),
        ("verified_at", "Verified Timestamp"),
    ]
    _style_worksheet_table(
        ws_crt,
        [label for _, label in crt_cols],
        [[c.get(k) for k, _ in crt_cols] for c in courts if c.get("source_origin") == "live_public"],
    )

    # --- Sheet 5: Platform Coverage Matrix ---
    ws_mat = wb.create_sheet("Platform Coverage Matrix")
    mat_cols = [
        ("priority_rank", "Priority Rank"),
        ("platform_name", "Platform Name"),
        ("platform_id", "Platform ID"),
        ("county_dublin_active_clubs", "Dublin Active Clubs"),
        ("county_dublin_active_courts", "Dublin Active Courts"),
        ("share_of_dublin_courts_pct", "Dublin Court Share (%)"),
        ("public_or_hybrid_courts_dublin", "Public/Hybrid Courts"),
        ("live_metadata_monitored_clubs", "Live Metadata Monitored Clubs"),
        ("greater_dublin_border_clubs", "Commuter-Belt Clubs"),
        ("greater_dublin_border_courts", "Commuter-Belt Courts"),
        ("official_api_status", "Official API Availability"),
        ("public_calendar_auth", "Public Calendar Auth Gate"),
        ("automated_slot_access", "Automated Slot Access"),
        ("robots_policy_summary", "robots.txt & Technical Findings"),
        ("recommended_approach", "Compliant Integration Strategy"),
    ]
    _style_worksheet_table(
        ws_mat,
        [label for _, label in mat_cols],
        [[m.get(k) for k, _ in mat_cols] for m in matrix],
    )

    # --- Sheet 6: Live Endpoint Test Log ---
    ws_runs = wb.create_sheet("Live Endpoint Test Log")
    run_cols = [
        ("run_id", "Run ID"),
        ("started_at", "Tested At (ISO-8601)"),
        ("adapter_name", "Adapter / Probe"),
        ("platform_id", "Platform"),
        ("club_id", "Club ID"),
        ("target_url", "Tested URL"),
        ("http_status", "HTTP Status"),
        ("robots_allowed", "robots.txt Allowed (1/0)"),
        ("auth_required", "Auth Required (1/0)"),
        ("run_status", "Outcome Status"),
        ("data_origin", "Data Origin"),
        ("records_written", "Records Extracted"),
        ("error_message", "Findings / Compliance Block Reason"),
    ]
    _style_worksheet_table(
        ws_runs,
        [label for _, label in run_cols],
        [[r.get(k) for k, _ in run_cols] for r in runs],
    )

    # --- Sheet 7: Development Roadmap ---
    ws_rd = wb.create_sheet("Development Roadmap")
    rd_cols = [
        ("phase", "Phase"),
        ("priority", "Priority"),
        ("target_platform_or_clubs", "Target Platform / Segment"),
        ("clubs_covered", "Clubs Covered"),
        ("technical_action", "Technical & Commercial Action"),
        ("compliance_status", "Compliance Posture"),
        ("expected_coverage_gain", "Expected Market Coverage Gain"),
    ]
    _style_worksheet_table(
        ws_rd,
        [label for _, label in rd_cols],
        [[r.get(k) for k, _ in rd_cols] for r in DEVELOPMENT_ROADMAP_ROWS],
    )

    inv_xlsx = output_dir / "dublin_padel_club_inventory.xlsx"
    wb.save(inv_xlsx)
    paths["inventory_xlsx"] = inv_xlsx
    return paths


def export_observations_and_occupancy_excel(
    *,
    db: PadelDatabase,
    observations: Sequence[AvailabilityObservation],
    breakdowns: dict[str, list[OccupancySummaryRow]],
    output_dir: Path,
    synthetic_csv_path: Path,
    dataset_label: str = "synthetic_validation",
) -> dict[str, Path]:
    """Produce Deliverable 7 (synthetic/normalized CSV) and Deliverable 9 (Excel export of observations & occupancy)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    synthetic_csv_path.parent.mkdir(parents=True, exist_ok=True)

    obs_dicts = [asdict(o) for o in observations]
    section6_cols = [
        "club_id",
        "court_id",
        "booking_platform",
        "target_date",
        "start_time",
        "end_time",
        "duration_minutes",
        "price_eur",
        "availability_status",
        "status_detail",
        "observed_at",
        "lead_time_hours",
        "snapshot_bucket",
        "source_url",
        "data_origin",
        "run_id",
    ]
    write_dicts_to_csv(synthetic_csv_path, obs_dicts, fieldnames=section6_cols)

    all_summary_rows: list[dict[str, Any]] = []
    for rows in breakdowns.values():
        for r in rows:
            all_summary_rows.append({"dataset_origin": dataset_label, **asdict(r)})
    occ_csv = output_dir / "occupancy_summary_all_dimensions.csv"
    write_dicts_to_csv(occ_csv, all_summary_rows)

    with db.connect() as conn:
        hours = [dict(r) for r in conn.execute("SELECT * FROM club_opening_hours ORDER BY club_id, day_of_week").fetchall()]
        events = [dict(r) for r in conn.execute("SELECT * FROM public_club_events ORDER BY club_id, start_time").fetchall()]

    wb = openpyxl.Workbook()

    # --- Sheet 1: Methodology & Disclaimers ---
    ws_m = wb.active
    ws_m.title = "Methodology & Disclaimers"
    ws_m["A1"] = "Occupancy & Gross Booking Value (GBV) Estimation Methodology — Critical Disclaimers"
    ws_m["A1"].font = TITLE_FONT

    disclaimers = [
        ("1. Data Provenance Banner", f"Active slot-observation dataset in this workbook has data_origin = '{dataset_label}'. Live-verified Playtomic opening hours (72 rows) and public events are live_public (captured 2026-11-06). Slot-level rows are EXPLICITLY LABELED synthetic_validation data generated over the real verified courts & hours of 9 Dublin Playtomic clubs because robots.txt Disallows /api and /*?*date= without official OAuth2 credentials."),
        ("2. Core Occupancy Formula", "Estimated Occupancy (%) = (Unavailable Bookable Court-Hours / Total Offered Bookable Court-Hours) × 100. Blocked hours (maintenance/coaching) and Unknown hours (HTTP failures/outages) are tracked separately and excluded from the bookable denominator."),
        ("3. Overlapping Duration Deduplication", "Platforms offer simultaneous 60-min and 90-min slots on 30-min boundaries (e.g. 18:00–19:00 [60m], 18:00–19:30 [90m], 18:30–19:30 [60m]). Our engine decomposes operating hours into non-overlapping 15-minute physical court slices in UTC/Europe/Dublin. A 15-min slice is 'available' if ANY covering duration is bookable, eliminating overlap double-counting."),
        ("4. Unavailable != Paid Booking", "A slot disappearing from public availability (available -> unavailable) is an occupancy proxy, NOT proof of paid retail revenue. Courts may be taken by club members on discounted/unlimited plans, internal matches, coaching, or unpaid holds."),
        ("5. Gross Booking Value (GBV) Proxy", "GBV carries forward the last observed 60-min equivalent price before a slice became unavailable. We report both Headline Retail GBV and a Conservative Member-Adjusted Low Estimate (0.82× accounting for ~15–20% Playtomic Gold/Platinum & club member discounts + off-peak bundles)."),
        ("6. Timezone & DST Handling", "All timestamps carry explicit ISO-8601 offsets and are normalized to Europe/Dublin (GMT +00:00 in winter / IST +01:00 in summer). Atomic slice stepping occurs in UTC so spring-forward and autumn-fallback DST transitions never skip or duplicate physical court-hours."),
    ]
    _style_worksheet_table(ws_m, ["Topic", "Methodology Rule / Transparency Note"], disclaimers, start_row=3)
    for row_i in range(4, 4 + len(disclaimers)):
        ws_m.cell(row=row_i, column=1).font = BOLD_FONT
        if row_i in (4, 7):
            ws_m.cell(row=row_i, column=2).fill = WARN_FILL

    # --- Sheet 2: Live Opening Hours ---
    ws_oh = wb.create_sheet("Live Opening Hours")
    oh_cols = [
        ("club_id", "Club ID"),
        ("day_of_week", "Day of Week"),
        ("opening_time", "Opening Time (Europe/Dublin)"),
        ("closing_time", "Closing Time (Europe/Dublin)"),
        ("closes_next_day", "Closes After Midnight (1/0)"),
        ("source_origin", "Data Origin"),
        ("updated_at", "Verified At"),
    ]
    _style_worksheet_table(ws_oh, [lbl for _, lbl in oh_cols], [[h.get(k) for k, _ in oh_cols] for h in hours])

    # --- Sheet 3: Live Public Events ---
    ws_ev = wb.create_sheet("Live Public Events")
    ev_cols = [
        ("club_id", "Club ID"),
        ("event_id", "Event UUID"),
        ("event_type", "Event Type"),
        ("title", "Title"),
        ("start_time", "Start Time (Europe/Dublin)"),
        ("source_url", "Source URL"),
        ("observed_at", "Observed At"),
    ]
    _style_worksheet_table(ws_ev, [lbl for _, lbl in ev_cols], [[e.get(k) for k, _ in ev_cols] for e in events])

    # --- Sheet 4: Observations Sample (up to 4,000 rows in Excel; 100% in CSV) ---
    ws_obs = wb.create_sheet("Observations Sample (4000)")
    sample_obs = obs_dicts[:4000]
    _style_worksheet_table(
        ws_obs,
        section6_cols,
        [[o.get(c) for c in section6_cols] for o in sample_obs],
    )

    occ_cols = [
        ("dimension_type", "Dimension"),
        ("dimension_key", "Segment / Key"),
        ("total_capacity_hours", "Total Capacity Hrs"),
        ("offered_bookable_hours", "Offered Bookable Hrs"),
        ("available_hours", "Final Available Hrs"),
        ("confirmed_booked_hours", "Confirmed Booked Hrs"),
        ("inferred_transition_unavail_hours", "Transition Unavail Hrs (Avail->Unavail)"),
        ("static_unavail_hours", "Static Unavail Hrs"),
        ("total_unavailable_bookable_hours", "Total Unavail Bookable Hrs"),
        ("blocked_hours", "Blocked Hrs"),
        ("unknown_hours", "Unknown Hrs"),
        ("estimated_occupancy_pct", "Est. Occupancy (%)"),
        ("transition_backed_occupancy_pct", "Transition-Backed Occ (%)"),
        ("avg_hourly_price_eur", "Avg Price (EUR/hr)"),
        ("est_gross_booking_value_eur", "Est. Retail GBV (EUR)"),
        ("est_gbv_member_adjusted_low_eur", "Member-Adjusted GBV Low (EUR)"),
        ("confidence_level", "Confidence Level"),
        ("confidence_score", "Confidence Score (0-100)"),
        ("confidence_notes", "Confidence Diagnostics"),
    ]

    def _add_occ_sheet(title: str, rows: Sequence[OccupancySummaryRow]) -> None:
        ws = wb.create_sheet(title)
        row_dicts = [asdict(r) for r in rows]
        _style_worksheet_table(
            ws,
            [lbl for _, lbl in occ_cols],
            [[rd.get(k) for k, _ in occ_cols] for rd in row_dicts],
        )

    _add_occ_sheet("Occupancy by Club", breakdowns["by_club"])
    _add_occ_sheet("Occupancy by Court", breakdowns["by_court"])
    _add_occ_sheet("Occupancy by Hour & Day", breakdowns["by_hour"] + breakdowns["by_day_of_week"])
    _add_occ_sheet(
        "Peak Weekend & LeadTime",
        breakdowns["by_weekday_weekend"]
        + breakdowns["by_peak_offpeak"]
        + breakdowns["by_indoor_outdoor"]
        + breakdowns["by_lead_time_capture"],
    )

    obs_xlsx = output_dir / "availability_observations_and_occupancy.xlsx"
    wb.save(obs_xlsx)
    return {
        "observations_xlsx": obs_xlsx,
        "synthetic_csv": synthetic_csv_path,
        "occupancy_summary_csv": occ_csv,
    }


def export_non_api_measurements_excel(
    *,
    db: PadelDatabase,
    velocity_pairs: Sequence[dict[str, Any]],
    output_dir: Path,
    data_dir: Path,
) -> dict[str, Path]:
    """Export the complete API-Free Court Activity, Sign-Up Velocity & Club Utilization Workbook + CSVs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    norm_dir = data_dir / "normalized"
    norm_dir.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        club_lookup = {
            r["club_id"]: dict(r)
            for r in conn.execute("SELECT * FROM clubs").fetchall()
        }
        measurements_raw = [
            dict(r)
            for r in conn.execute(
                """
                SELECT m.*, c.club_name, c.county, c.in_county_dublin, c.area,
                       c.court_environment, c.platform_id, c.access_model,
                       c.off_peak_price_eur, c.peak_price_eur
                FROM non_api_club_measurements m
                JOIN clubs c ON c.club_id = m.club_id
                ORDER BY c.in_county_dublin DESC, m.total_occupied_court_hours_point DESC, c.club_name ASC
                """
            ).fetchall()
        ]
        all_telemetry = [
            dict(r)
            for r in conn.execute(
                """
                SELECT t.*, c.club_name
                FROM activity_telemetry_snapshots t
                JOIN clubs c ON c.club_id = t.club_id
                ORDER BY t.observed_at DESC, t.club_id ASC, t.start_time ASC
                """
            ).fetchall()
        ]
        # Latest snapshot per (club_id, activity_id)
        latest_telemetry_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in all_telemetry:
            key = (row["club_id"], row["activity_id"])
            if key not in latest_telemetry_map:
                latest_telemetry_map[key] = row
        latest_telemetry = list(latest_telemetry_map.values())
        latest_telemetry.sort(
            key=lambda r: (
                r["club_id"],
                r["start_time"] or "9999",
                r["title"],
            )
        )

        memberships = [
            dict(r)
            for r in conn.execute(
                """
                SELECT o.*, c.club_name
                FROM club_membership_offers o
                JOIN clubs c ON c.club_id = o.club_id
                ORDER BY c.club_name ASC, o.offer_kind ASC, o.price_eur ASC
                """
            ).fetchall()
        ]

    live_obs = [asdict(o) for o in db.fetch_observations(data_origin="live_public")]

    # Enrich measurements with annualized GBV point
    measurements: list[dict[str, Any]] = []
    for m in measurements_raw:
        m_copy = dict(m)
        m_copy["est_annual_gbv_eur_point"] = round(float(m["est_weekly_gbv_eur_point"]) * 52.0, 0)
        measurements.append(m_copy)

    vel_enriched: list[dict[str, Any]] = []
    for v in sorted(
        velocity_pairs,
        key=lambda x: (int(x.get("delta_players") or 0), float(x.get("fill_pct_t2") or 0.0)),
        reverse=True,
    ):
        vc = dict(v)
        vc["club_name"] = club_lookup.get(v["club_id"], {}).get("club_name", v["club_id"])
        vel_enriched.append(vc)

    # Write CSV deliverables
    paths: dict[str, Path] = {}
    paths["non_api_measurements_csv"] = write_dicts_to_csv(
        output_dir / "non_api_club_measurements.csv", measurements
    )
    paths["live_activity_telemetry_csv"] = write_dicts_to_csv(
        output_dir / "live_activity_telemetry.csv", latest_telemetry
    )
    paths["live_velocity_csv"] = write_dicts_to_csv(
        output_dir / "live_24h_signup_velocity.csv", vel_enriched
    )
    paths["live_memberships_csv"] = write_dicts_to_csv(
        output_dir / "live_club_memberships.csv", memberships
    )
    paths["live_public_observations_csv"] = write_dicts_to_csv(
        norm_dir / "live_public_court_observations.csv", live_obs
    )

    wb = openpyxl.Workbook()

    # --- Sheet 1: Executive Summary (No-API) ---
    ws_sum = wb.active
    ws_sum.title = "Executive Summary (No-API)"
    ws_sum["A1"] = "Dublin Padel API-Free Court Measurement & Demand Telemetry Report (Nov 2026)"
    ws_sum["A1"].font = TITLE_FONT

    dub_m = [m for m in measurements if int(m["in_county_dublin"]) == 1]
    bor_m = [m for m in measurements if int(m["in_county_dublin"]) == 0]

    dub_courts = sum(int(m["verified_padel_courts"]) for m in dub_m)
    dub_cap = sum(float(m["weekly_capacity_court_hours"]) for m in dub_m)
    dub_org = sum(float(m["direct_organised_court_hours"]) for m in dub_m)
    dub_occ_low = sum(float(m["total_occupied_court_hours_low"]) for m in dub_m)
    dub_occ_pt = sum(float(m["total_occupied_court_hours_point"]) for m in dub_m)
    dub_occ_hi = sum(float(m["total_occupied_court_hours_high"]) for m in dub_m)
    dub_gbv_low = sum(float(m["est_weekly_gbv_eur_low"]) for m in dub_m)
    dub_gbv_pt = sum(float(m["est_weekly_gbv_eur_point"]) for m in dub_m)
    dub_gbv_hi = sum(float(m["est_weekly_gbv_eur_high"]) for m in dub_m)

    bor_courts = sum(int(m["verified_padel_courts"]) for m in bor_m)
    bor_cap = sum(float(m["weekly_capacity_court_hours"]) for m in bor_m)
    bor_occ_pt = sum(float(m["total_occupied_court_hours_point"]) for m in bor_m)
    bor_gbv_pt = sum(float(m["est_weekly_gbv_eur_point"]) for m in bor_m)

    tournaments_latest = [a for a in latest_telemetry if a["activity_category"] == "tournament"]
    classes_latest = [a for a in latest_telemetry if a["activity_category"] == "public_class"]
    courses_latest = [a for a in latest_telemetry if a["activity_category"] == "academy_course"]

    tot_reg_players = sum(int(a["registered_players"] or 0) for a in latest_telemetry)
    tot_max_players = sum(int(a["max_players"] or 0) for a in latest_telemetry)
    tot_obs_act_rev = sum(float(a["observed_gross_revenue_eur"] or 0.0) for a in latest_telemetry)
    net_24h_signups = sum(int(v["delta_players"]) for v in vel_enriched)
    pos_24h_events = sum(1 for v in vel_enriched if int(v["delta_players"]) > 0)

    kpis = [
        (
            "Zero-API Collection Architecture",
            "100% API-Free & robots.txt Compliant",
            f"{len(all_telemetry)} total snapshot cards ({len(latest_telemetry)} unique live activities)",
            "Harvested strictly from permitted SSR pages (/clubs/<slug>, /competitions/tournaments, /academy/courses) + 5 club websites without any OAuth2 API key",
        ),
        (
            "County Dublin Public/Hybrid Scope",
            f"{len(dub_m)} active venues ({dub_courts} padel courts)",
            f"{dub_cap:,.1f} offered court-hours / week",
            "Covers 100% of public pay-and-play & hybrid venues in County Dublin (11 private members-only LTCs/gyms excluded from commercial GBV)",
        ),
        (
            "Live Organised Activities Harvested",
            f"{len(tournaments_latest)} tournaments | {len(classes_latest)} public classes | {len(courses_latest)} courses",
            f"{dub_org:,.1f} direct organised court-hrs/wk (reference week)",
            f"Directly observed {tot_reg_players:,} / {tot_max_players:,} registered player spots ({100.0*tot_reg_players/max(tot_max_players,1):.1f}% aggregate fill across all horizons) & EUR {tot_obs_act_rev:,.2f} in gross sign-up fees",
        ),
        (
            "Real 24-Hour Sign-Up Velocity (T1 -> T2)",
            f"{len(vel_enriched)} tracked multi-snapshot activities",
            f"Net +{net_24h_signups} player registrations ({pos_24h_events} events grew overnight)",
            "Measured across live captures on 2026-11-06T22:21Z (T1) vs 2026-11-07T23:28Z (T2) (~25 hours elapsed)",
        ),
        (
            "Live Court-Level Slot Projections",
            f"{len(live_obs)} real live_public court observations",
            "Stored in availability_observations",
            "Dated tournaments & public classes mapped onto verified physical club courts (confirmed_booking when >=4 players registered)",
        ),
        (
            "County Dublin Court Utilization (No-API)",
            f"Point: {100.0*dub_occ_pt/max(dub_cap,1):.1f}%",
            f"Range: [{100.0*dub_occ_low/max(dub_cap,1):.1f}% – {100.0*dub_occ_hi/max(dub_cap,1):.1f}%]",
            f"{dub_occ_pt:,.1f} occupied court-hrs/wk [Low {dub_occ_low:,.1f}h – High {dub_occ_hi:,.1f}h] across {dub_courts} courts",
        ),
        (
            "County Dublin Estimated Weekly GBV",
            f"EUR {dub_gbv_pt:,.0f} / week",
            f"Range: [EUR {dub_gbv_low:,.0f} – EUR {dub_gbv_hi:,.0f} / week]",
            f"Annualized run-rate ~EUR {dub_gbv_pt*52:,.0f}/yr (member-discount & free-off-peak adjusted; NOT audited net club revenue)",
        ),
        (
            "Greater Dublin Commuter-Belt (4 Venues)",
            f"{bor_courts} courts ({bor_cap:,.1f} court-hrs/wk)",
            f"Point Utilization: {100.0*bor_occ_pt/max(bor_cap,1):.1f}% ({bor_occ_pt:,.1f}h/wk)",
            f"PadelZone Celbridge (8 indoor), Indoor Padel Navan (6 indoor), K Club (2), Shoreline Greystones (2) — Est. GBV EUR {bor_gbv_pt:,.0f}/wk",
        ),
        (
            "Human-in-the-Loop Zero-API Extensions",
            "Browser Dropzone + Stratified Spot Checks",
            "data/inbox/ & field_spot_checks table",
            "Drop Ctrl+S / Ctrl+C calendar pages into data/inbox/ or log 5-min phone/visual checks via `monitor.py log-spot-check` to narrow CI bands to +/-3%",
        ),
    ]
    _style_worksheet_table(
        ws_sum,
        ["Measurement Dimension", "Headline Result", "Interval / Volume Metric", "Methodology & Provenance Notes"],
        kpis,
        start_row=3,
    )

    # --- Sheet 2: Club Utilization & GBV (22) ---
    ws_clubs = wb.create_sheet("Club Utilization & GBV (22)")
    m_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("county", "County"),
        ("in_county_dublin", "In Co. Dublin (1/0)"),
        ("court_environment", "Environment"),
        ("platform_id", "Platform"),
        ("verified_padel_courts", "Padel Courts"),
        ("weekly_capacity_court_hours", "Weekly Capacity Hrs"),
        ("direct_organised_court_hours", "Direct Organised Hrs/Wk"),
        ("live_activities_count", "Live Activities"),
        ("live_registered_players", "Live Reg. Players"),
        ("live_max_player_spots", "Live Max Spots"),
        ("live_activity_fill_rate_pct", "Near-Term Activity Fill (%)"),
        ("player_signup_24h_delta", "24h Sign-Up Delta"),
        ("verified_activity_revenue_eur", "Observed Activity Fees (EUR)"),
        ("spot_check_samples", "Spot Checks"),
        ("estimated_casual_court_hours_point", "Est. Casual Hrs/Wk (Point)"),
        ("total_occupied_court_hours_low", "Occupied Hrs Low"),
        ("total_occupied_court_hours_point", "Occupied Hrs Point"),
        ("total_occupied_court_hours_high", "Occupied Hrs High"),
        ("estimated_occupancy_pct_low", "Occupancy Low (%)"),
        ("estimated_occupancy_pct_point", "Occupancy Point (%)"),
        ("estimated_occupancy_pct_high", "Occupancy High (%)"),
        ("peak_occupancy_pct_point", "Peak Occ (%)"),
        ("offpeak_occupancy_pct_point", "Off-Peak Occ (%)"),
        ("est_weekly_gbv_eur_low", "Weekly GBV Low (EUR)"),
        ("est_weekly_gbv_eur_point", "Weekly GBV Point (EUR)"),
        ("est_weekly_gbv_eur_high", "Weekly GBV High (EUR)"),
        ("est_annual_gbv_eur_point", "Annualized GBV Point (EUR)"),
        ("measurement_tier", "Measurement Tier"),
        ("confidence_level", "Confidence"),
        ("confidence_score", "Score (0-100)"),
        ("methodology_notes", "Signal Breakdown"),
    ]
    _style_worksheet_table(
        ws_clubs,
        [lbl for _, lbl in m_cols],
        [[m.get(k) for k, _ in m_cols] for m in measurements],
    )

    # --- Sheet 3: Live Tournaments & Classes ---
    ws_tc = wb.create_sheet("Live Tournaments & Classes")
    dated_acts = [
        a for a in latest_telemetry if a["activity_category"] in ("tournament", "public_class")
    ]
    tc_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("activity_category", "Category"),
        ("title", "Activity Title"),
        ("start_time", "Start Time (Europe/Dublin)"),
        ("end_time", "End Time (Europe/Dublin)"),
        ("duration_minutes", "Duration (min)"),
        ("skill_band", "Skill Level"),
        ("coach_name", "Coach"),
        ("registered_players", "Registered Players"),
        ("max_players", "Max Players"),
        ("fill_rate_pct", "Fill Rate (%)"),
        ("courts_occupied", "Est. Courts Used"),
        ("court_hours_occupied", "Court-Hours Occupied"),
        ("price_per_player_eur", "Price / Player (EUR)"),
        ("observed_gross_revenue_eur", "Observed Gross Fees (EUR)"),
        ("full_capacity_revenue_eur", "Full Capacity Fees (EUR)"),
        ("observed_at", "Observed At"),
        ("source_url", "Permitted Public URL"),
    ]
    _style_worksheet_table(
        ws_tc,
        [lbl for _, lbl in tc_cols],
        [[a.get(k) for k, _ in tc_cols] for a in dated_acts],
    )

    # --- Sheet 4: 24h Sign-Up Velocity ---
    ws_vel = wb.create_sheet("24h Sign-Up Velocity")
    vel_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("activity_category", "Category"),
        ("title", "Activity Title"),
        ("start_time", "Event Start (Europe/Dublin)"),
        ("snapshot_1_at", "Snapshot T1"),
        ("snapshot_2_at", "Snapshot T2"),
        ("elapsed_hours", "Elapsed Hours"),
        ("players_t1", "Players T1"),
        ("players_t2", "Players T2"),
        ("max_players", "Max Players"),
        ("delta_players", "24h Net Player Delta"),
        ("fill_pct_t1", "Fill T1 (%)"),
        ("fill_pct_t2", "Fill T2 (%)"),
        ("delta_gross_revenue_eur", "24h Gross Fee Delta (EUR)"),
    ]
    _style_worksheet_table(
        ws_vel,
        [lbl for _, lbl in vel_cols],
        [[v.get(k) for k, _ in vel_cols] for v in vel_enriched],
    )

    # --- Sheet 5: Live Academy Courses ---
    ws_crs = wb.create_sheet("Live Academy Courses")
    crs_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("title", "Course Title"),
        ("course_span_text", "Course Date Span"),
        ("duration_minutes", "Session Duration (min)"),
        ("skill_band", "Skill Level"),
        ("coach_name", "Coach"),
        ("price_per_player_eur", "Course Fee (EUR)"),
        ("observed_at", "Observed At"),
        ("source_url", "Permitted Public URL"),
    ]
    _style_worksheet_table(
        ws_crs,
        [lbl for _, lbl in crs_cols],
        [[a.get(k) for k, _ in crs_cols] for a in courses_latest],
    )

    # --- Sheet 6: Live Memberships & Offers ---
    ws_mem = wb.create_sheet("Live Memberships & Offers")
    mem_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("offer_kind", "Offer Kind"),
        ("title", "Membership / Bundle Name"),
        ("price_eur", "Price (EUR)"),
        ("billing_period", "Billing Period"),
        ("court_discount_pct", "Court Discount (%)"),
        ("advance_booking_days", "Advance Booking Window (Days)"),
        ("off_peak_unlimited", "Unlimited Off-Peak Courts (1/0)"),
        ("description", "Benefits & Terms"),
        ("observed_at", "Observed At"),
    ]
    _style_worksheet_table(
        ws_mem,
        [lbl for _, lbl in mem_cols],
        [[m.get(k) for k, _ in mem_cols] for m in memberships],
    )

    # --- Sheet 7: Live Court Slot Projections ---
    ws_proj = wb.create_sheet("Live Court Slot Projections")
    proj_cols = [
        "club_id",
        "court_id",
        "booking_platform",
        "target_date",
        "start_time",
        "end_time",
        "duration_minutes",
        "price_eur",
        "availability_status",
        "status_detail",
        "observed_at",
        "lead_time_hours",
        "snapshot_bucket",
        "source_url",
        "data_origin",
    ]
    _style_worksheet_table(
        ws_proj,
        proj_cols,
        [[o.get(c) for c in proj_cols] for o in live_obs[:4000]],
    )

    non_api_xlsx = output_dir / "dublin_padel_non_api_court_measurements.xlsx"
    wb.save(non_api_xlsx)
    paths["non_api_xlsx"] = non_api_xlsx
    return paths


def export_field_spot_check_template_excel(db: PadelDatabase, output_dir: Path) -> Path:
    """Create a structured Field / Telephone / Visual Spot-Check Workbook for non-API clubs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        existing = [
            dict(r)
            for r in conn.execute("SELECT * FROM field_spot_checks ORDER BY observed_at DESC").fetchall()
        ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spot Check Entry Sheet"
    ws["A1"] = "Dublin Padel Zero-API Stratified Spot-Check Log (Padel Dome, PSLC, Bushy Park, dlr, Padel 100)"
    ws["A1"].font = TITLE_FONT

    headers = [
        "club_id",
        "target_slot_start (ISO Europe/Dublin)",
        "target_slot_end (ISO Europe/Dublin)",
        "time_stratum (auto)",
        "total_courts_checked",
        "occupied_courts",
        "blocked_courts",
        "displayed_price_eur",
        "collection_method",
        "observer_notes",
    ]
    sample_rows: list[list[Any]] = []
    if existing:
        for r in existing:
            sample_rows.append(
                [
                    r["club_id"],
                    r["target_slot_start"],
                    r["target_slot_end"],
                    r["time_stratum"],
                    r["total_courts_checked"],
                    r["occupied_courts"],
                    r["blocked_courts"],
                    r["displayed_price_eur"],
                    r["collection_method"],
                    r["observer_notes"],
                ]
            )
    else:
        # Recommended stratified sampling schedule (non-Playtomic priority clubs)
        sample_rows = [
            ["padel_dome", "2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", "weekday_peak", 9, None, 0, 40.0, "telephone_enquiry", "TEMPLATE: Ask how many indoor courts free Tue 7pm"],
            ["padel_dome", "2026-11-11T11:00:00+00:00", "2026-11-11T12:00:00+00:00", "weekday_offpeak", 9, None, 0, 30.0, "telephone_enquiry", "TEMPLATE: Ask how many indoor courts free Wed 11am"],
            ["padel_dome", "2026-11-14T11:00:00+00:00", "2026-11-14T12:00:00+00:00", "weekend_prime", 9, None, 0, 40.0, "on_site_headcount", "TEMPLATE: Weekend prime visual headcount"],
            ["portmarnock", "2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", "weekday_peak", 5, None, 0, 36.0, "browser_visual_check", "TEMPLATE: GladstoneGo visual check"],
            ["bushy_park", "2026-11-12T19:00:00+00:00", "2026-11-12T20:00:00+00:00", "weekday_peak", 4, None, 0, 27.0, "browser_visual_check", "TEMPLATE: Playbypoint browser check"],
            ["meadowbrook", "2026-11-14T14:00:00+00:00", "2026-11-14T15:00:00+00:00", "weekend_prime", 2, None, 0, 30.0, "telephone_enquiry", "TEMPLATE: dlr Leisure desk check"],
            ["dlr_loughlinstown", "2026-11-14T15:00:00+00:00", "2026-11-14T16:00:00+00:00", "weekend_prime", 3, None, 0, 30.0, "telephone_enquiry", "TEMPLATE: dlr Loughlinstown desk check"],
            ["padel100_ring_commons", "2026-11-11T13:00:00+00:00", "2026-11-11T14:00:00+00:00", "weekday_offpeak", 3, None, 0, 0.0, "app_visual_check", "TEMPLATE: Free Play 11:00-15:00 window check"],
        ]
    _style_worksheet_table(ws, headers, sample_rows, start_row=3)

    ws_guide = wb.create_sheet("Sampling Protocol & CLI")
    guide_rows = [
        ("1. Why Stratified Spot Checks?", "Clubs like Padel Dome (Matchpoint login wall), Bushy Park (Cloudflare 403), Portmarnock PSLC (GladstoneGo SPA), and dlr Leisure (phone/enquiry) block bots. Just 2 spot checks per stratum (8 checks/week total) shrink occupancy 95% confidence bounds to +/- 6-8 percentage points without any API!"),
        ("2. Four Canonical Dublin Strata", "weekday_offpeak (Mon-Fri open-17:00, weight 0.45) | weekday_peak (Mon-Fri 17:00-22:30, weight 0.28) | weekend_prime (Sat-Sun 09:00-20:00, weight 0.22) | weekend_offpeak (Sat-Sun <09:00 & >=20:00, weight 0.05)."),
        ("3. One-Command CLI Logging", "python monitor.py log-spot-check --club padel_dome --start 2026-11-10T19:00:00+00:00 --end 2026-11-10T20:00:00+00:00 --total 9 --occupied 8 --price 40 --method telephone_enquiry"),
        ("4. Browser Dropzone Alternative", "Open any club booking calendar in your normal browser, press Ctrl+S or copy-paste visible text into data/inbox/<club_id>__YYYY-MM-DD__<tag>.txt (or fill weekly bundles in data/inbox/week_YYYY-MM-DD/), and run: python monitor.py analyze-week --week-start 2026-11-09"),
    ]
    _style_worksheet_table(ws_guide, ["Step", "Instructions"], guide_rows, start_row=1)

    out_path = output_dir / "field_spot_check_template.xlsx"
    wb.save(out_path)
    return out_path


HEAT_PEAK_FILL = PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid")
HEAT_HIGH_FILL = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
HEAT_MOD_FILL = PatternFill(start_color="DEF7EC", end_color="DEF7EC", fill_type="solid")
HEAT_LOW_FILL = PatternFill(start_color="E1EFFE", end_color="E1EFFE", fill_type="solid")
HEAT_CLOSED_FILL = PatternFill(start_color="EDF2F7", end_color="EDF2F7", fill_type="solid")

HEAT_PEAK_FONT = Font(name="Calibri", size=10, bold=True, color="9B1C1C")
HEAT_HIGH_FONT = Font(name="Calibri", size=10, bold=True, color="92400E")
HEAT_MOD_FONT = Font(name="Calibri", size=10, color="03543F")
HEAT_LOW_FONT = Font(name="Calibri", size=10, color="1E429F")
HEAT_CLOSED_FONT = Font(name="Calibri", size=9, italic=True, color="718096")


def _apply_heatmap_cell_style(cell, pct_val: float | None) -> None:
    cell.border = THIN_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center")
    if pct_val is None:
        cell.value = "Closed"
        cell.fill = HEAT_CLOSED_FILL
        cell.font = HEAT_CLOSED_FONT
        return
    cell.value = round(float(pct_val), 1)
    cell.number_format = "0.0"
    if pct_val >= 80.0:
        cell.fill = HEAT_PEAK_FILL
        cell.font = HEAT_PEAK_FONT
    elif pct_val >= 60.0:
        cell.fill = HEAT_HIGH_FILL
        cell.font = HEAT_HIGH_FONT
    elif pct_val >= 35.0:
        cell.fill = HEAT_MOD_FILL
        cell.font = HEAT_MOD_FONT
    else:
        cell.fill = HEAT_LOW_FILL
        cell.font = HEAT_LOW_FONT


def export_weekly_peak_offpeak_heatmap_excel(
    *,
    week_start_iso: str,
    week_end_iso: str,
    dataset_origin: str,
    club_hour_matrix: Sequence[dict[str, Any]],
    club_daytype_hour_matrix: Sequence[dict[str, Any]],
    empirical_bands: Sequence[dict[str, Any]],
    progression_rows: Sequence[dict[str, Any]],
    live_organised_grid: Sequence[dict[str, Any]],
    schedule_rows: Sequence[dict[str, Any]],
    output_dir: Path,
    hours_grid: Sequence[int] = tuple(range(6, 24)),
) -> dict[str, Path]:
    """Export the 6-tab Color-Coded Weekly Club x Hour Peak/Off-Peak Heatmap Workbook + CSVs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    hour_cols = [f"{h:02d}:00" for h in hours_grid]

    # Write CSV exports
    matrix_csv = write_dicts_to_csv(
        output_dir / "club_hourly_occupancy_matrix.csv",
        [{"dataset_origin": dataset_origin, "week_start": week_start_iso, **r} for r in club_hour_matrix],
    )
    bands_csv = write_dicts_to_csv(
        output_dir / "club_empirical_peak_offpeak_bands.csv",
        [{"dataset_origin": dataset_origin, "week_start": week_start_iso, **r} for r in empirical_bands],
    )
    prog_csv = write_dicts_to_csv(
        output_dir / "snapshot_leadtime_progression.csv",
        [{"dataset_origin": dataset_origin, "week_start": week_start_iso, **r} for r in progression_rows],
    )
    sched_csv = write_dicts_to_csv(
        output_dir / "next_week_snapshot_schedule.csv",
        schedule_rows,
    )

    wb = openpyxl.Workbook()
    legend_text = (
        f"Target Week: {week_start_iso} to {week_end_iso} (Europe/Dublin)  |  "
        f"Dataset Origin: {dataset_origin}  |  "
        "Heatmap Key (% Occupied): Coral >=80% (Peak) | Amber 60-79% (High) | Green 35-59% (Moderate) | Blue <35% (Off-Peak) | Grey = Closed"
    )

    # --- Sheet 1: Club x Hour Heatmap (06-23) ---
    ws_hm = wb.active
    ws_hm.title = "Club x Hour Heatmap (06-23)"
    ws_hm["A1"] = f"Dublin Padel — Weekly Club × Hour Occupancy (%) Heatmap ({week_start_iso} to {week_end_iso})"
    ws_hm["A1"].font = TITLE_FONT
    ws_hm["A2"] = legend_text
    ws_hm["A2"].font = BOLD_FONT
    ws_hm["A2"].fill = WARN_FILL if "synthetic" in dataset_origin.lower() else SUBHEADER_FILL

    hm_meta_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("environment", "Environment"),
        ("padel_courts", "Courts"),
        ("offered_hours", "Offered Hrs"),
        ("overall_occ_pct", "Overall (%)"),
        ("std_peak_occ_pct", "Std Peak (%)"),
        ("std_offpeak_occ_pct", "Std Off-Peak (%)"),
    ]
    hm_headers = [lbl for _, lbl in hm_meta_cols] + hour_cols
    hm_rows = [
        [r.get(k) for k, _ in hm_meta_cols] + [r.get(hc) for hc in hour_cols]
        for r in club_hour_matrix
    ]
    _style_worksheet_table(ws_hm, hm_headers, hm_rows, start_row=4)

    first_hr_col = len(hm_meta_cols) + 1
    for r_offset, row_dict in enumerate(club_hour_matrix, start=1):
        excel_r = 4 + r_offset
        for c_off, hc in enumerate(hour_cols):
            cell = ws_hm.cell(row=excel_r, column=first_hr_col + c_off)
            _apply_heatmap_cell_style(cell, row_dict.get(hc))
    for c_off in range(len(hour_cols)):
        ws_hm.column_dimensions[get_column_letter(first_hr_col + c_off)].width = 7.8

    # --- Sheet 2: Weekday vs Weekend Heatmap ---
    ws_dt = wb.create_sheet("Weekday vs Weekend Heatmap")
    ws_dt["A1"] = f"Weekday (Mon–Fri) vs Weekend (Sat–Sun) Hourly Occupancy (%) Heatmap ({week_start_iso} to {week_end_iso})"
    ws_dt["A1"].font = TITLE_FONT
    ws_dt["A2"] = legend_text
    ws_dt["A2"].font = BOLD_FONT
    ws_dt["A2"].fill = WARN_FILL if "synthetic" in dataset_origin.lower() else SUBHEADER_FILL

    dt_meta_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("day_segment", "Day Segment"),
        ("offered_hours", "Offered Hrs"),
        ("segment_occ_pct", "Segment Occ (%)"),
    ]
    dt_headers = [lbl for _, lbl in dt_meta_cols] + hour_cols
    dt_rows = [
        [r.get(k) for k, _ in dt_meta_cols] + [r.get(hc) for hc in hour_cols]
        for r in club_daytype_hour_matrix
    ]
    _style_worksheet_table(ws_dt, dt_headers, dt_rows, start_row=4)

    dt_first_hr_col = len(dt_meta_cols) + 1
    for r_offset, row_dict in enumerate(club_daytype_hour_matrix, start=1):
        excel_r = 4 + r_offset
        for c_off, hc in enumerate(hour_cols):
            cell = ws_dt.cell(row=excel_r, column=dt_first_hr_col + c_off)
            _apply_heatmap_cell_style(cell, row_dict.get(hc))
    for c_off in range(len(hour_cols)):
        ws_dt.column_dimensions[get_column_letter(dt_first_hr_col + c_off)].width = 7.8

    # --- Sheet 3: Empirical Peak & Off-Peak ---
    ws_emp = wb.create_sheet("Empirical Peak & Off-Peak")
    ws_emp["A1"] = f"Discovered Empirical Peak, Shoulder & Off-Peak Windows by Club ({dataset_origin})"
    ws_emp["A1"].font = TITLE_FONT
    emp_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("environment", "Environment"),
        ("padel_courts", "Padel Courts"),
        ("empirical_peak_windows", "Empirical Peak Hours (Europe/Dublin)"),
        ("empirical_shoulder_windows", "Empirical Shoulder Hours"),
        ("empirical_offpeak_windows", "Empirical Off-Peak Hours"),
        ("busiest_hour", "Single Busiest Hour"),
        ("quietest_hour", "Single Quietest Hour"),
        ("empirical_peak_occ_pct", "Peak Band Occ (%)"),
        ("empirical_offpeak_occ_pct", "Off-Peak Band Occ (%)"),
        ("peak_vs_offpeak_spread_pp", "Peak - Off-Peak Spread (pp)"),
        ("avg_peak_price_eur", "Avg Peak Price (EUR/hr)"),
        ("avg_offpeak_price_eur", "Avg Off-Peak Price (EUR/hr)"),
    ]
    _style_worksheet_table(
        ws_emp,
        [lbl for _, lbl in emp_cols],
        [[r.get(k) for k, _ in emp_cols] for r in empirical_bands],
        start_row=3,
    )

    # --- Sheet 4: 5-Snapshot Progression ---
    ws_prog = wb.create_sheet("5-Snapshot Progression")
    ws_prog["A1"] = f"Cumulative Booking Progression Across Multi-Snapshot Lead-Time Passes ({dataset_origin})"
    ws_prog["A1"].font = TITLE_FONT
    prog_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("snapshot_stage", "Cumulative Snapshot Stage"),
        ("overall_occupancy_pct", "Cumulative Overall Occ (%)"),
        ("incremental_gain_pp", "Stage Gain (+pp)"),
        ("peak_occupancy_pct", "Cumulative Peak Occ (%)"),
        ("offpeak_occupancy_pct", "Cumulative Off-Peak Occ (%)"),
    ]
    _style_worksheet_table(
        ws_prog,
        [lbl for _, lbl in prog_cols],
        [[r.get(k) for k, _ in prog_cols] for r in progression_rows],
        start_row=3,
    )

    # --- Sheet 5: Live Next-Week Organised ---
    ws_org = wb.create_sheet("Live Next-Week Organised")
    ws_org["A1"] = (
        f"LIVE PUBLIC Organised Court-Hours Already Scheduled ({week_start_iso} to {week_end_iso}) — "
        "Tournaments & Public Classes (data_origin = 'live_public')"
    )
    ws_org["A1"].font = TITLE_FONT
    org_meta_cols = [
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("padel_courts", "Padel Courts"),
        ("next_week_live_events", "Live Events Next Week"),
        ("registered_players_so_far", "Registered Players So Far"),
        ("total_organised_court_hours", "Organised Court-Hrs"),
    ]
    org_headers = [lbl for _, lbl in org_meta_cols] + hour_cols
    org_rows = [
        [r.get(k) for k, _ in org_meta_cols] + [r.get(hc) for hc in hour_cols]
        for r in live_organised_grid
    ]
    _style_worksheet_table(ws_org, org_headers, org_rows, start_row=3)
    org_first_hr = len(org_meta_cols) + 1
    for r_offset, row_dict in enumerate(live_organised_grid, start=1):
        excel_r = 3 + r_offset
        for c_off, hc in enumerate(hour_cols):
            val = row_dict.get(hc)
            cell = ws_org.cell(row=excel_r, column=org_first_hr + c_off)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if val is not None and val > 0:
                cell.fill = HEAT_HIGH_FILL if val >= 3.0 else HEAT_MOD_FILL
                cell.font = HEAT_HIGH_FONT if val >= 3.0 else HEAT_MOD_FONT
    for c_off in range(len(hour_cols)):
        ws_org.column_dimensions[get_column_letter(org_first_hr + c_off)].width = 7.8

    # --- Sheet 6: 5-Snapshot Gathering Plan ---
    ws_plan = wb.create_sheet("5-Snapshot Gathering Plan")
    ws_plan["A1"] = f"Multi-Snapshot Next-Week Data Gathering Schedule ({week_start_iso} to {week_end_iso})"
    ws_plan["A1"].font = TITLE_FONT
    plan_cols = [
        ("capture_day", "Capture When (Europe/Dublin)"),
        ("priority_group", "Cohort"),
        ("club_id", "Club ID"),
        ("club_name", "Club Name"),
        ("verified_courts", "Courts"),
        ("platform_id", "Platform"),
        ("target_date", "Play Date"),
        ("target_day_of_week", "Day"),
        ("snapshot_pass_tag", "Pass Tag"),
        ("canonical_bucket", "Lead Bucket"),
        ("nominal_lead_hours", "Nominal Lead Hrs"),
        ("bundle_dropzone_file", "Weekly Bundle File (Recommended)"),
        ("public_booking_url", "Public Booking URL"),
        ("what_it_measures", "What This Snapshot Measures"),
    ]
    _style_worksheet_table(
        ws_plan,
        [lbl for _, lbl in plan_cols],
        [[r.get(k) for k, _ in plan_cols] for r in schedule_rows],
        start_row=3,
    )

    heatmap_xlsx = output_dir / "weekly_peak_offpeak_club_heatmap.xlsx"
    wb.save(heatmap_xlsx)
    return {
        "weekly_heatmap_xlsx": heatmap_xlsx,
        "club_hourly_matrix_csv": matrix_csv,
        "empirical_bands_csv": bands_csv,
        "snapshot_progression_csv": prog_csv,
        "snapshot_schedule_csv": sched_csv,
    }

