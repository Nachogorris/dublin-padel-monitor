"""Automated tests for normalization, validation, and Europe/Dublin DST timezones."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.models import AvailabilityObservation
from padel_monitor.time_utils import (
    DUBLIN_TZ,
    generate_atomic_slices,
    is_peak_dublin_hour,
    parse_iso_aware,
    resolve_operating_window,
    to_dublin,
)


class NormalizationAndTimezoneTests(unittest.TestCase):
    def test_utc_to_dublin_summer_ist_vs_winter_gmt(self) -> None:
        # Summer: Ireland is on IST (UTC+01:00)
        obs_summer = AvailabilityObservation.from_raw(
            club_id="house_of_padel",
            court_id="court_1",
            booking_platform="Playtomic",
            start_time="2026-07-15T17:00:00Z",
            end_time="2026-07-15T18:30:00Z",
            price_eur="€75.00",
            availability_status="AVAILABLE",
            observed_at="2026-07-14T17:00:00Z",
            source_url="https://playtomic.com/clubs/house-of-padel",
        )
        self.assertEqual(obs_summer.start_time, "2026-07-15T18:00:00+01:00")
        self.assertEqual(obs_summer.end_time, "2026-07-15T19:30:00+01:00")
        self.assertEqual(obs_summer.duration_minutes, 90)
        self.assertEqual(obs_summer.price_eur, 75.0)
        self.assertEqual(obs_summer.snapshot_bucket, "24h")
        self.assertEqual(obs_summer.lead_time_hours, 24.0)

        # Winter: Ireland is on GMT (UTC+00:00)
        obs_winter = AvailabilityObservation.from_raw(
            club_id="house_of_padel",
            court_id="court_1",
            booking_platform="playtomic",
            start_time="2026-11-15T18:00:00+01:00",  # CET input converted to Dublin GMT
            end_time="2026-11-15T19:00:00+01:00",
            price_eur=40,
            availability_status="available",
            observed_at="2026-11-12T18:00:00+01:00",
            source_url="https://playtomic.com/clubs/house-of-padel",
        )
        self.assertEqual(obs_winter.start_time, "2026-11-15T17:00:00+00:00")
        self.assertEqual(obs_winter.duration_minutes, 60)
        self.assertEqual(obs_winter.snapshot_bucket, "3d")

    def test_dst_spring_forward_and_autumn_fallback_physical_slices(self) -> None:
        # Spring forward in Europe/Dublin: Sunday 2026-03-29 01:00 GMT -> 02:00 IST
        # Wall-clock 00:00 to 04:00 is only 3.0 physical hours (12 x 15-min slices)
        w_start, w_end = resolve_operating_window(date(2026, 3, 29), "00:00", "04:00")
        spring_slices = generate_atomic_slices(w_start, w_end, slice_minutes=15)
        self.assertEqual(len(spring_slices), 12)

        # Autumn fallback in Europe/Dublin: Sunday 2026-10-25 02:00 IST -> 01:00 GMT
        # Wall-clock 00:00 to 04:00 is 5.0 physical hours (20 x 15-min slices)
        f_start, f_end = resolve_operating_window(date(2026, 10, 25), "00:00", "04:00")
        fall_slices = generate_atomic_slices(f_start, f_end, slice_minutes=15)
        self.assertEqual(len(fall_slices), 20)

    def test_post_midnight_closing_window(self) -> None:
        # House of Padel Friday hours: 06:00 -> 01:00 next calendar morning (19 hours)
        open_dt, close_dt = resolve_operating_window(date(2026, 11, 13), "06:00", "01:00")
        self.assertEqual(open_dt.date(), date(2026, 11, 13))
        self.assertEqual(close_dt.date(), date(2026, 11, 14))
        slices_30m = generate_atomic_slices(open_dt, close_dt, slice_minutes=30)
        self.assertEqual(len(slices_30m), 38)  # 19.0 hours

    def test_validation_rejects_naive_and_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            parse_iso_aware("2026-11-07T18:00:00")  # Missing timezone offset

        with self.assertRaises(ValueError):
            AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="c1",
                booking_platform="playtomic",
                start_time="2026-11-07T19:00:00+00:00",
                end_time="2026-11-07T18:00:00+00:00",  # end before start
                availability_status="available",
                observed_at="2026-11-06T18:00:00+00:00",
                source_url="https://example.com",
            )

        with self.assertRaises(ValueError):
            AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="c1",
                booking_platform="playtomic",
                start_time="2026-11-07T18:00:00+00:00",
                end_time="2026-11-07T19:00:00+00:00",
                availability_status="maybe_free",  # invalid status
                observed_at="2026-11-06T18:00:00+00:00",
                source_url="https://example.com",
            )

        with self.assertRaises(ValueError):
            AvailabilityObservation.from_raw(
                club_id="house_of_padel",
                court_id="c1",
                booking_platform="playtomic",
                start_time="2026-11-07T18:00:00+00:00",
                end_time="2026-11-07T19:00:00+00:00",
                price_eur=-10.0,  # negative price
                availability_status="available",
                observed_at="2026-11-06T18:00:00+00:00",
                source_url="https://example.com",
            )

    def test_peak_vs_offpeak_classification(self) -> None:
        mon_morning = to_dublin(datetime.fromisoformat("2026-11-09T11:00:00+00:00"))
        mon_evening = to_dublin(datetime.fromisoformat("2026-11-09T18:30:00+00:00"))
        sat_midday = to_dublin(datetime.fromisoformat("2026-11-14T12:00:00+00:00"))
        sat_late = to_dublin(datetime.fromisoformat("2026-11-14T21:00:00+00:00"))
        self.assertFalse(is_peak_dublin_hour(mon_morning))
        self.assertTrue(is_peak_dublin_hour(mon_evening))
        self.assertTrue(is_peak_dublin_hour(sat_midday))
        self.assertFalse(is_peak_dublin_hour(sat_late))


if __name__ == "__main__":
    unittest.main()

