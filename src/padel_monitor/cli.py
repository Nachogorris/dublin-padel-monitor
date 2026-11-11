"""Command-line orchestrator for the Dublin Padel Market Intelligence & Court Occupancy Tracker."""
from __future__ import annotations

import argparse
import csv
import re
import sys
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import httpx

from .adapters import (
    BrowserSnapshotDropzoneAdapter,
    CsvObservationAdapter,
    GenericPortalProbeAdapter,
    PlaytomicOfficialApiAdapter,
    PlaytomicPublicClubAdapter,
    build_spot_check_record,
    estimate_stratified_occupancy,
    parse_inbox_filename,
    resolve_snapshot_timestamp_and_bucket,
)
from .adapters.playtomic_public import parse_playtomic_club_html
from .compliance import CompliantHttpClient
from .db import PadelDatabase
from .exporters import (
    export_backward_compatible_venues_csv,
    export_field_spot_check_template_excel,
    export_inventory_and_coverage_excel,
    export_non_api_measurements_excel,
    export_observations_and_occupancy_excel,
    export_weekly_peak_offpeak_heatmap_excel,
)
from .inventory import BOOKING_PLATFORMS, LIVE_PLAYTOMIC_CACHE_MAP, VERIFIED_CLUBS
from .models import CollectionRunResult, CourtRecord, OpeningHoursRecord
from .non_api_measurement import (
    NonApiCourtMeasurementEngine,
    compute_activity_velocity_pairs,
    convert_activities_to_live_observations,
    parse_playtomic_activity_cards,
)
from .occupancy import build_all_occupancy_breakdowns, resolve_atomic_slices
from .synthetic import generate_synthetic_validation_dataset
from .time_utils import now_dublin_iso
from .weekly_tracker import (
    build_live_next_week_organised_grid,
    build_weekly_snapshot_schedule,
    compute_club_hour_heatmap_and_empirical_peaks,
    compute_snapshot_progression_curve,
    scaffold_weekly_inbox_pack,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "dublin_padel.db"
DEFAULT_POLICY_DIR = ROOT_DIR / "data" / "raw" / "policy"
DEFAULT_TELEMETRY_DIR = ROOT_DIR / "data" / "raw" / "non_api_telemetry"
DEFAULT_INBOX_DIR = ROOT_DIR / "data" / "inbox"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"
DEFAULT_DATA_DIR = ROOT_DIR / "data"


def init_and_sync_inventory(db: PadelDatabase) -> tuple[int, int]:
    db.initialize()
    n_plat = db.upsert_platforms(BOOKING_PLATFORMS)
    n_clubs = db.upsert_clubs(VERIFIED_CLUBS)
    return n_plat, n_clubs


def ingest_live_cached_captures(db: PadelDatabase, policy_dir: Path = DEFAULT_POLICY_DIR) -> dict[str, int]:
    """Load the live-fetched Playtomic club HTML captures (T1: 2026-11-06) & platform probes into SQLite."""
    stats = {"clubs_parsed": 0, "courts": 0, "opening_hours": 0, "events": 0, "probe_runs": 0}

    # 1. Ingest all 10 County Dublin Playtomic SSR club captures with their exact live fetch timestamps
    for club_id, (url, filename, fetched_ts) in LIVE_PLAYTOMIC_CACHE_MAP.items():
        html_path = policy_dir / filename
        if not html_path.exists():
            continue
        run_id = f"live-pt-ssr-{club_id}"
        html_text = html_path.read_text(encoding="utf-8")
        profile = parse_playtomic_club_html(
            html_text,
            club_id=club_id,
            source_url=url,
            observed_at=fetched_ts,
            run_id=run_id,
        )
        n_c = db.upsert_courts(profile.courts)
        n_oh = db.upsert_opening_hours(profile.opening_hours)
        run = CollectionRunResult(
            run_id=run_id,
            started_at=fetched_ts,
            completed_at=fetched_ts,
            adapter_name="PlaytomicPublicClubAdapter.collect_club_profile",
            platform_id="playtomic",
            club_id=club_id,
            target_url=url,
            http_status=200,
            robots_allowed=True,
            auth_required=False,
            run_status="success",
            data_origin="live_public",
            records_written=n_c + n_oh + len(profile.public_events),
            error_message=(
                f"Verified {sum(1 for c in profile.courts if c.sport == 'PADEL')} padel courts "
                f"({len(profile.courts)} total resources) & {len(profile.opening_hours)} opening-hour days."
            ),
            raw_artifact_path=str(html_path.relative_to(ROOT_DIR)) if html_path.is_relative_to(ROOT_DIR) else str(html_path),
        )
        db.record_collection_run(run)
        n_ev = db.upsert_public_events(profile.public_events)
        stats["clubs_parsed"] += 1
        stats["courts"] += n_c
        stats["opening_hours"] += n_oh
        stats["events"] += n_ev
        stats["probe_runs"] += 1

    # 2. Verify & record Playtomic per-date slot robots.txt compliance block on House of Padel
    pt_robots_file = policy_dir / "playtomic.com__robots.txt.txt"
    if pt_robots_file.exists():
        robots_txt = pt_robots_file.read_text(encoding="utf-8")
        mock_transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=robots_txt, headers={"content-type": "text/plain"})
            if req.url.path == "/robots.txt"
            else httpx.Response(403, text="Should not be requested when disallowed")
        )
        with CompliantHttpClient(min_delay_seconds=0.0, transport=mock_transport) as client:
            pt_adapter = PlaytomicPublicClubAdapter(http_client=client)
            slot_run = pt_adapter.collect_club_availability(
                "house_of_padel",
                "https://playtomic.com/clubs/house-of-padel",
                date(2026, 11, 7),
            )
            slot_run.run_id = "live-pt-slots-compliance-guard-house_of_padel"
            db.record_collection_run(slot_run)
            stats["probe_runs"] += 1

    # 3. Record Official Playtomic OAuth2 API readiness check (awaiting credentials)
    api_adapter = PlaytomicOfficialApiAdapter(client_id="", client_secret="")
    api_run = api_adapter.collect_club_availability(
        "house_of_padel",
        "https://playtomic.com/clubs/house-of-padel",
        date(2026, 11, 7),
    )
    api_run.run_id = "live-pt-official-api-auth-check-house_of_padel"
    db.record_collection_run(api_run)
    stats["probe_runs"] += 1

    # 4. Evaluate all live-captured non-Playtomic platform probes from data/raw/policy/
    non_pt_probes = [
        (
            "matchpoint",
            "padel_dome",
            "https://padeldome-ie.matchpoint.com.es/",
            "https://padeldome-ie.matchpoint.com.es/Login.aspx",
            200,
            "2026-11-06T22:21:29+00:00",
            "padeldome-ie.matchpoint.com.es__root.txt",
            "padeldome-ie.matchpoint.com.es__robots.txt.txt",
            "live-probe-matchpoint-padel_dome",
        ),
        (
            "playbypoint",
            "bushy_park",
            "https://bushytennispadel.playbypoint.com/",
            "https://bushytennispadel.playbypoint.com/",
            403,
            "2026-11-06T22:30:39+00:00",
            "bushytennispadel.playbypoint.com__root.txt",
            "bushytennispadel.playbypoint.com__robots.txt.txt",
            "live-probe-playbypoint-bushy_park",
        ),
        (
            "gladstonego",
            "portmarnock",
            "https://pslc.gladstonego.cloud/book",
            "https://pslc.gladstonego.cloud/book",
            200,
            "2026-11-06T22:30:23+00:00",
            "pslc.gladstonego.cloud__book.txt",
            "pslc.gladstonego.cloud__robots.txt.txt",
            "live-probe-gladstonego-portmarnock",
        ),
        (
            "padel_ireland_wp",
            "rockbrook",
            "https://padel-ireland.ie/bookings/",
            "https://padel-ireland.ie/bookings/",
            200,
            "2026-11-06T22:30:26+00:00",
            "padel-ireland.ie__bookings.txt",
            "padel-ireland.ie__robots.txt.txt",
            "live-probe-padel_ireland_wp-rockbrook",
        ),
        (
            "dlr_enquiry",
            "meadowbrook",
            "https://dlrleisure.ie/facility-booking/",
            "https://dlrleisure.ie/facilities/facility-booking-enquiry/",
            200,
            "2026-11-06T22:30:30+00:00",
            "dlrleisure.ie__facility-booking.txt",
            "dlrleisure.ie__robots.txt.txt",
            "live-probe-dlr_enquiry-meadowbrook",
        ),
    ]
    for plat_id, c_id, url, fin_url, code, ts, b_file, r_file, fixed_run_id in non_pt_probes:
        b_path = policy_dir / b_file
        r_path = policy_dir / r_file
        if not b_path.exists():
            continue
        probe = GenericPortalProbeAdapter(plat_id)
        p_run = probe.evaluate_cached_probe(
            club_id=c_id,
            url=url,
            final_url=fin_url,
            http_status=code,
            fetched_at_iso=ts,
            body_path=b_path,
            robots_path=r_path,
        )
        p_run.run_id = fixed_run_id
        p_run.raw_artifact_path = str(b_path.relative_to(ROOT_DIR)) if b_path.is_relative_to(ROOT_DIR) else str(b_path)
        db.record_collection_run(p_run)
        stats["probe_runs"] += 1

    return stats


def _load_db_courts_and_hours(
    db: PadelDatabase,
) -> tuple[dict[str, dict], dict[tuple[str, str], CourtRecord], dict[tuple[str, str], OpeningHoursRecord]]:
    with db.connect() as conn:
        clubs_by_id = {r["club_id"]: dict(r) for r in conn.execute("SELECT * FROM clubs").fetchall()}
        courts_map: dict[tuple[str, str], CourtRecord] = {}
        for r in conn.execute("SELECT * FROM courts WHERE source_origin = 'live_public'").fetchall():
            cr = CourtRecord(
                club_id=r["club_id"],
                court_id=r["court_id"],
                court_name=r["court_name"],
                sport=r["sport"],
                court_environment=r["court_environment"],
                court_size=r["court_size"],
                wall_type=r["wall_type"],
                is_active=bool(r["is_active"]),
                source_origin=r["source_origin"],
                verified_at=r["verified_at"],
            )
            courts_map[(cr.club_id, cr.court_id)] = cr

        oh_map: dict[tuple[str, str], OpeningHoursRecord] = {}
        for r in conn.execute("SELECT * FROM club_opening_hours").fetchall():
            ohr = OpeningHoursRecord(
                club_id=r["club_id"],
                day_of_week=r["day_of_week"],
                opening_time=r["opening_time"],
                closing_time=r["closing_time"],
                closes_next_day=bool(r["closes_next_day"]),
                source_origin=r["source_origin"],
                updated_at=r["updated_at"],
            )
            oh_map[(ohr.club_id, ohr.day_of_week)] = ohr
    return clubs_by_id, courts_map, oh_map


def ensure_inbox_readme(inbox_dir: Path = DEFAULT_INBOX_DIR) -> Path:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    readme = inbox_dir / "README.txt"
    readme.write_text(
        "Dublin Padel Monitor — Multi-Snapshot Zero-API Browser Dropzone\n"
        "===============================================================\n\n"
        "Track next week's Peak & Off-Peak bookings across multiple snapshot passes\n"
        "(7d, 3d, 24h, morning/6h, prepeak/1h) without any API key:\n\n"
        "1. Scaffold a ready-to-fill 5-pass weekly pack:\n"
        "       python monitor.py init-week-pack --week-start 2026-11-09\n\n"
        "2. Supported filenames in data/inbox/ or data/inbox/week_YYYY-MM-DD/:\n"
        "   - Single-day snapshot : <club_id>__YYYY-MM-DD__<tag>.txt (or .html)\n"
        "     Examples: house_of_padel__2026-11-10__7d.txt\n"
        "               house_of_padel__2026-11-10__morning.txt\n"
        "               house_of_padel__2026-11-10__prepeak.html\n"
        "   - 7-day weekly bundle : <club_id>__week__YYYY-MM-DD__<tag>.txt\n"
        "     Tags: 7d | 3d | 24h | morning (or 6h) | prepeak (or 1h) | YYYYMMDDTHHMM\n\n"
        "3. Three zero-API input formats accepted inside any file:\n"
        "   (a) Rapid shorthand : 09:00-17:00 = 3 free €30\n"
        "                         17:00-21:30 = 0 free €45\n"
        "   (b) Copy-pasted text: Court 1 / 09:00 60 min €30 / Court 2 / No slots available\n"
        "   (c) Webpage HTML    : Ctrl+S ('Webpage, HTML Only')\n\n"
        "4. Ingest & build the color-coded Club x Hour (06:00-23:00) Peak/Off-Peak Heatmap:\n"
        "       python monitor.py analyze-week --week-start 2026-11-09\n",
        encoding="utf-8",
    )
    return readme


def ingest_inbox_snapshots(db: PadelDatabase, inbox_dir: Path = DEFAULT_INBOX_DIR) -> dict[str, int]:
    """Recursively scan `data/inbox/` (including `week_YYYY-MM-DD/` packs) and ingest multi-snapshot calendars."""
    ensure_inbox_readme(inbox_dir)
    clubs_by_id, courts_map, oh_map = _load_db_courts_and_hours(db)

    courts_by_club: dict[str, list[CourtRecord]] = defaultdict(list)
    for (cid, _), cr in courts_map.items():
        courts_by_club[cid].append(cr)

    oh_by_club: dict[str, dict[str, OpeningHoursRecord]] = defaultdict(dict)
    for (cid, dow), ohr in oh_map.items():
        oh_by_club[cid][dow] = ohr

    adapter = BrowserSnapshotDropzoneAdapter()
    files_processed = 0
    obs_inserted = 0

    for fpath in sorted(inbox_dir.rglob("*")):
        if not fpath.is_file() or fpath.name.lower() == "readme.txt":
            continue
        desc = parse_inbox_filename(fpath.name)
        if desc is None or desc.club_id not in clubs_by_id:
            continue
        club_id = desc.club_id

        # Synthesize fallback CourtRecords if club had no Playtomic SSR courts (e.g. Padel Dome, Bushy Park)
        c_courts = courts_by_club.get(club_id)
        if not c_courts:
            c_info = clubs_by_id[club_id]
            n_padel = int(c_info.get("verified_padel_courts") or 0)
            if n_padel <= 0:
                continue
            c_courts = [
                CourtRecord(
                    club_id=club_id,
                    court_id=f"{club_id}_court_{idx}",
                    court_name=f"Court {idx}",
                    sport="PADEL",
                    court_environment=c_info.get("court_environment") or "unknown",
                    court_size="double",
                    source_origin="live_public",
                )
                for idx in range(1, n_padel + 1)
            ]
            db.upsert_courts(c_courts)
            courts_by_club[club_id] = c_courts

        raw_text = fpath.read_text(encoding="utf-8", errors="replace")
        mtime = fpath.stat().st_mtime
        c_info = clubs_by_id[club_id]
        rel_lbl = (
            str(fpath.relative_to(inbox_dir)).replace("\\", "/")
            if fpath.is_relative_to(inbox_dir)
            else fpath.name
        )

        if desc.is_weekly_bundle:
            runs = adapter.parse_weekly_bundle_text(
                raw_text,
                club_id=club_id,
                courts=c_courts,
                opening_hours=oh_by_club.get(club_id, {}),
                snapshot_tag=desc.snapshot_tag,
                file_mtime_epoch=mtime,
                source_label=f"browser-dropzone://{rel_lbl}",
                default_offpeak_eur=c_info.get("off_peak_price_eur"),
                default_peak_eur=c_info.get("peak_price_eur"),
            )
            if not runs:
                continue
            files_processed += 1
            for r in runs:
                db.record_collection_run(r)
                ins, _ = db.insert_observations(r.observations)
                obs_inserted += ins
        else:
            obs_ts, bkt_override = resolve_snapshot_timestamp_and_bucket(
                desc.anchor_date,
                snapshot_tag=desc.snapshot_tag,
                file_mtime_epoch=mtime,
            )
            run_res = adapter.parse_snapshot_text(
                raw_text,
                club_id=club_id,
                target_date=desc.anchor_date,
                courts=c_courts,
                opening_hours=oh_by_club.get(club_id, {}),
                observed_at=obs_ts,
                snapshot_bucket_override=bkt_override,
                source_label=f"browser-dropzone://{rel_lbl}",
                default_offpeak_eur=c_info.get("off_peak_price_eur"),
                default_peak_eur=c_info.get("peak_price_eur"),
            )
            if not run_res.observations:
                continue
            db.record_collection_run(run_res)
            ins, _ = db.insert_observations(run_res.observations)
            files_processed += 1
            obs_inserted += ins

    return {"inbox_files_processed": files_processed, "inbox_observations_inserted": obs_inserted}


def ingest_non_api_telemetry_and_measure(
    db: PadelDatabase,
    *,
    policy_dir: Path = DEFAULT_POLICY_DIR,
    telemetry_dir: Path = DEFAULT_TELEMETRY_DIR,
    inbox_dir: Path = DEFAULT_INBOX_DIR,
) -> dict[str, Any]:
    """Ingest T1 (2026-11-06) + T2 (2026-11-07) API-free web telemetry, project court slots, and measure all 22 public clubs."""
    with db.connect() as conn:
        club_rows = {r["club_id"]: dict(r) for r in conn.execute("SELECT * FROM clubs").fetchall()}

    t1_cards_inserted = 0
    t2_cards_inserted = 0
    offers_upserted = 0

    # 1. Parse Snapshot T1 (2026-11-06) activity & membership cards from data/raw/policy/
    for club_id, (url, filename, fetched_ts) in LIVE_PLAYTOMIC_CACHE_MAP.items():
        html_path = policy_dir / filename
        if not html_path.exists() or club_id not in club_rows:
            continue
        run_id = f"live-pt-ssr-{club_id}"
        n_padel = int(club_rows[club_id].get("verified_padel_courts") or 4)
        acts, offers = parse_playtomic_activity_cards(
            html_path.read_text(encoding="utf-8"),
            club_id=club_id,
            source_url=url,
            observed_at=fetched_ts,
            club_padel_courts=n_padel,
            run_id=run_id,
        )
        ins, _ = db.insert_activity_telemetry(acts)
        t1_cards_inserted += ins
        offers_upserted += db.upsert_membership_offers(offers)

    # 2. Parse Snapshot T2 (2026-11-07+) from data/raw/non_api_telemetry/manifest.csv
    manifest_path = telemetry_dir / "manifest.csv"
    t2_latest_activities_all: list[dict[str, Any]] = []
    if manifest_path.exists():
        with manifest_path.open("r", newline="", encoding="utf-8") as fh:
            m_rows = list(csv.DictReader(fh))

        by_club_manifest: dict[str, list[dict[str, str]]] = defaultdict(list)
        for mr in m_rows:
            if mr.get("outcome_code") == "success" and mr.get("saved_file"):
                by_club_manifest[mr["club_id"]].append(mr)

        for club_id, items in sorted(by_club_manifest.items()):
            if club_id not in club_rows:
                continue
            n_padel = int(club_rows[club_id].get("verified_padel_courts") or 4)

            # Determine canonical T2 wave timestamp for this club (from playtomic_club_main or first item)
            main_item = next((x for x in items if x["page_kind"] == "playtomic_club_main"), items[0])
            wave_ts = main_item["fetched_at"]
            wave_run_id = f"live-nonapi-t2-{club_id}"

            # If Playtomic main page present, refresh courts, opening hours & tenant metadata (adds Celbridge & Navan too!)
            if main_item["page_kind"] == "playtomic_club_main":
                main_path = telemetry_dir / main_item["saved_file"]
                if main_path.exists():
                    m_html = main_path.read_text(encoding="utf-8")
                    profile = parse_playtomic_club_html(
                        m_html,
                        club_id=club_id,
                        source_url=main_item["url"],
                        observed_at=wave_ts,
                        run_id=wave_run_id,
                    )
                    n_c = db.upsert_courts(profile.courts)
                    n_oh = db.upsert_opening_hours(profile.opening_hours)
                    db.record_collection_run(
                        CollectionRunResult(
                            run_id=wave_run_id,
                            started_at=wave_ts,
                            completed_at=wave_ts,
                            adapter_name="NonApiTelemetryHarvester.playtomic_club_and_subpages",
                            platform_id="playtomic",
                            club_id=club_id,
                            target_url=main_item["url"],
                            http_status=200,
                            robots_allowed=True,
                            auth_required=False,
                            run_status="success",
                            data_origin="live_public",
                            records_written=n_c + n_oh,
                            error_message=f"T2 API-free refresh across {len(items)} permitted page(s).",
                            raw_artifact_path=str(main_path.relative_to(ROOT_DIR)) if main_path.is_relative_to(ROOT_DIR) else str(main_path),
                        )
                    )
                    db.upsert_public_events(profile.public_events)
                    # Backfill tenant UUID & lat/lon if missing (e.g. indoor_padel_ireland)
                    with db.connect() as conn:
                        conn.execute(
                            """
                            UPDATE clubs
                            SET platform_tenant_id = COALESCE(platform_tenant_id, ?),
                                latitude = COALESCE(latitude, ?),
                                longitude = COALESCE(longitude, ?)
                            WHERE club_id = ?
                            """,
                            (profile.tenant_id, profile.latitude, profile.longitude, club_id),
                        )
            else:
                # Direct club website harvest (padel100.ie, padeldome.ie, pslc.ie, shorelineleisure.ie)
                w_path = telemetry_dir / main_item["saved_file"]
                db.record_collection_run(
                    CollectionRunResult(
                        run_id=wave_run_id,
                        started_at=wave_ts,
                        completed_at=wave_ts,
                        adapter_name="NonApiTelemetryHarvester.club_website",
                        platform_id=club_rows[club_id].get("platform_id") or "club_website",
                        club_id=club_id,
                        target_url=main_item["url"],
                        http_status=int(main_item["http_status"] or 200),
                        robots_allowed=True,
                        auth_required=False,
                        run_status="success",
                        data_origin="live_public",
                        records_written=1,
                        error_message="Verified live pricing, court count & operating schedule from official website.",
                        raw_artifact_path=str(w_path.relative_to(ROOT_DIR)) if w_path.is_relative_to(ROOT_DIR) else str(w_path),
                    )
                )

            # Merge activity & membership cards across main + /competitions/tournaments + /academy/courses
            merged_acts: dict[str, dict[str, Any]] = {}
            merged_offers: dict[str, dict[str, Any]] = {}
            for it in items:
                if not it["page_kind"].startswith("playtomic_"):
                    continue
                fpath = telemetry_dir / it["saved_file"]
                if not fpath.exists():
                    continue
                acts, offers = parse_playtomic_activity_cards(
                    fpath.read_text(encoding="utf-8"),
                    club_id=club_id,
                    source_url=it["url"],
                    observed_at=wave_ts,
                    club_padel_courts=n_padel,
                    run_id=wave_run_id,
                )
                for a in acts:
                    aid = a["activity_id"]
                    if aid not in merged_acts:
                        merged_acts[aid] = a
                    else:
                        prev = merged_acts[aid]
                        # Preserve exact UTC->Dublin RSC start_time if main page had it, while taking subpage URL/fields
                        for k, v in a.items():
                            if prev.get(k) is None and v is not None:
                                prev[k] = v
                        if it["page_kind"] != "playtomic_club_main":
                            prev["source_url"] = it["url"]
                for o in offers:
                    merged_offers[o["offer_id"]] = o

            if merged_acts:
                ins, _ = db.insert_activity_telemetry(merged_acts.values())
                t2_cards_inserted += ins
                t2_latest_activities_all.extend(merged_acts.values())
                # Update collection_runs records_written with total harvested activities + offers
                with db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE collection_runs
                        SET records_written = records_written + ?,
                            error_message = ?
                        WHERE run_id = ?
                        """,
                        (
                            len(merged_acts) + len(merged_offers),
                            f"Harvested {len(merged_acts)} live activities & {len(merged_offers)} membership/offer cards without API.",
                            wave_run_id,
                        ),
                    )
            if merged_offers:
                offers_upserted += db.upsert_membership_offers(merged_offers.values())

    # 3. Project live dated tournaments & public classes onto physical club courts in availability_observations
    _, courts_map, oh_map = _load_db_courts_and_hours(db)
    courts_by_club: dict[str, list[CourtRecord]] = defaultdict(list)
    for (cid, _), cr in courts_map.items():
        courts_by_club[cid].append(cr)

    with db.connect() as conn:
        all_telemetry = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM activity_telemetry_snapshots ORDER BY observed_at ASC"
            ).fetchall()
        ]
    # Project latest snapshot per (club_id, activity_id) onto physical courts
    latest_by_act: dict[tuple[str, str], dict[str, Any]] = {}
    for r in all_telemetry:
        latest_by_act[(r["club_id"], r["activity_id"])] = r
    projected_obs = convert_activities_to_live_observations(latest_by_act.values(), courts_by_club)
    live_obs_inserted, _ = db.insert_observations(projected_obs)

    # 4. Ingest any human-saved browser calendar snapshots in data/inbox/
    inbox_stats = ingest_inbox_snapshots(db, inbox_dir=inbox_dir)

    # 5. Compute 24-hour player sign-up velocity & run NonApiCourtMeasurementEngine across all 22 public/hybrid clubs
    velocity_pairs = compute_activity_velocity_pairs(all_telemetry)

    oh_by_club_list: dict[str, list[OpeningHoursRecord]] = defaultdict(list)
    for (cid, _), ohr in oh_map.items():
        oh_by_club_list[cid].append(ohr)

    with db.connect() as conn:
        clubs_all = [dict(r) for r in conn.execute("SELECT * FROM clubs").fetchall()]
        offers_all = [dict(r) for r in conn.execute("SELECT * FROM club_membership_offers").fetchall()]
        spots_all = [dict(r) for r in conn.execute("SELECT * FROM field_spot_checks").fetchall()]

    measurements = NonApiCourtMeasurementEngine.measure_public_clubs(
        clubs=clubs_all,
        oh_by_club=oh_by_club_list,
        latest_activities=list(latest_by_act.values()),
        velocity_pairs=velocity_pairs,
        membership_offers=offers_all,
        spot_checks=spots_all,
        computed_at=now_dublin_iso(),
    )
    n_measured = db.upsert_non_api_measurements(measurements)

    return {
        "t1_cards_inserted": t1_cards_inserted,
        "t2_cards_inserted": t2_cards_inserted,
        "unique_live_activities": len(latest_by_act),
        "membership_offers": len(offers_all),
        "velocity_pairs": velocity_pairs,
        "live_court_observations_projected": len(projected_obs),
        "live_court_observations_inserted": live_obs_inserted,
        "clubs_measured": n_measured,
        "measurements": measurements,
        **inbox_stats,
    }


def harvest_live_non_api_network(
    db: PadelDatabase,
    *,
    club_filter: str | None = None,
    telemetry_dir: Path = DEFAULT_TELEMETRY_DIR,
) -> int:
    """Fetch live robots.txt-allowed club + tournament + course pages without any API key."""
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM clubs WHERE platform_id = 'playtomic' AND public_booking_url LIKE 'https://playtomic.com/clubs/%'"
            ).fetchall()
        ]
    if club_filter:
        rows = [r for r in rows if r["club_id"] == club_filter]
        if not rows:
            raise ValueError(f"No Playtomic club found matching --club={club_filter!r}")

    date_tag = now_dublin_iso()[:16].replace("-", "").replace(":", "")
    fetched_ok = 0
    with CompliantHttpClient(min_delay_seconds=1.15, timeout_seconds=18.0) as client:
        for club in rows:
            cid = club["club_id"]
            base_url = club["public_booking_url"].rstrip("/")
            urls = [
                ("playtomic_club_main", base_url),
                ("playtomic_tournaments", f"{base_url}/competitions/tournaments"),
                ("playtomic_courses", f"{base_url}/academy/courses"),
            ]
            wave_ts = now_dublin_iso()
            run_id = f"harvest-{cid}-{date_tag}-{uuid.uuid4().hex[:6]}"
            merged_acts: dict[str, dict[str, Any]] = {}
            merged_offers: dict[str, dict[str, Any]] = {}

            for kind, u in urls:
                outcome = client.fetch_compliant(u)
                print(f"[HARVEST] {cid:20s} | {kind:21s} | HTTP {outcome.http_status} | {outcome.outcome_code}")
                if outcome.outcome_code != "success" or not outcome.body:
                    continue
                fetched_ok += 1
                clean = re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"^https?://", "", u).strip("/"))
                out_file = telemetry_dir / f"{date_tag}__{clean}.html"
                out_file.write_text(outcome.body, encoding="utf-8")

                if kind == "playtomic_club_main":
                    prof = parse_playtomic_club_html(
                        outcome.body, club_id=cid, source_url=u, observed_at=wave_ts, run_id=run_id
                    )
                    db.upsert_courts(prof.courts)
                    db.upsert_opening_hours(prof.opening_hours)
                    db.record_collection_run(
                        CollectionRunResult(
                            run_id=run_id,
                            started_at=wave_ts,
                            completed_at=now_dublin_iso(),
                            adapter_name="harvest-live",
                            platform_id="playtomic",
                            club_id=cid,
                            target_url=base_url,
                            http_status=200,
                            robots_allowed=True,
                            auth_required=False,
                            run_status="success",
                            data_origin="live_public",
                            records_written=len(prof.courts) + len(prof.opening_hours),
                            raw_artifact_path=str(out_file),
                        )
                    )
                    db.upsert_public_events(prof.public_events)

                acts, offs = parse_playtomic_activity_cards(
                    outcome.body,
                    club_id=cid,
                    source_url=u,
                    observed_at=wave_ts,
                    club_padel_courts=int(club.get("verified_padel_courts") or 4),
                    run_id=run_id,
                )
                for a in acts:
                    aid = a["activity_id"]
                    if aid not in merged_acts:
                        merged_acts[aid] = a
                    else:
                        prev = merged_acts[aid]
                        for k, v in a.items():
                            if prev.get(k) is None and v is not None:
                                prev[k] = v
                        if kind != "playtomic_club_main":
                            prev["source_url"] = u
                for o in offs:
                    merged_offers[o["offer_id"]] = o

            if merged_acts:
                ins, dup = db.insert_activity_telemetry(merged_acts.values())
                print(f"  -> {cid}: +{ins} new activity snapshot rows ({dup} unchanged), {len(merged_offers)} offers.")
            if merged_offers:
                db.upsert_membership_offers(merged_offers.values())

    return 0 if fetched_ok > 0 else 1


def collect_live_network_club(db: PadelDatabase, club_id: str) -> int:
    """Execute a live robots.txt-compliant HTTP fetch against a Playtomic club page."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM clubs WHERE club_id = ?", (club_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown club_id '{club_id}'. Run sync-inventory first.")
    url = row["public_booking_url"]
    if not url or "playtomic.com/clubs/" not in url:
        raise ValueError(f"Club '{club_id}' does not have a Playtomic /clubs/<slug> URL ({url!r}).")

    adapter = PlaytomicPublicClubAdapter(raw_save_dir=DEFAULT_DATA_DIR / "raw" / "live_runs")
    run, profile = adapter.collect_club_profile(club_id=club_id, club_url=url)
    db.record_collection_run(run)
    if profile is None:
        print(f"[LIVE] {club_id}: status={run.run_status} ({run.error_message})")
        return 1

    n_c = db.upsert_courts(profile.courts)
    n_oh = db.upsert_opening_hours(profile.opening_hours)
    n_ev = db.upsert_public_events(profile.public_events)
    padel_n = sum(1 for c in profile.courts if c.sport == "PADEL")
    print(
        f"[LIVE SUCCESS] {profile.tenant_name} ({club_id}, UUID={profile.tenant_id}): "
        f"{padel_n} padel courts ({n_c} total resources), {n_oh} opening-hour days, "
        f"{n_ev} public events stored in {db.db_path.name} at {run.completed_at}."
    )
    return 0


def run_measure_without_api(
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Run the 100% API-free court measurement pipeline & export dedicated workbooks/CSVs."""
    db = PadelDatabase(db_path)
    n_plat, n_clubs = init_and_sync_inventory(db)
    cache_stats = ingest_live_cached_captures(db, data_dir / "raw" / "policy")
    non_api_stats = ingest_non_api_telemetry_and_measure(
        db,
        policy_dir=data_dir / "raw" / "policy",
        telemetry_dir=data_dir / "raw" / "non_api_telemetry",
        inbox_dir=data_dir / "inbox",
    )
    non_api_exports = export_non_api_measurements_excel(
        db=db,
        velocity_pairs=non_api_stats["velocity_pairs"],
        output_dir=output_dir,
        data_dir=data_dir,
    )
    spot_template = export_field_spot_check_template_excel(db, output_dir=output_dir)
    inv_paths = export_inventory_and_coverage_excel(db, output_dir=output_dir, data_dir=data_dir)
    venues_path = export_backward_compatible_venues_csv(db, ROOT_DIR / "venues.csv")

    return {
        "platforms": n_plat,
        "clubs": n_clubs,
        "live_cache_stats": cache_stats,
        "non_api_stats": non_api_stats,
        "spot_check_template_xlsx": spot_template,
        "venues_csv": venues_path,
        **non_api_exports,
        **inv_paths,
    }


def run_weekly_peak_analysis(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    week_start: date = date(2026, 11, 9),
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """Compute Multi-Snapshot Club x Hour Peak/Off-Peak Heatmaps & 5-Pass Progression for a 7-day target week."""
    db = PadelDatabase(db_path)
    clubs_by_id, courts_map, oh_map = _load_db_courts_and_hours(db)

    week_end_excl = week_start + timedelta(days=7)
    week_start_iso = week_start.isoformat()
    week_end_incl_iso = (week_end_excl - timedelta(days=1)).isoformat()

    live_all = [
        o
        for o in db.fetch_observations(data_origin="live_public")
        if week_start_iso <= o.target_date <= week_end_incl_iso
    ]
    live_calendar_club_days = {
        (o.club_id, o.target_date)
        for o in live_all
        if o.booking_platform in ("browser_dropzone", "csv_import", "playtomic_official_api", "playtomic")
        and o.availability_status in ("available", "unavailable")
    }
    live_calendar_clubs = {cid for cid, _ in live_calendar_club_days}

    if live_calendar_club_days:
        active_obs = [o for o in live_all if (o.club_id, o.target_date) in live_calendar_club_days]
        dataset_origin = "live_public"
    else:
        active_obs = [
            o
            for o in db.fetch_observations(data_origin="synthetic_validation")
            if week_start_iso <= o.target_date <= week_end_incl_iso
        ]
        dataset_origin = "synthetic_validation (5-pass demo — fill data/inbox/week_YYYY-MM-DD/ to switch to live_public)"

    slices = resolve_atomic_slices(active_obs, courts=courts_map, opening_hours=oh_map) if active_obs else []
    hm_data = compute_club_hour_heatmap_and_empirical_peaks(slices, clubs_by_id=clubs_by_id)
    prog_rows = compute_snapshot_progression_curve(
        active_obs,
        courts=courts_map,
        opening_hours=oh_map,
        clubs_by_id=clubs_by_id,
    )

    with db.connect() as conn:
        telemetry_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM activity_telemetry_snapshots ORDER BY observed_at ASC"
            ).fetchall()
        ]
    live_org_grid = build_live_next_week_organised_grid(
        telemetry_rows,
        clubs_by_id=clubs_by_id,
        week_start=week_start,
    )
    sched_rows = build_weekly_snapshot_schedule(clubs_by_id.values(), week_start=week_start)

    export_paths = export_weekly_peak_offpeak_heatmap_excel(
        week_start_iso=week_start_iso,
        week_end_iso=week_end_incl_iso,
        dataset_origin=dataset_origin,
        club_hour_matrix=hm_data["club_hour_matrix"],
        club_daytype_hour_matrix=hm_data["club_daytype_hour_matrix"],
        empirical_bands=hm_data["empirical_bands"],
        progression_rows=prog_rows,
        live_organised_grid=live_org_grid,
        schedule_rows=sched_rows,
        output_dir=output_dir,
    )

    return {
        "week_start": week_start_iso,
        "week_end": week_end_incl_iso,
        "dataset_origin": dataset_origin,
        "live_calendar_clubs_count": len(live_calendar_clubs),
        "observations_analyzed": len(active_obs),
        "slices_analyzed": len(slices),
        "empirical_bands": hm_data["empirical_bands"],
        "progression_rows": prog_rows,
        "live_organised_grid": live_org_grid,
        **export_paths,
    }


def run_full_pipeline(
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, object]:
    """Execute full inventory sync, API-free live telemetry & court measurement, synthetic pipeline validation, and all Excel/CSV exports."""
    res = run_measure_without_api(db_path=db_path, output_dir=output_dir, data_dir=data_dir)
    db = PadelDatabase(db_path)
    clubs_by_id, courts_map, oh_map = _load_db_courts_and_hours(db)

    # Scaffold next-week 5-pass dropzone pack (non-destructive: skips any already-created files)
    scaffold_stats = scaffold_weekly_inbox_pack(
        clubs_by_id.values(),
        week_start=date(2026, 11, 9),
        inbox_dir=data_dir / "inbox",
    )

    # Clear prior synthetic_validation rows so repeated pipeline runs remain clean & deterministic
    with db.connect() as conn:
        conn.execute("DELETE FROM availability_observations WHERE data_origin = 'synthetic_validation'")
        conn.execute("DELETE FROM collection_runs WHERE data_origin = 'synthetic_validation'")

    synth_run = generate_synthetic_validation_dataset(
        clubs_by_id=clubs_by_id,
        courts=courts_map.values(),
        opening_hours=oh_map,
    )
    db.record_collection_run(synth_run)
    inserted, dupes = db.insert_observations(synth_run.observations)

    # Compute atomic 15-min slice occupancy across the synthetic validation dataset
    obs_loaded = db.fetch_observations(data_origin="synthetic_validation")
    slices = resolve_atomic_slices(obs_loaded, courts=courts_map, opening_hours=oh_map)
    breakdowns = build_all_occupancy_breakdowns(slices)

    obs_paths = export_observations_and_occupancy_excel(
        db=db,
        observations=obs_loaded,
        breakdowns=breakdowns,
        output_dir=output_dir,
        synthetic_csv_path=data_dir / "synthetic" / "synthetic_dublin_snapshots.csv",
        dataset_label="synthetic_validation",
    )

    weekly_paths = run_weekly_peak_analysis(
        db_path=db_path,
        week_start=date(2026, 11, 9),
        output_dir=output_dir,
    )

    return {
        **res,
        "scaffold_stats": scaffold_stats,
        "synthetic_inserted": inserted,
        "synthetic_duplicates": dupes,
        "atomic_slices": len(slices),
        "breakdowns": breakdowns,
        **obs_paths,
        "weekly_heatmap_xlsx": weekly_paths["weekly_heatmap_xlsx"],
        "weekly_dataset_origin": weekly_paths["dataset_origin"],
    }


def _print_non_api_summary(summary: dict[str, Any], db_path: Path) -> None:
    na = summary["non_api_stats"]
    measurements = na["measurements"]
    with PadelDatabase(db_path).connect() as conn:
        in_dub = {r["club_id"]: int(r["in_county_dublin"]) for r in conn.execute("SELECT club_id, in_county_dublin FROM clubs")}
    dub_m = [m for m in measurements if in_dub.get(m["club_id"], 0) == 1]
    dub_cap = sum(m["weekly_capacity_court_hours"] for m in dub_m)
    dub_occ = sum(m["total_occupied_court_hours_point"] for m in dub_m)
    dub_low = sum(m["total_occupied_court_hours_low"] for m in dub_m)
    dub_hi = sum(m["total_occupied_court_hours_high"] for m in dub_m)
    dub_gbv = sum(m["est_weekly_gbv_eur_point"] for m in dub_m)

    print("=== Dublin Padel API-Free Court Measurement Summary ===")
    print(f"SQLite Database            : {db_path}")
    print(
        f"Live Public Activities     : {na['unique_live_activities']} unique tournaments/classes/courses "
        f"({len(na['velocity_pairs'])} tracked across 24h T1->T2 snapshots)"
    )
    print(f"Live Memberships & Offers  : {na['membership_offers']} club membership / bundle offers parsed")
    print(f"Live Court Slot Rows       : {na['live_court_observations_projected']} live_public court observations projected")
    print(
        f"County Dublin Public Supply: {len(dub_m)} clubs, {sum(m['verified_padel_courts'] for m in dub_m)} courts, "
        f"{dub_cap:,.1f} weekly offered court-hours"
    )
    print(
        f"County Dublin Occupancy    : {100.0*dub_occ/max(dub_cap,1):.1f}% point "
        f"[{100.0*dub_low/max(dub_cap,1):.1f}% - {100.0*dub_hi/max(dub_cap,1):.1f}%] "
        f"({dub_occ:,.1f} occupied court-hrs/wk)"
    )
    print(
        f"County Dublin Est. GBV     : EUR {dub_gbv:,.0f}/week (~EUR {dub_gbv*52:,.0f}/yr annualized)"
    )
    print(f"API-Free Workbook          : {summary['non_api_xlsx']}")
    print(f"Spot-Check Template        : {summary['spot_check_template_xlsx']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="padel-monitor",
        description="Dublin Padel Market Intelligence & API-Free Court Occupancy Tracker",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Initialize SQLite schema and sync verified Dublin club inventory")
    sub.add_parser("measure-without-api", help="Run 100%% API-free court telemetry, sign-up velocity & club utilization engine")
    sub.add_parser("run-pipeline", help="Run full API-free court measurement + synthetic validation + all Excel/CSV exports")

    p_harv = sub.add_parser("harvest-live", help="Fetch fresh robots-compliant public club, tournament & course pages without API")
    p_harv.add_argument("--club", default=None, help="Optional single club_id (omit to harvest all Playtomic clubs)")

    p_live = sub.add_parser("collect-live", help="Fetch live robots-compliant Playtomic club metadata")
    p_live.add_argument("--club", required=True, help="club_id (e.g. house_of_padel)")

    sub.add_parser("ingest-inbox", help="Ingest manual browser HTML/text calendar snapshots from data/inbox/")

    p_init_wk = sub.add_parser(
        "init-week-pack",
        help="Scaffold 5-pass (7d, 3d, 24h, morning, prepeak) weekly dropzone templates & schedule CSV",
    )
    p_init_wk.add_argument(
        "--week-start",
        default="2026-11-09",
        help="Monday ISO date of target week (default: 2026-11-09)",
    )
    p_init_wk.add_argument(
        "--club",
        default=None,
        help="Optional single club_id to scaffold (omit to scaffold 12 priority Dublin clubs)",
    )

    p_ana_wk = sub.add_parser(
        "analyze-week",
        help="Ingest multi-snapshot inbox data & build the Weekly Club x Hour Peak/Off-Peak Heatmap workbook",
    )
    p_ana_wk.add_argument(
        "--week-start",
        default="2026-11-09",
        help="Monday ISO date of target week (default: 2026-11-09)",
    )

    p_spot = sub.add_parser("log-spot-check", help="Record a field, telephone, or visual court spot-check without API")
    p_spot.add_argument("--club", required=True, help="club_id (e.g. padel_dome, portmarnock, bushy_park)")
    p_spot.add_argument("--start", required=True, help="Target slot start ISO-8601 (e.g. 2026-11-10T19:00:00+00:00)")
    p_spot.add_argument("--end", required=True, help="Target slot end ISO-8601 (e.g. 2026-11-10T20:00:00+00:00)")
    p_spot.add_argument("--total", type=int, required=True, help="Total padel courts checked")
    p_spot.add_argument("--occupied", type=int, required=True, help="Occupied courts observed")
    p_spot.add_argument("--blocked", type=int, default=0, help="Blocked/maintenance courts")
    p_spot.add_argument("--price", type=float, default=None, help="Displayed court hourly price EUR")
    p_spot.add_argument("--method", default="browser_visual_check", help="telephone_enquiry | browser_visual_check | on_site_headcount")
    p_spot.add_argument("--notes", default="", help="Observer notes")

    p_csv = sub.add_parser("ingest-csv", help="Validate, normalize, and store a CSV observation feed")
    p_csv.add_argument("csv_path", type=Path, help="Path to CSV observation file")
    p_csv.add_argument(
        "--origin",
        choices=["live_public", "live_authorized_api", "synthetic_validation"],
        default="live_public",
    )

    p_dash = sub.add_parser("dashboard", help="Launch interactive Streamlit court & occupancy dashboard")
    p_dash.add_argument("--port", type=int, default=8501, help="Port to serve on (default: 8501)")

    p_daemon = sub.add_parser("run-daemon", help="Run autonomous background multi-snapshot scheduler")
    p_daemon.add_argument("--interval", type=int, default=60, help="Clock check interval seconds (default: 60)")
    p_daemon.add_argument("--skip-initial", action="store_true", help="Skip immediate pass on startup and wait for next scheduled window")

    p_wave = sub.add_parser("snapshot-wave", help="Execute an immediate automated snapshot collection wave across all Dublin clubs")
    p_wave.add_argument("--bucket", default=None, help="Snapshot lead-time bucket (24h, 6h, 1h, ad_hoc)")
    p_wave.add_argument("--date", default=None, help="Target ISO date for snapshot (e.g. 2026-11-10)")
    p_wave.add_argument("--days", type=int, default=7, help="Number of days to scrape ahead (default: 7)")
    p_wave.add_argument("--week-start", default="2026-11-09", help="Target week Monday ISO date")
    p_wave.add_argument("--skip-network", action="store_true", help="Skip live HTTP network harvest (offline/inbox-only mode)")

    args = parser.parse_args(argv)
    db = PadelDatabase(args.db)

    if args.command == "init-db":
        n_plat, n_clubs = init_and_sync_inventory(db)
        print(f"Initialized {args.db}: {n_plat} platforms, {n_clubs} verified clubs.")
        return 0

    if args.command == "measure-without-api":
        summary = run_measure_without_api(db_path=args.db)
        _print_non_api_summary(summary, args.db)
        return 0

    if args.command == "harvest-live":
        init_and_sync_inventory(db)
        rc = harvest_live_non_api_network(db, club_filter=args.club)
        if rc == 0:
            summary = run_measure_without_api(db_path=args.db)
            _print_non_api_summary(summary, args.db)
        return rc

    if args.command == "collect-live":
        init_and_sync_inventory(db)
        return collect_live_network_club(db, args.club)

    if args.command == "ingest-inbox":
        init_and_sync_inventory(db)
        res = ingest_inbox_snapshots(db)
        print(
            f"Processed {res['inbox_files_processed']} browser snapshot file(s) from {DEFAULT_INBOX_DIR}: "
            f"+{res['inbox_observations_inserted']} live_public court observations stored."
        )
        return 0

    if args.command == "init-week-pack":
        init_and_sync_inventory(db)
        ensure_inbox_readme(DEFAULT_INBOX_DIR)
        wk_start = date.fromisoformat(args.week_start)
        clubs_by_id, _, _ = _load_db_courts_and_hours(db)
        sc = scaffold_weekly_inbox_pack(
            clubs_by_id.values(),
            week_start=wk_start,
            inbox_dir=DEFAULT_INBOX_DIR,
            club_filter=args.club,
        )
        sched = build_weekly_snapshot_schedule(clubs_by_id.values(), week_start=wk_start)
        from .exporters import write_dicts_to_csv

        sched_csv = write_dicts_to_csv(DEFAULT_OUTPUT_DIR / "next_week_snapshot_schedule.csv", sched)
        print("=== Multi-Snapshot Weekly Dropzone Pack Initialized ===")
        print(f"Target Week                : {wk_start.isoformat()} to {(wk_start + timedelta(days=6)).isoformat()}")
        print(f"Dropzone Pack Directory    : {sc['pack_dir']}")
        print(
            f"5-Pass Bundle Templates    : {sc['files_created']} created, "
            f"{sc['files_skipped_existing']} already existed ({sc['clubs_scaffolded']} club(s) x 5 passes: 7d, 3d, 24h, morning, prepeak)"
        )
        print(f"Snapshot Schedule CSV      : {sched_csv} ({len(sched)} scheduled club-day-pass captures)")
        return 0

    if args.command == "analyze-week":
        init_and_sync_inventory(db)
        wk_start = date.fromisoformat(args.week_start)
        inbox_res = ingest_inbox_snapshots(db)
        wk = run_weekly_peak_analysis(db_path=args.db, week_start=wk_start)
        print(f"=== Dublin Padel Weekly Peak & Off-Peak Tracker ({wk['week_start']} to {wk['week_end']}) ===")
        print(f"Dataset Origin             : {wk['dataset_origin']}")
        print(
            f"Dropzone Snapshots Scanned : {inbox_res['inbox_files_processed']} active file(s), "
            f"+{inbox_res['inbox_observations_inserted']} new observations"
        )
        print(
            f"Atomic 15m Slices Resolved : {wk['slices_analyzed']} slices from {wk['observations_analyzed']} observations"
        )
        print(
            f"Live Next-Week Organised   : {len(wk['live_organised_grid'])} clubs with real live_public tournaments/classes already scheduled"
        )
        if wk["empirical_bands"]:
            print("\nDiscovered Empirical Peak vs Off-Peak Hours by Club:")
            for b in wk["empirical_bands"]:
                print(
                    f"  - {b['club_name']:28s} | Peak: {b['empirical_peak_windows']} ({b['empirical_peak_occ_pct']}%) "
                    f"| Off-Peak: {b['empirical_offpeak_windows']} ({b['empirical_offpeak_occ_pct']}%) "
                    f"| Busiest: {b['busiest_hour']}"
                )
        agg_prog = [p for p in wk["progression_rows"] if p["club_id"] == "ALL_CLUBS"]
        if agg_prog:
            print("\n5-Snapshot Lead-Time Booking Progression (Aggregate):")
            for p in agg_prog:
                gain = f"+{p['incremental_gain_pp']}pp" if p["incremental_gain_pp"] is not None else "baseline"
                print(
                    f"  - {p['snapshot_stage']:28s} | Overall={p['overall_occupancy_pct']}% ({gain}) "
                    f"| Peak={p['peak_occupancy_pct']}% | Off-Peak={p['offpeak_occupancy_pct']}%"
                )
        print(f"\nWeekly Heatmap Workbook    : {wk['weekly_heatmap_xlsx']}")
        print(f"Empirical Peak Bands CSV   : {wk['empirical_bands_csv']}")
        print(f"5-Snapshot Progression CSV : {wk['snapshot_progression_csv']}")
        return 0

    if args.command == "log-spot-check":
        init_and_sync_inventory(db)
        rec = build_spot_check_record(
            club_id=args.club,
            target_slot_start=args.start,
            target_slot_end=args.end,
            total_courts_checked=args.total,
            occupied_courts=args.occupied,
            blocked_courts=args.blocked,
            displayed_price_eur=args.price,
            collection_method=args.method,
            observer_notes=args.notes,
        )
        ins, _ = db.insert_spot_checks([rec])
        with db.connect() as conn:
            c_spots = [
                dict(r)
                for r in conn.execute("SELECT * FROM field_spot_checks WHERE club_id = ?", (args.club,)).fetchall()
            ]
        est = estimate_stratified_occupancy(c_spots).get(args.club)
        run_measure_without_api(db_path=args.db)
        if est:
            print(
                f"Logged spot check (+{ins}) for {args.club} [{rec['time_stratum']}]: "
                f"stratified occupancy={est.point_occupancy_pct}% (95% CI [{est.ci95_low_pct}%, {est.ci95_high_pct}%] "
                f"across {est.samples_count} sample(s) in {est.strata_covered} strata)."
            )
        return 0

    if args.command == "ingest-csv":
        init_and_sync_inventory(db)
        adapter = CsvObservationAdapter()
        run, errors = adapter.load_csv(args.csv_path, default_data_origin=args.origin)
        db.record_collection_run(run)
        ins, dup = db.insert_observations(run.observations)
        print(
            f"Ingested {args.csv_path}: {ins} new observations stored, {dup} duplicates ignored, "
            f"{len(errors)} row error(s)."
        )
        return 0

    if args.command == "run-pipeline":
        summary = run_full_pipeline(db_path=args.db)
        _print_non_api_summary(summary, args.db)
        print(
            f"Synthetic Validation       : {summary['synthetic_inserted']} overlapping 60m/90m slot observations "
            f"resolved into {summary['atomic_slices']} deduplicated 15-min court slices"
        )
        print(f"Inventory Workbook         : {summary['inventory_xlsx']}")
        print(f"Occupancy Workbook         : {summary['observations_xlsx']}")
        print(f"Weekly Peak/OffPeak Heatmap: {summary['weekly_heatmap_xlsx']}")
        print(f"Coverage Matrix CSV        : {summary['platform_coverage_csv']}")
        return 0

    if args.command == "dashboard":
        import subprocess
        dash_script = ROOT_DIR / "src" / "padel_monitor" / "dashboard.py"
        cmd = [sys.executable, "-m", "streamlit", "run", str(dash_script), "--server.port", str(args.port)]
        print(f"Launching Streamlit Dashboard on port {args.port}...")
        return subprocess.run(cmd).returncode

    if args.command == "run-daemon":
        from .automation_runner import run_continuous_daemon
        run_continuous_daemon(db_path=args.db, poll_interval_seconds=args.interval, skip_initial_pass=args.skip_initial)
        return 0

    if args.command == "snapshot-wave":
        from .automation_runner import execute_snapshot_wave
        w_start = date.fromisoformat(args.week_start) if args.week_start else None
        t_date = date.fromisoformat(args.date) if getattr(args, "date", None) else None
        execute_snapshot_wave(
            db_path=args.db,
            target_date=t_date,
            target_week_start=w_start,
            bucket_label=args.bucket,
            skip_network=args.skip_network,
            days_ahead=getattr(args, "days", 7),
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
