"""Root Streamlit Entrypoint for Dublin Padel Monitor.

Run directly via:
    streamlit run app.py
or:
    python monitor.py dashboard
"""
from pathlib import Path
import runpy
import sys

# Ensure src directory is in sys.path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

target = Path(__file__).resolve().parent / "src" / "padel_monitor" / "dashboard.py"
runpy.run_path(str(target), run_name="__main__")

