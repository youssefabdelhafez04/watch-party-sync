from datetime import UTC, datetime

import pytest

from server import (
    PlaybackEvent,
    build_ffmpeg_command,
    build_hls_master_playlist,
    calculate_client_drift,
    estimate_sync_offset,
    is_steady_state,
    client_stream_url,
    render_metrics,
    resolve_stream_url,
    rewrite_m3u8,
    summarize_drift,
)


def test_rewrite_m3u8_converts_relative_segments_to_proxy_urls():
    content = "#EXTM3U\n#EXTINF:3,\nseg00001.ts\n"

    rewritten = rewrite_m3u8(content, "room123", "https://cdn.example.com/path/out.m3u8")

    assert "/stream/room123?url=https%3A%2F%2Fcdn.example.com%2Fpath%2Fseg00001.ts" in rewritten


def test_estimate_sync_offset_accounts_for_half_round_trip_latency():
    assert estimate_sync_offset(100.0, 98.0, 200.0) == 2.1


def test_playback_event_validation_accepts_valid_event():
    event = PlaybackEvent(
        room_id="abc123",
        event_type="seek",
        video_time_sec=42.5,
        client_timestamp=datetime.now(UTC),
        latency_ms=120,
    )

    assert event.room_id == "abc123"


def test_render_metrics_exposes_prometheus_style_counters():
    output = render_metrics()

    assert "rooms_created_total" in output
    assert "active_rooms" in output


def test_drift_projects_playing_clients_to_same_corrected_time():
    measurements = [
        {"position": 10.0, "corrected_time": 100.0, "playing": True},
        {"position": 10.2, "corrected_time": 100.2, "playing": True},
    ]

    assert calculate_client_drift(measurements) == pytest.approx(0.0)


def test_drift_summary_calculates_p50_p95_and_max_in_milliseconds():
    summary = summarize_drift([0.01, 0.02, 0.03, 0.04, 0.05])

    assert summary == {"samples": 5, "p50_ms": 30.0, "p95_ms": 48.0, "max_ms": 50.0}


def test_steady_state_excludes_three_seconds_after_playback_action():
    room = {"last_playback_event_at": 100.0}

    assert not is_steady_state(room, 102.999)
    assert is_steady_state(room, 103.0)


def test_abr_master_playlist_contains_all_renditions():
    playlist = build_hls_master_playlist()

    assert playlist.startswith("#EXTM3U\n")
    assert "RESOLUTION=640x360\n360p/index.m3u8" in playlist
    assert "RESOLUTION=1280x720\n720p/index.m3u8" in playlist
    assert "RESOLUTION=1920x1080\n1080p/index.m3u8" in playlist


def test_ffmpeg_command_generates_master_and_three_variant_playlists(tmp_path):
    command = build_ffmpeg_command("https://example.com/video.mp4", str(tmp_path))

    assert command[command.index("-master_pl_name") + 1] == "master.m3u8"
    assert command[command.index("-var_stream_map") + 1].count("name:") == 3
    assert command[-1] == str(tmp_path / "%v" / "index.m3u8")


def test_local_stream_url_still_uses_proxy(monkeypatch):
    monkeypatch.setattr("server.DIRECT_MEDIA", False)
    monkeypatch.setattr("server.MEDIA_BASE_URL", "")

    assert client_stream_url("abc123", {"stream_url": "https://example.com/movie.mp4"}) == "/stream/abc123?v=1"


def test_aws_media_id_resolves_to_cloudfront(monkeypatch):
    monkeypatch.setattr("server.MEDIA_BASE_URL", "https://cdn.example.com/media")
    monkeypatch.setattr("server.DIRECT_MEDIA", True)

    resolved = resolve_stream_url("movie-night")

    assert resolved == "https://cdn.example.com/media/movie-night/master.m3u8"
    assert client_stream_url("abc123", {"stream_url": resolved}) == resolved


def test_aws_external_url_uses_proxy(monkeypatch):
    monkeypatch.setattr("server.MEDIA_BASE_URL", "https://cdn.example.com/media")
    monkeypatch.setattr("server.DIRECT_MEDIA", True)

    assert client_stream_url(
        "abc123", {"stream_url": "https://video.example.com/movie.m3u8"}
    ) == "/stream/abc123?v=1"


def test_aws_media_id_rejects_path_traversal(monkeypatch):
    monkeypatch.setattr("server.MEDIA_BASE_URL", "https://cdn.example.com/media")

    with pytest.raises(ValueError, match="Media IDs"):
        resolve_stream_url("../private")


def test_aws_proxy_rejects_instance_metadata_address(monkeypatch):
    monkeypatch.setattr("server.MEDIA_BASE_URL", "https://cdn.example.com/media")
    monkeypatch.setattr("server.BLOCK_PRIVATE_URLS", True)

    with pytest.raises(ValueError, match="private or reserved"):
        resolve_stream_url("http://169.254.169.254/latest/meta-data/")
