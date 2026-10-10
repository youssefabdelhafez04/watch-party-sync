#!/usr/bin/env python3
"""Print persisted production telemetry without contacting external services."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watchparty_telemetry import TelemetryStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    path = os.environ.get("WATCHPARTY_TELEMETRY_DB", "/app/data/telemetry.sqlite3")
    summary = TelemetryStore(path).summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    lengths = summary["session_lengths_seconds"]
    drift = summary["steady_state_drift_ms"]
    print(f"Sessions: {summary['sessions']}")
    print(f"Unique users: {summary['unique_users']}")
    print(
        "Session lengths (seconds): "
        f"n={lengths['samples']} mean={lengths['mean']:.3f} "
        f"p50={lengths['p50']:.3f} max={lengths['max']:.3f}"
    )
    print(
        "Steady-state drift (ms): "
        f"n={drift['samples']} p50={drift['p50']:.3f} "
        f"p95={drift['p95']:.3f} max={drift['max']:.3f}"
    )


if __name__ == "__main__":
    main()
