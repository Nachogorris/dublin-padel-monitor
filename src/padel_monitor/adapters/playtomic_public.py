"""Playtomic public SSR club & court collector + robots.txt compliance guard.

What IS permitted & collected live without authentication:
- GET https://playtomic.com/clubs/<slug> (allowed by https://playtomic.com/robots.txt)
- Extracts embedded Next.js RSC tenant JSON:
  * tenant_id, tenant_name, slug, address, lat/lon, Europe/Dublin timezone, isPrivate
  * Individual courts (resourceId, court name, sport=PADEL vs PICKLEBALL/SQUASH,
    Indoor / Roofed outdoor / Outdoor, Double / Single, Panoramic / Crystal)
  * Day-by-day opening_hours (including post-midnight closings like 00:30 and 01:00)
  * Publicly scheduled club competitions & academy courses (which occupy court capacity)
  * Cross-referenced nearby Playtomic clubs (otherRelevantTenants)

What is BLOCKED by compliance & never bypassed:
- Playtomic's robots.txt explicitly sets:
    Disallow: /api
    Disallow: /*?*sport=
    Disallow: /*?*date=
- Playtomic's Legal Conditions (Section 3) prohibit automated crawlers / commercial
  bulk extraction of calendar data without authorization.
- Attempting to collect per-date court availability via /clubs/<slug>?date=YYYY-MM-DD or
  /api/... is therefore intercepted by our compliance gatekeeper and logged cleanly with
  run_status='blocked_compliance', directing the operator to PlaytomicOfficialApiAdapter.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from ..compliance import CompliantHttpClient
from ..models import (
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
    PublicClubEvent,
)
from ..time_utils import DUBLIN_TZ, parse_hhmm, parse_iso_aware, now_dublin_iso, to_dublin
from .base import AdapterCapabilities, BasePlatformAdapter

RSC_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", flags=re.DOTALL)


@dataclass
class PlaytomicClubProfile:
    club_id: str
    tenant_id: str
    tenant_name: str
    slug: str
    is_private: bool
    street: str
    city: str
    postal_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    timezone: str
    website_url: Optional[str]
    courts: list[CourtRecord]
    opening_hours: list[OpeningHoursRecord]
    public_events: list[PublicClubEvent]
    other_relevant_tenants: list[dict]
    raw_tenant_dict: dict


def _classify_court_environment(features: list[str]) -> str:
    low = {f.lower() for f in features}
    if "indoor" in low:
        return "indoor"
    if any("roofed" in f or "covered" in f for f in low):
        return "covered_outdoor"
    if "outdoor" in low:
        return "outdoor"
    return "unknown"


def _classify_court_size(features: list[str], name: str) -> str:
    low = {f.lower() for f in features}
    if "single" in low or "single" in name.lower():
        return "single"
    if "double" in low:
        return "double"
    return "double"


def _classify_wall_type(features: list[str]) -> Optional[str]:
    for f in features:
        if f.lower() in ("panoramic", "crystal", "wall", "concrete"):
            return f
    return None


def parse_playtomic_club_html(
    html_text: str,
    club_id: str,
    source_url: str,
    observed_at: Optional[str] = None,
    run_id: Optional[str] = None,
) -> PlaytomicClubProfile:
    """Parse Next.js RSC stream embedded in a Playtomic /clubs/<slug> HTML document."""
    obs_ts = observed_at or now_dublin_iso()
    chunks = RSC_PUSH_RE.findall(html_text)
    payload_parts: list[str] = []
    for raw_chunk in chunks:
        try:
            arr = json.loads(raw_chunk)
            if len(arr) > 1 and isinstance(arr[1], str):
                payload_parts.append(arr[1])
        except json.JSONDecodeError:
            continue

    payload = "".join(payload_parts)
    marker_idx = payload.find('{"tenant":{"tenant_id"')
    if marker_idx < 0:
        raise ValueError(f"No embedded Playtomic tenant JSON found in {source_url}")

    obj, _ = json.JSONDecoder().raw_decode(payload, marker_idx)
    tn = obj["tenant"]
    addr = tn.get("address") or {}
    coord = addr.get("coordinate") or {}
    props = tn.get("properties") or {}

    courts: list[CourtRecord] = []
    for res in tn.get("resources") or []:
        feats = list(res.get("features") or [])
        c_name = str(res.get("name") or "").strip()
        courts.append(
            CourtRecord(
                club_id=club_id,
                court_id=str(res["resourceId"]),
                court_name=c_name,
                sport=str(res.get("sport") or "PADEL").upper(),
                court_environment=_classify_court_environment(feats),
                court_size=_classify_court_size(feats, c_name),
                wall_type=_classify_wall_type(feats),
                is_active=True,
                source_origin="live_public",
                verified_at=obs_ts,
            )
        )

    opening_hours: list[OpeningHoursRecord] = []
    for dow, spec in (tn.get("opening_hours") or {}).items():
        o_str = str(spec.get("opening_time") or "").strip()
        c_str = str(spec.get("closing_time") or "").strip()
        if not o_str or not c_str:
            continue
        closes_next = parse_hhmm(c_str) <= parse_hhmm(o_str) or c_str == "24:00"
        opening_hours.append(
            OpeningHoursRecord(
                club_id=club_id,
                day_of_week=dow.upper(),
                opening_time=o_str,
                closing_time=c_str,
                closes_next_day=closes_next,
                source_origin="live_public",
                updated_at=obs_ts,
            )
        )

    events: list[PublicClubEvent] = []
    for comp in obj.get("competitionCards") or []:
        if not isinstance(comp, dict) or not comp.get("id"):
            continue
        start_raw = comp.get("startAt")
        start_dub = (
            to_dublin(parse_iso_aware(start_raw)).isoformat(timespec="seconds")
            if start_raw
            else None
        )
        events.append(
            PublicClubEvent(
                club_id=club_id,
                event_id=str(comp["id"]),
                event_type="competition",
                title="Scheduled Club Competition / Americano",
                start_time=start_dub,
                price_eur=None,
                source_url=source_url,
                observed_at=obs_ts,
                run_id=run_id,
            )
        )

    return PlaytomicClubProfile(
        club_id=club_id,
        tenant_id=str(tn["tenant_id"]),
        tenant_name=str(tn.get("tenant_name") or "").strip(),
        slug=str(tn.get("slug") or "").strip(),
        is_private=bool(obj.get("isPrivate", False)),
        street=str(addr.get("street") or "").strip(),
        city=str(addr.get("city") or "").strip(),
        postal_code=str(addr.get("postal_code") or "").strip(),
        latitude=float(coord["lat"]) if coord.get("lat") is not None else None,
        longitude=float(coord["lon"]) if coord.get("lon") is not None else None,
        timezone=str(addr.get("timezone") or "Europe/Dublin"),
        website_url=str(props.get("WEBSITE_URL") or "").strip() or None,
        courts=courts,
        opening_hours=opening_hours,
        public_events=events,
        other_relevant_tenants=list(obj.get("otherRelevantTenants") or []),
        raw_tenant_dict=obj,
    )


class PlaytomicPublicClubAdapter(BasePlatformAdapter):
    """Live robots-compliant collector for Playtomic club metadata & compliance verifier."""

    def __init__(
        self,
        http_client: Optional[CompliantHttpClient] = None,
        raw_save_dir: Optional[Path] = None,
    ) -> None:
        self._http = http_client
        self._raw_save_dir = raw_save_dir

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform_id="playtomic",
            supports_live_public_metadata=True,
            supports_live_public_slots=False,
            supports_authorized_api_slots=True,
            requires_credentials_for_slots=True,
            compliance_notes=(
                "GET /clubs/<slug> is allowed by robots.txt and exposes verified court IDs, "
                "indoor/outdoor features, opening hours, and scheduled competitions. "
                "Per-date slot queries (/api, /*?*date=, /*?*sport=) are explicitly Disallowed "
                "in robots.txt and restricted under Legal Conditions Section 3; use "
                "PlaytomicOfficialApiAdapter with club/partner OAuth2 credentials for live slots."
            ),
        )

    def collect_club_profile(
        self,
        club_id: str,
        club_url: str,
        cached_html_path: Optional[Path] = None,
    ) -> tuple[CollectionRunResult, Optional[PlaytomicClubProfile]]:
        """Collect live (or cached live-fetched) club & court profile from /clubs/<slug>."""
        run_id = f"pt-meta-{club_id}-{uuid.uuid4().hex[:8]}"
        started = now_dublin_iso()

        if cached_html_path is not None and cached_html_path.exists():
            html_text = cached_html_path.read_text(encoding="utf-8")
            profile = parse_playtomic_club_html(
                html_text, club_id=club_id, source_url=club_url, observed_at=started, run_id=run_id
            )
            completed = now_dublin_iso()
            run = CollectionRunResult(
                run_id=run_id,
                started_at=started,
                completed_at=completed,
                adapter_name="PlaytomicPublicClubAdapter.collect_club_profile",
                platform_id="playtomic",
                club_id=club_id,
                target_url=club_url,
                http_status=200,
                robots_allowed=True,
                auth_required=False,
                run_status="success",
                data_origin="live_public",
                records_written=len(profile.courts) + len(profile.opening_hours) + len(profile.public_events),
                raw_artifact_path=str(cached_html_path),
            )
            return run, profile

        own_client = self._http is None
        client = self._http or CompliantHttpClient()
        try:
            outcome = client.fetch_compliant(club_url)
            completed = now_dublin_iso()
            if outcome.outcome_code != "success":
                return (
                    CollectionRunResult(
                        run_id=run_id,
                        started_at=started,
                        completed_at=completed,
                        adapter_name="PlaytomicPublicClubAdapter.collect_club_profile",
                        platform_id="playtomic",
                        club_id=club_id,
                        target_url=club_url,
                        http_status=outcome.http_status,
                        robots_allowed=outcome.robots_allowed,
                        auth_required=outcome.auth_required,
                        run_status=outcome.outcome_code,
                        data_origin="live_public",
                        records_written=0,
                        error_message=outcome.detail,
                    ),
                    None,
                )

            artifact_path: Optional[str] = None
            if self._raw_save_dir is not None:
                self._raw_save_dir.mkdir(parents=True, exist_ok=True)
                out_file = self._raw_save_dir / f"playtomic__{club_id}.html"
                out_file.write_text(outcome.body, encoding="utf-8")
                artifact_path = str(out_file)

            try:
                profile = parse_playtomic_club_html(
                    outcome.body, club_id=club_id, source_url=club_url, observed_at=completed, run_id=run_id
                )
            except Exception as exc:
                return (
                    CollectionRunResult(
                        run_id=run_id,
                        started_at=started,
                        completed_at=completed,
                        adapter_name="PlaytomicPublicClubAdapter.collect_club_profile",
                        platform_id="playtomic",
                        club_id=club_id,
                        target_url=club_url,
                        http_status=outcome.http_status,
                        robots_allowed=True,
                        auth_required=False,
                        run_status="parse_error",
                        data_origin="live_public",
                        records_written=0,
                        error_message=str(exc),
                        raw_artifact_path=artifact_path,
                    ),
                    None,
                )

            return (
                CollectionRunResult(
                    run_id=run_id,
                    started_at=started,
                    completed_at=completed,
                    adapter_name="PlaytomicPublicClubAdapter.collect_club_profile",
                    platform_id="playtomic",
                    club_id=club_id,
                    target_url=club_url,
                    http_status=outcome.http_status,
                    robots_allowed=True,
                    auth_required=False,
                    run_status="success",
                    data_origin="live_public",
                    records_written=len(profile.courts) + len(profile.opening_hours) + len(profile.public_events),
                    raw_artifact_path=artifact_path,
                ),
                profile,
            )
        finally:
            if own_client:
                client.close()

    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
    ) -> CollectionRunResult:
        """Attempt compliant date-specific availability fetch; enforces robots.txt Disallow."""
        run_id = f"pt-slots-{club_id}-{target_date.isoformat()}-{uuid.uuid4().hex[:6]}"
        started = now_dublin_iso()
        dated_url = f"{booking_url.rstrip('/')}?date={target_date.isoformat()}&sport=PADEL"

        own_client = self._http is None
        client = self._http or CompliantHttpClient()
        try:
            allowed = client.is_robots_allowed(dated_url)
            completed = now_dublin_iso()
            if not allowed:
                return CollectionRunResult(
                    run_id=run_id,
                    started_at=started,
                    completed_at=completed,
                    adapter_name="PlaytomicPublicClubAdapter.collect_club_availability",
                    platform_id="playtomic",
                    club_id=club_id,
                    target_url=dated_url,
                    http_status=None,
                    robots_allowed=False,
                    auth_required=False,
                    run_status="blocked_compliance",
                    data_origin="live_public",
                    records_written=0,
                    error_message=(
                        "Blocked by https://playtomic.com/robots.txt (Disallow: /*?*date=, "
                        "Disallow: /*?*sport=, Disallow: /api) and Playtomic Legal Conditions "
                        "Section 3. Use PlaytomicOfficialApiAdapter with authorized club/partner credentials."
                    ),
                )
            # Defensive fallback if robots.txt ever changes in the future:
            return CollectionRunResult(
                run_id=run_id,
                started_at=started,
                completed_at=completed,
                adapter_name="PlaytomicPublicClubAdapter.collect_club_availability",
                platform_id="playtomic",
                club_id=club_id,
                target_url=dated_url,
                http_status=None,
                robots_allowed=True,
                auth_required=False,
                run_status="blocked_compliance",
                data_origin="live_public",
                records_written=0,
                error_message="SSR HTML omits slot grid; /api remains restricted to Playtomic Connect partners.",
            )
        finally:
            if own_client:
                client.close()

