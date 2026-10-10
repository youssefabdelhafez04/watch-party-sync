#!/usr/bin/env python3
"""WebSocket proxy that injects per-message latency, jitter, and packet loss."""

import argparse
import asyncio
import random

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


async def forward(source, destination, delay_ms: float, jitter_ms: float, loss: float):
    async for message in source:
        if random.random() < loss:
            continue
        delay = max(0.0, random.gauss(delay_ms, jitter_ms)) / 1000
        await asyncio.sleep(delay)
        await destination.send(message)


async def main(args):
    async def handler(client):
        path = client.request.path
        async with connect(args.upstream.rstrip("/") + path) as upstream:
            tasks = [
                asyncio.create_task(forward(client, upstream, args.delay_ms, args.jitter_ms, args.loss)),
                asyncio.create_task(forward(upstream, client, args.delay_ms, args.jitter_ms, args.loss)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()

    async with serve(handler, args.host, args.port):
        print(f"ws://{args.host}:{args.port} -> {args.upstream}")
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="Upstream base, e.g. ws://127.0.0.1:8000")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--delay-ms", type=float, default=20)
    parser.add_argument("--jitter-ms", type=float, default=0)
    parser.add_argument("--loss", type=float, default=0.0, help="Probability from 0 to 1")
    asyncio.run(main(parser.parse_args()))
