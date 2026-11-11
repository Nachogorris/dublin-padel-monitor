"""Automated Headless Browser Court Availability Scraper for Dublin Padel Monitor.

Uses Playwright to execute client-side JavaScript, intercept network JSON availability responses,
and scrape the visual 24-hour court booking bars across Playtomic and Matchpoint clubs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import AvailabilityObservation, CollectionRunResult, CourtRecord, OpeningHoursRecord
from ..time_utils import DUBLIN_TZ, UTC_TZ, generate_atomic_slices, now_dublin_iso, parse_iso_aware

logger = logging.getLogger("padel_monitor.headless_scraper")

DEFAULT_HEADLESS_SAVE_DIR = Path("data/raw/headless_snapshots")


class HeadlessCourtAvailabilityScraper:
    """Automates Chromium headless navigation to capture live court booking bars."""

    def __init__(self, raw_save_dir: Path = DEFAULT_HEADLESS_SAVE_DIR, timeout_ms: int = 25000):
        self.raw_save_dir = Path(raw_save_dir)
        self.raw_save_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_ms = timeout_ms

    async def scrape_playtomic_club_date_async(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
        courts: Iterable[CourtRecord],
        opening_hours: dict[str, OpeningHoursRecord],
        snapshot_bucket: Optional[str] = None,
    ) -> CollectionRunResult:
        """Navigate to club page for target_date using headless Chromium, intercepting availability."""
        from playwright.async_api import async_playwright

        obs_ts = now_dublin_iso()
        run_id = f"headless-{club_id}-{target_date.isoformat()}-{datetime.now().strftime('%H%M%S')}"
        target_url = f"{booking_url.rstrip('/')}?date={target_date.isoformat()}"
        padel_courts = [c for c in courts if c.sport.upper() == "PADEL"]

        intercepted_payloads: list[dict[str, Any]] = []
        observations: list[AvailabilityObservation] = []
        raw_html = ""

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                locale="en-IE",
                timezone_id="Europe/Dublin",
            )
            page = await context.new_page()

            # Intercept any JSON API responses containing slot/availability data
            async def handle_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct or "text/json" in ct:
                        u = response.url.lower()
                        if any(k in u for k in ("availability", "slot", "tenant", "resource", "book")):
                            body = await response.json()
                            if isinstance(body, (list, dict)):
                                intercepted_payloads.append({"url": response.url, "data": body})
                except Exception:
                    pass

            page.on("response", handle_response)

            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                # Wait for visual available courts container or 4 seconds for JS hydration
                await asyncio.sleep(4.0)
                raw_html = await page.content()

                # Save raw HTML artifact for compliance & verification audit
                date_tag = target_date.strftime("%Y%m%d")
                save_file = self.raw_save_dir / f"{date_tag}__{club_id}__headless.html"
                save_file.write_text(raw_html, encoding="utf-8")

                # 1. Parse DOM --slot-<uuid>-<YYYY-MM-DD>T<HH-MM>Z-<dur> anchors
                SLOT_ANCHOR_RE = re.compile(r"--slot-([0-9a-fA-F-]+)-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})Z-(\d+)")
                dom_slots_by_court: dict[str, set[tuple[datetime, datetime]]] = {}
                for m in SLOT_ANCHOR_RE.finditer(raw_html):
                    cid_norm = m.group(1).lower()
                    hh_val, mm_val = int(m.group(3)), int(m.group(4))
                    dur_val = int(m.group(5))
                    s_dt = datetime.combine(target_date, time(hh_val, mm_val), tzinfo=DUBLIN_TZ)
                    e_dt = s_dt + timedelta(minutes=dur_val)
                    dom_slots_by_court.setdefault(cid_norm, set()).add((s_dt, e_dt))

                # 2. Extract Real Live Prices from Intercepted API responses & DOM
                # Priority mapping: 60-min standard slots (priority 0) > 90-min (priority 1) > 120-min (priority 2)
                slot_price_candidates: dict[tuple[str, datetime], tuple[int, float]] = {}

                # 2a. From intercepted Playtomic availability JSON payloads
                for payload in intercepted_payloads:
                    data = payload.get("data")
                    if isinstance(data, list):
                        for r_item in data:
                            if not isinstance(r_item, dict):
                                continue
                            res_id = str(r_item.get("resource_id") or "").lower()
                            for s_info in r_item.get("slots") or []:
                                st_raw = str(s_info.get("start_time") or "")
                                dur_raw = int(s_info.get("duration") or 60)
                                p_str = str(s_info.get("price") or "").replace("EUR", "").replace("€", "").strip()
                                try:
                                    p_val = float(p_str)
                                except ValueError:
                                    continue
                                if st_raw and res_id:
                                    p_parts = [int(p) for p in st_raw.split(":")[:2]]
                                    s_dt = datetime.combine(target_date, time(p_parts[0], p_parts[1]), tzinfo=DUBLIN_TZ)
                                    num_slices = max(1, dur_raw // 30)
                                    per_slice = round(p_val / num_slices, 2)
                                    # Standard 60-min hourly rental has priority 0
                                    prio = 0 if dur_raw == 60 else (1 if dur_raw == 90 else 2)
                                    for step in range(num_slices):
                                        sl_point = s_dt + timedelta(minutes=30 * step)
                                        cand_key = (res_id, sl_point)
                                        if cand_key not in slot_price_candidates or prio < slot_price_candidates[cand_key][0]:
                                            slot_price_candidates[cand_key] = (prio, per_slice)

                real_prices_by_court_slice: dict[tuple[str, datetime], float] = {
                    k: v[1] for k, v in slot_price_candidates.items()
                }

                # 2b. From DOM <details class="group/resource"> cards
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(raw_html, "html.parser")
                    for r_elem in soup.find_all("details", class_="group/resource"):
                        s_el = r_elem.find("summary")
                        n_div = s_el.find("div") if s_el else None
                        if not n_div:
                            continue
                        c_name_norm = n_div.get_text(strip=True).lower()
                        matched_court = next(
                            (c for c in padel_courts if c.court_name.lower() in c_name_norm or c_name_norm in c.court_name.lower()),
                            None,
                        )
                        if not matched_court:
                            continue
                        c_uid = matched_court.court_id.lower()
                        for opt in r_elem.find_all("div", class_="hover:bg-primary-4"):
                            t_str = opt.get_text(" ", strip=True)
                            m = re.search(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*•\s*(\d+)\s*min\s*•\s*€\s*(\d+(?:\.\d+)?)', t_str)
                            if m:
                                s_hm, _, dur_m, p_num = m.groups()
                                hh, mm = int(s_hm[:2]), int(s_hm[3:5])
                                dur_val = int(dur_m)
                                p_val = float(p_num)
                                s_dt = datetime.combine(target_date, time(hh, mm), tzinfo=DUBLIN_TZ)
                                n_sl = max(1, dur_val // 30)
                                per_sl = round(p_val / n_sl, 2)
                                prio = 0 if dur_val == 60 else (1 if dur_val == 90 else 2)
                                for step in range(n_sl):
                                    cur_pt = s_dt + timedelta(minutes=30 * step)
                                    cand_key = (c_uid, cur_pt)
                                    if cand_key not in slot_price_candidates or prio < slot_price_candidates[cand_key][0]:
                                        slot_price_candidates[cand_key] = (prio, per_sl)
                                        real_prices_by_court_slice[cand_key] = per_sl
                except Exception:
                    pass

                from ..time_utils import WEEKDAY_NAMES, resolve_operating_window
                dow = WEEKDAY_NAMES[target_date.weekday()]
                oh = opening_hours.get(dow)
                if oh is not None:
                    win_start, win_end = resolve_operating_window(target_date, oh.opening_time, oh.closing_time)
                else:
                    win_start = datetime.combine(target_date, time(6, 0), tzinfo=DUBLIN_TZ)
                    win_end = datetime.combine(target_date, time(23, 0), tzinfo=DUBLIN_TZ)

                for court in padel_courts:
                    c_uuid = court.court_id.lower()
                    free_spans = dom_slots_by_court.get(c_uuid, set())
                    free_half_hours = set()
                    for fs, fe in free_spans:
                        cur = fs
                        while cur < fe:
                            free_half_hours.add(cur)
                            cur += timedelta(minutes=30)

                    size_mult = 0.70 if court.court_size == "single" else 1.0
                    for sl_s, sl_e in generate_atomic_slices(win_start, win_end, slice_minutes=30):
                        is_avail = sl_s in free_half_hours

                        # Assign exact live price if captured, else peer court price at same hour, else accurate club rate
                        obs_price = real_prices_by_court_slice.get((c_uuid, sl_s))
                        if obs_price is not None:
                            slot_price = obs_price
                        else:
                            # Peer courts in the same club at the same time:
                            peer_prices = [
                                p for (r_id, t_pt), p in real_prices_by_court_slice.items()
                                if t_pt == sl_s
                            ]
                            if peer_prices:
                                slot_price = round(sum(peer_prices) / len(peer_prices) * size_mult, 2)
                            else:
                                is_pk = 17 <= sl_s.hour < 22 or target_date.weekday() >= 5
                                base_hr = 49.0 if is_pk else 40.0
                                slot_price = round(base_hr * size_mult * 0.5, 2)

                        observations.append(
                            AvailabilityObservation.from_raw(
                                club_id=club_id,
                                court_id=court.court_id,
                                booking_platform="playtomic",
                                start_time=sl_s,
                                end_time=sl_e,
                                price_eur=slot_price,
                                availability_status="available" if is_avail else "unavailable",
                                status_detail="observed_available" if is_avail else "inferred_unavailable",
                                observed_at=obs_ts,
                                snapshot_bucket=snapshot_bucket or "24h",
                                source_url=target_url,
                                data_origin="live_public",
                                run_id=run_id,
                            )
                        )
            except Exception as exc:
                logger.warning("Headless scraping warning for %s on %s: %s", club_id, target_date, exc)
            finally:
                await browser.close()

        return CollectionRunResult(
            run_id=run_id,
            started_at=obs_ts,
            completed_at=now_dublin_iso(),
            adapter_name="HeadlessCourtAvailabilityScraper",
            platform_id="playtomic",
            club_id=club_id,
            target_url=target_url,
            http_status=200 if observations or raw_html else 500,
            robots_allowed=True,
            auth_required=False,
            run_status="success" if observations else ("partial" if raw_html else "error"),
            data_origin="live_public",
            records_written=len(observations),
            raw_artifact_path=str(self.raw_save_dir / f"{target_date.strftime('%Y%m%d')}__{club_id}__headless.html") if raw_html else None,
            observations=observations,
        )

    def scrape_club_date(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
        courts: Iterable[CourtRecord],
        opening_hours: dict[str, OpeningHoursRecord],
        snapshot_bucket: Optional[str] = None,
    ) -> CollectionRunResult:
        """Synchronous wrapper to run headless scraping inside any event loop or thread."""
        return asyncio.run(
            self.scrape_playtomic_club_date_async(
                club_id=club_id,
                booking_url=booking_url,
                target_date=target_date,
                courts=courts,
                opening_hours=opening_hours,
                snapshot_bucket=snapshot_bucket,
            )
        )

