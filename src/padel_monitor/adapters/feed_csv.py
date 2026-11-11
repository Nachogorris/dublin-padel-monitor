"""CSV observation feed adapter supporting both Section 6 canonical CSVs and starter CSVs."""
from __future__ import annotations

import csv
import uuid
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..models import AvailabilityObservation, CollectionRunResult
from ..time_utils import now_dublin_iso
from .base import AdapterCapabilities, BasePlatformAdapter


def infer_platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "playtomic" in host:
        return "playtomic"
    if "matchpoint" in host:
        return "matchpoint"
    if "playbypoint" in host:
        return "playbypoint"
    if "gladstonego" in host:
        return "gladstonego"
    if "smartclubcloud" in host:
        return "smartclubcloud"
    if "padel-ireland" in host:
        return "padel_ireland_wp"
    if "dlrleisure" in host:
        return "dlr_enquiry"
    return "csv_feed"


class CsvObservationAdapter(BasePlatformAdapter):
    """Ingest, validate, normalize, and deduplicate CSV availability observation files."""

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform_id="csv_feed",
            supports_live_public_metadata=False,
            supports_live_public_slots=False,
            supports_authorized_api_slots=True,
            requires_credentials_for_slots=False,
            compliance_notes=(
                "Offline/authorized CSV ingestion adapter supporting both the Section 6 "
                "canonical observation schema and legacy starter example_observations.csv."
            ),
        )

    def load_csv(
        self,
        csv_path: str | Path,
        *,
        default_data_origin: str = "live_public",
        strict: bool = False,
    ) -> tuple[CollectionRunResult, list[str]]:
        """Parse a CSV file into validated AvailabilityObservation objects.

        Returns (CollectionRunResult, row_error_messages).
        """
        path = Path(csv_path)
        run_id = f"csv-{path.stem}-{uuid.uuid4().hex[:8]}"
        started = now_dublin_iso()

        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")

        observations: list[AvailabilityObservation] = []
        seen_keys: set[tuple[str, str, str, str, str, str]] = set()
        duplicate_rows = 0
        row_errors: list[str] = []

        with path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            headers = set(reader.fieldnames or [])
            if not headers:
                raise ValueError(f"CSV file has no header row: {path}")

            is_canonical = {"club_id", "court_id", "start_time", "end_time", "availability_status", "observed_at", "source_url"}.issubset(headers)
            is_legacy = {"venue_id", "court_id", "slot_start", "slot_end", "status", "observed_at", "source_url"}.issubset(headers)

            if not is_canonical and not is_legacy:
                raise ValueError(
                    f"Unsupported CSV columns in {path.name}: {sorted(headers)}. "
                    "Expected Section 6 canonical columns or starter venue_id/slot_start columns."
                )

            for line_num, row in enumerate(reader, start=2):
                try:
                    club_id = row.get("club_id") if is_canonical else row.get("venue_id")
                    start_raw = row.get("start_time") if is_canonical else row.get("slot_start")
                    end_raw = row.get("end_time") if is_canonical else row.get("slot_end")
                    status_raw = row.get("availability_status") if is_canonical else row.get("status")
                    src_url = (row.get("source_url") or "").strip()
                    platform = (row.get("booking_platform") or "").strip() or infer_platform_from_url(src_url)
                    origin = (row.get("data_origin") or "").strip() or default_data_origin
                    status_detail = (row.get("status_detail") or "").strip() or "unspecified"
                    bucket = (row.get("snapshot_bucket") or "").strip() or None

                    obs = AvailabilityObservation.from_raw(
                        club_id=club_id or "",
                        court_id=row.get("court_id") or "",
                        booking_platform=platform,
                        start_time=start_raw or "",
                        end_time=end_raw or "",
                        availability_status=status_raw or "",
                        observed_at=row.get("observed_at") or "",
                        source_url=src_url,
                        price_eur=row.get("price_eur"),
                        status_detail=status_detail,
                        snapshot_bucket=bucket,
                        data_origin=origin,
                        run_id=run_id,
                    )
                    dedup_key = (
                        obs.club_id,
                        obs.court_id,
                        obs.start_time,
                        obs.end_time,
                        obs.observed_at,
                        obs.data_origin,
                    )
                    if dedup_key in seen_keys:
                        duplicate_rows += 1
                        continue
                    seen_keys.add(dedup_key)
                    observations.append(obs)
                except Exception as exc:
                    msg = f"Row {line_num}: {exc}"
                    if strict:
                        raise ValueError(msg) from exc
                    row_errors.append(msg)

        completed = now_dublin_iso()
        err_summary: Optional[str] = None
        if row_errors or duplicate_rows:
            parts = []
            if duplicate_rows:
                parts.append(f"deduplicated {duplicate_rows} duplicate row(s)")
            if row_errors:
                parts.append(f"skipped {len(row_errors)} invalid row(s): {row_errors[0]}")
            err_summary = "; ".join(parts)

        run_status = "parse_error" if (row_errors and not observations) else "success"
        result = CollectionRunResult(
            run_id=run_id,
            started_at=started,
            completed_at=completed,
            adapter_name="CsvObservationAdapter",
            platform_id="csv_feed",
            club_id=None,
            target_url=path.as_posix(),
            http_status=None,
            robots_allowed=True,
            auth_required=False,
            run_status=run_status,
            data_origin=default_data_origin,
            records_written=len(observations),
            error_message=err_summary,
            raw_artifact_path=str(path),
            observations=observations,
        )
        return result, row_errors

    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
    ) -> CollectionRunResult:
        result, _ = self.load_csv(booking_url)
        filtered = [
            o for o in result.observations if o.club_id == club_id and o.target_date == target_date.isoformat()
        ]
        result.club_id = club_id
        result.observations = filtered
        result.records_written = len(filtered)
        return result

