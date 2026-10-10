#!/usr/bin/env python3
"""Sweep Locust concurrency and stop after WebSocket p95 exceeds 200 ms."""

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path


def percentile_column(row):
    for candidate in ("95%", "95%ile", "95%ile (ms)"):
        if candidate in row:
            return float(row[candidate])
    raise KeyError("Locust stats CSV has no p95 column")


def draw(rows, path):
    width, height, margin = 760, 360, 55
    max_users = max(row["users"] for row in rows)
    max_p95 = max(220, max(row["p95_ms"] for row in rows))
    points = []
    for row in rows:
        x = margin + row["users"] / max_users * 650
        y = 300 - row["p95_ms"] / max_p95 * 250
        points.append(f"{x:.1f},{y:.1f}")
    threshold_y = 300 - 200 / max_p95 * 250
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<rect width="100%" height="100%" fill="white"/><text x="20" y="25" font-size="18">WebSocket event latency capacity sweep</text>
<line x1="{margin}" y1="300" x2="720" y2="300" stroke="#333"/><line x1="{margin}" y1="40" x2="{margin}" y2="300" stroke="#333"/>
<line x1="{margin}" y1="{threshold_y:.1f}" x2="720" y2="{threshold_y:.1f}" stroke="#ef4444" stroke-dasharray="6 4"/>
<text x="625" y="{threshold_y-6:.1f}" fill="#ef4444">200 ms limit</text><polyline points="{' '.join(points)}" fill="none" stroke="#2563eb" stroke-width="3"/>
<text x="330" y="340">concurrent clients</text><text transform="translate(16 230) rotate(-90)">p95 latency (ms)</text></svg>''', encoding="utf-8")


def main(args):
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env["WATCHPARTY_LOAD_ROOMS"] = str(args.rooms)
    rows = []
    for users in args.users:
        prefix = output / f"locust_{users}"
        subprocess.run([
            sys.executable, "-m", "locust", "-f", str(Path(__file__).with_name("locustfile.py")), "--headless",
            "--host", args.base_url, "-u", str(users), "-r", str(args.spawn_rate),
            "-t", args.duration, "--csv", str(prefix), "--only-summary",
        ], check=True, env=env)
        with (Path(str(prefix) + "_stats.csv")).open(newline="", encoding="utf-8") as handle:
            stats = list(csv.DictReader(handle))
        event = next(row for row in stats if row["Name"] == "sync_event")
        result = {"users": users, "p95_ms": percentile_column(event), "failures": int(event["Failure Count"])}
        rows.append(result)
        if result["p95_ms"] > 200:
            break
    with (output / "load_capacity.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("users", "p95_ms", "failures")); writer.writeheader(); writer.writerows(rows)
    draw(rows, output / "load_latency.svg")
    exceeded = next((row for row in rows if row["p95_ms"] > 200), None)
    print(f"Capacity limit: {exceeded['users']} clients" if exceeded else "Capacity limit not reached in configured sweep")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, nargs="+", default=[25, 50, 100, 200, 400])
    parser.add_argument("--rooms", type=int, default=20)
    parser.add_argument("--spawn-rate", type=int, default=25)
    parser.add_argument("--duration", default="30s")
    parser.add_argument("--output", default="results")
    main(parser.parse_args())
