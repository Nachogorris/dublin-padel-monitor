"""Automated validation tests for the API-Free Court Measurement System."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.adapters import (
    BrowserSnapshotDropzoneAdapter,
    build_spot_check_record,
    classify_spot_stratum,
    estimate_stratified_occupancy,
)
from padel_monitor.inventory import VERIFIED_CLUBS
from padel_monitor.models import CourtRecord, OpeningHoursRecord
from padel_monitor.non_api_measurement import (
    NonApiCourtMeasurementEngine,
    compute_activity_velocity_pairs,
    convert_activities_to_live_observations,
    parse_playtomic_activity_cards,
)
from padel_monitor.occupancy import build_all_occupancy_breakdowns, resolve_atomic_slices


class TestNonApiMeasurementSystem(unittest.TestCase):
    def test_parse_playtomic_activity_and_membership_cards_live_html(self) -> None:
        main_html_path = ROOT / "data" / "raw" / "non_api_telemetry" / "20261107__playtomic.com_clubs_house-of-padel.html"
        tourn_html_path = (
            ROOT
            / "data"
            / "raw"
            / "non_api_telemetry"
            / "20261107__playtomic.com_clubs_house-of-padel_competitions_tournaments.html"
        )
        self.assertTrue(main_html_path.exists(), "Expected cached T2 House of Padel main HTML")
        self.assertTrue(tourn_html_path.exists(), "Expected cached T2 House of Padel tournaments HTML")

        acts_main, offers_main = parse_playtomic_activity_cards(
            main_html_path.read_text(encoding="utf-8"),
            club_id="house_of_padel",
            source_url="https://playtomic.com/clubs/house-of-padel",
            observed_at="2026-11-07T23:27:57+00:00",
            club_padel_courts=5,
        )
        self.assertGreaterEqual(len(acts_main), 6)
        # Verify memberships & coaching offers parsed without any API
        offer_titles = {o["title"]: o for o in offers_main}
        self.assertIn("Off Peak Unlimited", offer_titles)
        self.assertEqual(offer_titles["Off Peak Unlimited"]["price_eur"], 80.0)
        self.assertEqual(offer_titles["Off Peak Unlimited"]["off_peak_unlimited"], 1)
        self.assertEqual(offer_titles["Off Peak Unlimited"]["advance_booking_days"], 14)

        self.assertIn("Membership", offer_titles)
        self.assertEqual(offer_titles["Membership"]["price_eur"], 40.0)
        self.assertEqual(offer_titles["Membership"]["court_discount_pct"], 25.0)

        acts_tourn, _ = parse_playtomic_activity_cards(
            tourn_html_path.read_text(encoding="utf-8"),
            club_id="house_of_padel",
            source_url="https://playtomic.com/clubs/house-of-padel/competitions/tournaments",
            observed_at="2026-11-07T23:28:04+00:00",
            club_padel_courts=5,
        )
        self.assertGreaterEqual(len(acts_tourn), 25)
        for a in acts_tourn:
            self.assertEqual(a["activity_category"], "tournament")
            self.assertIsNotNone(a["start_time"])
            self.assertGreaterEqual(a["courts_occupied"], 1)
            self.assertLessEqual(a["courts_occupied"], 5)

        # Verify Next.js RSC Suspense S:XX -> P:XX resolution on Druid Kimmage tournaments page
        kimmage_tourn_path = (
            ROOT
            / "data"
            / "raw"
            / "non_api_telemetry"
            / "20261107__playtomic.com_clubs_druid-padel-kimmage_competitions_tournaments.html"
        )
        acts_kim, _ = parse_playtomic_activity_cards(
            kimmage_tourn_path.read_text(encoding="utf-8"),
            club_id="druid_kimmage",
            source_url="https://playtomic.com/clubs/druid-padel-kimmage/competitions/tournaments",
            observed_at="2026-11-07T23:28:23+00:00",
            club_padel_courts=3,
        )
        distinct_dates = {a["start_time"][:10] for a in acts_kim if a.get("start_time")}
        self.assertGreaterEqual(len(distinct_dates), 15)

    def test_24h_live_signup_velocity_pair_detection(self) -> None:
        t1_path = ROOT / "data" / "raw" / "policy" / "playtomic.com__clubs_house-of-padel.txt"
        t2_path = ROOT / "data" / "raw" / "non_api_telemetry" / "20261107__playtomic.com_clubs_house-of-padel.html"

        acts_t1, _ = parse_playtomic_activity_cards(
            t1_path.read_text(encoding="utf-8"),
            club_id="house_of_padel",
            source_url="https://playtomic.com/clubs/house-of-padel",
            observed_at="2026-11-06T22:21:18+00:00",
            club_padel_courts=5,
        )
        acts_t2, _ = parse_playtomic_activity_cards(
            t2_path.read_text(encoding="utf-8"),
            club_id="house_of_padel",
            source_url="https://playtomic.com/clubs/house-of-padel",
            observed_at="2026-11-07T23:27:57+00:00",
            club_padel_courts=5,
        )
        pairs = compute_activity_velocity_pairs(acts_t1 + acts_t2)
        self.assertGreaterEqual(len(pairs), 2)
        by_id = {p["activity_id"]: p for p in pairs}
        self.assertIn("61863e13-04c2-4727-afae-520ef63b9a0a", by_id)
        lunch = by_id["61863e13-04c2-4727-afae-520ef63b9a0a"]
        self.assertEqual(lunch["players_t1"], 6)
        self.assertEqual(lunch["players_t2"], 7)
        self.assertEqual(lunch["delta_players"], 1)
        self.assertGreater(lunch["elapsed_hours"], 24.0)

        # Sub-hour duplicate fetches (<1h apart) must NOT emit spurious velocity rows
        acts_t2_dup = [dict(a, observed_at="2026-11-07T23:28:05+00:00") for a in acts_t2]
        self.assertEqual(compute_activity_velocity_pairs(acts_t2 + acts_t2_dup), [])

    def test_convert_activities_to_live_court_observations(self) -> None:
        courts = {
            "star_padel": [
                CourtRecord("star_padel", "c1", "Court 1", "PADEL", "indoor", "double"),
                CourtRecord("star_padel", "c2", "Court 2", "PADEL", "indoor", "double"),
                CourtRecord("star_padel", "c3", "Court 3", "PADEL", "indoor", "double"),
            ]
        }
        sample_acts = [
            {
                "club_id": "star_padel",
                "activity_id": "act-full",
                "activity_category": "tournament",
                "start_time": "2026-11-18T19:00:00+00:00",
                "end_time": "2026-11-18T20:30:00+00:00",
                "courts_occupied": 3,
                "registered_players": 11,
                "max_players": 12,
                "price_per_player_eur": 15.0,
                "observed_at": "2026-11-07T23:28:11+00:00",
                "source_url": "https://playtomic.com/clubs/star-padel-club/competitions/tournaments",
                "run_id": "test-run",
            },
            {
                "club_id": "star_padel",
                "activity_id": "act-empty",
                "activity_category": "tournament",
                "start_time": "2026-11-25T19:00:00+00:00",
                "end_time": "2026-11-25T20:30:00+00:00",
                "courts_occupied": 2,
                "registered_players": 0,
                "max_players": 8,
                "price_per_player_eur": 15.0,
                "observed_at": "2026-11-07T23:28:11+00:00",
                "source_url": "https://playtomic.com/clubs/star-padel-club/competitions/tournaments",
                "run_id": "test-run",
            },
        ]
        obs = convert_activities_to_live_observations(sample_acts, courts)
        self.assertEqual(len(obs), 5)
        confirmed = [o for o in obs if o.status_detail == "confirmed_booking"]
        blocked = [o for o in obs if o.status_detail == "blocked_coaching_or_event"]
        self.assertEqual(len(confirmed), 3)
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all(o.data_origin == "live_public" for o in obs))
        self.assertEqual(confirmed[0].price_eur, 60.0)
        self.assertEqual(confirmed[0].duration_minutes, 90)

    def test_browser_snapshot_dropzone_adapter_reconstructs_full_day(self) -> None:
        courts = [
            CourtRecord("padel_dome", "pd_1", "Court 1", "PADEL", "indoor", "double"),
            CourtRecord("padel_dome", "pd_2", "Court 2", "PADEL", "indoor", "double"),
        ]
        oh = {
            "TUESDAY": OpeningHoursRecord("padel_dome", "TUESDAY", "09:00", "12:00", False),
        }
        pasted_text = """
        Padel Dome Santry — Tuesday 2026-11-10
        Court 1
        09:00
        60 min €30
        10:00
        90 min €45
        Court 2
        No slots available
        """
        adapter = BrowserSnapshotDropzoneAdapter()
        res = adapter.parse_snapshot_text(
            pasted_text,
            club_id="padel_dome",
            target_date=date(2026, 11, 10),
            courts=courts,
            opening_hours=oh,
            observed_at="2026-11-09T20:00:00+00:00",
        )
        self.assertEqual(res.data_origin, "live_public")
        # Court 1: 09:00-10:00 (avail) + 10:00-11:30 (avail) = 2.5h free, 0.5h (11:30-12:00) unavailable
        # Court 2: 3.0h (09:00-12:00) unavailable -> Total 3.5h unavailable / 6.0h offered = 58.3% occupancy
        courts_map = {(c.club_id, c.court_id): c for c in courts}
        oh_map = {("padel_dome", "TUESDAY"): oh["TUESDAY"]}
        slices = resolve_atomic_slices(res.observations, courts=courts_map, opening_hours=oh_map)
        club_summary = build_all_occupancy_breakdowns(slices)["by_club"][0]
        self.assertAlmostEqual(club_summary.offered_bookable_hours, 6.0, places=2)
        self.assertAlmostEqual(club_summary.available_hours, 2.5, places=2)
        self.assertAlmostEqual(club_summary.total_unavailable_bookable_hours, 3.5, places=2)
        self.assertAlmostEqual(club_summary.estimated_occupancy_pct, 58.3, places=1)

    def test_stratified_spot_check_estimator_and_bounds(self) -> None:
        self.assertEqual(classify_spot_stratum("2026-11-10T11:00:00+00:00"), "weekday_offpeak")
        self.assertEqual(classify_spot_stratum("2026-11-10T19:00:00+00:00"), "weekday_peak")
        self.assertEqual(classify_spot_stratum("2026-11-14T11:00:00+00:00"), "weekend_prime")
        self.assertEqual(classify_spot_stratum("2026-11-14T21:00:00+00:00"), "weekend_offpeak")

        checks = [
            build_spot_check_record(
                club_id="padel_dome",
                target_slot_start="2026-11-10T11:00:00+00:00",
                target_slot_end="2026-11-10T12:00:00+00:00",
                total_courts_checked=9,
                occupied_courts=5,
            ),
            build_spot_check_record(
                club_id="padel_dome",
                target_slot_start="2026-11-10T19:00:00+00:00",
                target_slot_end="2026-11-10T20:00:00+00:00",
                total_courts_checked=9,
                occupied_courts=9,
            ),
            build_spot_check_record(
                club_id="padel_dome",
                target_slot_start="2026-11-14T11:00:00+00:00",
                target_slot_end="2026-11-14T12:00:00+00:00",
                total_courts_checked=9,
                occupied_courts=8,
            ),
        ]
        est = estimate_stratified_occupancy(checks)["padel_dome"]
        self.assertEqual(est.samples_count, 3)
        self.assertEqual(est.strata_covered, 3)
        self.assertLessEqual(est.ci95_low_pct, est.point_occupancy_pct)
        self.assertLessEqual(est.point_occupancy_pct, est.ci95_high_pct)
        self.assertGreater(est.point_occupancy_pct, 65.0)

    def test_non_api_court_measurement_engine_invariants(self) -> None:
        oh_by_club = {
            "house_of_padel": [
                OpeningHoursRecord("house_of_padel", d, "06:00", "23:00", False)
                for d in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
            ]
        }
        rows = NonApiCourtMeasurementEngine.measure_public_clubs(
            clubs=VERIFIED_CLUBS,
            oh_by_club=oh_by_club,
            latest_activities=[
                {
                    "club_id": "house_of_padel",
                    "activity_id": "a1",
                    "activity_category": "tournament",
                    "start_time": "2026-11-10T18:00:00+00:00",
                    "court_hours_occupied": 6.0,
                    "registered_players": 14,
                    "max_players": 16,
                    "observed_gross_revenue_eur": 210.0,
                }
            ],
            velocity_pairs=[],
            membership_offers=[
                {"club_id": "house_of_padel", "off_peak_unlimited": 1, "advance_booking_days": 14}
            ],
            spot_checks=[],
            computed_at="2026-11-07T23:30:00+00:00",
        )
        # Exactly 22 public/hybrid active venues (18 County Dublin + 4 Greater Dublin border)
        self.assertEqual(len(rows), 22)
        measured_ids = {r["club_id"] for r in rows}
        self.assertNotIn("west_wood", measured_ids)  # members_only excluded
        self.assertNotIn("swords_pavilions", measured_ids)  # planned 0-court venue excluded

        for r in rows:
            self.assertLessEqual(r["direct_organised_court_hours"], r["total_occupied_court_hours_low"] + 0.05)
            self.assertLessEqual(r["total_occupied_court_hours_low"], r["total_occupied_court_hours_point"])
            self.assertLessEqual(r["total_occupied_court_hours_point"], r["total_occupied_court_hours_high"])
            self.assertLessEqual(r["total_occupied_court_hours_high"], r["weekly_capacity_court_hours"])
            self.assertLessEqual(r["est_weekly_gbv_eur_low"], r["est_weekly_gbv_eur_point"])
            self.assertLessEqual(r["est_weekly_gbv_eur_point"], r["est_weekly_gbv_eur_high"])


if __name__ == "__main__":
    unittest.main()
