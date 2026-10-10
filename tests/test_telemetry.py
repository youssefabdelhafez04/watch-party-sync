from watchparty_telemetry import TelemetryStore


def test_persistent_summary_survives_store_reopen(tmp_path):
    path = tmp_path / "telemetry.sqlite3"
    store = TelemetryStore(str(path))
    store.create_session("room-a", 100.0)
    store.record_user("room-a", "client-123", 105.0)
    store.record_user("room-a", "client-456", 106.0)
    store.record_drift("room-a", 0.020, True, 110.0)
    store.record_drift("room-a", 0.040, True, 120.0)
    store.record_drift("room-a", 2.0, False, 121.0)

    summary = TelemetryStore(str(path)).summary()

    assert summary["sessions"] == 1
    assert summary["unique_users"] == 2
    assert summary["session_lengths_seconds"]["max"] == 21.0
    assert summary["steady_state_drift_ms"]["samples"] == 2
    assert summary["steady_state_drift_ms"]["p50"] == 30.0
    assert summary["steady_state_drift_ms"]["p95"] == 39.0
