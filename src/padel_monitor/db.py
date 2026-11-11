"""SQLite persistence layer: schema initialization, idempotent upserts, and snapshot queries."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import (
    AvailabilityObservation,
    CollectionRunResult,
    CourtRecord,
    OpeningHoursRecord,
    PublicClubEvent,
)
from .time_utils import now_dublin_iso

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


class PadelDatabase:
    """Managed SQLite repository for Dublin padel facilities and availability snapshots."""

    def __init__(self, db_path: str | Path, schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
        self.db_path = Path(db_path)
        self.schema_path = schema_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        ddl = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(ddl)

    def upsert_platforms(self, platforms: Iterable[dict]) -> int:
        ts = now_dublin_iso()
        count = 0
        with self.connect() as conn:
            for p in platforms:
                conn.execute(
                    """
                    INSERT INTO booking_platforms (
                        platform_id, platform_name, vendor_website, official_api_status,
                        official_api_docs_url, public_calendar_auth, robots_policy_summary,
                        terms_scraping_policy, automated_slot_access, recommended_approach,
                        priority_rank, notes, updated_at
                    ) VALUES (
                        :platform_id, :platform_name, :vendor_website, :official_api_status,
                        :official_api_docs_url, :public_calendar_auth, :robots_policy_summary,
                        :terms_scraping_policy, :automated_slot_access, :recommended_approach,
                        :priority_rank, :notes, :updated_at
                    )
                    ON CONFLICT(platform_id) DO UPDATE SET
                        platform_name=excluded.platform_name,
                        vendor_website=excluded.vendor_website,
                        official_api_status=excluded.official_api_status,
                        official_api_docs_url=excluded.official_api_docs_url,
                        public_calendar_auth=excluded.public_calendar_auth,
                        robots_policy_summary=excluded.robots_policy_summary,
                        terms_scraping_policy=excluded.terms_scraping_policy,
                        automated_slot_access=excluded.automated_slot_access,
                        recommended_approach=excluded.recommended_approach,
                        priority_rank=excluded.priority_rank,
                        notes=excluded.notes,
                        updated_at=excluded.updated_at
                    """,
                    {**p, "updated_at": p.get("updated_at") or ts},
                )
                count += 1
        return count

    def upsert_clubs(self, clubs: Iterable[dict]) -> int:
        ts = now_dublin_iso()
        count = 0
        with self.connect() as conn:
            for c in clubs:
                conn.execute(
                    """
                    INSERT INTO clubs (
                        club_id, club_name, area, address, eircode, county, in_county_dublin,
                        inventory_cohort, latitude, longitude, timezone, website_url,
                        preliminary_courts, verified_padel_courts, doubles_courts, singles_courts,
                        other_sport_courts, court_environment, platform_id, platform_tenant_id,
                        public_booking_url, access_model, unauthenticated_calendar_visible,
                        automated_collection_status, off_peak_price_eur, peak_price_eur,
                        price_notes, opening_hours_summary, verification_confidence,
                        verification_sources, verification_notes, updated_at
                    ) VALUES (
                        :club_id, :club_name, :area, :address, :eircode, :county, :in_county_dublin,
                        :inventory_cohort, :latitude, :longitude, :timezone, :website_url,
                        :preliminary_courts, :verified_padel_courts, :doubles_courts, :singles_courts,
                        :other_sport_courts, :court_environment, :platform_id, :platform_tenant_id,
                        :public_booking_url, :access_model, :unauthenticated_calendar_visible,
                        :automated_collection_status, :off_peak_price_eur, :peak_price_eur,
                        :price_notes, :opening_hours_summary, :verification_confidence,
                        :verification_sources, :verification_notes, :updated_at
                    )
                    ON CONFLICT(club_id) DO UPDATE SET
                        club_name=excluded.club_name,
                        area=excluded.area,
                        address=excluded.address,
                        eircode=excluded.eircode,
                        county=excluded.county,
                        in_county_dublin=excluded.in_county_dublin,
                        inventory_cohort=excluded.inventory_cohort,
                        latitude=COALESCE(excluded.latitude, clubs.latitude),
                        longitude=COALESCE(excluded.longitude, clubs.longitude),
                        timezone=excluded.timezone,
                        website_url=COALESCE(excluded.website_url, clubs.website_url),
                        preliminary_courts=excluded.preliminary_courts,
                        verified_padel_courts=excluded.verified_padel_courts,
                        doubles_courts=excluded.doubles_courts,
                        singles_courts=excluded.singles_courts,
                        other_sport_courts=excluded.other_sport_courts,
                        court_environment=excluded.court_environment,
                        platform_id=excluded.platform_id,
                        platform_tenant_id=COALESCE(excluded.platform_tenant_id, clubs.platform_tenant_id),
                        public_booking_url=COALESCE(excluded.public_booking_url, clubs.public_booking_url),
                        access_model=excluded.access_model,
                        unauthenticated_calendar_visible=excluded.unauthenticated_calendar_visible,
                        automated_collection_status=excluded.automated_collection_status,
                        off_peak_price_eur=excluded.off_peak_price_eur,
                        peak_price_eur=excluded.peak_price_eur,
                        price_notes=excluded.price_notes,
                        opening_hours_summary=excluded.opening_hours_summary,
                        verification_confidence=excluded.verification_confidence,
                        verification_sources=excluded.verification_sources,
                        verification_notes=excluded.verification_notes,
                        updated_at=excluded.updated_at
                    """,
                    {**c, "updated_at": c.get("updated_at") or ts},
                )
                count += 1
        return count

    def upsert_courts(self, courts: Iterable[CourtRecord]) -> int:
        ts = now_dublin_iso()
        count = 0
        with self.connect() as conn:
            for c in courts:
                conn.execute(
                    """
                    INSERT INTO courts (
                        club_id, court_id, court_name, sport, court_environment,
                        court_size, wall_type, is_active, source_origin, verified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(club_id, court_id) DO UPDATE SET
                        court_name=excluded.court_name,
                        sport=excluded.sport,
                        court_environment=excluded.court_environment,
                        court_size=excluded.court_size,
                        wall_type=excluded.wall_type,
                        is_active=excluded.is_active,
                        source_origin=excluded.source_origin,
                        verified_at=excluded.verified_at
                    """,
                    (
                        c.club_id,
                        c.court_id,
                        c.court_name,
                        c.sport,
                        c.court_environment,
                        c.court_size,
                        c.wall_type,
                        1 if c.is_active else 0,
                        c.source_origin,
                        c.verified_at or ts,
                    ),
                )
                count += 1
        return count

    def upsert_opening_hours(self, records: Iterable[OpeningHoursRecord]) -> int:
        ts = now_dublin_iso()
        count = 0
        with self.connect() as conn:
            for r in records:
                conn.execute(
                    """
                    INSERT INTO club_opening_hours (
                        club_id, day_of_week, opening_time, closing_time,
                        closes_next_day, source_origin, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(club_id, day_of_week) DO UPDATE SET
                        opening_time=excluded.opening_time,
                        closing_time=excluded.closing_time,
                        closes_next_day=excluded.closes_next_day,
                        source_origin=excluded.source_origin,
                        updated_at=excluded.updated_at
                    """,
                    (
                        r.club_id,
                        r.day_of_week,
                        r.opening_time,
                        r.closing_time,
                        1 if r.closes_next_day else 0,
                        r.source_origin,
                        r.updated_at or ts,
                    ),
                )
                count += 1
        return count

    def record_collection_run(self, run: CollectionRunResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_runs (
                    run_id, started_at, completed_at, adapter_name, platform_id,
                    club_id, target_url, http_status, robots_allowed, auth_required,
                    run_status, data_origin, records_written, error_message, raw_artifact_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    completed_at=excluded.completed_at,
                    http_status=excluded.http_status,
                    robots_allowed=excluded.robots_allowed,
                    auth_required=excluded.auth_required,
                    run_status=excluded.run_status,
                    records_written=excluded.records_written,
                    error_message=excluded.error_message,
                    raw_artifact_path=excluded.raw_artifact_path
                """,
                (
                    run.run_id,
                    run.started_at,
                    run.completed_at,
                    run.adapter_name,
                    run.platform_id,
                    run.club_id,
                    run.target_url,
                    run.http_status,
                    None if run.robots_allowed is None else (1 if run.robots_allowed else 0),
                    None if run.auth_required is None else (1 if run.auth_required else 0),
                    run.run_status,
                    run.data_origin,
                    run.records_written,
                    run.error_message,
                    run.raw_artifact_path,
                ),
            )

    def upsert_public_events(self, events: Iterable[PublicClubEvent]) -> int:
        count = 0
        with self.connect() as conn:
            for e in events:
                conn.execute(
                    """
                    INSERT INTO public_club_events (
                        club_id, event_id, event_type, title, start_time,
                        price_eur, source_url, observed_at, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(club_id, event_id) DO UPDATE SET
                        title=excluded.title,
                        start_time=excluded.start_time,
                        price_eur=excluded.price_eur,
                        observed_at=excluded.observed_at,
                        run_id=excluded.run_id
                    """,
                    (
                        e.club_id,
                        e.event_id,
                        e.event_type,
                        e.title,
                        e.start_time,
                        e.price_eur,
                        e.source_url,
                        e.observed_at,
                        e.run_id,
                    ),
                )
                count += 1
        return count

    def upsert_membership_offers(self, offers: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self.connect() as conn:
            for o in offers:
                conn.execute(
                    """
                    INSERT INTO club_membership_offers (
                        club_id, offer_id, offer_kind, title, price_eur,
                        billing_period, court_discount_pct, advance_booking_days,
                        off_peak_unlimited, description, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(club_id, offer_id) DO UPDATE SET
                        offer_kind=excluded.offer_kind,
                        title=excluded.title,
                        price_eur=excluded.price_eur,
                        billing_period=excluded.billing_period,
                        court_discount_pct=excluded.court_discount_pct,
                        advance_booking_days=excluded.advance_booking_days,
                        off_peak_unlimited=excluded.off_peak_unlimited,
                        description=excluded.description,
                        observed_at=excluded.observed_at
                    """,
                    (
                        o["club_id"],
                        o["offer_id"],
                        o.get("offer_kind", "membership"),
                        o["title"],
                        o.get("price_eur"),
                        o.get("billing_period"),
                        o.get("court_discount_pct"),
                        o.get("advance_booking_days"),
                        1 if o.get("off_peak_unlimited") else 0,
                        o.get("description"),
                        o["observed_at"],
                    ),
                )
                count += 1
        return count

    def upsert_non_api_measurements(self, measurements: Iterable[dict[str, Any]]) -> int:
        count = 0
        cols = [
            "club_id", "reference_week_start", "verified_padel_courts", "weekly_capacity_court_hours",
            "direct_organised_court_hours", "live_activities_count", "live_registered_players",
            "live_max_player_spots", "live_activity_fill_rate_pct", "player_signup_24h_delta",
            "verified_activity_revenue_eur", "spot_check_samples", "estimated_casual_court_hours_point",
            "total_occupied_court_hours_low", "total_occupied_court_hours_point", "total_occupied_court_hours_high",
            "estimated_occupancy_pct_low", "estimated_occupancy_pct_point", "estimated_occupancy_pct_high",
            "peak_occupancy_pct_point", "offpeak_occupancy_pct_point", "est_weekly_gbv_eur_low",
            "est_weekly_gbv_eur_point", "est_weekly_gbv_eur_high", "measurement_tier",
            "confidence_level", "confidence_score", "methodology_notes", "computed_at"
        ]
        col_names = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        update_clause = ", ".join([f"{c}=excluded.{c}" for c in cols if c != "club_id"])
        sql = f"""
            INSERT INTO non_api_club_measurements ({col_names})
            VALUES ({placeholders})
            ON CONFLICT(club_id) DO UPDATE SET {update_clause}
        """
        with self.connect() as conn:
            for m in measurements:
                vals = [m.get(c) for c in cols]
                conn.execute(sql, vals)
                count += 1
        return count

    def insert_observations(self, observations: Iterable[AvailabilityObservation]) -> tuple[int, int]:
        """Insert normalized observations, deduplicating exact repeats cleanly.

        Returns (inserted_count, skipped_duplicate_count).
        """
        inserted = 0
        duplicates = 0
        with self.connect() as conn:
            for obs in observations:
                # Ensure parent court exists so ad-hoc CSV imports don't violate FK
                conn.execute(
                    """
                    INSERT OR IGNORE INTO courts (
                        club_id, court_id, court_name, sport, court_environment,
                        court_size, source_origin, verified_at
                    ) VALUES (?, ?, ?, 'PADEL', 'unknown', 'unknown', ?, ?)
                    """,
                    (obs.club_id, obs.court_id, obs.court_id, obs.data_origin, obs.observed_at),
                )
                # Ensure run_id exists in collection_runs if passed to avoid FK constraint error
                if obs.run_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO collection_runs (
                            run_id, started_at, completed_at, adapter_name, platform_id,
                            club_id, target_url, http_status, run_status, data_origin, records_written
                        ) VALUES (?, ?, ?, 'stub_ref', ?, ?, '', 200, 'success', ?, 0)
                        """,
                        (obs.run_id, obs.observed_at, obs.observed_at, obs.booking_platform, obs.club_id, obs.data_origin),
                    )
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO availability_observations (
                        club_id, court_id, booking_platform, target_date,
                        start_time, end_time, duration_minutes, price_eur,
                        availability_status, status_detail, observed_at,
                        lead_time_hours, snapshot_bucket, source_url,
                        data_origin, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obs.club_id,
                        obs.court_id,
                        obs.booking_platform,
                        obs.target_date,
                        obs.start_time,
                        obs.end_time,
                        obs.duration_minutes,
                        obs.price_eur,
                        obs.availability_status,
                        obs.status_detail,
                        obs.observed_at,
                        obs.lead_time_hours,
                        obs.snapshot_bucket,
                        obs.source_url,
                        obs.data_origin,
                        obs.run_id,
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    duplicates += 1
        return inserted, duplicates

    def update_court_slot_ledger(
        self,
        observations: Iterable[AvailabilityObservation],
    ) -> dict[str, int]:
        """
        Progressively updates the stateful Master Court Slot Ledger from snapshot observations.
        
        State Machine Logic:
        1. If slot is in future (start_time >= observed_at):
           - If status is 'unavailable'/'blocked':
             - If not already booked: mark 'booked', record first_detected_at = observed_at,
               and calculate true lead_time_hours.
             - If already booked: preserve first_detected_at & lead_time_hours, update last_snapshot_at,
               and increment snapshots_seen_count.
           - If status is 'available':
             - Update last_snapshot_at.
        2. If slot is in past (start_time < observed_at):
           - In Playtomic UI, past slots are disabled/greyed out.
           - PRESERVATION RULE: If the ledger already recorded this slot as 'booked' earlier,
             IT REMAINS 'booked' (confirmed completed rental)!
           - If it was never booked, it remains marked as 'unbooked_passed'.
        """
        from datetime import datetime
        stats = {"inserted_new": 0, "new_bookings": 0, "reconfirmed_bookings": 0, "available_slots": 0}
        now_ts = now_dublin_iso()

        with self.connect() as conn:
            for obs in observations:
                st_hm = obs.start_time[11:16]
                slot_id = f"{obs.club_id}:{obs.court_id}:{obs.target_date}:{st_hm}"

                st_dt = datetime.fromisoformat(obs.start_time)
                obs_dt = datetime.fromisoformat(obs.observed_at)
                is_future = bool(st_dt >= obs_dt)
                is_booked = obs.availability_status in ("unavailable", "blocked", "confirmed_booking")

                lead_h = round(max(0.0, (st_dt - obs_dt).total_seconds() / 3600.0), 2) if is_future else None

                existing = conn.execute(
                    "SELECT booking_status, first_detected_at, lead_time_hours, price_eur, snapshots_seen_count FROM court_slot_ledger WHERE slot_id = ?",
                    (slot_id,),
                ).fetchone()

                p_val = getattr(obs, "price_eur", None)

                is_event = (
                    getattr(obs, "status_detail", "") == "blocked_coaching_or_event"
                    or getattr(obs, "availability_status", "") == "blocked"
                )

                if existing is None:
                    if is_future:
                        status = "booked" if is_booked else "available"
                        first_det = obs.observed_at if is_booked else None
                        lh = (lead_h if (is_booked and not is_event) else None)
                    else:
                        status = "unbooked_passed"
                        first_det = None
                        lh = None

                    conn.execute(
                        """
                        INSERT INTO court_slot_ledger (
                            slot_id, club_id, court_id, target_date, start_time, end_time,
                            slot_time, booking_status, first_detected_at, lead_time_hours,
                            price_eur, last_snapshot_at, snapshots_seen_count, data_origin, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            slot_id,
                            obs.club_id,
                            obs.court_id,
                            obs.target_date,
                            obs.start_time,
                            obs.end_time,
                            st_hm,
                            status,
                            first_det,
                            lh,
                            p_val,
                            obs.observed_at,
                            obs.data_origin,
                            now_ts,
                            now_ts,
                        ),
                    )
                    stats["inserted_new"] += 1
                    if status == "booked":
                        stats["new_bookings"] += 1
                    else:
                        stats["available_slots"] += 1

                else:
                    cur_status = existing["booking_status"]
                    seen_cnt = (existing["snapshots_seen_count"] or 1) + 1

                    if is_future:
                        if is_booked:
                            if cur_status != "booked":
                                assigned_lh = (lead_h if not is_event else None)
                                conn.execute(
                                    """
                                    UPDATE court_slot_ledger SET
                                        booking_status = 'booked',
                                        first_detected_at = ?,
                                        lead_time_hours = ?,
                                        price_eur = COALESCE(?, price_eur),
                                        last_snapshot_at = ?,
                                        snapshots_seen_count = ?,
                                        updated_at = ?
                                    WHERE slot_id = ?
                                    """,
                                    (obs.observed_at, assigned_lh, p_val, obs.observed_at, seen_cnt, now_ts, slot_id),
                                )
                                stats["new_bookings"] += 1
                            else:
                                conn.execute(
                                    """
                                    UPDATE court_slot_ledger SET
                                        price_eur = COALESCE(?, price_eur),
                                        last_snapshot_at = ?,
                                        snapshots_seen_count = ?,
                                        updated_at = ?
                                    WHERE slot_id = ?
                                    """,
                                    (p_val, obs.observed_at, seen_cnt, now_ts, slot_id),
                                )
                                stats["reconfirmed_bookings"] += 1
                        else:
                            if cur_status == "available":
                                conn.execute(
                                    """
                                    UPDATE court_slot_ledger SET
                                        price_eur = COALESCE(?, price_eur),
                                        last_snapshot_at = ?,
                                        snapshots_seen_count = ?,
                                        updated_at = ?
                                    WHERE slot_id = ?
                                    """,
                                    (p_val, obs.observed_at, seen_cnt, now_ts, slot_id),
                                )
                                stats["available_slots"] += 1
                    else:
                        if cur_status == "booked":
                            stats["reconfirmed_bookings"] += 1
                        elif cur_status == "available":
                            conn.execute(
                                "UPDATE court_slot_ledger SET booking_status = 'unbooked_passed', updated_at = ? WHERE slot_id = ?",
                                (now_ts, slot_id),
                            )
        return stats

    def clear_test_rental_data(self) -> dict[str, int]:
        """Completely purges all test court availability observations, ledger, runs, and telemetry."""
        purged = {}
        with self.connect() as conn:
            purged["availability_observations"] = conn.execute("DELETE FROM availability_observations").rowcount
            purged["court_slot_ledger"] = conn.execute("DELETE FROM court_slot_ledger").rowcount
            purged["public_club_events"] = conn.execute("DELETE FROM public_club_events").rowcount
            purged["activity_telemetry_snapshots"] = conn.execute("DELETE FROM activity_telemetry_snapshots").rowcount
            purged["non_api_club_measurements"] = conn.execute("DELETE FROM non_api_club_measurements").rowcount
            purged["field_spot_checks"] = conn.execute("DELETE FROM field_spot_checks").rowcount
            purged["collection_runs"] = conn.execute("DELETE FROM collection_runs").rowcount
        return purged

    def fetch_observations(
        self,
        *,
        data_origin: Optional[str] = None,
        club_id: Optional[str] = None,
    ) -> list[AvailabilityObservation]:
        clauses = []
        params: list[object] = []
        if data_origin:
            clauses.append("data_origin = ?")
            params.append(data_origin)
        if club_id:
            clauses.append("club_id = ?")
            params.append(club_id)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT club_id, court_id, booking_platform, target_date,
                       start_time, end_time, duration_minutes, price_eur,
                       availability_status, status_detail, observed_at,
                       lead_time_hours, snapshot_bucket, source_url,
                       data_origin, run_id
                FROM availability_observations
                {where_sql}
                ORDER BY target_date, club_id, court_id, start_time, observed_at
                """,
                params,
            ).fetchall()
        return [AvailabilityObservation(**dict(r)) for r in rows]

    def insert_activity_telemetry(self, records: Iterable[dict]) -> tuple[int, int]:
        """Insert live API-free activity & player fill-rate observations idempotently."""
        inserted = 0
        duplicates = 0
        with self.connect() as conn:
            for r in records:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO activity_telemetry_snapshots (
                        club_id, activity_id, activity_category, title,
                        start_time, end_time, duration_minutes, course_span_text,
                        skill_band, coach_name, registered_players, max_players,
                        fill_rate_pct, courts_occupied, court_hours_occupied,
                        price_per_player_eur, observed_gross_revenue_eur,
                        full_capacity_revenue_eur, observed_at, source_url, run_id
                    ) VALUES (
                        :club_id, :activity_id, :activity_category, :title,
                        :start_time, :end_time, :duration_minutes, :course_span_text,
                        :skill_band, :coach_name, :registered_players, :max_players,
                        :fill_rate_pct, :courts_occupied, :court_hours_occupied,
                        :price_per_player_eur, :observed_gross_revenue_eur,
                        :full_capacity_revenue_eur, :observed_at, :source_url, :run_id
                    )
                    """,
                    r,
                )
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    duplicates += 1
        return inserted, duplicates

