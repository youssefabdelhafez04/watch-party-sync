"""Small persistent telemetry store for single-process Watch Party deployments."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


class TelemetryStore:
    def __init__(self, database_path: str | None) -> None:
        self.database_path = Path(database_path) if database_path else None
        if self.database_path:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @property
    def enabled(self) -> bool:
        return self.database_path is not None

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path:
            raise RuntimeError("Persistent telemetry is not enabled")
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_activity_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS users (
                    client_id TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_users (
                    session_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (session_id, client_id)
                );
                CREATE TABLE IF NOT EXISTS drift_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    drift_seconds REAL NOT NULL,
                    steady_state INTEGER NOT NULL CHECK (steady_state IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS drift_session_time
                    ON drift_samples(session_id, observed_at);
                """
            )
            # A process restart ends any in-memory room. Preserve its last observed
            # activity as the session endpoint instead of extending it to restart time.
            connection.execute(
                "UPDATE sessions SET ended_at = last_activity_at WHERE ended_at IS NULL"
            )

    def create_session(self, session_id: str, created_at: float | None = None) -> None:
        if not self.enabled:
            return
        created_at = created_at or time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions "
                "(session_id, room_id, created_at, last_activity_at, ended_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (session_id, session_id, created_at, created_at),
            )

    def touch_session(self, session_id: str, observed_at: float | None = None) -> None:
        if not self.enabled:
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET last_activity_at = ?, ended_at = NULL WHERE session_id = ?",
                (observed_at or time.time(), session_id),
            )

    def record_user(
        self, session_id: str, client_id: str, observed_at: float | None = None
    ) -> None:
        if not self.enabled:
            return
        observed_at = observed_at or time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO users (client_id, first_seen, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(client_id) DO UPDATE SET last_seen = excluded.last_seen",
                (client_id, observed_at, observed_at),
            )
            connection.execute(
                "INSERT INTO session_users (session_id, client_id, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, client_id) "
                "DO UPDATE SET last_seen = excluded.last_seen",
                (session_id, client_id, observed_at, observed_at),
            )
            connection.execute(
                "UPDATE sessions SET last_activity_at = ?, ended_at = NULL WHERE session_id = ?",
                (observed_at, session_id),
            )

    def record_drift(
        self,
        session_id: str,
        drift_seconds: float,
        steady_state: bool,
        observed_at: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        observed_at = observed_at or time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO drift_samples "
                "(session_id, observed_at, drift_seconds, steady_state) VALUES (?, ?, ?, ?)",
                (session_id, observed_at, drift_seconds, int(steady_state)),
            )
            connection.execute(
                "UPDATE sessions SET last_activity_at = ?, ended_at = NULL WHERE session_id = ?",
                (observed_at, session_id),
            )

    def summary(self) -> dict:
        if not self.enabled:
            return {
                "sessions": 0,
                "unique_users": 0,
                "session_lengths_seconds": {"samples": 0, "mean": 0.0, "p50": 0.0, "max": 0.0},
                "steady_state_drift_ms": {"samples": 0, "p50": 0.0, "p95": 0.0, "max": 0.0},
            }
        with self._connect() as connection:
            sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            lengths = [
                max(0.0, end - start)
                for start, end in connection.execute(
                    "SELECT created_at, COALESCE(ended_at, last_activity_at) FROM sessions"
                )
            ]
            drifts = [
                row[0] * 1000
                for row in connection.execute(
                    "SELECT drift_seconds FROM drift_samples WHERE steady_state = 1"
                )
            ]
        return {
            "sessions": sessions,
            "unique_users": users,
            "session_lengths_seconds": {
                "samples": len(lengths),
                "mean": round(sum(lengths) / len(lengths), 3) if lengths else 0.0,
                "p50": round(percentile(lengths, 0.50), 3),
                "max": round(max(lengths, default=0.0), 3),
            },
            "steady_state_drift_ms": {
                "samples": len(drifts),
                "p50": round(percentile(drifts, 0.50), 3),
                "p95": round(percentile(drifts, 0.95), 3),
                "max": round(max(drifts, default=0.0), 3),
            },
        }
