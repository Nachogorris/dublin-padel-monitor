"""Automated Multi-Snapshot Background Runner & Scheduler for Dublin Padel Monitor.

Automates:
1. Timed snapshot passes throughout the day (08:00, 12:30, 16:30, 20:00, 23:30 Europe/Dublin).
2. Live telemetry harvest across all County Dublin clubs without API keys.
3. Ingestion of any newly dropped inbox calendar snapshots.
4. Resolution of 15-minute atomic court slices and updating dublin_padel.db.
5. Exporting updated Excel heatmaps and CSV matrices for the live dashboard.
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .cli import (
    DEFAULT_DATA_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_INBOX_DIR,
    DEFAULT_OUTPUT_DIR,
    _load_db_courts_and_hours,
    harvest_live_non_api_network,
    ingest_inbox_snapshots,
    run_measure_without_api,
    run_weekly_peak_analysis,
)
from .db import PadelDatabase
from .models import CollectionRunResult
from .scheduler import STANDARD_LEAD_BUCKETS
from .time_utils import DUBLIN_TZ, now_dublin_iso

logger = logging.getLogger("padel_monitor.automation")

DAILY_SCHEDULE_WINDOWS = [
    ("08:00", "6h", "Morning baseline & daytime cancellations"),
    ("12:30", "6h", "Midday / lunchtime casual bookings"),
    ("16:30", "1h", "Pre-peak final lock-in before 17:00"),
    ("20:00", "1h", "Prime peak active utilization check"),
    ("23:30", "24h", "Night lock-in for tomorrow's 24h advance bookings"),
]


def determine_current_snapshot_bucket(now_dt: Optional[datetime] = None) -> str:
    """Classify the current time of day into the nearest operational snapshot bucket."""
    now_dt = now_dt or datetime.now(DUBLIN_TZ)
    h = now_dt.hour
    if h >= 22 or h < 6:
        return "24h"  # Night pass capturing tomorrow's full day
    elif 6 <= h < 11:
        return "6h"   # Morning pass
    elif 11 <= h < 15:
        return "6h"   # Midday pass
    elif 15 <= h < 18:
        return "1h"   # Pre-peak pass
    else:
        return "1h"   # Prime peak pass


def execute_snapshot_wave(
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
    target_date: Optional[date] = None,
    target_week_start: Optional[date] = None,
    bucket_label: Optional[str] = None,
    skip_network: bool = False,
    days_ahead: int = 7,
) -> dict[str, Any]:
    """Execute a complete, unattended snapshot collection wave across registered Playtomic clubs over a multi-day span."""
    db = PadelDatabase(db_path)
    started_at = now_dublin_iso()
    bucket = bucket_label or determine_current_snapshot_bucket()
    week_start = target_week_start or (date.today() - timedelta(days=date.today().weekday()))
    base_date = target_date or date.today()
    dates_to_scrape = [base_date + timedelta(days=i) for i in range(max(1, days_ahead))]

    print(
        f"\n[{started_at}] 🚀 STARTING AUTOMATED PLAYTOMIC SNAPSHOT WAVE "
        f"({len(dates_to_scrape)} days ahead: {dates_to_scrape[0]} -> {dates_to_scrape[-1]}, Default Bucket: {bucket})",
        flush=True,
    )

    # 1. Harvest live public pages across registered Playtomic clubs
    harvest_rc = 0
    if not skip_network:
        try:
            harvest_rc = harvest_live_non_api_network(db)
            print(f"  ✓ Live web telemetry harvest completed (exit_code={harvest_rc}).", flush=True)
        except Exception as exc:
            print(f"  ⚠ Live network harvest notice: {exc}", flush=True)

        # 1b. Scrape interactive court availability bars via headless Chromium
        try:
            from .adapters.headless_court_scraper import HeadlessCourtAvailabilityScraper
            scraper = HeadlessCourtAvailabilityScraper(raw_save_dir=data_dir / "raw" / "headless_snapshots")
            with db.connect() as conn:
                pt_clubs = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT club_id, public_booking_url 
                        FROM clubs 
                        WHERE platform_id = 'playtomic' 
                          AND public_booking_url LIKE 'https://playtomic.com/clubs/%'
                        ORDER BY club_name
                        """
                    ).fetchall()
                ]
            clubs_by_id, courts_map, oh_map = _load_db_courts_and_hours(db)
            headless_inserted = 0
            for idx, c_info in enumerate(pt_clubs, 1):
                cid = c_info["club_id"]
                url = c_info["public_booking_url"]
                c_courts = [c for (cl_id, _), c in courts_map.items() if cl_id == cid and c.sport.upper() == "PADEL"]
                c_oh = {dow: ohr for (cl_id, dow), ohr in oh_map.items() if cl_id == cid}
                print(f"    [{idx}/{len(pt_clubs)}] 🎾 Scraping live slots & prices for {cid} ({len(dates_to_scrape)} days: {dates_to_scrape[0]} to {dates_to_scrape[-1]})...", flush=True)
                res = scraper.scrape_club_dates(
                    club_id=cid,
                    booking_url=url,
                    target_dates=dates_to_scrape,
                    courts=c_courts,
                    opening_hours=c_oh,
                    snapshot_bucket=bucket_label,
                )
                if res.observations:
                    db.record_collection_run(res)
                    ins, _ = db.insert_observations(res.observations)
                    headless_inserted += ins
                    db.update_court_slot_ledger(res.observations)
                    print(f"       ✓ {len(res.observations)} slots captured ({len(dates_to_scrape)} days, prices extracted, ledger updated).", flush=True)
                else:
                    print(f"       ⚠ No observations returned ({res.run_status}: {res.error_message}).", flush=True)
            print(
                f"  ✓ Headless browser court scraper: +{headless_inserted} live court observations across {len(pt_clubs)} Playtomic clubs (Span: {dates_to_scrape[0]} to {dates_to_scrape[-1]}).",
                flush=True,
            )
        except Exception as exc:
            print(f"  ℹ Headless scraper note: {exc}", flush=True)
    else:
        print("  ℹ Network harvest skipped (--skip-network).", flush=True)

    # 2. Ingest any dropzone inbox snapshots (browser calendar captures or text dumps)
    inbox_res = ingest_inbox_snapshots(db, inbox_dir=data_dir / "inbox")
    print(
        f"  ✓ Processed {inbox_res['inbox_files_processed']} inbox file(s): "
        f"+{inbox_res['inbox_observations_inserted']} observations stored.",
        flush=True,
    )

    # 3. Run Non-API court measurement & update core DB tables
    meas_summary = run_measure_without_api(db_path=db_path, output_dir=output_dir, data_dir=data_dir)
    print(f"  ✓ Non-API Court Measurement Engine updated ({meas_summary['non_api_stats']['clubs_measured']} clubs).", flush=True)

    # 4. Re-calculate weekly peak/off-peak heatmaps & progression curves
    analysis_res = run_weekly_peak_analysis(db_path=db_path, week_start=week_start, output_dir=output_dir)
    print(
        f"  ✓ Weekly analysis refreshed: {analysis_res['slices_analyzed']} slices analyzed across "
        f"{analysis_res['live_calendar_clubs_count']} live clubs.",
        flush=True,
    )
    print(f"  ✓ Heatmap Workbook updated: {analysis_res['weekly_heatmap_xlsx']}", flush=True)

    completed_at = now_dublin_iso()
    print(f"[{completed_at}] ✅ SNAPSHOT WAVE COMPLETE.\n", flush=True)

    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "snapshot_bucket": bucket,
        "target_week_start": week_start.isoformat(),
        "harvest_exit_code": harvest_rc,
        "inbox_res": inbox_res,
        "analysis_res": analysis_res,
    }


def run_continuous_daemon(
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
    poll_interval_seconds: int = 60,
    skip_initial_pass: bool = False,
) -> None:
    """Run an autonomous background loop that triggers snapshot waves on schedule."""
    print("=================================================================")
    print("🎾 DUBLIN PADEL MONITOR — AUTONOMOUS BACKGROUND DAEMON RUNNING")
    print("=================================================================")
    print("Scheduled Daily Snapshot Passes (Europe/Dublin):")
    for tm, bkt, desc in DAILY_SCHEDULE_WINDOWS:
        print(f"  - {tm} ({bkt:3s}) : {desc}")
    print(f"Targeting ONLY verified Playtomic clubs.")
    print(f"Polling clock every {poll_interval_seconds}s. Press Ctrl+C to terminate.")
    print("=================================================================\n")

    fired_minutes: set[str] = set()

    # Run initial pass on startup unless explicitly skipped
    if not skip_initial_pass:
        try:
            print("Executing initial baseline pass on startup...")
            execute_snapshot_wave(db_path=db_path, output_dir=output_dir, data_dir=data_dir)
        except Exception as exc:
            print(f"Initial startup pass exception: {exc}")
    else:
        now_cur = datetime.now(DUBLIN_TZ).strftime("%H:%M")
        next_w = next((w for w in DAILY_SCHEDULE_WINDOWS if w[0] > now_cur), DAILY_SCHEDULE_WINDOWS[0])
        print(f"Initial pass skipped. Waiting for next scheduled window ({next_w[0]} Dublin time, {next_w[2]})...")

    while True:
        try:
            now_dt = datetime.now(DUBLIN_TZ)
            hhmm = now_dt.strftime("%H:%M")
            date_hhmm = now_dt.strftime("%Y-%m-%d_%H:%M")

            # Check if current time matches any scheduled window
            match = next((item for item in DAILY_SCHEDULE_WINDOWS if item[0] == hhmm), None)
            if match and date_hhmm not in fired_minutes:
                fired_minutes.add(date_hhmm)
                _, bkt, desc = match
                print(f"\n[DAEMON TRIGGER] Firing scheduled pass '{desc}' ({hhmm} Dublin time, bucket={bkt})")
                execute_snapshot_wave(db_path=db_path, output_dir=output_dir, data_dir=data_dir, bucket_label=bkt)

            time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            print("\nDaemon terminated by user.")
            break
        except Exception as exc:
            print(f"Daemon cycle error: {exc}")
            time.sleep(poll_interval_seconds)
