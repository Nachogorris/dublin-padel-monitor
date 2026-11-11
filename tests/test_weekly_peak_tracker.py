"""Automated unit & integration tests for the Multi-Snapshot Weekly Peak/Off-Peak Tracker."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.adapters import (
    BrowserSnapshotDropzoneAdapter,
    parse_inbox_filename,
    resolve_snapshot_timestamp_and_bucket,
)
from padel_monitor.cli import (
    ingest_inbox_snapshots,
    init_and_sync_inventory,
    run_weekly_peak_analysis,
)
from padel_monitor.db import PadelDatabase
from padel_monitor.models import (
    AvailabilityObservation,
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
)
from padel_monitor.occupancy import resolve_atomic_slices
from padel_monitor.weekly_tracker import (
    compress_hour_spans,
    compute_club_hour_heatmap_and_empirical_peaks,
    compute_snapshot_progression_curve,
    scaffold_weekly_inbox_pack,
)


class TestWeeklyPeakOffPeakTracker(unittest.TestCase):
    def test_parse_inbox_filename_and_snapshot_timestamps(self) -> None:
        d1 = parse_inbox_filename("house_of_padel__2026-11-10__7d.txt")
        self.assertIsNotNone(d1)
        assert d1 is not None
        self.assertEqual(d1.club_id, "house_of_padel")
        self.assertFalse(d1.is_weekly_bundle)
        self.assertEqual(d1.anchor_date, date(2026, 11, 10))
        self.assertEqual(d1.snapshot_tag, "7d")

        ts_7d, bkt_7d = resolve_snapshot_timestamp_and_bucket(date(2026, 11, 10), snapshot_tag="7d")
        self.assertEqual(ts_7d, "2026-11-03T18:00:00+00:00")
        self.assertEqual(bkt_7d, "7d")

        ts_morn, bkt_morn = resolve_snapshot_timestamp_and_bucket(date(2026, 11, 10), snapshot_tag="morning")
        self.assertEqual(ts_morn, "2026-11-10T06:00:00+00:00")
        self.assertEqual(bkt_morn, "6h")

        ts_pk, bkt_pk = resolve_snapshot_timestamp_and_bucket(date(2026, 11, 10), snapshot_tag="prepeak")
        self.assertEqual(ts_pk, "2026-11-10T16:30:00+00:00")
        self.assertEqual(bkt_pk, "1h")

        d_bundle = parse_inbox_filename("padel_dome__week__2026-11-09__24h.txt")
        self.assertIsNotNone(d_bundle)
        assert d_bundle is not None
        self.assertTrue(d_bundle.is_weekly_bundle)
        self.assertEqual(d_bundle.club_id, "padel_dome")
        self.assertEqual(d_bundle.anchor_date, date(2026, 11, 9))
        self.assertEqual(d_bundle.snapshot_tag, "24h")

    def test_scaffold_blank_bundle_produces_zero_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox_dir = Path(tmp) / "inbox"
            clubs = [
                {
                    "club_id": "house_of_padel",
                    "club_name": "House of Padel",
                    "verified_padel_courts": 5,
                    "public_booking_url": "https://playtomic.com/clubs/house-of-padel",
                    "opening_hours_summary": "07:00-23:00",
                    "off_peak_price_eur": 45.0,
                    "peak_price_eur": 60.0,
                }
            ]
            stats = scaffold_weekly_inbox_pack(
                clubs,
                week_start=date(2026, 11, 9),
                inbox_dir=inbox_dir,
                club_filter="house_of_padel",
            )
            self.assertEqual(stats["files_created"], 5)
            bundle_path = stats["pack_dir"] / "house_of_padel__week__2026-11-09__7d.txt"
            self.assertTrue(bundle_path.exists())

            adapter = BrowserSnapshotDropzoneAdapter()
            courts = [
                CourtRecord(club_id="house_of_padel", court_id=f"c{i}", court_name=f"Court {i}", sport="PADEL")
                for i in range(1, 6)
            ]
            runs = adapter.parse_weekly_bundle_text(
                bundle_path.read_text(encoding="utf-8"),
                club_id="house_of_padel",
                courts=courts,
                opening_hours={},
                snapshot_tag="7d",
            )
            self.assertEqual(runs, [])

    def test_multi_snapshot_intraday_fusion_and_hidden_peak_discovery(self) -> None:
        self.assertEqual(
            compress_hour_spans([7, 12, 13, 17, 18, 19, 20]),
            "07:00–08:00, 12:00–14:00, 17:00–21:00",
        )

        courts = {
            ("test_club", f"c{i}"): CourtRecord(
                club_id="test_club",
                court_id=f"c{i}",
                court_name=f"Court {i}",
                sport="PADEL",
                court_environment="indoor",
            )
            for i in range(1, 5)
        }
        oh = {
            ("test_club", "TUESDAY"): OpeningHoursRecord(
                club_id="test_club",
                day_of_week="TUESDAY",
                opening_time="07:00",
                closing_time="22:00",
            )
        }
        adapter = BrowserSnapshotDropzoneAdapter()

        # Pass 1 (7d ahead): light early bookings in prime evening only
        b_7d = (
            "=== 2026-11-10 ===\n"
            "07:00-17:00 = 4 free €30\n"
            "17:00-21:00 = 2 free €48\n"
            "21:00-22:00 = 4 free €40\n"
        )
        # Pass 3 (24h ahead): pre-work 07:00 & lunchtime 12:00-14:00 starting to fill, evening 3/4 booked
        b_24h = (
            "=== 2026-11-10 ===\n"
            "07:00-08:00 = 1 free €30\n"
            "08:00-12:00 = 4 free €30\n"
            "12:00-14:00 = 1 free €32\n"
            "14:00-17:00 = 4 free €30\n"
            "17:00-21:00 = 1 free €48\n"
            "21:00-22:00 = 3 free €40\n"
        )
        # Pass 4 (morning 06:00 on play day): pre-work 07:00 & lunchtime 12:00-14:00 SOLD OUT, off-peak 25% booked
        b_morn = (
            "=== 2026-11-10 ===\n"
            "07:00-08:00 = 0 free €30\n"
            "08:00-12:00 = 3 free €30\n"
            "12:00-14:00 = 0 free €32\n"
            "14:00-17:00 = 3 free €30\n"
            "17:00-21:00 = 1 free €48\n"
            "21:00-22:00 = 3 free €40\n"
        )
        # Pass 5 (prepeak 16:30 on play day): captures final evening 17:00-21:00 SOLD OUT + 21:00-22:00 50% booked
        b_prepeak = (
            "=== 2026-11-10 ===\n"
            "17:00-21:00 = 0 free €48\n"
            "21:00-22:00 = 2 free €40\n"
        )

        all_obs = []
        for tag, txt in (("7d", b_7d), ("24h", b_24h), ("morning", b_morn), ("prepeak", b_prepeak)):
            runs = adapter.parse_weekly_bundle_text(
                txt,
                club_id="test_club",
                courts=courts.values(),
                opening_hours={"TUESDAY": oh[("test_club", "TUESDAY")]},
                snapshot_tag=tag,
            )
            self.assertEqual(len(runs), 1)
            all_obs.extend(runs[0].observations)

        slices = resolve_atomic_slices(all_obs, courts=courts, opening_hours=oh)
        clubs_by_id = {
            "test_club": {
                "club_id": "test_club",
                "club_name": "Test Indoor Club",
                "in_county_dublin": 1,
                "verified_padel_courts": 4,
                "court_environment": "indoor",
            }
        }
        hm = compute_club_hour_heatmap_and_empirical_peaks(slices, clubs_by_id=clubs_by_id)
        mat_row = hm["club_hour_matrix"][0]
        self.assertEqual(mat_row["07:00"], 100.0)
        self.assertEqual(mat_row["09:00"], 25.0)
        self.assertEqual(mat_row["12:00"], 100.0)
        self.assertEqual(mat_row["13:00"], 100.0)
        self.assertEqual(mat_row["15:00"], 25.0)
        self.assertEqual(mat_row["18:00"], 100.0)
        self.assertEqual(mat_row["21:00"], 50.0)

        emp = hm["empirical_bands"][0]
        self.assertEqual(emp["empirical_peak_windows"], "07:00–08:00, 12:00–14:00, 17:00–21:00")
        self.assertEqual(emp["empirical_shoulder_windows"], "21:00–22:00")
        self.assertEqual(emp["empirical_offpeak_windows"], "08:00–12:00, 14:00–17:00")
        self.assertEqual(emp["empirical_peak_occ_pct"], 100.0)
        self.assertEqual(emp["empirical_offpeak_occ_pct"], 25.0)

        prog = compute_snapshot_progression_curve(all_obs, courts=courts, opening_hours=oh, clubs_by_id=clubs_by_id)
        agg = [r for r in prog if r["club_id"] == "ALL_CLUBS"]
        pcts = [r["overall_occupancy_pct"] for r in agg]
        self.assertEqual(pcts, sorted(pcts))
        self.assertGreater(pcts[-1], pcts[0])

    def test_end_to_end_weekly_inbox_switches_heatmap_to_live_public_and_preserves_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"
            inbox_dir = tmp_path / "inbox"
            out_dir = tmp_path / "output"

            db = PadelDatabase(db_path)
            init_and_sync_inventory(db)

            # Seed 2 live courts + Tuesday opening hours for house_of_padel
            courts = [
                CourtRecord(club_id="house_of_padel", court_id="hop_c1", court_name="Court 1", sport="PADEL", court_environment="indoor"),
                CourtRecord(club_id="house_of_padel", court_id="hop_c2", court_name="Court 2", sport="PADEL", court_environment="indoor"),
            ]
            db.upsert_courts(courts)
            db.upsert_opening_hours(
                [
                    OpeningHoursRecord(
                        club_id="house_of_padel",
                        day_of_week="TUESDAY",
                        opening_time="08:00",
                        closing_time="22:00",
                    )
                ]
            )

            # Insert a real live_public tournament confirmed_booking on hop_c1 19:00-20:00
            ev_run = CollectionRunResult(
                run_id="live-ev-hop",
                started_at="2026-11-07T12:00:00+00:00",
                completed_at="2026-11-07T12:00:01+00:00",
                adapter_name="test",
                platform_id="playtomic",
                club_id="house_of_padel",
                target_url="https://playtomic.com/clubs/house-of-padel",
                http_status=200,
                robots_allowed=True,
                auth_required=False,
                run_status="success",
                data_origin="live_public",
                records_written=1,
            )
            db.record_collection_run(ev_run)
            db.insert_observations(
                [
                    AvailabilityObservation.from_raw(
                        club_id="house_of_padel",
                        court_id="hop_c1",
                        booking_platform="playtomic",
                        start_time="2026-11-10T19:00:00+00:00",
                        end_time="2026-11-10T20:00:00+00:00",
                        price_eur=60.0,
                        availability_status="unavailable",
                        status_detail="confirmed_booking",
                        observed_at="2026-11-07T12:00:00+00:00",
                        source_url="https://playtomic.com/clubs/house-of-padel",
                        data_origin="live_public",
                        run_id="live-ev-hop",
                    )
                ]
            )

            pack_dir = inbox_dir / "week_2026-11-09"
            pack_dir.mkdir(parents=True)
            (pack_dir / "house_of_padel__week__2026-11-09__7d.txt").write_text(
                "=== 2026-11-10 ===\n08:00-22:00 = 2 free €45\n",
                encoding="utf-8",
            )
            (pack_dir / "house_of_padel__week__2026-11-09__prepeak.txt").write_text(
                "=== 2026-11-10 ===\n17:00-22:00 = 0 free €60\n",
                encoding="utf-8",
            )

            res1 = ingest_inbox_snapshots(db, inbox_dir=inbox_dir)
            self.assertEqual(res1["inbox_files_processed"], 2)
            self.assertGreater(res1["inbox_observations_inserted"], 0)

            # Verify idempotency on second scan
            res2 = ingest_inbox_snapshots(db, inbox_dir=inbox_dir)
            self.assertEqual(res2["inbox_observations_inserted"], 0)

            wk = run_weekly_peak_analysis(db_path=db_path, week_start=date(2026, 11, 9), output_dir=out_dir)
            self.assertEqual(wk["dataset_origin"], "live_public")
            self.assertTrue(wk["weekly_heatmap_xlsx"].exists())

            # Check that confirmed_booking from Playtomic tournament was preserved alongside prepeak dropzone snapshot
            live_obs = db.fetch_observations(data_origin="live_public", club_id="house_of_padel")
            _, c_map, oh_map = (
                None,
                {("house_of_padel", c.court_id): c for c in courts},
                {("house_of_padel", "TUESDAY"): OpeningHoursRecord("house_of_padel", "TUESDAY", "08:00", "22:00")},
            )
            slices = resolve_atomic_slices(live_obs, courts=c_map, opening_hours=oh_map)
            confirmed_19 = [s for s in slices if s.court_id == "hop_c1" and s.hour_of_day == 19]
            self.assertEqual(len(confirmed_19), 4)
            self.assertTrue(all(s.final_state == "confirmed_booking" for s in confirmed_19))

            wb = openpyxl.load_workbook(io.BytesIO(wk["weekly_heatmap_xlsx"].read_bytes()))
            self.assertEqual(
                wb.sheetnames,
                [
                    "Club x Hour Heatmap (06-23)",
                    "Weekday vs Weekend Heatmap",
                    "Empirical Peak & Off-Peak",
                    "5-Snapshot Progression",
                    "Live Next-Week Organised",
                    "5-Snapshot Gathering Plan",
                ],
            )


if __name__ == "__main__":
    unittest.main()
