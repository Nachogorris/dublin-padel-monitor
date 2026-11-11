"""Automated tests for SQLite/CSV deduplication and 60m/90m overlapping slot resolution."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.adapters import CsvObservationAdapter
from padel_monitor.db import PadelDatabase
from padel_monitor.inventory import BOOKING_PLATFORMS, VERIFIED_CLUBS
from padel_monitor.models import AvailabilityObservation
from padel_monitor.occupancy import resolve_atomic_slices, summarize_occupancy


class DedupAndOverlapTests(unittest.TestCase):
    def test_sqlite_duplicate_observations_are_ignored_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = PadelDatabase(Path(tmp) / "test.db")
            db.initialize()
            db.upsert_platforms(BOOKING_PLATFORMS)
            db.upsert_clubs(VERIFIED_CLUBS)

            obs = AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="court_1",
                booking_platform="playtomic",
                start_time="2026-11-10T18:00:00+00:00",
                end_time="2026-11-10T19:00:00+00:00",
                price_eur=50.0,
                availability_status="available",
                observed_at="2026-11-09T18:00:00+00:00",
                source_url="https://playtomic.com/clubs/house-of-padel",
            )
            ins1, dup1 = db.insert_observations([obs, obs])
            self.assertEqual((ins1, dup1), (1, 1))

            ins2, dup2 = db.insert_observations([obs])
            self.assertEqual((ins2, dup2), (0, 1))
            self.assertEqual(len(db.fetch_observations()), 1)

    def test_csv_adapter_handles_duplicates_and_bad_rows_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_file = Path(tmp) / "feed.csv"
            csv_file.write_text(
                "observed_at,venue_id,court_id,slot_start,slot_end,status,price_eur,source_url\n"
                "2026-11-06T12:00:00+00:00,house_of_padel,c1,2026-11-07T18:00:00+00:00,2026-11-07T19:00:00+00:00,available,40.0,https://playtomic.com/clubs/house-of-padel\n"
                "2026-11-06T12:00:00+00:00,house_of_padel,c1,2026-11-07T18:00:00+00:00,2026-11-07T19:00:00+00:00,available,40.0,https://playtomic.com/clubs/house-of-padel\n"
                "2026-11-06T12:00:00,house_of_padel,c1,2026-11-07T19:00:00+00:00,2026-11-07T20:00:00+00:00,available,40.0,https://playtomic.com/clubs/house-of-padel\n",
                encoding="utf-8",
            )
            adapter = CsvObservationAdapter()
            run, errors = adapter.load_csv(csv_file, strict=False)
            self.assertEqual(len(run.observations), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("timezone offset", errors[0])

    def test_overlapping_60_and_90_minute_slots_do_not_double_count_capacity(self) -> None:
        """5 overlapping 60m & 90m slots across 18:00–20:00 (raw sum = 360 mins = 6h)
        must resolve to exactly 2.00 physical court-hours!
        """
        slots = [
            ("2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", 50.0, "available"),
            ("2026-11-10T18:00:00+00:00", "2026-11-10T19:30:00+00:00", 75.0, "available"),
            ("2026-11-10T18:30:00+00:00", "2026-11-10T19:30:00+00:00", 50.0, "available"),
            ("2026-11-10T18:30:00+00:00", "2026-11-10T20:00:00+00:00", 75.0, "available"),
            ("2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", 50.0, "available"),
        ]
        obs = [
            AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="court_1",
                booking_platform="playtomic",
                start_time=s,
                end_time=e,
                price_eur=p,
                availability_status=st,
                observed_at="2026-11-09T18:00:00+00:00",
                source_url="synthetic://test",
                data_origin="synthetic_validation",
            )
            for s, e, p, st in slots
        ]
        slices = resolve_atomic_slices(obs)
        summary = summarize_occupancy(slices, "club", lambda s: s.club_id)[0]
        self.assertEqual(summary.offered_bookable_hours, 2.0)
        self.assertEqual(summary.available_hours, 2.0)
        self.assertEqual(summary.estimated_occupancy_pct, 0.0)

    def test_partial_overlap_booking_resolves_exact_free_and_booked_hours(self) -> None:
        """Suppose 19:00–20:00 is booked. Then:
        - 18:00–19:00 (60m) is AVAILABLE
        - 18:00–19:30 (90m) is UNAVAILABLE (blocked by 19:00 booking)
        - 18:30–19:30 (60m) is UNAVAILABLE (blocked by 19:00 booking)
        - 18:30–20:00 (90m) is UNAVAILABLE (blocked by 19:00 booking)
        - 19:00–20:00 (60m) is UNAVAILABLE
        Physical reality: 18:00–19:00 (including 18:30–19:00!) is FREE (1.0h), and 19:00–20:00 is BOOKED (1.0h) -> 50.0% occupancy!
        """
        slots = [
            ("2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", 50.0, "available"),
            ("2026-11-10T18:00:00+00:00", "2026-11-10T19:30:00+00:00", None, "unavailable"),
            ("2026-11-10T18:30:00+00:00", "2026-11-10T19:30:00+00:00", None, "unavailable"),
            ("2026-11-10T18:30:00+00:00", "2026-11-10T20:00:00+00:00", None, "unavailable"),
            ("2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", None, "unavailable"),
        ]
        obs = [
            AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="court_1",
                booking_platform="playtomic",
                start_time=s,
                end_time=e,
                price_eur=p,
                availability_status=st,
                observed_at="2026-11-10T12:00:00+00:00",
                source_url="synthetic://test",
                data_origin="synthetic_validation",
            )
            for s, e, p, st in slots
        ]
        slices = resolve_atomic_slices(obs)
        summary = summarize_occupancy(slices, "club", lambda s: s.club_id)[0]
        self.assertEqual(summary.offered_bookable_hours, 2.0)
        self.assertEqual(summary.available_hours, 1.0)
        self.assertEqual(summary.total_unavailable_bookable_hours, 1.0)
        self.assertEqual(summary.estimated_occupancy_pct, 50.0)


if __name__ == "__main__":
    unittest.main()

