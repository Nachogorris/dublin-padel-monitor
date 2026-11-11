"""Polite, robots.txt-enforced harvester for API-free public court & activity telemetry.

Fetches ONLY URLs permitted by each domain's robots.txt via CompliantHttpClient
(RFC 9309 wildcard matcher + >=1.25s per-host rate limiting, zero auth/CAPTCHA bypass):
1. Fresh Snapshot T2 (2026-11-07) of all 10 County Dublin Playtomic club pages (/clubs/<slug>)
   to compute real 24-hour player sign-up velocity against Snapshot T1 (2026-11-06).
2. Full Playtomic club subpages (/competitions/tournaments, /academy/classes, /academy/courses)
   which expose all scheduled court-occupying tournaments, classes, courses, player fill counts,
   durations, and EUR prices without any API key.
3. Greater Dublin commuter-belt Playtomic clubs (padelzone-celbridge, indoor-padel-ireland, the-k-club).
4. Official public websites of non-Playtomic Dublin venues (padel100.ie, padeldome.ie, pslc.ie,
   bushytennispadel.ie, shankilltennisclub.com, shorelineleisure.ie) to extract public schedules,
   pricing tables, and court metadata.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from padel_monitor.compliance import CompliantHttpClient
from padel_monitor.time_utils import now_dublin_iso

OUT_DIR = ROOT / "data" / "raw" / "non_api_telemetry"

PLAYTOMIC_DUBLIN_SLUGS: list[tuple[str, str, bool]] = [
    # (club_id, slug, fetch_activity_subpages)
    ("house_of_padel", "house-of-padel", True),
    ("star_padel", "star-padel-club", True),
    ("ob_padel", "ob-padel", True),
    ("druid_kimmage", "druid-padel-kimmage", True),
    ("druid_coolmine", "druid-padel-at-coolmine", True),
    ("druid_sportsco", "druid-padel-at-sportsco-members-only", False),
    ("leopardstown_golf", "leopardstown-golf-padel-range", True),
    ("padel_pines", "padel-pines", True),
    ("halpenny", "halpenny-padel-club", True),
    ("fitzpatrick_castle", "padel-at-fitzpatrick-castle-hotel", False),
    # Commuter belt reference Playtomic clubs
    ("padelzone_celbridge", "padelzone-celbridge", False),
    ("indoor_padel_ireland", "indoor-padel-ireland", False),
    ("the_k_club", "the-k-club", False),
]

DIRECT_CLUB_WEBSITES: list[tuple[str, str]] = [
    ("padel100_ring_commons", "https://padel100.ie/"),
    ("padel_dome", "https://padeldome.ie/"),
    ("portmarnock", "https://pslc.ie/padel/"),
    ("bushy_park", "https://bushytennispadel.ie/"),
    ("shankill", "https://shankilltennisclub.com/padel/"),
    ("shoreline", "https://shorelineleisure.ie/"),
]


def safe_filename(prefix: str, url: str) -> str:
    clean = re.sub(r"^https?://", "", url).strip("/")
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", clean)
    return f"{prefix}__{clean}.html"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "manifest.csv"

    targets: list[tuple[str, str, str]] = []
    for club_id, slug, want_sub in PLAYTOMIC_DUBLIN_SLUGS:
        base = f"https://playtomic.com/clubs/{slug}"
        targets.append((club_id, "playtomic_club_main", base))
        if want_sub:
            targets.append((club_id, "playtomic_tournaments", f"{base}/competitions/tournaments"))
            targets.append((club_id, "playtomic_classes", f"{base}/academy/classes"))
            targets.append((club_id, "playtomic_courses", f"{base}/academy/courses"))

    for club_id, web_url in DIRECT_CLUB_WEBSITES:
        targets.append((club_id, "club_website", web_url))

    rows: list[dict[str, object]] = []
    with CompliantHttpClient(min_delay_seconds=1.1, timeout_seconds=18.0) as client:
        for idx, (club_id, page_kind, url) in enumerate(targets, start=1):
            ts = now_dublin_iso()
            outcome = client.fetch_compliant(url)
            saved_file = ""
            if outcome.outcome_code == "success" and outcome.body:
                fname = safe_filename("20261107", url)
                (OUT_DIR / fname).write_text(outcome.body, encoding="utf-8")
                saved_file = fname
            print(
                f"[{idx:02d}/{len(targets):02d}] {club_id:22s} | {page_kind:21s} | "
                f"status={str(outcome.http_status):4s} | {outcome.outcome_code:18s} | bytes={len(outcome.body)}"
            )
            rows.append(
                {
                    "fetched_at": ts,
                    "club_id": club_id,
                    "page_kind": page_kind,
                    "url": url,
                    "final_url": outcome.final_url or "",
                    "http_status": outcome.http_status or "",
                    "robots_allowed": int(outcome.robots_allowed),
                    "auth_required": int(outcome.auth_required),
                    "antibot_detected": int(outcome.antibot_detected),
                    "outcome_code": outcome.outcome_code,
                    "bytes": len(outcome.body),
                    "saved_file": saved_file,
                    "detail": outcome.detail,
                }
            )

    with manifest_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    ok_count = sum(1 for r in rows if r["outcome_code"] == "success")
    print(f"\nCompleted non-API telemetry harvest: {ok_count}/{len(rows)} succeeded -> {manifest_path}")
    return 0 if ok_count > 0 else 2


if __name__ == "__main__":
    sys.exit(main())

