#!/usr/bin/env python3
"""Run real WebSocket sync traffic under repeatable application-layer impairments."""

import argparse
import asyncio
import csv
import json
import random
import time
import uuid
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

SCENARIOS = {
    "good": {"delay_ms": 20, "jitter_ms": 0, "loss": 0.0},
    "mediocre": {"delay_ms": 150, "jitter_ms": 50, "loss": 0.0},
    "bad": {"delay_ms": 300, "jitter_ms": 0, "loss": 0.02},
}


class SimulatedClient:
    def __init__(self, ws_url, condition, seed):
        self.ws_url, self.condition = ws_url, condition
        self.random = random.Random(seed)
        self.client_id = str(uuid.uuid4())
        self.position, self.playing, self.changed_at = 0.0, False, time.time()
        self.ws = None
        self.tasks = []

    def current_position(self):
        return self.position + (time.time() - self.changed_at if self.playing else 0.0)

    def apply(self, position, playing, server_time=None):
        now = time.time()
        projected = float(position) + (max(0.0, now - server_time) if playing and server_time else 0.0)
        self.position, self.playing, self.changed_at = projected, bool(playing), now

    async def impaired_wait(self):
        delay = max(0, self.random.gauss(self.condition["delay_ms"], self.condition["jitter_ms"]))
        await asyncio.sleep(delay / 1000)

    async def send(self, payload, allow_loss=True):
        await self.impaired_wait()
        if allow_loss and self.random.random() < self.condition["loss"]:
            return
        await self.ws.send(json.dumps(payload))

    async def listen(self):
        async for raw in self.ws:
            message = json.loads(raw)
            await self.impaired_wait()
            if self.random.random() < self.condition["loss"]:
                continue
            if message["type"] in ("sync", "play", "pause", "seek"):
                self.apply(message.get("position", 0), message.get("playing", False), message.get("ts"))

    async def measurements(self):
        while True:
            now = time.time()
            await self.send({
                "type": "measurement", "client_id": self.client_id,
                "position": self.current_position(), "corrected_time": now,
                "playing": self.playing,
            })
            await asyncio.sleep(0.2)

    async def open(self):
        self.ws = await connect(self.ws_url)
        self.tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.measurements())]

    async def action(self, kind, position=None, playing=None):
        position = self.current_position() if position is None else position
        next_playing = kind == "play" or (kind == "seek" and bool(playing))
        self.apply(position, next_playing)
        asyncio.create_task(self.send({"type": kind, "position": position, "playing": next_playing}))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await self.ws.close()


async def recovery_time(clients, started, threshold=0.2, timeout=5.0):
    while time.time() - started < timeout:
        positions = [client.current_position() for client in clients]
        same_state = all(client.playing == clients[0].playing for client in clients)
        if same_state and max(positions) - min(positions) <= threshold and time.time() - started > 0.01:
            return time.time() - started
        await asyncio.sleep(0.01)
    return timeout


async def run_scenario(base_url, scenario, condition):
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.post("/api/create", json={"stream_url": "http://127.0.0.1/benchmark.mp4"})
        response.raise_for_status()
        room_id = response.json()["room_id"]
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    clients = [SimulatedClient(f"{ws_base}/ws/{room_id}", condition, i + 41) for i in range(3)]
    for simulated in clients:
        await simulated.open()
    await asyncio.sleep(1)
    recoveries = []
    for kind, position, playing in (("play", 0, True), ("seek", 45, True), ("pause", None, False)):
        started = time.time()
        await clients[0].action(kind, position, playing)
        recoveries.append(await recovery_time(clients, started))
        # The server's default steady-state window is three seconds after an
        # action. Wait long enough to collect post-window measurements too.
        await asyncio.sleep(4)
    async with httpx.AsyncClient(base_url=base_url) as client:
        drift = (await client.get(f"/api/room/{room_id}/measurements")).json()["drift"]
    for simulated in clients:
        await simulated.close()
    session = drift["session_wide"]
    steady = drift["steady_state"]
    return {
        "scenario": scenario, **condition,
        "steady_state_exclusion_seconds": drift["steady_state_exclusion_seconds"],
        "session_samples": session["samples"],
        "session_p50_ms": session["p50_ms"], "session_p95_ms": session["p95_ms"],
        "session_max_ms": session["max_ms"],
        "steady_samples": steady["samples"],
        "steady_p50_ms": steady["p50_ms"], "steady_p95_ms": steady["p95_ms"],
        "steady_max_ms": steady["max_ms"],
        "seek_recovery_ms": round(recoveries[1] * 1000, 3),
        "pause_recovery_ms": round(recoveries[2] * 1000, 3),
    }


def write_svg(rows, path):
    width, height, margin = 760, 360, 55
    maximum = max(max(row["steady_p95_ms"], row["steady_max_ms"]) for row in rows) or 1
    colors = ("#22c55e", "#f59e0b", "#ef4444")
    bars = []
    for index, row in enumerate(rows):
        x = margin + index * 220
        for offset, key in enumerate(("steady_p50_ms", "steady_p95_ms", "steady_max_ms")):
            value = row[key]
            bar_height = value / maximum * 240
            bars.append(f'<rect x="{x + offset * 45}" y="{300-bar_height:.1f}" width="34" height="{bar_height:.1f}" fill="{colors[offset]}"/>')
            bars.append(f'<text x="{x + offset * 45 + 17}" y="{292-bar_height:.1f}" text-anchor="middle" font-size="11">{value:.1f}</text>')
        bars.append(f'<text x="{x+51}" y="325" text-anchor="middle">{row["scenario"]}</text>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><text x="20" y="25" font-size="18">Steady-state playback drift by network scenario (ms)</text>
<line x1="{margin-10}" y1="300" x2="720" y2="300" stroke="#333"/>{''.join(bars)}
<text x="520" y="25" fill="#22c55e">p50</text><text x="570" y="25" fill="#f59e0b">p95</text><text x="620" y="25" fill="#ef4444">max</text></svg>'''
    path.write_text(svg, encoding="utf-8")


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    rows = [await run_scenario(args.base_url, name, condition) for name, condition in SCENARIOS.items()]
    with (output / "network_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    write_svg(rows, output / "network_drift.svg")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="results")
    asyncio.run(main(parser.parse_args()))
