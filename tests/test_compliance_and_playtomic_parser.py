"""Automated tests for robots.txt compliance gates, portal probes, and live Playtomic SSR HTML parsing."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "data" / "raw" / "policy"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.adapters import GenericPortalProbeAdapter, PlaytomicPublicClubAdapter
from padel_monitor.adapters.playtomic_public import parse_playtomic_club_html
from padel_monitor.compliance import CompliantHttpClient


class ComplianceAndPlaytomicParserTests(unittest.TestCase):
    def test_playtomic_robots_allows_club_page_and_blocks_dated_slot_queries(self) -> None:
        robots_txt = (POLICY_DIR / "playtomic.com__robots.txt.txt").read_text(encoding="utf-8")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text=robots_txt, headers={"content-type": "text/plain"})
            return httpx.Response(200, text="<html>ok</html>")

        with CompliantHttpClient(min_delay_seconds=0.0, transport=httpx.MockTransport(handler)) as client:
            self.assertTrue(client.is_robots_allowed("https://playtomic.com/clubs/house-of-padel"))
            self.assertFalse(client.is_robots_allowed("https://playtomic.com/api/v1/availability"))
            self.assertFalse(
                client.is_robots_allowed("https://playtomic.com/clubs/house-of-padel?date=2026-11-07&sport=PADEL")
            )

            adapter = PlaytomicPublicClubAdapter(http_client=client)
            res = adapter.collect_club_availability(
                "house_of_padel",
                "https://playtomic.com/clubs/house-of-padel",
                date(2026, 11, 7),
            )
            self.assertEqual(res.run_status, "blocked_compliance")
            self.assertFalse(res.robots_allowed)
            self.assertEqual(res.records_written, 0)

    def test_parse_live_saved_house_of_padel_html(self) -> None:
        html_file = POLICY_DIR / "playtomic.com__clubs_house-of-padel.txt"
        html_text = html_file.read_text(encoding="utf-8")
        profile = parse_playtomic_club_html(
            html_text,
            club_id="house_of_padel",
            source_url="https://playtomic.com/clubs/house-of-padel",
            observed_at="2026-11-06T22:21:20+00:00",
        )
        self.assertEqual(profile.tenant_id, "732095a5-96ee-46a3-bbf6-b649533552d8")
        self.assertEqual(profile.timezone, "Europe/Dublin")
        self.assertFalse(profile.is_private)

        padel_courts = [c for c in profile.courts if c.sport == "PADEL"]
        non_padel = [c for c in profile.courts if c.sport != "PADEL"]
        self.assertEqual(len(padel_courts), 5)
        self.assertEqual(len(non_padel), 1)
        self.assertEqual(non_padel[0].sport, "PICKLEBALL")

        doubles = [c for c in padel_courts if c.court_size == "double"]
        singles = [c for c in padel_courts if c.court_size == "single"]
        self.assertEqual(len(doubles), 4)
        self.assertEqual(len(singles), 1)
        self.assertTrue(all(c.court_environment == "indoor" for c in padel_courts))

        # Check post-midnight closing hours (e.g. Friday 06:00 -> 01:00 closes_next_day=True)
        by_dow = {oh.day_of_week: oh for oh in profile.opening_hours}
        self.assertEqual(by_dow["FRIDAY"].opening_time, "06:00")
        self.assertEqual(by_dow["FRIDAY"].closing_time, "01:00")
        self.assertTrue(by_dow["FRIDAY"].closes_next_day)

    def test_matchpoint_playbypoint_gladstone_and_rockbrook_cached_probes(self) -> None:
        mp = GenericPortalProbeAdapter("matchpoint").evaluate_cached_probe(
            club_id="padel_dome",
            url="https://padeldome-ie.matchpoint.com.es/",
            final_url="https://padeldome-ie.matchpoint.com.es/Login.aspx",
            http_status=200,
            fetched_at_iso="2026-11-06T22:21:29+00:00",
            body_path=POLICY_DIR / "padeldome-ie.matchpoint.com.es__root.txt",
            robots_path=POLICY_DIR / "padeldome-ie.matchpoint.com.es__robots.txt.txt",
        )
        self.assertEqual(mp.run_status, "blocked_auth")
        self.assertTrue(mp.auth_required)

        pbp = GenericPortalProbeAdapter("playbypoint").evaluate_cached_probe(
            club_id="bushy_park",
            url="https://bushytennispadel.playbypoint.com/",
            final_url="https://bushytennispadel.playbypoint.com/",
            http_status=403,
            fetched_at_iso="2026-11-06T22:30:39+00:00",
            body_path=POLICY_DIR / "bushytennispadel.playbypoint.com__root.txt",
            robots_path=POLICY_DIR / "bushytennispadel.playbypoint.com__robots.txt.txt",
        )
        self.assertEqual(pbp.run_status, "blocked_antibot")

        glad = GenericPortalProbeAdapter("gladstonego").evaluate_cached_probe(
            club_id="portmarnock",
            url="https://pslc.gladstonego.cloud/book",
            final_url="https://pslc.gladstonego.cloud/book",
            http_status=200,
            fetched_at_iso="2026-11-06T22:30:23+00:00",
            body_path=POLICY_DIR / "pslc.gladstonego.cloud__book.txt",
            robots_path=POLICY_DIR / "pslc.gladstonego.cloud__robots.txt.txt",
        )
        self.assertEqual(glad.run_status, "blocked_compliance")
        self.assertIn("SPA", glad.error_message or "")

        rock = GenericPortalProbeAdapter("padel_ireland_wp").evaluate_cached_probe(
            club_id="rockbrook",
            url="https://padel-ireland.ie/bookings/",
            final_url="https://padel-ireland.ie/bookings/",
            http_status=200,
            fetched_at_iso="2026-11-06T22:30:26+00:00",
            body_path=POLICY_DIR / "padel-ireland.ie__bookings.txt",
            robots_path=POLICY_DIR / "padel-ireland.ie__robots.txt.txt",
        )
        self.assertEqual(rock.run_status, "parse_error")
        self.assertIn("rncbc_calendar", rock.error_message or "")


if __name__ == "__main__":
    unittest.main()

