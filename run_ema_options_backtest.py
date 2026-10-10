"""Run the EMA options backtest for dates configured below."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Edit these two dates before running this file. Use YYYY-MM-DD format.
START_DATE = "2026-01-01"
END_DATE = "2026-09-30"


def main() -> int:
    """Validate the configured date range and launch the existing backtest CLI."""
    try:
        start_date = date.fromisoformat(START_DATE)
        end_date = date.fromisoformat(END_DATE)
    except ValueError:
        print("Set START_DATE and END_DATE to valid YYYY-MM-DD dates in this file.")
        return 2

    if start_date > end_date:
        print("START_DATE must not be after END_DATE.")
        return 2

    command = [
        sys.executable,
        str(REPO_ROOT / "algo.py"),
        "backtest",
        "--strategy",
        "ema-options",
        "--start-date",
        start_date.isoformat(),
        "--end-date",
        end_date.isoformat(),
    ]
    print(f"Running EMA options backtest from {start_date} through {end_date}...")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
