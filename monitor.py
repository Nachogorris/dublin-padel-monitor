"""Dublin Padel Monitor — backward-compatible entrypoint & CLI dispatcher.

Supports both:
1. Starter CSV mode:
     python monitor.py data/example_observations.csv
2. Full Market Intelligence & API-Free Court Measurement CLI subcommands:
     python monitor.py measure-without-api
     python monitor.py run-pipeline
     python monitor.py init-week-pack --week-start 2026-11-09
     python monitor.py analyze-week --week-start 2026-11-09
     python monitor.py harvest-live --club house_of_padel
     python monitor.py ingest-inbox
     python monitor.py log-spot-check --club padel_dome --start 2026-11-10T19:00:00+00:00 --end 2026-11-10T20:00:00+00:00 --total 9 --occupied 8
     python monitor.py collect-live --club house_of_padel
     python monitor.py ingest-csv data/example_observations.csv
     python monitor.py init-db
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

VALID = {"available", "unavailable", "blocked", "unknown"}
FIELDS = [
    "observed_at",
    "venue_id",
    "court_id",
    "slot_start",
    "slot_end",
    "status",
    "price_eur",
    "source_url",
]
CLI_SUBCOMMANDS = {
    "init-db",
    "measure-without-api",
    "run-pipeline",
    "init-week-pack",
    "analyze-week",
    "harvest-live",
    "collect-live",
    "ingest-inbox",
    "log-spot-check",
    "ingest-csv",
    "dashboard",
    "run-daemon",
    "snapshot-wave",
    "-h",
    "--help",
}


def load(path: str | Path) -> list[dict]:
    """Legacy starter CSV loader preserved for backward compatibility."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns: {sorted(missing)}")
        out = []
        for i, r in enumerate(reader, start=2):
            if r["status"] not in VALID:
                raise ValueError(f"Row {i}: bad status")
            for key in ("observed_at", "slot_start", "slot_end"):
                r[key + "_dt"] = datetime.fromisoformat(r[key].replace("Z", "+00:00"))
                if r[key + "_dt"].utcoffset() is None:
                    raise ValueError(f"Row {i}: {key} must include timezone offset")
            if r["slot_end_dt"] <= r["slot_start_dt"]:
                raise ValueError(f"Row {i}: slot end <= start")
            out.append(r)
        return out


def summarize(rows: list[dict]) -> dict[str, dict]:
    """Legacy slot-level transition summary preserved for backward compatibility."""
    by_slot = defaultdict(list)
    for r in rows:
        k = (r["venue_id"], r["court_id"], r["slot_start_dt"], r["slot_end_dt"])
        by_slot[k].append(r)
    by_venue = defaultdict(
        lambda: dict(observed=0, available=0, unavailable=0, blocked=0, unknown=0, conversions=0)
    )
    for key, samples in by_slot.items():
        samples.sort(key=lambda r: r["observed_at_dt"])
        state = samples[-1]["status"]
        stats = by_venue[key[0]]
        stats["observed"] += 1
        stats[state] += 1
        for a, b in zip(samples, samples[1:]):
            if a["status"] == "available" and b["status"] == "unavailable":
                stats["conversions"] += 1
    return by_venue


def _run_csv_summary(csv_file: Path) -> int:
    from padel_monitor.adapters import CsvObservationAdapter
    from padel_monitor.occupancy import build_all_occupancy_breakdowns, resolve_atomic_slices

    adapter = CsvObservationAdapter()
    run_res, errors = adapter.load_csv(csv_file, strict=True)
    slices = resolve_atomic_slices(run_res.observations)
    club_occ = build_all_occupancy_breakdowns(slices)["by_club"]

    print("Note: unavailable is NOT a verified paid booking.")
    # If starter columns exist, print legacy line too for exact compatibility
    try:
        rows = load(csv_file)
        for venue, s in summarize(rows).items():
            denominator = s["available"] + s["unavailable"]
            proxy = 100 * s["unavailable"] / denominator if denominator else None
            print(
                f"{venue}: {s['observed']} unique slots, {s['conversions']} available->unavailable transitions, "
                f"latest unavailability proxy={proxy:.1f}%"
                if proxy is not None
                else f"{venue}: no comparable slots"
            )
    except ValueError:
        pass

    for row in club_occ:
        print(
            f"[Atomic 15m Europe/Dublin] {row.dimension_key}: "
            f"offered={row.offered_bookable_hours:.2f}h, "
            f"unavail={row.total_unavailable_bookable_hours:.2f}h, "
            f"occupancy={row.estimated_occupancy_pct}% "
            f"(est_GBV=EUR {row.est_gross_booking_value_eur:.2f}, confidence={row.confidence_level}/{row.confidence_score})"
        )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and (sys.argv[1] in CLI_SUBCOMMANDS or sys.argv[1].startswith("--db")):
        from padel_monitor.cli import main as cli_main

        return cli_main(sys.argv[1:])

    filename = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/example_observations.csv")
    return _run_csv_summary(filename)


if __name__ == "__main__":
    sys.exit(main())
