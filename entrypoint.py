"""Production Entrypoint for Render Container Deployment.

Runs:
1. Automated snapshot daemon in background (passes at 08:00, 12:30, 16:30, 20:00, 23:30).
2. Streamlit Dashboard as primary web service on dynamic Render $PORT.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "dublin_padel.db"
SEED_DB = Path("data_seed") / "dublin_padel.db"


def ensure_database() -> None:
    """Ensure database exists on the persistent or container volume."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        if SEED_DB.exists():
            print(f"[ENTRYPOINT] Seeding database from {SEED_DB} -> {DB_PATH}...", flush=True)
            shutil.copy2(SEED_DB, DB_PATH)
        else:
            print("[ENTRYPOINT] Initializing new database schema...", flush=True)
            subprocess.run([sys.executable, "monitor.py", "init-db"], check=True)
    else:
        print(f"[ENTRYPOINT] Existing database detected at {DB_PATH}.", flush=True)


def main() -> None:
    ensure_database()

    port = os.environ.get("PORT", "8501")
    print(f"[ENTRYPOINT] Starting background autonomous scheduler daemon...", flush=True)
    daemon_proc = subprocess.Popen(
        [sys.executable, "-u", "monitor.py", "run-daemon", "--skip-initial"]
    )

    print(f"[ENTRYPOINT] Starting Streamlit dashboard on 0.0.0.0:{port}...", flush=True)
    streamlit_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.port",
        port,
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
    ]
    streamlit_proc = subprocess.Popen(streamlit_cmd)

    def handle_exit(signum, frame):
        print(f"[ENTRYPOINT] Received signal {signum}, stopping services...", flush=True)
        try:
            streamlit_proc.terminate()
        except Exception:
            pass
        try:
            daemon_proc.terminate()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    # Monitor processes
    while True:
        # Check Streamlit
        s_poll = streamlit_proc.poll()
        if s_poll is not None:
            print(f"[ENTRYPOINT] Streamlit process exited with code {s_poll}. Exiting container...", flush=True)
            try:
                daemon_proc.terminate()
            except Exception:
                pass
            sys.exit(s_poll)

        # Check Daemon - if crashed, restart it
        d_poll = daemon_proc.poll()
        if d_poll is not None:
            print(f"[ENTRYPOINT] Daemon process exited unexpectedly (code {d_poll}). Restarting daemon...", flush=True)
            daemon_proc = subprocess.Popen(
                [sys.executable, "-u", "monitor.py", "run-daemon", "--skip-initial"]
            )

        time.sleep(5)


if __name__ == "__main__":
    main()
