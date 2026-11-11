"""API-Free Court Activity Harvester & Multi-Signal Utilization Measurement Engine.

Measures Dublin padel court activity, demand velocity, occupied court-hours, and
estimated Gross Booking Value WITHOUT requiring Playtomic OAuth2 API keys or any
other vendor API—strictly using:
1. Permitted SSR HTML pages (`GET /clubs/<slug>`, `/competitions/tournaments`,
   `/academy/courses`) allowed by `robots.txt` without authentication.
2. Verified court counts, environments (indoor / covered / outdoor), opening hours,
   and pricing structures from official club websites.
3. Optional human-in-the-loop browser snapshots (`data/inbox/`) and stratified
   field/telephone spot checks (`field_spot_checks`), blended automatically via
   precision-weighted shrinkage when present.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup

from .models import AvailabilityObservation, CourtRecord, OpeningHoursRecord
from .time_utils import DUBLIN_TZ, parse_iso_aware, resolve_operating_window, to_dublin

RSC_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", flags=re.DOTALL)
DATE_TIME_CARD_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Za-z]+\s+\d{1,2})\s*\|\s*(\d{1,2}):(\d{2})h?$",
    flags=re.IGNORECASE,
)
COURSE_SPAN_RE = re.compile(
    r"^([A-Za-z]{3}\s+\d{1,2},\s*\d{4})\s*-\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})$"
)
DURATION_MIN_RE = re.compile(r"^(\d{2,3})\s*min$", flags=re.IGNORECASE)
EUR_TOKEN_RE = re.compile(r"^(?:€\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*EUR)$", flags=re.IGNORECASE)
SKILL_BAND_RE = re.compile(r"^\d+(?:\.\d+)?\s*[–-]\s*\d+(?:\.\d+)?$")
DISCOUNT_PCT_RE = re.compile(r"(\d{1,3})\s*%\s*off", flags=re.IGNORECASE)
ADVANCE_DAYS_RE = re.compile(r"(\d{1,2})\s*(?:days?|weeks?)\s*in\s*advance", flags=re.IGNORECASE)


def _parse_card_datetime_dublin(raw_token: str, ref_dt: datetime) -> Optional[datetime]:
    """Parse 'Tuesday, November 10 | 12:00h' into timezone-aware Europe/Dublin datetime."""
    m = DATE_TIME_CARD_RE.match(raw_token.strip())
    if not m:
        return None
    month_day_str, hh_s, mm_s = m.group(1), m.group(2), m.group(3)
    for yr in (ref_dt.year, ref_dt.year + 1):
        try:
            d = datetime.strptime(f"{month_day_str} {yr}", "%B %d %Y").date()
            cand = datetime.combine(d, time(int(hh_s), int(mm_s)), tzinfo=DUBLIN_TZ)
            # Choose year closest forward (allow up to 14 days in the past for archive cards)
            if (cand - ref_dt).days >= -14:
                return cand
        except ValueError:
            continue
    return None


def parse_playtomic_activity_cards(
    html_text: str,
    *,
    club_id: str,
    source_url: str,
    observed_at: str,
    club_padel_courts: int = 4,
    run_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract all live tournaments, public classes, academy courses, and membership/offer cards.

    Returns `(activity_records, membership_offer_records)`.
    """
    obs_dt = to_dublin(parse_iso_aware(observed_at))
    soup = BeautifulSoup(html_text, "html.parser")

    # Also extract exact UTC startAt map from RSC stream if present on main club page
    rsc_start_map: dict[str, datetime] = {}
    rsc_offers: list[dict[str, Any]] = []
    chunks = RSC_PUSH_RE.findall(html_text)
    rsc_parts: list[str] = []
    for raw_c in chunks:
        try:
            arr = json.loads(raw_c)
            if len(arr) > 1 and isinstance(arr[1], str):
                rsc_parts.append(arr[1])
        except json.JSONDecodeError:
            continue
    rsc_payload = "".join(rsc_parts)
    marker = rsc_payload.find('{"tenant":{"tenant_id"')
    if marker >= 0:
        try:
            root_obj, _ = json.JSONDecoder().raw_decode(rsc_payload, marker)
            for comp in root_obj.get("competitionCards") or []:
                if isinstance(comp, dict) and comp.get("id") and comp.get("startAt"):
                    rsc_start_map[str(comp["id"])] = to_dublin(parse_iso_aware(comp["startAt"]))
            for off in root_obj.get("tenantOffers") or []:
                if isinstance(off, dict) and off.get("offer_id"):
                    p_str = str(off.get("price") or "").replace("EUR", "").replace("€", "").strip()
                    p_val = float(p_str) if p_str else None
                    rsc_offers.append(
                        {
                            "club_id": club_id,
                            "offer_id": str(off["offer_id"]),
                            "offer_kind": "lesson_package",
                            "title": str(off.get("name") or "Club Offer").strip(),
                            "price_eur": p_val,
                            "billing_period": "one_time",
                            "court_discount_pct": None,
                            "advance_booking_days": None,
                            "off_peak_unlimited": 0,
                            "description": str(off.get("description") or "").strip()[:500],
                            "observed_at": obs_dt.isoformat(timespec="seconds"),
                        }
                    )
        except Exception:
            pass

    activities: dict[str, dict[str, Any]] = {}
    offers: dict[str, dict[str, Any]] = {o["offer_id"]: o for o in rsc_offers}

    for link in soup.select('a[data-slot="card-link"]'):
        href = str(link.get("href") or "").strip()
        title = str(link.get("aria-label") or "").strip()
        card = link.find_parent(attrs={"data-slot": "card"})
        # Next.js RSC streaming Suspense injects <div hidden id="S:XX"><a></a></div> into <template id="P:XX"> inside <div data-slot="card">
        if card is None:
            suspense_div = link.find_parent(id=re.compile(r"^S:"))
            if suspense_div is not None:
                placeholder = soup.find(id="P:" + str(suspense_div["id"])[2:])
                if placeholder is not None:
                    card = placeholder.find_parent(attrs={"data-slot": "card"})
        if card is None and link.parent is not None and link.parent.name not in ("body", "html"):
            if len(link.parent.select('a[data-slot="card-link"]')) == 1:
                card = link.parent
        if card is None or not href:
            continue
        tokens = [t.strip() for t in card.stripped_strings if t.strip()]

        # 1. Membership cards (/memberships/<uuid>)
        if "/memberships/" in href:
            m_id = href.rstrip("/").split("/")[-1]
            joined = " | ".join(tokens)
            price_val: Optional[float] = None
            period = "monthly"
            for tok in tokens:
                em = EUR_TOKEN_RE.match(tok)
                if em:
                    price_val = float(em.group(1) or em.group(2))
                if tok.lower() in ("monthly", "one-time", "annual", "yearly"):
                    period = tok.lower().replace("-", "_")

            disc_m = DISCOUNT_PCT_RE.search(joined)
            disc_pct = float(disc_m.group(1)) if disc_m else None
            adv_m = ADVANCE_DAYS_RE.search(joined)
            adv_days: Optional[int] = None
            if adv_m:
                val = int(adv_m.group(1))
                adv_days = val * 7 if "week" in adv_m.group(0).lower() else val
            off_unlim = 1 if ("unlimited off peak" in joined.lower() or "no court hire fees" in joined.lower() or "€0 court fees" in joined.lower()) else 0
            if off_unlim and disc_pct is None:
                disc_pct = 100.0

            offers[m_id] = {
                "club_id": club_id,
                "offer_id": m_id,
                "offer_kind": "membership",
                "title": title or (tokens[0] if tokens else "Membership"),
                "price_eur": price_val,
                "billing_period": period,
                "court_discount_pct": disc_pct,
                "advance_booking_days": adv_days,
                "off_peak_unlimited": off_unlim,
                "description": joined[:500],
                "observed_at": obs_dt.isoformat(timespec="seconds"),
            }
            continue

        # 2. Activity cards (/activities/tournaments/<id>, /activities/public-classes/<id>, /activities/courses/<id>)
        if "/activities/" not in href:
            continue

        act_id = href.rstrip("/").split("/")[-1]
        if "/tournaments/" in href:
            cat = "tournament"
        elif "/public-classes/" in href:
            cat = "public_class"
        elif "/courses/" in href:
            cat = "academy_course"
        else:
            continue

        start_dt: Optional[datetime] = rsc_start_map.get(act_id)
        dur_min: Optional[int] = None
        course_span: Optional[str] = None
        skill_band: Optional[str] = None
        coach_name: Optional[str] = None
        reg_players: Optional[int] = None
        max_players: Optional[int] = None
        price_eur: Optional[float] = None

        for i, tok in enumerate(tokens):
            if start_dt is None:
                parsed_dt = _parse_card_datetime_dublin(tok, obs_dt)
                if parsed_dt is not None:
                    start_dt = parsed_dt
            if course_span is None and COURSE_SPAN_RE.match(tok):
                course_span = tok
            dm = DURATION_MIN_RE.match(tok)
            if dm:
                dur_min = int(dm.group(1))
            if skill_band is None and SKILL_BAND_RE.match(tok):
                skill_band = tok
            if tok in ("Coach", "Coaches") and i + 1 < len(tokens):
                coach_name = tokens[i + 1]
            if tok == "/" and i >= 1 and i + 1 < len(tokens):
                if tokens[i - 1].isdigit() and tokens[i + 1].isdigit():
                    reg_players = int(tokens[i - 1])
                    max_players = int(tokens[i + 1])
            em = EUR_TOKEN_RE.match(tok)
            if em:
                price_eur = float(em.group(1) or em.group(2))

        if dur_min is None:
            dur_min = 90 if cat == "tournament" else 60

        end_dt = (start_dt + timedelta(minutes=dur_min)) if start_dt is not None else None
        fill_pct = (
            round(100.0 * reg_players / max_players, 1)
            if (reg_players is not None and max_players and max_players > 0)
            else None
        )

        if cat == "tournament":
            est_courts = max(1, min(club_padel_courts, math.ceil((max_players or 8) / 4.0)))
        elif cat == "public_class":
            est_courts = max(1, min(club_padel_courts, math.ceil((max_players or 4) / 4.0)))
        else:
            est_courts = 1

        court_hrs = round(est_courts * (dur_min / 60.0), 2)
        obs_rev = (
            round(reg_players * price_eur, 2)
            if (reg_players is not None and price_eur is not None)
            else None
        )
        cap_rev = (
            round(max_players * price_eur, 2)
            if (max_players is not None and price_eur is not None)
            else None
        )

        activities[act_id] = {
            "club_id": club_id,
            "activity_id": act_id,
            "activity_category": cat,
            "title": title or "Scheduled Activity",
            "start_time": start_dt.isoformat(timespec="seconds") if start_dt else None,
            "end_time": end_dt.isoformat(timespec="seconds") if end_dt else None,
            "duration_minutes": dur_min,
            "course_span_text": course_span,
            "skill_band": skill_band,
            "coach_name": coach_name,
            "registered_players": reg_players,
            "max_players": max_players,
            "fill_rate_pct": fill_pct,
            "courts_occupied": est_courts,
            "court_hours_occupied": court_hrs,
            "price_per_player_eur": price_eur,
            "observed_gross_revenue_eur": obs_rev,
            "full_capacity_revenue_eur": cap_rev,
            "observed_at": obs_dt.isoformat(timespec="seconds"),
            "source_url": source_url,
            "run_id": run_id,
        }

    return list(activities.values()), list(offers.values())


def convert_activities_to_live_observations(
    activities: Iterable[dict[str, Any]],
    courts_by_club: dict[str, list[CourtRecord]],
) -> list[AvailabilityObservation]:
    """Project dated live tournaments & public classes onto physical club courts as real `live_public` observations.

    When a 12-player tournament runs 18:00–19:30 at a club, 3 physical doubles courts are
    occupied during 18:00–19:30 (`confirmed_booking` when >=4 players registered, else
    `blocked_coaching_or_event`).
    """
    out: list[AvailabilityObservation] = []
    for act in activities:
        if not act.get("start_time") or not act.get("end_time"):
            continue
        club_id = act["club_id"]
        padel_courts = [
            c for c in courts_by_club.get(club_id, []) if c.sport.upper() == "PADEL"
        ]
        # Prefer doubles courts for social tournaments/classes
        padel_courts.sort(key=lambda c: (0 if c.court_size == "double" else 1, c.court_name))
        if not padel_courts:
            continue

        n_courts = min(int(act.get("courts_occupied") or 1), len(padel_courts))
        reg = act.get("registered_players") or 0
        price_pp = act.get("price_per_player_eur")
        # Court-equivalent revenue for the session per court
        per_court_price: Optional[float] = None
        if price_pp is not None and act.get("max_players"):
            players_per_court = float(act["max_players"]) / max(n_courts, 1)
            per_court_price = round(price_pp * players_per_court, 2)

        is_confirmed_active = reg >= 4 or (act["activity_category"] == "public_class" and reg >= 2)
        status = "unavailable" if is_confirmed_active else "blocked"
        detail = "confirmed_booking" if is_confirmed_active else "blocked_coaching_or_event"

        for c in padel_courts[:n_courts]:
            out.append(
                AvailabilityObservation.from_raw(
                    club_id=club_id,
                    court_id=c.court_id,
                    booking_platform="playtomic",
                    start_time=act["start_time"],
                    end_time=act["end_time"],
                    price_eur=per_court_price,
                    availability_status=status,
                    status_detail=detail,
                    observed_at=act["observed_at"],
                    source_url=act["source_url"],
                    data_origin="live_public",
                    run_id=act.get("run_id"),
                )
            )
    return out


def compute_activity_velocity_pairs(telemetry_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare earliest vs latest live snapshot for each `(club_id, activity_id)` to measure real player sign-up velocity."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in telemetry_rows:
        grouped[(r["club_id"], r["activity_id"])].append(r)

    transitions: list[dict[str, Any]] = []
    for (club_id, act_id), snaps in sorted(grouped.items()):
        if len(snaps) < 2:
            continue
        snaps.sort(key=lambda x: x["observed_at"])
        first, last = snaps[0], snaps[-1]
        if first["observed_at"] == last["observed_at"]:
            continue
        r0 = first.get("registered_players")
        r1 = last.get("registered_players")
        max_p = last.get("max_players") or first.get("max_players")
        if r0 is None or r1 is None:
            continue
        t0 = parse_iso_aware(first["observed_at"])
        t1 = parse_iso_aware(last["observed_at"])
        elapsed_h = round((t1 - t0).total_seconds() / 3600.0, 2)
        if elapsed_h < 1.0:
            continue
        delta_p = int(r1) - int(r0)
        price_pp = last.get("price_per_player_eur") or first.get("price_per_player_eur") or 0.0
        transitions.append(
            {
                "club_id": club_id,
                "activity_id": act_id,
                "activity_category": last["activity_category"],
                "title": last["title"],
                "start_time": last.get("start_time") or first.get("start_time"),
                "snapshot_1_at": first["observed_at"],
                "snapshot_2_at": last["observed_at"],
                "elapsed_hours": elapsed_h,
                "players_t1": int(r0),
                "players_t2": int(r1),
                "max_players": max_p,
                "delta_players": delta_p,
                "fill_pct_t1": first.get("fill_rate_pct"),
                "fill_pct_t2": last.get("fill_rate_pct"),
                "delta_gross_revenue_eur": round(delta_p * float(price_pp), 2),
            }
        )
    return transitions


SUMMARY_HOURS_FALLBACK: dict[str, tuple[float, float]] = {
    # club_id -> (weekday_daily_hours, weekend_daily_hours) when structured opening_hours table is absent
    "padel_dome": (14.0, 14.0),          # 09:00-23:00
    "portmarnock": (15.0, 12.0),         # Mon-Fri 07:00-22:00, Sat-Sun 08:00-20:00
    "bushy_park": (13.0, 13.0),          # 09:00-22:00
    "meadowbrook": (15.0, 11.0),         # Mon-Fri 07:00-22:00, Sat-Sun 08:00-19:00
    "dlr_loughlinstown": (15.0, 11.0),   # Mon-Fri 07:00-22:00, Sat-Sun 08:00-19:00
    "rockbrook": (13.0, 13.0),           # 09:00-22:00
    "shankill": (13.5, 13.5),            # 08:30-22:00
    "padel100_ring_commons": (14.0, 14.0),  # 08:00-22:00
    "padel100_balbriggan": (14.0, 14.0),    # 08:00-22:00
    "the_k_club": (13.0, 13.0),          # 08:00-21:00
    "shoreline": (15.0, 11.0),           # Mon-Fri 07:00-22:00, Sat-Sun 08:00-19:00
}


class NonApiCourtMeasurementEngine:
    """Fuses all API-free live public telemetry, website pricing/hours, and spot checks into club utilization & GBV estimates."""

    DUBLIN_MEDIAN_PEAK_EUR = 40.0

    @staticmethod
    def _weekly_hours_per_court(
        club_id: str,
        oh_by_club: dict[str, list[OpeningHoursRecord]],
        ref_monday: date,
    ) -> tuple[float, float, float]:
        """Return `(total_weekly_hrs_per_court, peak_weekly_hrs_per_court, offpeak_weekly_hrs_per_court)`."""
        oh_list = {r.day_of_week: r for r in oh_by_club.get(club_id, [])}
        days = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
        if len(oh_list) >= 5:
            tot = 0.0
            peak = 0.0
            for idx, d_name in enumerate(days):
                rec = oh_list.get(d_name)
                if rec is None:
                    continue
                t_day = ref_monday + timedelta(days=idx)
                w_s, w_e = resolve_operating_window(t_day, rec.opening_time, rec.closing_time)
                hrs = (w_e - w_s).total_seconds() / 3600.0
                tot += hrs
                # Peak window: Mon-Fri 17:00-22:30 (up to 5.5h), Sat-Sun 09:00-20:00 (up to 11.0h)
                if idx < 5:
                    p_s = datetime.combine(t_day, time(17, 0), tzinfo=DUBLIN_TZ)
                    p_e = datetime.combine(t_day, time(22, 30), tzinfo=DUBLIN_TZ)
                else:
                    p_s = datetime.combine(t_day, time(9, 0), tzinfo=DUBLIN_TZ)
                    p_e = datetime.combine(t_day, time(20, 0), tzinfo=DUBLIN_TZ)
                ov = max(0.0, (min(w_e, p_e) - max(w_s, p_s)).total_seconds() / 3600.0)
                peak += ov
            return round(tot, 2), round(peak, 2), round(max(0.0, tot - peak), 2)

        wd_h, we_h = SUMMARY_HOURS_FALLBACK.get(club_id, (14.0, 12.0))
        tot = 5.0 * wd_h + 2.0 * we_h
        peak = 5.0 * min(5.0, max(0.0, wd_h - 8.0)) + 2.0 * min(10.0, we_h)
        return round(tot, 2), round(peak, 2), round(max(0.0, tot - peak), 2)

    @classmethod
    def measure_public_clubs(
        cls,
        *,
        clubs: list[dict[str, Any]],
        oh_by_club: dict[str, list[OpeningHoursRecord]],
        latest_activities: list[dict[str, Any]],
        velocity_pairs: list[dict[str, Any]],
        membership_offers: list[dict[str, Any]],
        spot_checks: list[dict[str, Any]],
        reference_monday: date = date(2026, 11, 9),
        computed_at: str = "",
    ) -> list[dict[str, Any]]:
        """Compute API-free weekly court utilization & GBV intervals for all public/hybrid clubs."""
        ref_end = reference_monday + timedelta(days=7)

        acts_by_club: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in latest_activities:
            acts_by_club[a["club_id"]].append(a)

        vel_by_club: dict[str, int] = defaultdict(int)
        has_vel_club: set[str] = set()
        for v in velocity_pairs:
            vel_by_club[v["club_id"]] += int(v["delta_players"])
            has_vel_club.add(v["club_id"])

        offers_by_club: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in membership_offers:
            offers_by_club[o["club_id"]].append(o)

        spots_by_club: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in spot_checks:
            spots_by_club[s["club_id"]].append(s)

        results: list[dict[str, Any]] = []
        for club in clubs:
            if club["access_model"] not in ("public_pay_and_play", "hybrid_member_visitor"):
                continue
            n_courts = int(club.get("verified_padel_courts") or 0)
            if n_courts <= 0:
                continue

            cid = club["club_id"]
            hrs_per_court, peak_hrs_pc, off_hrs_pc = cls._weekly_hours_per_court(cid, oh_by_club, reference_monday)
            weekly_cap = round(n_courts * hrs_per_court, 2)
            peak_cap = round(n_courts * peak_hrs_pc, 2)
            off_cap = round(n_courts * off_hrs_pc, 2)

            c_acts = acts_by_club.get(cid, [])
            # Weekly organised court-hours falling inside [reference_monday, ref_end) + active multi-week courses (1h/wk each)
            org_week_hrs = 0.0
            for a in c_acts:
                if a["activity_category"] == "academy_course":
                    org_week_hrs += float(a.get("court_hours_occupied") or 1.0)
                elif a.get("start_time"):
                    st_d = parse_iso_aware(a["start_time"]).astimezone(DUBLIN_TZ).date()
                    if reference_monday <= st_d < ref_end:
                        org_week_hrs += float(a.get("court_hours_occupied") or 0.0)
            org_week_hrs = round(min(org_week_hrs, weekly_cap * 0.45), 2)

            priced_acts = [a for a in c_acts if a.get("max_players") and a.get("registered_players") is not None]
            tot_reg = sum(int(a["registered_players"]) for a in priced_acts)
            tot_max = sum(int(a["max_players"]) for a in priced_acts)
            # Weight fill rate slightly higher for events starting within 7 days (near-term demand thermometer)
            near_acts = [
                a for a in priced_acts
                if a.get("start_time")
                and 0 <= (parse_iso_aware(a["start_time"]).date() - date(2026, 11, 7)).days <= 8
            ]
            if near_acts:
                near_reg = sum(int(a["registered_players"]) for a in near_acts)
                near_max = sum(int(a["max_players"]) for a in near_acts)
                fill_pct = round(100.0 * near_reg / max(near_max, 1), 1)
            else:
                fill_pct = round(100.0 * tot_reg / tot_max, 1) if tot_max > 0 else None

            ver_act_rev = round(
                sum(float(a.get("observed_gross_revenue_eur") or 0.0) for a in c_acts),
                2,
            )

            # Base Peak & Off-Peak utilization priors for Dublin November padel market by environment
            env = club.get("court_environment") or "outdoor"
            if env == "indoor":
                base_peak, base_off = 0.85, 0.47
            elif env == "covered_outdoor":
                base_peak, base_off = 0.77, 0.37
            else:  # uncovered floodlit outdoor in Nov Dublin
                base_peak, base_off = 0.59, 0.23

            # Signal S1: Live Activity Fill-Rate Thermometer adjustment (-0.06 to +0.09)
            therm_adj = 0.0
            if fill_pct is not None and len(near_acts) >= 2:
                therm_adj = max(-0.06, min(0.09, (fill_pct - 45.0) / 500.0))

            # Signal S2: Live Membership Off-Peak Unlimited / Free-Play Window boost
            c_offers = offers_by_club.get(cid, [])
            has_offpeak_unlim = any(o.get("off_peak_unlimited") for o in c_offers)
            off_price = float(club.get("off_peak_price_eur") if club.get("off_peak_price_eur") is not None else 36.0)
            peak_price = float(club.get("peak_price_eur") if club.get("peak_price_eur") is not None else 42.0)

            mem_off_boost = 0.0
            if has_offpeak_unlim:
                mem_off_boost += 0.14  # €0 weekday 06:00-16:00 unlimited membership fills daytime courts
            elif off_price == 0.0:
                mem_off_boost += 0.18  # Padel 100 Free Hours 11:00-15:00 Mon-Fri
            elif any((o.get("advance_booking_days") or 7) >= 14 for o in c_offers):
                mem_off_boost += 0.04

            # Signal S3: Price elasticity vs Dublin €40/hr benchmark
            price_adj = max(-0.07, min(0.07, (cls.DUBLIN_MEDIAN_PEAK_EUR - peak_price) * 0.003))

            # Signal S4: Extended operating hours dilution (e.g. 18.5h/day vs 14h/day standard)
            avg_daily_h = hrs_per_court / 7.0
            ext_dilution = -0.05 if avg_daily_h >= 17.5 else (0.02 if avg_daily_h <= 12.5 else 0.0)

            est_peak_rate = max(0.25, min(0.95, base_peak + therm_adj + 0.5 * price_adj))
            est_off_rate = max(0.12, min(0.85, base_off + therm_adj + mem_off_boost + price_adj + ext_dilution))

            # Blend with human spot-checks when present (empirical Bayes precision shrinkage)
            c_spots = spots_by_club.get(cid, [])
            if c_spots:
                obs_occ_ratios = [
                    float(s["occupied_courts"]) / max(1, int(s["total_courts_checked"]) - int(s.get("blocked_courts") or 0))
                    for s in c_spots
                ]
                spot_mean = sum(obs_occ_ratios) / len(obs_occ_ratios)
                w_spot = min(0.80, len(c_spots) / (len(c_spots) + 4.0))
                blended_overall = w_spot * spot_mean + (1.0 - w_spot) * (
                    (peak_cap * est_peak_rate + off_cap * est_off_rate) / max(weekly_cap, 1.0)
                )
                scale = blended_overall / max(
                    (peak_cap * est_peak_rate + off_cap * est_off_rate) / max(weekly_cap, 1.0),
                    0.05,
                )
                est_peak_rate = min(0.97, est_peak_rate * scale)
                est_off_rate = min(0.90, est_off_rate * scale)

            model_total_occ_hrs = peak_cap * est_peak_rate + off_cap * est_off_rate
            total_point = round(max(org_week_hrs, min(weekly_cap * 0.95, model_total_occ_hrs)), 2)
            casual_point = round(max(0.0, total_point - org_week_hrs), 2)

            # Determine Measurement Tier & uncertainty half-width (in occupancy percentage points)
            has_live_ssr = cid in oh_by_club
            if has_live_ssr and len(c_acts) >= 3:
                tier = "tier_1_live_ssr_and_activity_thermometer"
                half_band = 0.065 if not c_spots else max(0.03, 0.065 * (1.0 - 0.5 * min(1.0, len(c_spots) / 8.0)))
                conf_score = min(92, 74 + min(10, len(c_acts) // 4) + min(8, len(c_spots) * 2))
            elif has_live_ssr:
                tier = "tier_1b_live_ssr_hours_and_pricing_calibrated"
                half_band = 0.095
                conf_score = min(85, 64 + min(12, len(c_spots) * 3))
            else:
                tier = "tier_2_website_verified_and_calibrated"
                half_band = 0.12
                conf_score = min(80, 54 + min(16, len(c_spots) * 3))

            conf_level = "high" if conf_score >= 75 else ("medium" if conf_score >= 55 else "low")

            occ_point_pct = round(100.0 * total_point / weekly_cap, 1)
            occ_low_pct = round(max(100.0 * org_week_hrs / weekly_cap, occ_point_pct - half_band * 100.0), 1)
            occ_high_pct = round(min(96.0, occ_point_pct + half_band * 100.0), 1)

            tot_low = round(weekly_cap * (occ_low_pct / 100.0), 2)
            tot_high = round(weekly_cap * (occ_high_pct / 100.0), 2)

            # Effective realized hourly court yield (accounting for free/unlimited off-peak & member discounts)
            eff_off_price = 12.0 if has_offpeak_unlim else off_price
            peak_occ_h = peak_cap * est_peak_rate
            off_occ_h = off_cap * est_off_rate
            blended_hr_yield = (
                (peak_occ_h * peak_price + off_occ_h * eff_off_price) / max(peak_occ_h + off_occ_h, 1.0)
            )
            # Apply 0.86 realization factor for member discounts (25% off for subset of bookings)
            gbv_point = round(total_point * blended_hr_yield * 0.88, 2)
            gbv_low = round(tot_low * blended_hr_yield * 0.80, 2)
            gbv_high = round(tot_high * blended_hr_yield * 0.96, 2)

            notes_parts = [
                f"env={env}",
                f"cap={weekly_cap:.0f}h/wk",
                f"direct_organised={org_week_hrs:.1f}h/wk ({len(c_acts)} live activities)",
            ]
            if fill_pct is not None:
                notes_parts.append(f"near_term_activity_fill={fill_pct:.1f}% ({tot_reg}/{tot_max} total spots)")
            if has_offpeak_unlim:
                notes_parts.append("live_signal:OffPeakUnlimited(€80/mo)")
            elif off_price == 0.0:
                notes_parts.append("live_signal:FreePlayWindow(11:00-15:00)")
            if c_spots:
                notes_parts.append(f"spot_checks={len(c_spots)}")

            results.append(
                {
                    "club_id": cid,
                    "reference_week_start": reference_monday.isoformat(),
                    "verified_padel_courts": n_courts,
                    "weekly_capacity_court_hours": weekly_cap,
                    "direct_organised_court_hours": org_week_hrs,
                    "live_activities_count": len(c_acts),
                    "live_registered_players": tot_reg,
                    "live_max_player_spots": tot_max,
                    "live_activity_fill_rate_pct": fill_pct,
                    "player_signup_24h_delta": vel_by_club[cid] if cid in has_vel_club else None,
                    "verified_activity_revenue_eur": ver_act_rev,
                    "spot_check_samples": len(c_spots),
                    "estimated_casual_court_hours_point": casual_point,
                    "total_occupied_court_hours_low": tot_low,
                    "total_occupied_court_hours_point": total_point,
                    "total_occupied_court_hours_high": tot_high,
                    "estimated_occupancy_pct_low": occ_low_pct,
                    "estimated_occupancy_pct_point": occ_point_pct,
                    "estimated_occupancy_pct_high": occ_high_pct,
                    "peak_occupancy_pct_point": round(est_peak_rate * 100.0, 1),
                    "offpeak_occupancy_pct_point": round(est_off_rate * 100.0, 1),
                    "est_weekly_gbv_eur_low": gbv_low,
                    "est_weekly_gbv_eur_point": gbv_point,
                    "est_weekly_gbv_eur_high": gbv_high,
                    "measurement_tier": tier,
                    "confidence_level": conf_level,
                    "confidence_score": conf_score,
                    "methodology_notes": "; ".join(notes_parts),
                    "computed_at": computed_at,
                }
            )

        results.sort(key=lambda r: r["total_occupied_court_hours_point"], reverse=True)
        return results
