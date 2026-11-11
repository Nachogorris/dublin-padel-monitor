# Dublin Padel Market Intelligence & Court Occupancy Tracker
## Technical Architecture, Legal Compliance Audit & Occupancy Methodology

---

## 1. System Architecture Overview

The system separates **compliance-checked acquisition**, **canonical `Europe/Dublin` normalization**, **append-only SQLite historical persistence**, **atomic 15-minute physical court-slice occupancy resolution**, and **multi-format BI export** into decoupled modules under `src/padel_monitor/`:

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        1. Acquisition & Compliance Layer                   │
│  CompliantHttpClient (RFC 9309 wildcard robots.txt + >=1.5s host throttle) │
│   ├── PlaytomicPublicClubAdapter   (LIVE: /clubs/<slug> SSR RSC stream)    │
│   ├── PlaytomicOfficialApiAdapter  (OAUTH2: third-party.playtomic.io)      │
│   ├── GenericPortalProbeAdapter    (Matchpoint / Playbypoint / Gladstone)  │
│   ├── CsvObservationAdapter        (Club B2B exports & starter CSVs)       │
│   └── SyntheticValidationGenerator (Explicitly tagged synthetic_validation)│
└───────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                   2. Validation & Europe/Dublin Normalization              │
│  models.AvailabilityObservation.from_raw() + time_utils.py                 │
│   • Requires timezone-aware ISO-8601 inputs; normalizes to Europe/Dublin   │
│   • Handles GMT (+00:00) <-> IST (+01:00) DST boundaries & 00:30/01:00     │
│     post-midnight club closings via UTC interval arithmetic                │
│   • Classifies lead-time buckets: 7d, 3d, 24h, 6h, 1h, ad_hoc              │
│   • Enforces strict data_origin provenance tags on every row               │
└───────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                       3. SQLite 3 Persistence Layer                        │
│  sql/schema.sql -> data/dublin_padel.db (PadelDatabase)                    │
│   • booking_platforms (10)  • clubs (35)          • courts (34 live)       │
│   • club_opening_hours (72) • public_club_events  • collection_runs (audit)│
│   • availability_observations (append-only, UNIQUE idempotent dedup key)   │
└───────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│              4. Atomic 15-Min Court-Slice Occupancy & GBV Engine           │
│  occupancy.py: resolve_atomic_slices() + build_all_occupancy_breakdowns()  │
│   • Eliminates 60-min / 90-min overlapping duration double-counting        │
│   • Separates confirmed, transition-inferred, static-unavail, blocked,     │
│     and unknown court-hours                                                │
│   • Carries forward last observed hourly EUR price for GBV proxy           │
└───────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     5. Excel & CSV Analytics Exporters                     │
│  exporters.py -> output/*.xlsx, output/*.csv, data/normalized/*.csv        │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Empirical Platform Investigation & Compliance Audit (Tested 2026-11-06)

Every platform operating in County Dublin was tested live between `2026-11-06T22:21:15Z` and `2026-11-06T22:35:41Z`. Raw HTTP artifacts and `robots.txt` policies are preserved in `data/raw/policy/` and logged in `collection_runs`:

| Priority | Platform | County Dublin Coverage | Public Web Behaviour (Unauthenticated) | `robots.txt` & Terms Findings | Compliant Integration Path |
|---:|---|---|---|---|---|
| **1** | **Playtomic** | **12 active clubs / 37 padel courts** (`40.7%` of County Dublin; +16 courts in commuter belt) | `GET https://playtomic.com/clubs/<slug>` returns HTTP 200 SSR HTML with embedded Next.js RSC JSON (`tenant_id`, exact court UUIDs, `PADEL` vs `PICKLEBALL`/`SQUASH`, `Indoor`/`Roofed outdoor`/`Outdoor`, `Double`/`Single`, weekly `opening_hours`, `competitionCards`). Slot calendar grid is omitted from SSR HTML. | `robots.txt` **allows** `/clubs/<slug>` & sitemaps, but **explicitly disallows** `Disallow: /api`, `Disallow: /*?*sport=`, `Disallow: /*?*date=`. Legal Conditions Sec. 3 prohibit automated crawlers/scrapers on web/app. | **Implemented dual adapter**: `PlaytomicPublicClubAdapter` collects live permitted club/court/hours/event metadata; `PlaytomicOfficialApiAdapter` connects to official OAuth2 `https://third-party.playtomic.io/api/v1/bookings` once club (`Champion`/`Master` tier) or **Playtomic Connect** partner credentials are provisioned. |
| **2** | **TPC Matchpoint** | **1 club / 9 indoor courts** (`9.9%` — Padel Dome Santry) | `GET https://padeldome-ie.matchpoint.com.es/` HTTP 302 redirects to `/Login.aspx` (login wall; no slots visible to unauthenticated visitors). | `robots.txt` explicitly sets `Disallow: /Booking/`, `Disallow: /Reservas/`, `Disallow: /Services/`, `Disallow: /Login.aspx`. | Direct B2B benchmark partnership with Padel Dome management exporting anonymized weekly CSV sheets into `CsvObservationAdapter`. |
| **3** | **GladstoneGo** | **1 club / 5 outdoor courts** (`5.5%` — Portmarnock PSLC) | `GET https://pslc.gladstonego.cloud/book` returns a 2,791-byte empty Angular SPA shell (`<app-root>`). Even `/robots.txt` returns the SPA HTML fallback. | Client SPA relies on undocumented internal tenant APIs; no public API documentation. | Operator report / Gladstone API export agreement with Portmarnock Sports & Leisure Club. |
| **4** | **Playbypoint** | **1 club / 4 outdoor courts** (`4.4%` — Bushy Park) | `GET https://bushytennispadel.playbypoint.com/` returns **HTTP 403** Cloudflare Turnstile anti-bot challenge (`Just a moment...`). | `robots.txt` sets `Disallow: /reservations/*`, `Disallow: /home` and explicitly reserves Text & Data Mining rights under **EU Directive 2019/790 Art. 4**. | Never bypass Cloudflare or TDM opt-out; request periodic aggregate utilization summary from Bushy Park management. |
| **5** | **dlr Leisure** | **2 clubs / 5 outdoor courts** (`5.5%` — Meadowbrook 2 + Loughlinstown 3) | `GET https://dlrleisure.ie/facilities/padel-courts/` HTTP 200 exposes verified court counts & prices (€24/€30), while `/facility-booking/` redirects to `/facilities/facility-booking-enquiry/`. | `robots.txt` allows public pages; bookings are handled offline by reception/phone. | Municipal research / FOI aggregate utilization request to dlr Leisure. |
| **6** | **SmartClubCloud** | **3 clubs / 6 outdoor courts** (`6.6%` — Shankill 2, Sutton 2, Castleknock 2) | Member / registered-visitor login portal required before court sheets load. | Authenticated account wall. | Club committee benchmark data exchange (starting with hybrid-access Shankill TC). |
| **7** | **Padel Ireland WP** | **1 club / 2 outdoor courts** (`2.2%` — Rockbrook Padel) | `GET https://padel-ireland.ie/bookings/` HTTP 200 renders broken unexpanded WordPress shortcode `[rncbc_calendar id="1"]` + member login box. | `robots.txt` allows `/bookings/` with `Crawl-delay: 10`. | Re-probe periodically (respecting >=10s crawl delay) in case calendar plugin is repaired; otherwise direct outreach. |
| **8–10** | **CourtReserve, MyCourts & Member Portals** | **8 private clubs / 23 courts** (`25.3%` — West Wood x2, Elm Park, David Lloyd, Fitzwilliam, Bective, Malahide, Charleville) | Strictly private health-club or lawn-tennis-club member apps/portals. | Private member authentication walls. | Track court supply in capacity denominators; exclude from public pay-and-play scraping. |

> **Engineering Note on RFC 9309 Wildcards**: Python's standard `urllib.robotparser.RobotFileParser` treats `*` as a literal character and silently fails to enforce wildcard rules such as `Disallow: /*?*date=`. We implemented `Rfc9309RobotsPolicy` in `src/padel_monitor/compliance.py` to enforce full RFC 9309 `*` wildcard and `$` end-anchor semantics before every outbound request.

---

## 3. Multi-Snapshot Strategy & Overlapping Duration Resolution

### 3.1 Scheduled Lead-Time Buckets
`src/padel_monitor/scheduler.py` plans 5 canonical snapshots per target date anchored against peak evening play (`18:00` `Europe/Dublin`):
- **`7d` (`168h` prior)**: Captures baseline court inventory, member early-bird bookings (14-day window clubs like House of Padel), and standing coaching/league blocks.
- **`3d` (`72h` prior)**: Captures primary open-match formation window.
- **`24h` (`24h` prior)**: Captures day-before confirmations and cancellation releases.
- **`6h` (`6h` prior)**: Captures same-day lunchtime bookings.
- **`1h` (`1h` prior)**: Captures final walk-up / last-minute fill state prior to play.

### 3.2 Solving the 60-Min / 90-Min Overlap Problem
Booking platforms advertise overlapping slot durations on 30-minute boundaries—for example, on a 2-hour window `18:00–20:00`, a free court simultaneously lists:
1. `18:00–19:00` (60 min)
2. `18:00–19:30` (90 min)
3. `18:30–19:30` (60 min)
4. `18:30–20:00` (90 min)
5. `19:00–20:00` (60 min)

Summing slot durations naively yields **6.0 hours instead of 2.0 physical hours**—and worse, if a player books `19:00–20:00`, slots (2), (3), (4), and (5) all become `unavailable` (4 out of 5 slots = `80%` naive slot unavailability!) even though `18:00–19:00` (`50%` of physical court time) is completely free.

**Our Solution (`src/padel_monitor/occupancy.py`)**:
1. Partition each court's `Europe/Dublin` operating window in UTC into **non-overlapping 15-minute physical court slices** (`[s, e)`).
2. At each snapshot timestamp $T_{\text{obs}} \le e$, inspect all observed slots $[S_i, E_i)$ that completely cover $[s, e)$ ($S_i \le s \land E_i \ge e$):
   - **`confirmed_booking`** if any authorized booking covers $[s, e)$.
   - **`blocked`** if any maintenance/coaching/tournament block covers $[s, e)$.
   - **`available`** if **at least one** covering slot duration is `available` (proving the physical court is free during $[s, e)$!).
   - **`unavailable`** if covering slots exist and **all** of them are `unavailable`.
   - **`unknown`** if no valid observation covers $[s, e)$ or all covering observations failed (`unknown`).

---

## 4. Occupancy & Gross Booking Value (GBV) Estimation

### 4.1 Formulas
For any aggregation slice group $G$ (by club, court, hour, day of week, weekday/weekend, peak/off-peak, indoor/outdoor, or lead-time bucket), let slice duration $h = 0.25\text{ hr}$:

$$
\text{Offered Bookable Hours} = H_{\text{avail}} + H_{\text{confirmed}} + H_{\text{trans\_unavail}} + H_{\text{static\_unavail}}
$$

$$
\text{Estimated Occupancy (\%)} = \frac{H_{\text{confirmed}} + H_{\text{trans\_unavail}} + H_{\text{static\_unavail}}}{\text{Offered Bookable Hours}} \times 100
$$

$$
\text{Transition-Backed Occupancy (\%)} = \frac{H_{\text{confirmed}} + H_{\text{trans\_unavail}}}{\text{Offered Bookable Hours}} \times 100
$$

Note that **`blocked` hours** ($H_{\text{blocked}}$) and **`unknown` hours** ($H_{\text{unknown}}$) are reported explicitly in `total_capacity_hours` but **excluded from `offered_bookable_hours`** so scraper outages or maintenance closures never distort booking occupancy rates.

### 4.2 Gross Booking Value (GBV) Proxy & Key Caveats
- When a slice transitions `available -> unavailable`, public platforms typically hide the price on the unavailable slot. `resolve_atomic_slices()` carries forward the last observed 60-minute-equivalent EUR rate $r_s$ from prior `available` snapshots on that slice:
  $$\text{Headline Retail GBV} = \sum_{s \in G_{\text{unavail\_bookable}}} 0.25 \times r_s$$
- We also report **`est_gbv_member_adjusted_low_eur`** ($0.82 \times \text{Retail GBV}$) to reflect member discounts (e.g., House of Padel Gold/Platinum 15–20% discounts, Druid Padel memberships, off-peak bundles, and coaching/internal court usage).
- **Important Limitation**: Public slot disappearance (`unavailable`) is an **occupancy proxy**, not audited club revenue. Slots may disappear due to unpaid holds, club social sessions, weather closures on outdoor courts, or member-included play.

### 4.3 Confidence Scoring (`0–100` / `high | medium | low`)
Each summary row computes a composite confidence score weighting:
1. **Lead-time depth (35 pts)**: average number of distinct lead-time buckets observed per slice (targeting $\ge 4$ buckets).
2. **Transition / confirmation backing (30 pts)**: share of unavailable hours backed by an observed `available -> unavailable` transition or `confirmed_booking` rather than static first-snapshot unavailability.
3. **Data completeness (25 pts)**: $1 - \text{unknown\_share}$ (penalizing HTTP/parse gaps; $>10\%$ unknown caps confidence below `high`).
4. **Verified court metadata (10 pts)**: known indoor/covered/outdoor court classification.

---

## 5. 100% API-Free Court Measurement System (`non_api_measurement.py`)

When official vendor API credentials (`PlaytomicOfficialApiAdapter`) or B2B club exports are unavailable, the system measures Dublin padel courts **entirely without APIs** while strictly obeying `robots.txt`, authentication walls, and Cloudflare anti-bot boundaries:

### 5.1 Permitted Multi-Page Public Telemetry Harvester
While `https://playtomic.com/robots.txt` disallows `/api`, `/tournaments/*`, `/activities/*`, `/*?*sport=`, and `/*?*date=`, it **explicitly permits**:
- `GET /clubs/<slug>` (main club SSR page + embedded Next.js RSC stream)
- `GET /clubs/<slug>/competitions/tournaments` (complete club schedule of upcoming social Americanos, King-of-the-Court sessions, and leagues)
- `GET /clubs/<slug>/academy/courses` (multi-week coaching courses)

`parse_playtomic_activity_cards()` resolves Next.js streaming Suspense placeholders (`<div hidden id="S:XX">` $\rightarrow$ `<template id="P:XX">` inside `<div data-slot="card">`) to extract:
1. **Exact `Europe/Dublin` start/end timestamps & durations** (`60`, `90`, `120`, `240` min).
2. **Live registered vs maximum player counts** (e.g. `13 / 16` = `81.3%` fill, `11 / 12` = `91.7%` fill) and per-player EUR entry fees.
3. **Physical court footprint**: each tournament of $M$ max players occupies $\min(C_{\text{club}}, \lceil M / 4 \rceil)$ physical doubles courts; dated events project directly into `availability_observations` as real `live_public` court-slot rows (`confirmed_booking` when $\ge 4$ players are registered, else `blocked_coaching_or_event`).
4. **Real Multi-Snapshot Sign-Up Velocity (`compute_activity_velocity_pairs`)**: Comparing repeated snapshots separated by $\ge 1\text{ hr}$ (e.g., Snapshot T1 on `2026-11-06` vs Snapshot T2 on `2026-11-07`, ~25 hours apart) measures live overnight player sign-up progression (`+1` to `+4` players/day per active tournament).
5. **Club Membership & Wallet Pricing Signals (`club_membership_offers`)**: Extracts recurring memberships and wallet bundles directly from club pages (e.g., House of Padel `Off Peak Unlimited €80/mo` with `€0` court fees `06:00–16:00` Mon–Fri + `14-day` advance booking; Padel & Pines `Oak €1,000` `28-day` window; Druid Kimmage `Pay €220, Get €264` wallet bonus; Padel 100 `€0` Free Hours `11:00–15:00`).

### 5.2 Multi-Signal Empirical Calibration (`NonApiCourtMeasurementEngine`)
For all **22 active public/hybrid clubs** (18 County Dublin = 62 courts; 4 commuter-belt = 18 courts), weekly court utilization is decomposed into:

$$
H_{\text{occupied, total}} = H_{\text{organised, live}} + H_{\text{casual, calibrated}}
$$

where casual Peak (`Mon–Fri 17:00–22:30`, `Sat–Sun 09:00–20:00`) and Off-Peak utilization rates fuse:
- **Environment weather resilience** (November Dublin indoor vs canopy-covered outdoor vs uncovered floodlit outdoor).
- **Signal S1 — Near-Term Live Activity Fill Thermometer**: club-specific player fill rate on upcoming 7-day social tournaments & drop-in classes.
- **Signal S2 — Off-Peak Unlimited / Free-Play Window Boost**: live-verified `€0` daytime member or public court windows.
- **Signal S3 & S4 — Price Elasticity & Extended-Hours Dilution**: hourly EUR rate relative to the `€40/hr` Dublin median and daily operating span (`12.5h` vs `18.5h/day`).
- **Signal S5 — Empirical-Bayes Spot-Check Shrinkage**: whenever human-in-the-loop spot checks (`field_spot_checks`) or browser dropzone snapshots (`data/inbox/`) are logged via `monitor.py log-spot-check` or `monitor.py ingest-inbox`, the club's utilization estimate shrinks toward observed Stratified Horvitz-Thompson occupancy with 95% confidence intervals.

---

## 6. Multi-Snapshot Next-Week Booking Tracker & Peak / Off-Peak Discovery (`weekly_tracker.py`)

To track an entire upcoming 7-day week across clubs and empirically identify each club's true **Peak**, **Shoulder**, and **Off-Peak** hours without any vendor API:

### 6.1 Five-Pass Multi-Snapshot Dropzone Workflow (`data/inbox/week_<YYYY-MM-DD>/`)
`python monitor.py init-week-pack --week-start YYYY-MM-DD` scaffolds 5 weekly bundle templates per club (`<club_id>__week__YYYY-MM-DD__<tag>.txt`) corresponding to the 5 canonical lead-time passes:
1. **`7d` (`168h` prior)**: Week-ahead baseline (`Pass 1`), capturing `14d`/`28d` priority member bookings and standing leagues.
2. **`3d` (`72h` prior)**: Core Playtomic 4-player open-match formation window (`Pass 2`).
3. **`24h` (`24h` prior)**: Day-before lock-in (`Pass 3`), capturing cancellation deadline releases and evening-before bookings.
4. **`morning` / `6h` (`06:00` local on play date)**: Intra-day morning check (`Pass 4`), locking in final realized morning (`06:00–12:00`) and lunchtime (`12:00–15:00`) occupancy before play starts.
5. **`prepeak` / `1h` (`16:30` local on play date)**: Intra-day pre-peak check (`Pass 5`), locking in final realized evening prime (`17:00–23:00`) occupancy without overwriting earlier morning slices (`resolve_atomic_slices()` enforces $T_{\text{obs}} \le e$ per 15-minute physical court slice).

Each file accepts (a) `Ctrl+S` webpage HTML, (b) `Ctrl+A`/`Ctrl+C` per-court calendar text, or (c) 10-second court-count shorthand (`09:00-17:00 = 3 free €30`, `17:00-21:30 = 0 free €50`), safely ignoring unfilled blank days.

### 6.2 Empirical Peak / Shoulder / Off-Peak Band Classifier & Heatmap Export
`compute_club_hour_heatmap_and_empirical_peaks()` aggregates resolved 15-minute physical slices into a `Club × Hour (06:00–23:00)` occupancy matrix (both All-Week and Weekday-vs-Weekend split) and dynamically classifies each club's operating hours into:
- **Empirical Peak Windows**: $\text{Occ}_h \ge \min(75\%, \max(55\%, \text{Occ}_{\max} - 10\text{ pp}))$ — automatically catching pre-work (`07:00–08:00`) and lunchtime (`12:00–14:00`) surges alongside evening prime (`17:00–22:00`).
- **Empirical Off-Peak Windows**: $\text{Occ}_h < \max(45\%, \min(\text{PeakThresh} - 12\text{ pp}, \text{Occ}_{\min} + 10\text{ pp}))$ — identifying every club's lowest-demand hours even at high-utilization indoor venues.
- **5-Snapshot Lead-Time Progression (`compute_snapshot_progression_curve`)**: Re-evaluates atomic slices cumulatively across `Pass 1 (7d)` $\rightarrow$ `Pass 2 (3d)` $\rightarrow$ `Pass 3 (24h)` $\rightarrow$ `Pass 4 (6h/Morning)` $\rightarrow$ `Pass 5 (1h/Pre-Peak)` to quantify exact `+pp` booking velocity by lead-time stage, exported to the 6-tab color-coded workbook `output/weekly_peak_offpeak_club_heatmap.xlsx`.



