"""Compliance probes for non-Playtomic Dublin booking platforms (Section 4 & Section 9).

Evaluates public accessibility, robots.txt permissions, login redirects, Cloudflare
anti-bot walls, client-side SPA shells, broken calendar shortcodes, and enquiry-only
pages across Matchpoint, Playbypoint, GladstoneGo, SmartClubCloud, Padel Ireland WP,
and dlr Leisure—both live via CompliantHttpClient and from saved audit captures.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Optional

import httpx

from ..compliance import CompliantHttpClient, Rfc9309RobotsPolicy
from ..models import CollectionRunResult
from ..time_utils import now_dublin_iso
from .base import AdapterCapabilities, BasePlatformAdapter


PLATFORM_CAPABILITIES_MAP: dict[str, AdapterCapabilities] = {
    "matchpoint": AdapterCapabilities(
        platform_id="matchpoint",
        supports_live_public_metadata=False,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=True,
        compliance_notes=(
            "Padel Dome (padeldome-ie.matchpoint.com.es) redirects unauthenticated visitors "
            "to /Login.aspx and robots.txt explicitly Disallows /Booking/, /Reservas/, /Services/."
        ),
    ),
    "playbypoint": AdapterCapabilities(
        platform_id="playbypoint",
        supports_live_public_metadata=False,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=True,
        compliance_notes=(
            "Bushy Park (bushytennispadel.playbypoint.com) serves HTTP 403 Cloudflare Turnstile "
            "challenge to automated clients; robots.txt Disallows /reservations/* and reserves TDM rights."
        ),
    ),
    "gladstonego": AdapterCapabilities(
        platform_id="gladstonego",
        supports_live_public_metadata=False,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=True,
        compliance_notes=(
            "Portmarnock PSLC (pslc.gladstonego.cloud/book) serves an empty Angular SPA shell "
            "(<app-root></app-root>) backed by undocumented internal tenant APIs without public API docs."
        ),
    ),
    "smartclubcloud": AdapterCapabilities(
        platform_id="smartclubcloud",
        supports_live_public_metadata=False,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=True,
        compliance_notes=(
            "Shankill TC, Sutton LTC, and Castleknock LTC use member/registered-account portals "
            "(SmartClubCloud / Club Manager 365); calendars require authenticated login."
        ),
    ),
    "padel_ireland_wp": AdapterCapabilities(
        platform_id="padel_ireland_wp",
        supports_live_public_metadata=True,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=True,
        compliance_notes=(
            "padel-ireland.ie/bookings/ is allowed by robots.txt (Crawl-delay: 10), but currently "
            "renders an unexpanded WordPress shortcode [rncbc_calendar id=\"1\"] + member login form."
        ),
    ),
    "dlr_enquiry": AdapterCapabilities(
        platform_id="dlr_enquiry",
        supports_live_public_metadata=True,
        supports_live_public_slots=False,
        supports_authorized_api_slots=False,
        requires_credentials_for_slots=False,
        compliance_notes=(
            "dlrleisure.ie/facilities/padel-courts/ publishes verified court counts & prices "
            "(Meadowbrook 2 courts, Loughlinstown 3 courts), while /facility-booking/ redirects "
            "to an enquiry form with no live online slot calendar."
        ),
    ),
}


def inspect_portal_body_limitations(final_url: str, body: str) -> Optional[tuple[str, str]]:
    """Detect non-auth structural limitations in HTTP 200 portal HTML responses."""
    low_url = final_url.lower()
    low_body = body.lower()

    if "<app-root" in low_body and len(body) < 10000:
        return (
            "blocked_compliance",
            "Client-side Angular SPA shell (<app-root>) with no SSR slot calendar and no documented public API.",
        )
    if "[rncbc_calendar" in low_body or "&#91;rncbc_calendar" in low_body:
        return (
            "parse_error",
            "Public page renders broken unexpanded WordPress shortcode [rncbc_calendar id=\"1\"] plus login gate; no live calendar rendered.",
        )
    if "facility-booking-enquiry" in low_url:
        return (
            "blocked_compliance",
            "Redirects to /facilities/facility-booking-enquiry/ contact form; bookings handled via reception/telephone.",
        )
    return None


class GenericPortalProbeAdapter(BasePlatformAdapter):
    """Compliance-safe probe adapter for non-Playtomic booking platforms."""

    def __init__(
        self,
        platform_id: str,
        http_client: Optional[CompliantHttpClient] = None,
    ) -> None:
        self.platform_id = platform_id
        self._http = http_client

    @property
    def capabilities(self) -> AdapterCapabilities:
        return PLATFORM_CAPABILITIES_MAP.get(
            self.platform_id,
            AdapterCapabilities(
                platform_id=self.platform_id,
                supports_live_public_metadata=False,
                supports_live_public_slots=False,
                supports_authorized_api_slots=False,
                requires_credentials_for_slots=True,
                compliance_notes="Proprietary member portal requiring authenticated club membership.",
            ),
        )

    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
    ) -> CollectionRunResult:
        run_id = f"probe-{self.platform_id}-{club_id}-{uuid.uuid4().hex[:6]}"
        started = now_dublin_iso()
        own_client = self._http is None
        client = self._http or CompliantHttpClient()
        try:
            outcome = client.fetch_compliant(booking_url)
            completed = now_dublin_iso()
            run_status = outcome.outcome_code
            err_msg = None if outcome.outcome_code == "success" else outcome.detail

            if outcome.outcome_code == "success" and outcome.final_url:
                limitation = inspect_portal_body_limitations(outcome.final_url, outcome.body)
                if limitation is not None:
                    run_status, err_msg = limitation

            return CollectionRunResult(
                run_id=run_id,
                started_at=started,
                completed_at=completed,
                adapter_name=f"GenericPortalProbeAdapter[{self.platform_id}]",
                platform_id=self.platform_id,
                club_id=club_id,
                target_url=booking_url,
                http_status=outcome.http_status,
                robots_allowed=outcome.robots_allowed,
                auth_required=outcome.auth_required,
                run_status=run_status,
                data_origin="live_public",
                records_written=0,
                error_message=err_msg,
            )
        finally:
            if own_client:
                client.close()

    def evaluate_cached_probe(
        self,
        *,
        club_id: Optional[str],
        url: str,
        final_url: str,
        http_status: Optional[int],
        fetched_at_iso: str,
        body_path: Optional[Path],
        robots_path: Optional[Path] = None,
    ) -> CollectionRunResult:
        """Evaluate a previously captured live HTTP response from data/raw/policy/."""
        run_id = f"live-audit-{self.platform_id}-{club_id or 'portal'}-{uuid.uuid4().hex[:6]}"
        body = body_path.read_text(encoding="utf-8", errors="replace") if (body_path and body_path.exists()) else ""

        robots_allowed = True
        if robots_path and robots_path.exists():
            r_txt = robots_path.read_text(encoding="utf-8", errors="replace")
            if not r_txt.lstrip().startswith("<!DOCTYPE") and "<html" not in r_txt[:500].lower():
                rp = Rfc9309RobotsPolicy(r_txt)
                robots_allowed = rp.can_fetch("*", url)

        if http_status is None:
            return CollectionRunResult(
                run_id=run_id,
                started_at=fetched_at_iso,
                completed_at=fetched_at_iso,
                adapter_name=f"GenericPortalProbeAdapter[{self.platform_id}]",
                platform_id=self.platform_id,
                club_id=club_id,
                target_url=url,
                http_status=None,
                robots_allowed=robots_allowed,
                auth_required=False,
                run_status="http_error",
                data_origin="live_public",
                records_written=0,
                error_message="DNS / TCP connection failure (endpoint unreachable)",
                raw_artifact_path=str(body_path) if body_path else None,
            )

        antibot = CompliantHttpClient.detect_antibot(http_status, body, httpx.Headers({}))
        auth_wall = CompliantHttpClient.detect_auth_wall(final_url or url, body)

        if not robots_allowed:
            status = "blocked_compliance"
            detail = "Disallowed by site robots.txt"
        elif antibot:
            status = "blocked_antibot"
            detail = f"HTTP {http_status} Cloudflare anti-bot challenge page; stopped per compliance policy"
        elif auth_wall:
            status = "blocked_auth"
            detail = f"Unauthenticated request redirected to login wall ({final_url})"
        elif http_status >= 400:
            status = "http_error"
            detail = f"HTTP {http_status}"
        else:
            lim = inspect_portal_body_limitations(final_url or url, body)
            if lim is not None:
                status, detail = lim
            else:
                status, detail = "success", None

        return CollectionRunResult(
            run_id=run_id,
            started_at=fetched_at_iso,
            completed_at=fetched_at_iso,
            adapter_name=f"GenericPortalProbeAdapter[{self.platform_id}]",
            platform_id=self.platform_id,
            club_id=club_id,
            target_url=url,
            http_status=http_status,
            robots_allowed=robots_allowed,
            auth_required=auth_wall,
            run_status=status,
            data_origin="live_public",
            records_written=0,
            error_message=detail,
            raw_artifact_path=str(body_path) if body_path else None,
        )
