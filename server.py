import asyncio
import atexit
from collections import deque
import contextlib
import json
import logging
import math
import os
import platform
import re
import ipaddress
import socket
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, quote, urlparse

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from watchparty_telemetry import TelemetryStore

FFMPEG   = shutil.which("ffmpeg")
FFPROBE  = shutil.which("ffprobe")
TRANSCODING_ENABLED = os.getenv("WATCHPARTY_DISABLE_TRANSCODING", "0") != "1"
MEDIA_BASE_URL = os.getenv("WATCHPARTY_MEDIA_BASE_URL", "").rstrip("/")
DIRECT_MEDIA = os.getenv("WATCHPARTY_DIRECT_MEDIA", "0") == "1"
PUBLIC_URL = os.getenv("WATCHPARTY_PUBLIC_URL", "").rstrip("/") or None
TELEMETRY_DB_PATH = os.getenv("WATCHPARTY_TELEMETRY_DB")
BLOCK_PRIVATE_URLS = os.getenv("WATCHPARTY_BLOCK_PRIVATE_URLS", "0") == "1"
PRELOADED_ONLY = os.getenv("WATCHPARTY_PRELOADED_ONLY", "0") == "1"
MEASUREMENT_MAX_AGE = 10.0
DRIFT_LOG_INTERVAL = int(os.getenv("DRIFT_LOG_INTERVAL", "20"))
STEADY_STATE_DELAY_SECONDS = float(os.getenv("DRIFT_STEADY_STATE_DELAY_SECONDS", "3"))
logger = logging.getLogger("watchparty")

RENDITIONS = (
    {"name": "360p", "width": 640, "height": 360, "video_bitrate": "800k", "bandwidth": 928000},
    {"name": "720p", "width": 1280, "height": 720, "video_bitrate": "2800k", "bandwidth": 2992000},
    {"name": "1080p", "width": 1920, "height": 1080, "video_bitrate": "5000k", "bandwidth": 5192000},
)

rooms: dict[str, dict] = {}
metrics = {
    "rooms_created_total": 0,
    "websocket_events_total": 0,
    "stream_changes_total": 0,
    "proxy_errors_total": 0,
    "playback_measurements_total": 0,
    "drift_samples_total": 0,
    "sessions_total": 0,
}
analytics = {"unique_users": set()}
telemetry = TelemetryStore(TELEMETRY_DB_PATH)


class PlaybackEvent(BaseModel):
    room_id: str
    user_id: str = "anonymous"
    event_type: str
    video_time_sec: float = Field(ge=0)
    client_timestamp: datetime
    latency_ms: float = Field(ge=0)


class PlaybackMeasurement(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    position: float = Field(ge=0)
    corrected_time: float = Field(gt=0)
    playing: bool = False
    observed_at: float = Field(default_factory=time.time)


def estimate_sync_offset(host_time: float, client_time: float, latency_ms: float) -> float:
    network_delay_sec = latency_ms / 2000
    return host_time - client_time + network_delay_sec


def percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated percentile for an in-memory sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def projected_position(measurement: dict, corrected_time: float) -> float:
    position = float(measurement["position"])
    if measurement.get("playing"):
        position += corrected_time - float(measurement["corrected_time"])
    return max(0.0, position)


def calculate_client_drift(measurements: list[dict]) -> float:
    """Calculate max client separation at the newest corrected-clock instant."""
    if len(measurements) < 2:
        return 0.0
    reference_time = max(float(item["corrected_time"]) for item in measurements)
    positions = [projected_position(item, reference_time) for item in measurements]
    return max(positions) - min(positions)


def is_steady_state(room: dict, now: float) -> bool:
    last_event = room.get("last_playback_event_at")
    return last_event is None or now - last_event >= STEADY_STATE_DELAY_SECONDS


def summarize_drift(samples: list[float]) -> dict:
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 0.50) * 1000, 3),
        "p95_ms": round(percentile(samples, 0.95) * 1000, 3),
        "max_ms": round(max(samples, default=0.0) * 1000, 3),
    }


def build_hls_master_playlist(renditions=RENDITIONS) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for rendition in renditions:
        lines.extend([
            f'#EXT-X-STREAM-INF:BANDWIDTH={rendition["bandwidth"]},RESOLUTION={rendition["width"]}x{rendition["height"]}',
            f'{rendition["name"]}/index.m3u8',
        ])
    return "\n".join(lines) + "\n"


def resolve_stream_url(value: str) -> str:
    """Resolve a short AWS media ID while leaving local/direct URLs unchanged."""
    value = value.strip()
    if PRELOADED_ONLY and value.startswith(("http://", "https://")):
        raise ValueError("Public demo accepts preloaded media IDs only; pasted links are disabled.")
    if PRELOADED_ONLY and not MEDIA_BASE_URL:
        raise ValueError("Preloaded-only mode requires a media base URL.")
    if MEDIA_BASE_URL and not value.startswith(("http://", "https://")):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("Media IDs may contain only letters, numbers, dots, dashes, and underscores.")
        return f"{MEDIA_BASE_URL}/{quote(value, safe='')}/master.m3u8"
    if BLOCK_PRIVATE_URLS:
        validate_public_media_url(value)
    return value


def validate_public_media_url(value: str) -> None:
    """Reject non-HTTP and non-public destinations in production proxy mode."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Stream URL must be a public HTTP or HTTPS URL.")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ValueError("Stream URL hostname could not be resolved.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Stream URL must not resolve to a private or reserved address.")


async def send_with_safe_redirects(
    client: httpx.AsyncClient, method: str, target: str, headers: dict[str, str]
) -> tuple[httpx.Response, str]:
    """Follow a small redirect chain while validating every destination."""
    for _ in range(6):
        if BLOCK_PRIVATE_URLS:
            validate_public_media_url(target)
        request = client.build_request(method, target, headers=headers)
        response = await client.send(request, stream=True)
        if response.is_redirect and response.headers.get("location"):
            next_target = urljoin(target, response.headers["location"])
            await response.aclose()
            target = next_target
            continue
        return response, target
    raise ValueError("Stream URL redirected too many times.")


def client_stream_url(room_id: str, room: dict) -> str:
    """Return local generated/proxied media or the configured CDN URL."""
    version = room.get("stream_version", 1)
    if "m3u8" in room:
        return f"/hls/{room_id}/master.m3u8?v={version}"
    stream_url = room["stream_url"]
    if MEDIA_BASE_URL and stream_url.startswith(f"{MEDIA_BASE_URL}/"):
        return stream_url
    if DIRECT_MEDIA and not MEDIA_BASE_URL:
        return stream_url
    return f"/stream/{room_id}?v={version}"


def ffmpeg_encoder() -> list[str]:
    configured = os.getenv("FFMPEG_VIDEO_ENCODER")
    if configured:
        return ["-c:v", configured]
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-allow_sw", "1", "-realtime", "1"]
    return ["-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast")]


def build_ffmpeg_command(url: str, tmpdir: str) -> list[str]:
    split_outputs = "".join(f"[v{i}]" for i in range(len(RENDITIONS)))
    filters = [f"[0:v]split={len(RENDITIONS)}{split_outputs}"]
    for i, rendition in enumerate(RENDITIONS):
        filters.append(
            f'[v{i}]scale=w={rendition["width"]}:h={rendition["height"]}:'
            "force_original_aspect_ratio=decrease,"
            f'pad={rendition["width"]}:{rendition["height"]}:(ow-iw)/2:(oh-ih)/2[v{i}out]'
        )
    command = [
        FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-analyzeduration", "500000", "-probesize", "500000", "-i", url,
        "-filter_complex", ";".join(filters),
    ]
    for i, rendition in enumerate(RENDITIONS):
        command.extend(["-map", f"[v{i}out]", "-map", "0:a:0?"])
    command.extend(ffmpeg_encoder())
    for i, rendition in enumerate(RENDITIONS):
        command.extend([
            f"-b:v:{i}", rendition["video_bitrate"], f"-maxrate:v:{i}", rendition["video_bitrate"],
            f"-bufsize:v:{i}", str(int(rendition["bandwidth"] * 2)), f"-profile:v:{i}", "high",
        ])
    command.extend([
        "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*3)",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-sn", "-f", "hls",
        "-hls_time", "3", "-hls_list_size", "0", "-hls_flags", "append_list+independent_segments",
        "-hls_segment_filename", os.path.join(tmpdir, "%v", "seg%05d.ts"),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(f"v:{i},a:{i},name:{r['name']}" for i, r in enumerate(RENDITIONS)),
        os.path.join(tmpdir, "%v", "index.m3u8"),
    ])
    return command


def render_metrics() -> str:
    lines = []
    for name, value in metrics.items():
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    lines.append(f"active_rooms {len(rooms)}")
    lines.append(f"unique_users_total {len(analytics['unique_users'])}")
    active_duration = sum(max(0.0, time.time() - r["created_at"]) for r in rooms.values())
    lines.append(f"session_duration_seconds_total {active_duration:.3f}")
    return "\n".join(lines) + "\n"


def room_drift_summary(room: dict) -> dict:
    session = summarize_drift(room.get("drift_samples", []))
    session["total_samples"] = room.get("drift_sample_count", session["samples"])
    steady = summarize_drift(room.get("steady_drift_samples", []))
    steady["total_samples"] = room.get("steady_drift_sample_count", steady["samples"])
    return {
        "session_wide": session,
        "steady_state": steady,
        "steady_state_exclusion_seconds": STEADY_STATE_DELAY_SECONDS,
    }


def log_drift_summary(room_id: str, room: dict, reason: str) -> None:
    logger.info(json.dumps({
        "event": "session_drift_summary", "room_id": room_id, "reason": reason,
        **room_drift_summary(room),
    }, sort_keys=True))


async def cleanup_room_media(room: dict):
    proc = room.get("proc")
    if proc and proc.returncode is None:
        proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        if proc.returncode is None:
            proc.kill()
    tmpdir = room.pop("tmpdir", None)
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    room.pop("m3u8", None)
    room.pop("proc", None)
    room.pop("transcode_start", None)


async def set_room_stream(room_id: str, room: dict, url: str):
    await cleanup_room_media(room)
    room["stream_url"] = url
    room["duration"] = 0
    room["source_size"] = 0
    room["stream_version"] = room.get("stream_version", 0) + 1
    room["state"] = {"playing": False, "position": 0.0, "ts": time.time()}
    if FFMPEG and TRANSCODING_ENABLED:
        tmpdir, m3u8, proc = await start_transcode(room_id, url)
        room.update(tmpdir=tmpdir, m3u8=m3u8, proc=proc, transcode_start=time.time())
        asyncio.create_task(probe_source_info(room_id, url))


# ── ffmpeg ────────────────────────────────────────────────────────────────────

async def probe_source_info(room_id: str, url: str):
    # Get duration
    if FFPROBE:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
                "-of", "csv=p=0", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            if room_id in rooms:
                rooms[room_id]["duration"] = float(stdout.decode().strip())
        except Exception:
            pass
    # Get source file size for progress estimation
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.head(url)
            size = int(resp.headers.get("content-length", 0))
            if room_id in rooms:
                rooms[room_id]["source_size"] = size
    except Exception:
        pass


async def start_transcode(room_id: str, url: str):
    tmpdir  = tempfile.mkdtemp(prefix=f"wp_{room_id}_")
    m3u8 = os.path.join(tmpdir, "master.m3u8")
    for rendition in RENDITIONS:
        os.makedirs(os.path.join(tmpdir, rendition["name"]), exist_ok=True)
    with open(m3u8, "w", encoding="utf-8") as master:
        master.write(build_hls_master_playlist())
    proc = await asyncio.create_subprocess_exec(
        *build_ffmpeg_command(url, tmpdir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return tmpdir, m3u8, proc


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def home():
    return FileResponse("static/index.html", headers={"cache-control": "no-store"})


@app.get("/health")
async def health():
    return {
        "ok": True, "ffmpeg_available": bool(FFMPEG),
        "transcoding_enabled": TRANSCODING_ENABLED, "active_rooms": len(rooms),
        "persistent_telemetry": telemetry.enabled,
    }


@app.get("/metrics")
async def prometheus_metrics():
    return Response(render_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/api/analytics")
async def analytics_summary():
    active_duration = sum(max(0.0, time.time() - r["created_at"]) for r in rooms.values())
    persisted = telemetry.summary() if telemetry.enabled else None
    return {
        "sessions": persisted["sessions"] if persisted else metrics["sessions_total"],
        "active_sessions": len(rooms),
        "unique_users": persisted["unique_users"] if persisted else len(analytics["unique_users"]),
        "session_duration_seconds": round(active_duration, 3),
        "privacy": "Only opaque client IDs and aggregate timing data are collected; no IP addresses, names, email addresses, or user-agent strings are stored.",
    }


@app.get("/watch/{room_id}")
async def watch(room_id: str):
    return FileResponse("static/index.html", headers={"cache-control": "no-store"})


# ── Room API ──────────────────────────────────────────────────────────────────

@app.post("/api/create")
async def create_room(request: Request):
    body = await request.json()
    try:
        url = resolve_stream_url(body.get("stream_url", ""))
    except (AttributeError, ValueError) as exc:
        return Response(status_code=400, content=str(exc))
    title = body.get("title", "").strip()
    if not url:
        return Response(status_code=400)
    room_id = uuid.uuid4().hex[:8]
    room: dict = {
        "stream_url": url,
        "sockets": set(),
        "state": {"playing": False, "position": 0.0, "ts": time.time()},
        "stream_version": 1,
        "title": title or "Watch Party",
        "created_at": time.time(),
        "measurements": {},
        "drift_samples": deque(maxlen=10_000),
        "drift_sample_count": 0,
        "steady_drift_samples": deque(maxlen=10_000),
        "steady_drift_sample_count": 0,
        "last_playback_event_at": None,
        "participants": set(),
    }
    if FFMPEG and TRANSCODING_ENABLED:
        tmpdir, m3u8, proc = await start_transcode(room_id, url)
        room.update(tmpdir=tmpdir, m3u8=m3u8, proc=proc, transcode_start=time.time())
        asyncio.create_task(probe_source_info(room_id, url))
    rooms[room_id] = room
    telemetry.create_session(room_id, room["created_at"])
    metrics["rooms_created_total"] += 1
    metrics["sessions_total"] += 1
    return {"room_id": room_id}


@app.get("/api/room/{room_id}")
async def room_info(room_id: str):
    r = rooms.get(room_id)
    if not r:
        return Response(status_code=404)
    st = r["state"].copy()
    if st["playing"]:
        st["position"] += time.time() - st["ts"]

    # Transcode progress
    transcode_done = False
    transcode_progress = 0.0
    if "m3u8" in r:
        proc = r["proc"]
        m3u8 = r["m3u8"]
        duration = r.get("duration", 0)
        if proc.returncode is not None:
            transcode_done = True
            transcode_progress = 100.0
        elif duration > 0 and r.get("transcode_start"):
            elapsed = time.time() - r["transcode_start"]
            # Estimate: 2x speed (video copy + aac transcode)
            estimated_progress = min(100.0, (elapsed / (duration / 2)) * 100)
            transcode_progress = estimated_progress
        elif os.path.exists(m3u8):
            transcode_progress = 5.0  # At least show something if m3u8 exists

    stream_url = client_stream_url(room_id, r)
    return {
        "room_id": room_id,
        "state": st,
        "stream_url": stream_url,
        "duration": r.get("duration", 0),
        "transcode_done": transcode_done,
        "transcode_progress": transcode_progress,
        "stream_version": r.get("stream_version", 1),
        "title": r.get("title", "Watch Party"),
        "drift": room_drift_summary(r),
        "participants": len(r.get("participants", set())),
        "session_duration_seconds": round(max(0.0, time.time() - r["created_at"]), 3),
    }


@app.get("/api/room/{room_id}/measurements")
async def room_measurements(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return Response(status_code=404)
    return {"room_id": room_id, "participants": len(room["participants"]), "drift": room_drift_summary(room)}


@app.get("/api/config")
async def get_config():
    return {
        "public_url": _public_url,
        "media_base_url": MEDIA_BASE_URL or None,
        "accepts_media_ids": bool(MEDIA_BASE_URL),
        "preloaded_only": PRELOADED_ONLY,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

async def broadcast(room: dict, msg: dict, exclude: Optional[WebSocket] = None):
    dead = set()
    for ws in list(room["sockets"]):
        if ws is exclude:
            continue
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    room["sockets"] -= dead


@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str):
    r = rooms.get(room_id)
    if not r:
        await websocket.close(code=4004)
        return
    await websocket.accept()
    telemetry.touch_session(room_id)
    r["sockets"].add(websocket)
    st = r["state"]
    # Send the raw authoritative timeline (position sampled at `ts`) plus the
    # server clock. The client advances `position` itself using its clock offset,
    # which cancels out network latency.
    await websocket.send_json({
        "type": "sync",
        "playing": st["playing"],
        "position": st["position"],
        "ts": st["ts"],
        "server_time": time.time(),
    })
    try:
        while True:
            data = await websocket.receive_json()
            telemetry.touch_session(room_id)
            t   = data.get("type")
            metrics["websocket_events_total"] += 1
            if t == "ping":
                # NTP-style clock sync: echo the client's send time + our clock.
                await websocket.send_json({"type": "pong", "t0": data.get("t0"), "server": time.time()})
            elif t == "measurement":
                try:
                    measurement = PlaybackMeasurement(**data)
                except Exception:
                    await websocket.send_json({"type": "error", "error": "invalid measurement"})
                    continue
                now = time.time()
                item = measurement.model_dump()
                item["observed_at"] = now
                r["measurements"][measurement.client_id] = item
                if measurement.client_id not in r["participants"]:
                    r["participants"].add(measurement.client_id)
                    analytics["unique_users"].add(measurement.client_id)
                telemetry.record_user(room_id, measurement.client_id, now)
                active = [
                    value for value in r["measurements"].values()
                    if now - value["observed_at"] <= MEASUREMENT_MAX_AGE
                ]
                metrics["playback_measurements_total"] += 1
                if len(active) >= 2:
                    drift = calculate_client_drift(active)
                    r["drift_samples"].append(drift)
                    r["drift_sample_count"] += 1
                    metrics["drift_samples_total"] += 1
                    if is_steady_state(r, now):
                        r["steady_drift_samples"].append(drift)
                        r["steady_drift_sample_count"] += 1
                        steady_state = True
                    else:
                        steady_state = False
                    telemetry.record_drift(room_id, drift, steady_state, now)
                    if r["drift_sample_count"] % DRIFT_LOG_INTERVAL == 0:
                        log_drift_summary(room_id, r, "periodic")
            elif t in ("play", "pause", "seek"):
                pos     = float(data.get("position", 0))
                playing = t == "play" or (t == "seek" and bool(data.get("playing", False)))
                now = time.time()
                r["state"].update(playing=playing, position=pos, ts=now)
                r["last_playback_event_at"] = now
                # Echo the authoritative server timestamp to the initiator too. Without
                # this acknowledgement, that client keeps its pre-network timestamp and
                # remains ahead of peers by roughly one outbound network delay.
                await broadcast(r, {"type": t, "position": pos, "playing": playing, "ts": now})
            elif t == "change_video":
                try:
                    url = resolve_stream_url(str(data.get("stream_url", "")))
                except ValueError:
                    await websocket.send_json({"type": "error", "error": "invalid media URL or ID"})
                    continue
                if not url:
                    continue
                title = str(data.get("title", "")).strip()
                if title:
                    r["title"] = title
                await set_room_stream(room_id, r, url)
                stream_url = client_stream_url(room_id, r)
                msg = {
                    "type": "change_video",
                    "stream_url": stream_url,
                    "stream_version": r.get("stream_version", 1),
                    "state": r["state"],
                    "title": r.get("title", "Watch Party"),
                }
                await websocket.send_json(msg)
                await broadcast(r, msg, exclude=websocket)
                metrics["stream_changes_total"] += 1
    except WebSocketDisconnect:
        r["sockets"].discard(websocket)
        log_drift_summary(room_id, r, "disconnect")
    except Exception:
        r["sockets"].discard(websocket)
        logger.exception("websocket error in room %s", room_id)


# ── HLS serving ───────────────────────────────────────────────────────────

async def wait_for(path: str, timeout: float = 60.0) -> bool:
    for _ in range(int(timeout / 0.5)):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        await asyncio.sleep(0.5)
    return False


@app.get("/hls/{room_id}/{asset:path}")
async def hls_asset(room_id: str, asset: str):
    r = rooms.get(room_id)
    if not r or "tmpdir" not in r or not asset.endswith((".m3u8", ".ts")):
        return Response(status_code=404)
    root = os.path.realpath(r["tmpdir"])
    path = os.path.realpath(os.path.join(root, asset))
    if os.path.commonpath((root, path)) != root:
        return Response(status_code=404)
    if not await wait_for(path, timeout=30.0):
        return Response(status_code=404)
    headers = {"access-control-allow-origin": "*", "cache-control": "no-store"}
    if asset.endswith(".ts"):
        return FileResponse(path, media_type="video/mp2t", headers=headers)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as manifest:
            content = manifest.read()
    except OSError:
        return Response(status_code=404)
    version = quote(str(r.get("stream_version", 1)), safe="")
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            joiner = "&" if "?" in stripped else "?"
            lines.append(f"{stripped}{joiner}v={version}")
        else:
            lines.append(line)
    return Response("\n".join(lines).encode(), media_type="application/vnd.apple.mpegurl", headers=headers)


# ── Transcode stream (fallback) ───────────────────────────────────────────────

# ── Proxy (fallback) ──────────────────────────────────────────────────────────

def rewrite_m3u8(content: str, room_id: str, base_url: str) -> str:
    out = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            abs_url = s if s.startswith(("http://", "https://")) else urljoin(base_url, s)
            out.append(f"/stream/{room_id}?url={quote(abs_url, safe='')}")
        else:
            out.append(line)
    return "\n".join(out)


TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=5.0)
FORWARD = {"range", "accept", "user-agent"}


@app.api_route("/stream/{room_id}", methods=["GET", "HEAD"])
async def proxy_stream(room_id: str, request: Request, url: Optional[str] = Query(None)):
    if PRELOADED_ONLY:
        return Response(
            status_code=403,
            content=b"Public stream proxy is disabled; preloaded media only.",
        )
    r = rooms.get(room_id)
    if not r:
        return Response(status_code=404)
    target = url if url else r["stream_url"]
    fwd    = {k: v for k, v in request.headers.items() if k.lower() in FORWARD}
    client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        if request.method == "HEAD":
            resp, _ = await send_with_safe_redirects(client, "HEAD", target, fwd)
            await resp.aread()
            await resp.aclose()
            await client.aclose()
            return Response(status_code=resp.status_code, headers={
                "access-control-allow-origin": "*", "accept-ranges": "bytes",
                "content-type": resp.headers.get("content-type", "video/mp4"),
            })
        resp, target = await send_with_safe_redirects(client, "GET", target, fwd)
        ct   = resp.headers.get("content-type", "")
        if "mpegurl" in ct.lower() or target.split("?")[0].endswith(".m3u8"):
            body = await resp.aread()
            await resp.aclose(); await client.aclose()
            return Response(content=rewrite_m3u8(body.decode(errors="replace"), room_id, target).encode(),
                            media_type="application/vnd.apple.mpegurl",
                            headers={"access-control-allow-origin": "*"})
        hdrs = {"access-control-allow-origin": "*", "content-type": ct or "video/mp4"}
        for h in ("content-length", "content-range", "accept-ranges"):
            if h in resp.headers:
                hdrs[h] = resp.headers[h]
        async def streamer():
            try:
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk
            finally:
                await resp.aclose(); await client.aclose()
        return StreamingResponse(streamer(), status_code=resp.status_code, headers=hdrs)
    except Exception as e:
        metrics["proxy_errors_total"] += 1
        try: await client.aclose()
        except: pass
        return Response(status_code=502, content=str(e).encode())


# ── Entry ─────────────────────────────────────────────────────────────────────

_public_url: Optional[str] = PUBLIC_URL
_tunnel_proc: Optional[subprocess.Popen] = None


def start_cloudflare_tunnel(port: int) -> Optional[str]:
    global _tunnel_proc
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        return None
    _tunnel_proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    atexit.register(lambda: _tunnel_proc and _tunnel_proc.terminate())
    deadline = time.time() + 20
    while time.time() < deadline and _tunnel_proc.poll() is None:
        line = _tunnel_proc.stdout.readline() if _tunnel_proc.stdout else ""
        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
        if match:
            return match.group(0)
    return None

if __name__ == "__main__":
    import uvicorn
    port = 8000
    tunnel_provider = None
    _public_url = start_cloudflare_tunnel(port)
    tunnel_provider = "Cloudflare" if _public_url else None

    sep = "=" * 54
    print(f"\n{sep}")
    print("  Watch Party — ready")
    print(f"  Local:  http://localhost:{port}")
    if _public_url:
        print(f"  Public: {_public_url} ({tunnel_provider})")
        print(f"\n  Send the Public URL to your friend")
    print(f"  ffmpeg: {'✓' if FFMPEG else 'NOT FOUND'}")
    print(f"{sep}\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
