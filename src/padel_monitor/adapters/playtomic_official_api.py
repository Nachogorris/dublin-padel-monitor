"""Authorized Playtomic Third-Party API adapter (https://third-party.playtomic.io/).

Designed for compliant deployment once a club manager (Champion / Master subscription:
Playtomic Manager -> Settings -> Developer Tools) or a certified Playtomic Connect
partner supplies `PLAYTOMIC_CLIENT_ID` and `PLAYTOMIC_CLIENT_SECRET`.

Reconstructs full court availability and confirmed bookings across a club's verified
Europe/Dublin opening hours from the official read-only Bookings endpoint.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from typing import Iterable, Optional

import httpx

from ..models import (
    AvailabilityObservation,
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
)
from ..time_utils import (
    DUBLIN_TZ,
    UTC_TZ,
    WEEKDAY_NAMES,
    generate_atomic_slices,
    now_dublin_iso,
    parse_iso_aware,
    resolve_operating_window,
)
from .base import AdapterCapabilities, BasePlatformAdapter

OFFICIAL_API_BASE = "https://third-party.playtomic.io"


class PlaytomicOfficialApiAdapter(BasePlatformAdapter):
    """Official OAuth2 read-only client for Playtomic Third-Party API."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: str = OFFICIAL_API_BASE,
    ) -> None:
        self.client_id = client_id or os.environ.get("PLAYTOMIC_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PLAYTOMIC_CLIENT_SECRET")
        self.base_url = base_url.rstrip("/")

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform_id="playtomic_official_api",
            supports_live_public_metadata=False,
            supports_live_public_slots=False,
            supports_authorized_api_slots=True,
            requires_credentials_for_slots=True,
            compliance_notes=(
                "Official Playtomic Third-Party API (third-party.playtomic.io). Requires "
                "Champion/Master club credentials or Playtomic Connect partner OAuth2 keys. "
                "Privacy filter strips any player PII (names, emails, phones) at ingestion."
            ),
        )

    def normalize_authorized_bookings(
        self,
        *,
        club_id: str,
        target_date: date,
        courts: Iterable[CourtRecord],
        opening_hours: dict[str, OpeningHoursRecord],
        bookings_payload: list[dict],
        observed_at: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[AvailabilityObservation]:
        """Convert official API bookings payload + court hours into privacy-safe 30-min observations.

        Never stores player IDs, names, emails, or payment PII (strictly court status, time, price).
        """
        obs_ts = observed_at or now_dublin_iso()
        dow = WEEKDAY_NAMES[target_date.weekday()]
        oh = opening_hours.get(dow)
        if oh is None:
            return []

        win_start, win_end = resolve_operating_window(target_date, oh.opening_time, oh.closing_time)
        source_url = f"{self.base_url}/api/v1/bookings"
        out: list[AvailabilityObservation] = []

        # Index active bookings per court (ignoring cancelled bookings)
        by_court: dict[str, list[tuple]] = {}
        for b in bookings_payload:
            if str(b.get("status", "")).upper() in ("CANCELLED", "CANCELED"):
                continue
            c_id = str(b.get("resource_id") or b.get("court_id") or "")
            if not c_id:
                continue
            b_start = parse_iso_aware(b["start_date"]).astimezone(UTC_TZ)
            b_end = parse_iso_aware(b["end_date"]).astimezone(UTC_TZ)
            b_type = str(b.get("booking_type") or b.get("type") or "PRIVATE_MATCH").upper()
            price_val = b.get("price")
            by_court.setdefault(c_id, []).append((b_start, b_end, b_type, price_val))

        for court in courts:
            if court.sport.upper() != "PADEL":
                continue
            c_bookings = by_court.get(court.court_id, [])
            for sl_s, sl_e in generate_atomic_slices(win_start, win_end, slice_minutes=30):
                sl_s_u = sl_s.astimezone(UTC_TZ)
                sl_e_u = sl_e.astimezone(UTC_TZ)
                overlapping = [b for b in c_bookings if b[0] < sl_e_u and b[1] > sl_s_u]
                if not overlapping:
                    out.append(
                        AvailabilityObservation.from_raw(
                            club_id=club_id,
                            court_id=court.court_id,
                            booking_platform="playtomic",
                            start_time=sl_s,
                            end_time=sl_e,
                            availability_status="available",
                            status_detail="observed_available",
                            observed_at=obs_ts,
                            source_url=source_url,
                            data_origin="live_authorized_api",
                            run_id=run_id,
                        )
                    )
                else:
                    b_start, b_end, b_type, price_val = overlapping[0]
                    is_block = any(k in b_type for k in ("LOCK", "MAINTENANCE", "BLOCK", "COURSE", "CLASS", "TOURNAMENT"))
                    b_dur_hrs = max((b_end - b_start).total_seconds() / 3600.0, 0.5)
                    slice_price = None
                    if price_val is not None and str(price_val).strip():
                        clean_p = float(str(price_val).replace("EUR", "").replace("€", "").strip())
                        slice_price = round(clean_p * (0.5 / b_dur_hrs), 2)
                    out.append(
                        AvailabilityObservation.from_raw(
                            club_id=club_id,
                            court_id=court.court_id,
                            booking_platform="playtomic",
                            start_time=sl_s,
                            end_time=sl_e,
                            price_eur=slice_price,
                            availability_status="blocked" if is_block else "unavailable",
                            status_detail="blocked_coaching_or_event" if is_block else "confirmed_booking",
                            observed_at=obs_ts,
                            source_url=source_url,
                            data_origin="live_authorized_api",
                            run_id=run_id,
                        )
                    )
        return out

    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
        venue_uuid: Optional[str] = None,
        courts: Optional[list[CourtRecord]] = None,
        opening_hours: Optional[dict[str, OpeningHoursRecord]] = None,
    ) -> CollectionRunResult:
        run_id = f"pt-api-{club_id}-{target_date.isoformat()}-{uuid.uuid4().hex[:6]}"
        started = now_dublin_iso()
        target_endpoint = f"{self.base_url}/api/v1/bookings"

        if not self.client_id or not self.client_secret:
            return CollectionRunResult(
                run_id=run_id,
                started_at=started,
                completed_at=now_dublin_iso(),
                adapter_name="PlaytomicOfficialApiAdapter",
                platform_id="playtomic",
                club_id=club_id,
                target_url=target_endpoint,
                http_status=None,
                robots_allowed=True,
                auth_required=True,
                run_status="blocked_auth",
                data_origin="live_authorized_api",
                records_written=0,
                error_message=(
                    "Awaiting PLAYTOMIC_CLIENT_ID and PLAYTOMIC_CLIENT_SECRET from club "
                    "(Champion/Master tier) or Playtomic Connect partner onboarding."
                ),
            )

        with httpx.Client(timeout=20.0) as client:
            token_resp = client.post(
                f"{self.base_url}/api/v1/auth/token",
                json={"client_id": self.client_id, "client_secret": self.client_secret},
            )
            if token_resp.status_code != 200:
                return CollectionRunResult(
                    run_id=run_id,
                    started_at=started,
                    completed_at=now_dublin_iso(),
                    adapter_name="PlaytomicOfficialApiAdapter",
                    platform_id="playtomic",
                    club_id=club_id,
                    target_url=target_endpoint,
                    http_status=token_resp.status_code,
                    robots_allowed=True,
                    auth_required=True,
                    run_status="blocked_auth",
                    data_origin="live_authorized_api",
                    records_written=0,
                    error_message=f"OAuth2 token request returned HTTP {token_resp.status_code}",
                )
            token = token_resp.json()["access_token"]
            params = {
                "start_date": f"{target_date.isoformat()}T00:00:00Z",
                "end_date": f"{(target_date + timedelta(days=1)).isoformat()}T04:00:00Z",
            }
            if venue_uuid:
                params["venue_id"] = venue_uuid
            b_resp = client.get(
                target_endpoint,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            completed = now_dublin_iso()
            if b_resp.status_code != 200:
                return CollectionRunResult(
                    run_id=run_id,
                    started_at=started,
                    completed_at=completed,
                    adapter_name="PlaytomicOfficialApiAdapter",
                    platform_id="playtomic",
                    club_id=club_id,
                    target_url=target_endpoint,
                    http_status=b_resp.status_code,
                    robots_allowed=True,
                    auth_required=True,
                    run_status="http_error",
                    data_origin="live_authorized_api",
                    records_written=0,
                    error_message=f"Bookings endpoint returned HTTP {b_resp.status_code}",
                )
            payload = b_resp.json()
            bookings_list = payload.get("data", payload) if isinstance(payload, dict) else payload
            obs = self.normalize_authorized_bookings(
                club_id=club_id,
                target_date=target_date,
                courts=courts or [],
                opening_hours=opening_hours or {},
                bookings_payload=bookings_list,
                observed_at=completed,
                run_id=run_id,
            )
            return CollectionRunResult(
                run_id=run_id,
                started_at=started,
                completed_at=completed,
                adapter_name="PlaytomicOfficialApiAdapter",
                platform_id="playtomic",
                club_id=club_id,
                target_url=target_endpoint,
                http_status=200,
                robots_allowed=True,
                auth_required=True,
                run_status="success",
                data_origin="live_authorized_api",
                records_written=len(obs),
                observations=obs,
            )

