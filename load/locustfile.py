import json
import os
import random
import time
import uuid

import requests
import websocket
from locust import User, between, events, task

ROOMS = []


@events.test_start.add_listener
def create_rooms(environment, **_kwargs):
    room_count = int(os.getenv("WATCHPARTY_LOAD_ROOMS", "20"))
    host = environment.host.rstrip("/")
    for index in range(room_count):
        response = requests.post(
            f"{host}/api/create",
            json={"stream_url": f"http://127.0.0.1/load-{index}.mp4"}, timeout=5,
        )
        response.raise_for_status()
        ROOMS.append(response.json()["room_id"])


class WatchPartyUser(User):
    wait_time = between(0.2, 0.6)

    def on_start(self):
        self.client_id = str(uuid.uuid4())
        self.room_id = random.choice(ROOMS)
        ws_host = self.environment.host.replace("https://", "wss://").replace("http://", "ws://")
        self.ws = websocket.create_connection(f"{ws_host}/ws/{self.room_id}", timeout=3)
        self.ws.recv()
        self.position = random.random() * 60

    @task(4)
    def measurement(self):
        now = time.time()
        self.ws.send(json.dumps({
            "type": "measurement", "client_id": self.client_id,
            "position": self.position, "corrected_time": now, "playing": False,
        }))

    @task(1)
    def sync_event_latency(self):
        started = time.perf_counter()
        try:
            sent = time.time()
            self.ws.send(json.dumps({"type": "ping", "t0": sent}))
            while True:
                message = json.loads(self.ws.recv())
                if message.get("type") == "pong" and message.get("t0") == sent:
                    break
            elapsed = (time.perf_counter() - started) * 1000
            self.environment.events.request.fire(
                request_type="WS", name="sync_event", response_time=elapsed,
                response_length=0, exception=None,
            )
        except Exception as exc:
            self.environment.events.request.fire(
                request_type="WS", name="sync_event",
                response_time=(time.perf_counter() - started) * 1000,
                response_length=0, exception=exc,
            )

    def on_stop(self):
        self.ws.close()
