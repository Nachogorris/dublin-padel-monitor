"""Zero-API Human-in-the-Loop Multi-Snapshot Browser Dropzone Adapter (`data/inbox/`).

Supports multi-lead-time (`7d`, `3d`, `24h`, `6h`/`morning`, `1h`/`prepeak`) calendar capture
across any booking platform (Playtomic, Matchpoint, GladstoneGo, Playbypoint, SmartClubCloud)
via three zero-API input formats:
  1. Saved Webpage HTML (`Ctrl+S` -> `<club_id>__YYYY-MM-DD__<tag>.html`)
  2. Copy-Pasted Per-Court Text (`Ctrl+A, Ctrl+C` -> `<club_id>__YYYY-MM-DD__<tag>.txt`)
  3. Rapid Court-Count Shorthand (single-day or 7-day weekly bundle `<club_id>__week__YYYY-MM-DD__<tag>.txt`), e.g.:
       === 2026-11-10 ===
       07:00-09:00 = 1 free €30
       09:00-17:00 = 4 free €30
       17:00-21:30 = 0 free €50
       21:30-23:00 = 2 free €40
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import (
    AvailabilityObservation,
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
)
from ..time_utils import (
    DUBLIN_TZ,
    WEEKDAY_NAMES,
    generate_atomic_slices,
    now_dublin_iso,
    parse_hhmm,
    parse_iso_aware,
    resolve_operating_window,
    to_dublin,
)
from .base import AdapterCapabilities, BasePlatformAdapter

TIME_PILL_RE = re.compile(r"\b([01]?\d|2[0-3]):([03]0)\b")
PRICE_NEAR_RE = re.compile(r"(?:€\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*EUR)", flags=re.IGNORECASE)
DUR_NEAR_RE = re.compile(r"\b(60|90|120)\s*(?:min|m)\b", flags=re.IGNORECASE)

# Shorthand syntax: "17:00-21:30 = 0 free €50" or "18:00: 2 occupied €40"
SHORTHAND_LINE_RE = re.compile(
    r"^\s*([01]?\d|2[0-3]):([03]0)"
    r"(?:\s*[–-]\s*([01]?\d|2[0-4]):([03]0))?"
    r"\s*(?:=|:|->)\s*(\d+)\s*(free|avail|available|occupied|booked|taken)\b(.*)$",
    flags=re.IGNORECASE,
)
# Per-court booked range syntax (matches Playtomic desktop grey-bar timeline):
# e.g. "CUPRA Court: booked 17:00-23:00" or "Court 3: booked 13:30-15:00, 16:30-22:30"
COURT_BOOKED_LINE_RE = re.compile(
    r"^\s*([^:]+?)\s*:\s*booked\s+(.+)$",
    flags=re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(
    r"\b([01]?\d|2[0-3]):([03]0)\s*[–-]\s*([01]?\d|2[0-4]):([03]0)\b"
)
HEADER_OBS_RE = re.compile(r"^\s*#\s*observed_at\s*:\s*(\S+)\s*$", flags=re.IGNORECASE)
BUNDLE_DAY_HEADER_RE = re.compile(r"^\s*===\s*(\d{4}-\d{2}-\d{2})\b.*===\s*$")

SINGLE_DAY_FILE_RE = re.compile(
    r"^([a-z0-9_]+)__(\d{4}-\d{2}-\d{2})(?:__([a-z0-9_-]+))?\.(?:html?|txt)$",
    flags=re.IGNORECASE,
)
WEEK_BUNDLE_FILE_RE = re.compile(
    r"^([a-z0-9_]+)__week__(\d{4}-\d{2}-\d{2})(?:__([a-z0-9_-]+))?\.txt$",
    flags=re.IGNORECASE,
)
EXPLICIT_TS_TAG_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[T_](\d{2}):?(\d{2})$")


@dataclass(frozen=True)
class ParsedInboxDescriptor:
    club_id: str
    is_weekly_bundle: bool
    anchor_date: date
    snapshot_tag: Optional[str]


def parse_inbox_filename(filename: str) -> Optional[ParsedInboxDescriptor]:
    """Parse single-day or weekly-bundle multi-snapshot filenames."""
    wm = WEEK_BUNDLE_FILE_RE.match(filename.strip())
    if wm:
        return ParsedInboxDescriptor(
            club_id=wm.group(1).lower(),
            is_weekly_bundle=True,
            anchor_date=date.fromisoformat(wm.group(2)),
            snapshot_tag=wm.group(3).lower() if wm.group(3) else None,
        )
    sm = SINGLE_DAY_FILE_RE.match(filename.strip())
    if sm:
        return ParsedInboxDescriptor(
            club_id=sm.group(1).lower(),
            is_weekly_bundle=False,
            anchor_date=date.fromisoformat(sm.group(2)),
            snapshot_tag=sm.group(3).lower() if sm.group(3) else None,
        )
    return None


def resolve_snapshot_timestamp_and_bucket(
    target_date: date,
    *,
    snapshot_tag: Optional[str] = None,
    file_mtime_epoch: Optional[float] = None,
    header_observed_at: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Resolve deterministic Europe/Dublin `observed_at` ISO timestamp & optional `snapshot_bucket`."""
    if header_observed_at:
        dt = to_dublin(parse_iso_aware(header_observed_at, "header_observed_at"))
        return dt.isoformat(timespec="seconds"), None

    if snapshot_tag:
        tag = snapshot_tag.strip().lower()
        tm = EXPLICIT_TS_TAG_RE.match(tag)
        if tm:
            yr, mo, dy, hh, mm = (int(tm.group(i)) for i in range(1, 6))
            dt = datetime(yr, mo, dy, hh, mm, tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), None

        if tag in ("7d", "pass1", "pass1_7d", "week_ahead"):
            dt = datetime.combine(target_date - timedelta(days=7), time(18, 0), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "7d"
        if tag in ("3d", "pass2", "pass2_3d"):
            dt = datetime.combine(target_date - timedelta(days=3), time(18, 0), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "3d"
        if tag in ("24h", "1d", "pass3", "pass3_24h", "day_before"):
            dt = datetime.combine(target_date - timedelta(days=1), time(18, 0), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "24h"
        if tag in ("6h", "morning", "pass4", "pass4_morning"):
            dt = datetime.combine(target_date, time(6, 0), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "6h"
        if tag in ("1h", "prepeak", "pass5", "pass5_prepeak"):
            dt = datetime.combine(target_date, time(16, 30), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "1h"
        if tag in ("full1h", "final", "0530"):
            dt = datetime.combine(target_date, time(5, 30), tzinfo=DUBLIN_TZ)
            return dt.isoformat(timespec="seconds"), "1h"

    if file_mtime_epoch is not None:
        dt = datetime.fromtimestamp(file_mtime_epoch, tz=DUBLIN_TZ)
        return dt.isoformat(timespec="seconds"), None

    return now_dublin_iso(), None


class BrowserSnapshotDropzoneAdapter(BasePlatformAdapter):
    """Ingests human-saved browser HTML, copy-pasted text, or shorthand multi-snapshot calendars."""

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform_id="browser_dropzone",
            supports_live_public_metadata=False,
            supports_live_public_slots=True,
            supports_authorized_api_slots=False,
            requires_credentials_for_slots=False,
            compliance_notes=(
                "100% offline human-in-the-loop multi-snapshot parser: processes HTML, text, or "
                "court-count shorthand saved into data/inbox/ across 7d, 3d, 24h, 6h, and 1h passes."
            ),
        )

    def parse_snapshot_text(
        self,
        raw_content: str,
        *,
        club_id: str,
        target_date: date,
        courts: Iterable[CourtRecord],
        opening_hours: dict[str, OpeningHoursRecord],
        observed_at: Optional[str] = None,
        snapshot_bucket_override: Optional[str] = None,
        source_label: str = "browser-dropzone://manual-capture",
        default_offpeak_eur: Optional[float] = None,
        default_peak_eur: Optional[float] = None,
    ) -> CollectionRunResult:
        header_obs: Optional[str] = None
        for raw_ln in raw_content.splitlines()[:10]:
            hm = HEADER_OBS_RE.match(raw_ln)
            if hm:
                header_obs = hm.group(1)
                break

        obs_ts = header_obs or observed_at or now_dublin_iso()
        det_hash = hashlib.sha1(
            f"{club_id}|{target_date.isoformat()}|{obs_ts}|{source_label}".encode("utf-8")
        ).hexdigest()[:8]
        run_id = f"inbox-{club_id}-{target_date.isoformat()}-{det_hash}"

        if "<html" in raw_content[:1000].lower() or "<div" in raw_content[:1000].lower():
            soup = BeautifulSoup(raw_content, "html.parser")
            lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
        else:
            lines = [
                ln.strip()
                for ln in raw_content.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]

        padel_courts = sorted(
            [c for c in courts if c.sport.upper() == "PADEL"],
            key=lambda c: c.court_name,
        )
        if not padel_courts:
            raise ValueError(f"No verified PADEL courts registered for club_id={club_id!r}")

        dow = WEEKDAY_NAMES[target_date.weekday()]
        oh = opening_hours.get(dow)
        if oh is not None:
            win_start, win_end = resolve_operating_window(target_date, oh.opening_time, oh.closing_time)
        else:
            win_start = datetime.combine(target_date, time(8, 0), tzinfo=DUBLIN_TZ)
            win_end = datetime.combine(target_date, time(22, 0), tzinfo=DUBLIN_TZ)

        observations: list[AvailabilityObservation] = []

        # --- Check Mode A: Rapid Shorthand Lines (e.g. "17:00-21:30 = 0 free €50") ---
        shorthand_entries: list[tuple[datetime, datetime, int, Optional[float]]] = []
        for ln in lines:
            sm = SHORTHAND_LINE_RE.match(ln)
            if not sm:
                continue
            sh1, sm1 = int(sm.group(1)), int(sm.group(2))
            s_dt = datetime.combine(target_date, time(sh1, sm1), tzinfo=DUBLIN_TZ)
            if s_dt < win_start and sh1 <= 2:
                s_dt += timedelta(days=1)

            if sm.group(3) is not None:
                eh1, em1 = int(sm.group(3)), int(sm.group(4))
                if eh1 == 24:
                    e_dt = datetime.combine(target_date + timedelta(days=1), time(0, 0), tzinfo=DUBLIN_TZ)
                else:
                    e_dt = datetime.combine(target_date, time(eh1, em1), tzinfo=DUBLIN_TZ)
                    if e_dt <= s_dt:
                        e_dt += timedelta(days=1)
            else:
                e_dt = s_dt + timedelta(minutes=60)

            count_val = int(sm.group(5))
            mode_word = sm.group(6).lower()
            tail = sm.group(7) or ""
            pm = PRICE_NEAR_RE.search(tail)
            hr_price = float(pm.group(1) or pm.group(2)) if pm else None

            n_total = len(padel_courts)
            if mode_word in ("free", "avail", "available"):
                n_free = max(0, min(n_total, count_val))
            else:
                n_free = max(0, min(n_total, n_total - count_val))
            shorthand_entries.append((s_dt, e_dt, n_free, hr_price))

        if shorthand_entries:
            for s_dt, e_dt, n_free, hr_price in shorthand_entries:
                for sl_s, sl_e in generate_atomic_slices(s_dt, e_dt, slice_minutes=30):
                    is_pk = 17 <= sl_s.hour < 22 or target_date.weekday() >= 5
                    fallback_hr = default_peak_eur if is_pk else default_offpeak_eur
                    eff_hr = hr_price if hr_price is not None else fallback_hr
                    slot_price = round(eff_hr * 0.5, 2) if eff_hr is not None else None

                    for idx_c, court in enumerate(padel_courts):
                        is_avail = idx_c < n_free
                        observations.append(
                            AvailabilityObservation.from_raw(
                                club_id=club_id,
                                court_id=court.court_id,
                                booking_platform="browser_dropzone",
                                start_time=sl_s,
                                end_time=sl_e,
                                price_eur=slot_price if is_avail else None,
                                availability_status="available" if is_avail else "unavailable",
                                status_detail="observed_available" if is_avail else "inferred_unavailable",
                                observed_at=obs_ts,
                                snapshot_bucket=snapshot_bucket_override,
                                source_url=source_label,
                                data_origin="live_public",
                                run_id=run_id,
                            )
                        )
            return CollectionRunResult(
                run_id=run_id,
                started_at=obs_ts,
                completed_at=now_dublin_iso(),
                adapter_name="BrowserSnapshotDropzoneAdapter.shorthand",
                platform_id="browser_dropzone",
                club_id=club_id,
                target_url=source_label,
                http_status=None,
                robots_allowed=True,
                auth_required=False,
                run_status="success",
                data_origin="live_public",
                records_written=len(observations),
                observations=observations,
            )

        # --- Mode A2: Per-Court Booked Bar Ranges (e.g. "Court 2: booked 07:00-08:00, 17:00-22:30") ---
        name_lookup = {c.court_name.strip().lower(): c for c in padel_courts}
        booked_ranges_by_court: dict[str, list[tuple[datetime, datetime]]] = {}
        for ln in lines:
            cbm = COURT_BOOKED_LINE_RE.match(ln)
            if not cbm:
                continue
            cname_raw = cbm.group(1).strip().lower().rstrip(".")
            tail_ranges = cbm.group(2).strip()
            matched_court = name_lookup.get(cname_raw)
            if matched_court is None:
                matched_court = next(
                    (
                        c
                        for nm, c in name_lookup.items()
                        if nm.startswith(cname_raw) or cname_raw.startswith(nm)
                    ),
                    None,
                )
            if matched_court is None:
                continue
            booked_ranges_by_court.setdefault(matched_court.court_id, [])
            for rm in TIME_RANGE_RE.finditer(tail_ranges):
                sh1, sm1 = int(rm.group(1)), int(rm.group(2))
                eh1, em1 = int(rm.group(3)), int(rm.group(4))
                s_dt = datetime.combine(target_date, time(sh1, sm1), tzinfo=DUBLIN_TZ)
                if s_dt < win_start and sh1 <= 2:
                    s_dt += timedelta(days=1)
                if eh1 == 24:
                    e_dt = datetime.combine(target_date + timedelta(days=1), time(0, 0), tzinfo=DUBLIN_TZ)
                else:
                    e_dt = datetime.combine(target_date, time(eh1, em1), tzinfo=DUBLIN_TZ)
                    if e_dt <= s_dt:
                        e_dt += timedelta(days=1)
                booked_ranges_by_court[matched_court.court_id].append((s_dt, e_dt))

        if booked_ranges_by_court:
            for court in padel_courts:
                if court.court_id not in booked_ranges_by_court:
                    continue
                b_intervals = booked_ranges_by_court[court.court_id]
                size_mult = 0.70 if court.court_size == "single" else 1.0
                for sl_s, sl_e in generate_atomic_slices(win_start, win_end, slice_minutes=30):
                    is_booked = any(bs <= sl_s and be >= sl_e for bs, be in b_intervals)
                    is_pk = 17 <= sl_s.hour < 22 or target_date.weekday() >= 5
                    base_hr = default_peak_eur if is_pk else default_offpeak_eur
                    slot_price = (
                        round(base_hr * size_mult * 0.5, 2)
                        if (base_hr is not None and not is_booked)
                        else None
                    )
                    observations.append(
                        AvailabilityObservation.from_raw(
                            club_id=club_id,
                            court_id=court.court_id,
                            booking_platform="browser_dropzone",
                            start_time=sl_s,
                            end_time=sl_e,
                            price_eur=slot_price,
                            availability_status="unavailable" if is_booked else "available",
                            status_detail="inferred_unavailable" if is_booked else "observed_available",
                            observed_at=obs_ts,
                            snapshot_bucket=snapshot_bucket_override,
                            source_url=source_label,
                            data_origin="live_public",
                            run_id=run_id,
                        )
                    )
            return CollectionRunResult(
                run_id=run_id,
                started_at=obs_ts,
                completed_at=now_dublin_iso(),
                adapter_name="BrowserSnapshotDropzoneAdapter.court_booked_ranges",
                platform_id="browser_dropzone",
                club_id=club_id,
                target_url=source_label,
                http_status=None,
                robots_allowed=True,
                auth_required=False,
                run_status="success",
                data_origin="live_public",
                records_written=len(observations),
                observations=observations,
            )

        # --- Mode B: Per-Court Calendar HTML / Copy-Pasted Text ---
        available_by_court: dict[str, list[tuple[int, int, int, Optional[float]]]] = {
            c.court_id: [] for c in padel_courts
        }
        explicitly_empty_courts: set[str] = set()

        active_court: Optional[CourtRecord] = None
        for idx, line in enumerate(lines):
            low = line.lower()
            if low in ("all sold out", "fully booked all courts", "no slots available all courts"):
                for c in padel_courts:
                    explicitly_empty_courts.add(c.court_id)
                continue

            if low in name_lookup:
                active_court = name_lookup[low]
                continue
            matched_c = next(
                (c for nm, c in name_lookup.items() if len(nm) >= 5 and (low.startswith(nm) or nm in low)),
                None,
            )
            if matched_c is not None and not TIME_PILL_RE.search(line):
                active_court = matched_c
                continue

            if active_court is None:
                continue

            if "no slots available" in low or "fully booked" in low or "sold out" in low:
                explicitly_empty_courts.add(active_court.court_id)
                continue

            for tm in TIME_PILL_RE.finditer(line):
                hh, mm = int(tm.group(1)), int(tm.group(2))
                context = " ".join(lines[idx : min(len(lines), idx + 3)])
                dm = DUR_NEAR_RE.search(context)
                dur = int(dm.group(1)) if dm else 60
                pm = PRICE_NEAR_RE.search(context)
                price = float(pm.group(1) or pm.group(2)) if pm else None
                available_by_court[active_court.court_id].append((hh, mm, dur, price))

        for court in padel_courts:
            avail_slots = available_by_court[court.court_id]
            if not avail_slots and court.court_id not in explicitly_empty_courts:
                continue

            free_half_hours: set[datetime] = set()
            for hh, mm, dur, price in avail_slots:
                s_dt = datetime.combine(target_date, time(hh, mm), tzinfo=DUBLIN_TZ)
                if s_dt < win_start and hh <= 2:
                    s_dt = s_dt + timedelta(days=1)
                e_dt = s_dt + timedelta(minutes=dur)
                if price is None:
                    is_pk = 17 <= s_dt.hour < 22 or target_date.weekday() >= 5
                    base_hr = default_peak_eur if is_pk else default_offpeak_eur
                    price = round(base_hr * (dur / 60.0), 2) if base_hr is not None else None

                observations.append(
                    AvailabilityObservation.from_raw(
                        club_id=club_id,
                        court_id=court.court_id,
                        booking_platform="browser_dropzone",
                        start_time=s_dt,
                        end_time=e_dt,
                        price_eur=price,
                        availability_status="available",
                        status_detail="observed_available",
                        observed_at=obs_ts,
                        snapshot_bucket=snapshot_bucket_override,
                        source_url=source_label,
                        data_origin="live_public",
                        run_id=run_id,
                    )
                )
                cur = s_dt
                while cur < e_dt:
                    free_half_hours.add(cur)
                    cur += timedelta(minutes=30)

            for sl_s, sl_e in generate_atomic_slices(win_start, win_end, slice_minutes=30):
                if sl_s not in free_half_hours:
                    observations.append(
                        AvailabilityObservation.from_raw(
                            club_id=club_id,
                            court_id=court.court_id,
                            booking_platform="browser_dropzone",
                            start_time=sl_s,
                            end_time=sl_e,
                            price_eur=None,
                            availability_status="unavailable",
                            status_detail="inferred_unavailable",
                            observed_at=obs_ts,
                            snapshot_bucket=snapshot_bucket_override,
                            source_url=source_label,
                            data_origin="live_public",
                            run_id=run_id,
                        )
                    )

        return CollectionRunResult(
            run_id=run_id,
            started_at=obs_ts,
            completed_at=now_dublin_iso(),
            adapter_name="BrowserSnapshotDropzoneAdapter",
            platform_id="browser_dropzone",
            club_id=club_id,
            target_url=source_label,
            http_status=None,
            robots_allowed=True,
            auth_required=False,
            run_status="success",
            data_origin="live_public",
            records_written=len(observations),
            observations=observations,
        )

    def parse_weekly_bundle_text(
        self,
        bundle_content: str,
        *,
        club_id: str,
        courts: Iterable[CourtRecord],
        opening_hours: dict[str, OpeningHoursRecord],
        snapshot_tag: Optional[str] = None,
        file_mtime_epoch: Optional[float] = None,
        source_label: str = "browser-dropzone://weekly-bundle",
        default_offpeak_eur: Optional[float] = None,
        default_peak_eur: Optional[float] = None,
    ) -> list[CollectionRunResult]:
        """Split a 7-day `=== YYYY-MM-DD ===` bundle file into per-day multi-snapshot runs."""
        header_obs: Optional[str] = None
        for raw_ln in bundle_content.splitlines()[:10]:
            hm = HEADER_OBS_RE.match(raw_ln)
            if hm:
                header_obs = hm.group(1)
                break

        sections: list[tuple[date, list[str]]] = []
        current_day: Optional[date] = None
        current_lines: list[str] = []

        for ln in bundle_content.splitlines():
            dm = BUNDLE_DAY_HEADER_RE.match(ln)
            if dm:
                if current_day is not None:
                    sections.append((current_day, current_lines))
                current_day = date.fromisoformat(dm.group(1))
                current_lines = []
            elif current_day is not None:
                current_lines.append(ln)

        if current_day is not None:
            sections.append((current_day, current_lines))

        courts_list = list(courts)
        runs: list[CollectionRunResult] = []
        for t_day, day_lines in sections:
            non_comment = [x.strip() for x in day_lines if x.strip() and not x.strip().startswith("#")]
            if not non_comment:
                continue
            obs_ts, bkt_override = resolve_snapshot_timestamp_and_bucket(
                t_day,
                snapshot_tag=snapshot_tag,
                file_mtime_epoch=file_mtime_epoch,
                header_observed_at=header_obs,
            )
            res = self.parse_snapshot_text(
                "\n".join(day_lines),
                club_id=club_id,
                target_date=t_day,
                courts=courts_list,
                opening_hours=opening_hours,
                observed_at=obs_ts,
                snapshot_bucket_override=bkt_override,
                source_label=f"{source_label}#{t_day.isoformat()}",
                default_offpeak_eur=default_offpeak_eur,
                default_peak_eur=default_peak_eur,
            )
            if res.observations:
                runs.append(res)
        return runs

    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
    ) -> CollectionRunResult:
        path = Path(booking_url)
        if not path.exists():
            raise FileNotFoundError(f"Snapshot file not found in dropzone: {path}")
        raise NotImplementedError("Use parse_snapshot_text() with club CourtRecord & OpeningHoursRecord.")
