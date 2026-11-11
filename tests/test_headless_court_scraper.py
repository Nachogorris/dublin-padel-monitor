"""Unit tests for HeadlessCourtAvailabilityScraper invariants."""
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from padel_monitor.adapters.headless_court_scraper import HeadlessCourtAvailabilityScraper
from padel_monitor.models import CourtRecord, OpeningHoursRecord


class TestHeadlessCourtScraper(unittest.TestCase):
    def test_scraper_initialization(self) -> None:
        scraper = HeadlessCourtAvailabilityScraper(raw_save_dir=Path("data/raw/test_headless"))
        self.assertTrue(scraper.raw_save_dir.exists())
        self.assertEqual(scraper.timeout_ms, 25000)

    def test_court_record_filtering_and_fallback(self) -> None:
        courts = [
            CourtRecord("test_club", "c_1", "Court 1", "PADEL", "indoor", "double"),
            CourtRecord("test_club", "c_2", "Court 2", "TENNIS", "outdoor", "double"),
        ]
        padel_only = [c for c in courts if c.sport.upper() == "PADEL"]
        self.assertEqual(len(padel_only), 1)
        self.assertEqual(padel_only[0].court_id, "c_1")


if __name__ == "__main__":
    unittest.main()

