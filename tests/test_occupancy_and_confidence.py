"""Automated tests for occupancy transitions, GBV price carry-forward, blocked/unknown handling, and Official API privacy."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.adapters import PlaytomicOfficialApiAdapter
from padel_monitor.models import AvailabilityObservation, CourtRecord, OpeningHoursRecord
from padel_monitor.occupancy import resolve_atomic_slices, summarize_occupancy


class OccupancyAndConfidenceTests(unittest.TestCase):
    def test_transition_detection_and_price_carry_forward_for_gbv(self) -> None:
        # 18:00-19:00 is available at €50 at 7d and 3d, then becomes unavailable (price=None) at 24h
        # 19:00-20:00 remains available at €50 across all snapshots
        snapshots = [
            ("2026-11-03T18:00:00+00:00", "2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", 50.0, "available"),
            ("2026-11-07T18:00:00+00:00", "2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", 50.0, "available"),
            ("2026-11-09T18:00:00+00:00", "2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", None, "unavailable"),
            ("2026-11-10T12:00:00+00:00", "2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", None, "unavailable"),
            ("2026-11-03T18:00:00+00:00", "2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", 50.0, "available"),
            ("2026-11-07T18:00:00+00:00", "2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", 50.0, "available"),
            ("2026-11-09T18:00:00+00:00", "2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", 50.0, "available"),
            ("2026-11-10T12:00:00+00:00", "2026-11-10T19:00:00+00:00", "2026-11-10T20:00:00+00:00", 50.0, "available"),
        ]
        obs = [
            AvailabilityObservation.from_raw(
                club_id="star_padel",
                court_id="padel_1",
                booking_platform="playtomic",
                start_time=s,
                end_time=e,
                price_eur=p,
                availability_status=st,
                observed_at=obs_t,
                source_url="synthetic://test",
                data_origin="synthetic_validation",
            )
            for obs_t, s, e, p, st in snapshots
        ]
        courts = {
            ("star_padel", "padel_1"): CourtRecord(
                club_id="star_padel",
                court_id="padel_1",
                court_name="Padel 1",
                sport="PADEL",
                court_environment="indoor",
                court_size="double",
            )
        }
        slices = resolve_atomic_slices(obs, courts=courts)
        summary = summarize_occupancy(slices, "club", lambda s: s.club_id)[0]

        self.assertEqual(summary.offered_bookable_hours, 2.0)
        self.assertEqual(summary.inferred_transition_unavail_hours, 1.0)
        self.assertEqual(summary.static_unavail_hours, 0.0)
        self.assertEqual(summary.estimated_occupancy_pct, 50.0)
        self.assertEqual(summary.transition_backed_occupancy_pct, 50.0)
        self.assertEqual(summary.est_gross_booking_value_eur, 50.0)
        self.assertEqual(summary.est_gbv_member_adjusted_low_eur, 41.0)
        self.assertEqual(summary.confidence_level, "high")

    def test_blocked_and_unknown_excluded_from_bookable_denominator(self) -> None:
        # 4 hours total: 1h available, 1h unavailable, 1h blocked (maintenance), 1h unknown (HTTP failure)
        # Bookable offered = 2.0h (1h avail + 1h unavail) -> Occupancy = 50.0%, NOT 25% or 75%!
        items = [
            ("2026-11-10T09:00:00+00:00", "2026-11-10T10:00:00+00:00", 40.0, "available"),
            ("2026-11-10T10:00:00+00:00", "2026-11-10T11:00:00+00:00", 40.0, "unavailable"),
            ("2026-11-10T11:00:00+00:00", "2026-11-10T12:00:00+00:00", None, "blocked"),
            ("2026-11-10T12:00:00+00:00", "2026-11-10T13:00:00+00:00", None, "unknown"),
        ]
        obs = [
            AvailabilityObservation.from_raw(
                club_id="halpenny",
                court_id="c1",
                booking_platform="playtomic",
                start_time=s,
                end_time=e,
                price_eur=p,
                availability_status=st,
                observed_at="2026-11-10T07:00:00+00:00",
                source_url="synthetic://test",
                data_origin="synthetic_validation",
            )
            for s, e, p, st in items
        ]
        slices = resolve_atomic_slices(obs)
        summary = summarize_occupancy(slices, "club", lambda s: s.club_id)[0]
        self.assertEqual(summary.total_capacity_hours, 4.0)
        self.assertEqual(summary.offered_bookable_hours, 2.0)
        self.assertEqual(summary.blocked_hours, 1.0)
        self.assertEqual(summary.unknown_hours, 1.0)
        self.assertEqual(summary.estimated_occupancy_pct, 50.0)
        # 25% unknown + single snapshot -> low confidence
        self.assertEqual(summary.confidence_level, "low")

    def test_official_api_adapter_reconstructs_confirmed_bookings_without_pii(self) -> None:
        adapter = PlaytomicOfficialApiAdapter(client_id="demo", client_secret="demo")
        courts = [
            CourtRecord(club_id="ob_padel", court_id="p1", court_name="Padel 1", sport="PADEL", court_environment="indoor"),
            CourtRecord(club_id="ob_padel", court_id="sq1", court_name="Squash", sport="SQUASH", court_environment="indoor"),
        ]
        oh = {
            "TUESDAY": OpeningHoursRecord(
                club_id="ob_padel",
                day_of_week="TUESDAY",
                opening_time="18:00",
                closing_time="20:00",
            )
        }
        payload = [
            {
                "resource_id": "p1",
                "start_date": "2026-11-10T18:00:00Z",
                "end_date": "2026-11-10T19:00:00Z",
                "booking_type": "PRIVATE_MATCH",
                "status": "CONFIRMED",
                "price": "70.00 EUR",
                "player_name": "Jane Doe",
                "player_email": "jane@example.com",
            },
            {
                "resource_id": "p1",
                "start_date": "2026-11-10T19:00:00Z",
                "end_date": "2026-11-10T19:30:00Z",
                "booking_type": "ACADEMY_CLASS",
                "status": "CONFIRMED",
                "price": "0 EUR",
            },
            {
                "resource_id": "p1",
                "start_date": "2026-11-10T19:30:00Z",
                "end_date": "2026-11-10T20:00:00Z",
                "booking_type": "PRIVATE_MATCH",
                "status": "CANCELLED",
                "price": "35.00 EUR",
            },
        ]
        obs = adapter.normalize_authorized_bookings(
            club_id="ob_padel",
            target_date=date(2026, 11, 10),
            courts=courts,
            opening_hours=oh,
            bookings_payload=payload,
            observed_at="2026-11-10T12:00:00+00:00",
        )
        # Squash court excluded; 2 hours -> 4 x 30-min observations on p1
        self.assertEqual(len(obs), 4)
        for o in obs:
            self.assertNotIn("Jane", repr(o))
            self.assertEqual(o.data_origin, "live_authorized_api")

        slices = resolve_atomic_slices(obs, courts={("ob_padel", "p1"): courts[0]})
        summary = summarize_occupancy(slices, "club", lambda s: s.club_id)[0]
        # 1.0h confirmed + 0.5h blocked + 0.5h available (cancelled slot became available)
        self.assertEqual(summary.confirmed_booked_hours, 1.0)
        self.assertEqual(summary.blocked_hours, 0.5)
        self.assertEqual(summary.available_hours, 0.5)
        self.assertEqual(summary.offered_bookable_hours, 1.5)
        self.assertAlmostEqual(summary.estimated_occupancy_pct or 0.0, 66.67, places=2)
        self.assertEqual(summary.est_gross_booking_value_eur, 70.0)


if __name__ == "__main__":
    unittest.main()

