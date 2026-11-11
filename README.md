# Dublin Padel Market Intelligence & API-Free Court Occupancy Tracker

A modular, legally compliant market intelligence and **100% API-free court measurement system** for County Dublin (and Greater Dublin commuter-belt) padel facilities.

## Key Capabilities

- **100% API-Free Live Court Activity & Utilization Measurement (`src/padel_monitor/non_api_measurement.py`)**:
  - Measures court capacity, directly scheduled court-hours, player fill rates, **24-hour player sign-up velocity**, membership discount structures, and club-by-club weekly occupancy & Gross Booking Value (`[Low, Point, High]`) **without Playtomic's OAuth2 API or any third-party vendor API**.
  - Harvests `robots.txt`-permitted SSR pages (`/clubs/<slug>`, `/clubs/<slug>/competitions/tournaments`, `/clubs/<slug>/academy/courses`, resolving Next.js streaming Suspense `$RS("S:XX", "P:XX")` cards) plus official club websites (`padel100.ie`, `padeldome.ie`, `pslc.ie`, `dlrleisure.ie`, `shorelineleisure.ie`).
  - Projects live tournaments, Americanos, and public classes directly onto verified physical club courts as real `live_public` `AvailabilityObservation` records (`343` live court-slot projections across Dublin & commuter belt).
- **Multi-Snapshot Next-Week Peak & Off-Peak Tracker (`src/padel_monitor/weekly_tracker.py`)**:
  - Coordinates **5-pass multi-snapshot data gathering** (`7d`, `3d`, `24h`, `morning`/`6h`, `prepeak`/`1h`) across any 7-day target week (`init-week-pack` & `analyze-week`) without overwriting earlier snapshots.
  - Builds a **6-tab Color-Coded `Club × Hour (06:00–23:00)` Peak/Off-Peak Heatmap Workbook** (`output/weekly_peak_offpeak_club_heatmap.xlsx`), splitting Weekday (`Mon–Fri`) vs Weekend (`Sat–Sun`), discovering each club's empirical **Peak**, **Shoulder**, and **Off-Peak** windows (catching pre-work `07:00` and lunchtime `12:00–14:00` peaks alongside `17:00–22:00`), and charting cumulative lead-time booking progression (`7d -> 3d -> 24h -> 6h -> 1h`).
- **Zero-API Human-in-the-Loop Casual Slot Extensions (`src/padel_monitor/adapters/`)**:
  - **`BrowserSnapshotDropzoneAdapter` (`data/inbox/`)**: Parses single-day (`<club_id>__YYYY-MM-DD__<tag>.{txt,html}`) or 7-day weekly bundle files (`<club_id>__week__YYYY-MM-DD__<tag>.txt`) in 3 formats—`Ctrl+S` HTML, `Ctrl+C` per-court text, or 10-second court-count shorthand (`17:00-21:30 = 0 free €50`)—across `7d`, `3d`, `24h`, `morning`, and `prepeak` passes.
  - **`FieldSpotCheckAdapter` (`spot_check.py`)**: Logs rapid telephone, walk-in, or visual court checks across 4 canonical Dublin demand strata (`weekday_offpeak`, `weekday_peak`, `weekend_prime`, `weekend_offpeak`) and computes **Stratified Ratio Occupancy with 95% confidence bounds**, automatically blending into `NonApiCourtMeasurementEngine`.
- **Verified Club & Platform Database (`sql/schema.sql`, `data/dublin_padel.db`)**:
  - Tracks **29 active County Dublin venues (91 verified padel courts: 88 doubles + 3 singles)**, **1 County Dublin planning pipeline project** (Swords Pavilions, 10 planned courts), and **5 Greater Dublin commuter-belt reference venues (20 courts)** across **10 booking platforms** (`11` relational tables + analytical view).
- **Atomic 15-Minute Occupancy & GBV Engine (`src/padel_monitor/occupancy.py`)**:
  - Resolves overlapping 60-minute and 90-minute slot offerings into non-overlapping 15-minute physical court slices in `Europe/Dublin` (DST-safe across GMT `+00:00` and IST `+01:00`), preventing double-counting and tracking `available -> unavailable` lead-time conversions (`7d`, `3d`, `24h`, `6h`, `1h`).

## Repository Structure

```text
dublin_padel_monitor/
├── monitor.py                              # CLI & backward-compatible CSV entrypoint
├── venues.csv                              # Verified club summary (backward-compatible format)
├── scripts/
│   └── fetch_live_non_api_telemetry.py     # Polite robots.txt-enforced live web harvester
├── sql/
│   └── schema.sql                          # SQLite 3 DDL (11 tables + analytical view)
├── src/padel_monitor/
│   ├── compliance.py                       # RFC 9309 robots.txt & rate-limit gatekeeper
│   ├── non_api_measurement.py              # API-free activity parser, 24h velocity & club utilization engine
│   ├── models.py                           # Validated dataclasses & normalization
│   ├── time_utils.py                       # Europe/Dublin DST & UTC atomic slice utilities
│   ├── db.py                               # SQLite repository & idempotent deduplication
│   ├── inventory.py                        # Verified 35-club & 10-platform dataset
│   ├── scheduler.py                        # 7d / 3d / 24h / 6h / 1h snapshot planner
│   ├── weekly_tracker.py                   # Multi-snapshot Club x Hour Peak/Off-Peak tracker & heatmap engine
│   ├── occupancy.py                        # Atomic 15-min occupancy, GBV & confidence engine
│   ├── synthetic.py                        # Explicitly labeled synthetic_validation generator
│   ├── exporters.py                        # Styled openpyxl Excel & CSV exporters
│   ├── cli.py                              # CLI subcommands
│   └── adapters/
│       ├── browser_snapshot.py             # Multi-snapshot data/inbox/ HTML, text & shorthand parser
│       ├── spot_check.py                   # Stratified field/phone spot-check 95% CI estimator
│       ├── playtomic_public.py             # Live /clubs/<slug> SSR club & court collector
│       ├── playtomic_official_api.py       # Optional OAuth2 adapter (if credentials acquired later)
│       ├── platform_probes.py              # Non-Playtomic compliance probes
│       └── feed_csv.py                     # Club B2B CSV feed adapter
├── tests/                                  # 25 automated unittest validation tests
├── data/
│   ├── dublin_padel.db                     # Populated SQLite database
│   ├── inbox/                              # Multi-snapshot browser dropzone + week_YYYY-MM-DD/ packs
│   ├── raw/policy/                         # Snapshot T1 (2026-11-06) HTTP, robots.txt & SSR captures
│   ├── raw/non_api_telemetry/              # Snapshot T2 (2026-11-07) tournaments, courses & club sites
│   ├── normalized/                         # Normalized live courts, hours, observations & audit logs
│   └── synthetic/                          # Explicitly tagged synthetic validation snapshots
├── output/
│   ├── weekly_peak_offpeak_club_heatmap.xlsx # Color-coded Club x Hour (06-23) Peak/Off-Peak workbook (6 tabs)
│   ├── dublin_padel_non_api_court_measurements.xlsx # Primary API-free court utilization & GBV workbook (7 tabs)
│   ├── field_spot_check_template.xlsx      # Ready-to-use phone/visual spot-check template
│   ├── dublin_padel_club_inventory.xlsx    # Verified 35-club & platform coverage workbook (7 tabs)
│   ├── availability_observations_and_occupancy.xlsx # Multi-snapshot atomic-slice workbook (8 tabs)
│   ├── club_hourly_occupancy_matrix.csv    # Club x Hour (06:00-23:00) occupancy % matrix
│   ├── club_empirical_peak_offpeak_bands.csv # Discovered Peak, Shoulder & Off-Peak hour spans per club
│   ├── snapshot_leadtime_progression.csv   # 5-pass (7d -> 3d -> 24h -> 6h -> 1h) booking progression
│   ├── next_week_snapshot_schedule.csv     # Chronological 5-snapshot gathering checklist
│   ├── non_api_club_measurements.csv       # Weekly utilization & GBV across all 22 public/hybrid clubs
│   ├── live_activity_telemetry.csv         # 154 live tournaments, classes & courses
│   ├── live_24h_signup_velocity.csv        # Real 24h sign-up transitions (2026-11-06 -> 2026-11-07)
│   └── live_club_memberships.csv           # 16 live club membership & wallet bundle offers
└── docs/
    └── ARCHITECTURE_AND_METHODOLOGY.md     # Full technical architecture & API-free methodology
```

## Quick Start

```powershell
# 1. Scaffold next week's 5-snapshot (7d, 3d, 24h, morning, prepeak) dropzone bundle templates
python monitor.py init-week-pack --week-start 2026-11-09

# 2. Ingest multi-snapshot inbox data & generate the Club x Hour (06:00-23:00) Peak/Off-Peak Heatmap
python monitor.py analyze-week --week-start 2026-11-09

# 3. Run the 100% API-free court measurement & telemetry pipeline
python monitor.py measure-without-api

# 4. Fetch fresh live public club, tournament & course pages (polite robots.txt-compliant HTTP)
python monitor.py harvest-live
python monitor.py harvest-live --club house_of_padel

# 5. Log a 30-second telephone or visual court spot-check (e.g. Padel Dome, Bushy Park, PSLC)
python monitor.py log-spot-check --club padel_dome --start 2026-11-10T19:00:00+00:00 --end 2026-11-10T20:00:00+00:00 --total 9 --occupied 8 --price 40 --method telephone_enquiry

# 6. Run the complete pipeline (API-free measurements + synthetic validation + all 5 Excel workbooks)
python monitor.py run-pipeline

# 7. Execute all 25 automated validation unit tests
python -m unittest discover -s tests -v
```
