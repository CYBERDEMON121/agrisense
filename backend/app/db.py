"""SQLite persistence for telemetry, alerts, missions and device state.

Writes happen on a background thread (batched) so the simulation loop and the
HTTP server never block on disk I/O.  SQLite in WAL mode comfortably handles the
few hundred inserts per second a full farm produces.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    area        TEXT,
    vendor      TEXT,
    model       TEXT,
    fw          TEXT,
    status      TEXT NOT NULL DEFAULT 'online',
    battery     REAL,
    rssi        INTEGER,
    meta        TEXT NOT NULL DEFAULT '{}',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    device_id  TEXT NOT NULL,
    topic      TEXT NOT NULL,
    value      REAL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS ix_telemetry_device_ts ON telemetry(device_id, ts);

CREATE TABLE IF NOT EXISTS plot_readings (
    ts        REAL NOT NULL,
    plot_id   TEXT NOT NULL,
    moisture  REAL,
    temp      REAL,
    ec        REAL,
    health    REAL,
    pest      REAL,
    stage     INTEGER,
    PRIMARY KEY (plot_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_plot_readings_ts ON plot_readings(ts);

CREATE TABLE IF NOT EXISTS irrigation_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    zone       TEXT NOT NULL,
    action     TEXT NOT NULL,
    flow_lpm   REAL,
    duration_s REAL,
    trigger    TEXT,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_irrigation_ts ON irrigation_events(ts);

CREATE TABLE IF NOT EXISTS missions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ref            TEXT UNIQUE,
    ts_start       REAL NOT NULL,
    ts_end         REAL,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    trigger        TEXT,
    area_ha        REAL,
    battery_used   REAL,
    payload_l      REAL,
    waypoints      TEXT NOT NULL DEFAULT '[]',
    notes          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_missions_ts ON missions(ts_start);

CREATE TABLE IF NOT EXISTS spray_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    mission_ref TEXT,
    plot_id     TEXT NOT NULL,
    dose_ml_ha  REAL,
    coverage    REAL,
    pest_before REAL,
    pest_after  REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    level      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    device_id  TEXT,
    plot_id    TEXT,
    acked      INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS ix_alerts_acked ON alerts(acked);

CREATE TABLE IF NOT EXISTS camera_detections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    camera_id  TEXT NOT NULL,
    plot_id    TEXT,
    label      TEXT NOT NULL,
    confidence REAL,
    bbox       TEXT NOT NULL DEFAULT '[]',
    severity   REAL
);
CREATE INDEX IF NOT EXISTS ix_detections_ts ON camera_detections(ts);

CREATE TABLE IF NOT EXISTS bus_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Database:
    """Thin, explicit SQLite wrapper with a background writer thread."""

    def __init__(self, path: Path | str, batch_size: int = 200, flush_interval: float = 0.5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[tuple[str, tuple, dict[str, Any]]] = queue.Queue(maxsize=20000)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer, name="db-writer", daemon=True)
        self._writer_conn: sqlite3.Connection | None = None
        self.dropped = 0
        self._init_schema()

    # ---- setup --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = _row_factory
        conn.executescript(SCHEMA)
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.commit()
        conn.close()

    def start(self) -> None:
        self._writer_conn = self._connect()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()
        if self._writer_conn is not None:
            self._writer_conn.commit()
            self._writer_conn.close()
            self._writer_conn = None

    # ---- write path ---------------------------------------------------
    def submit(self, sql: str, params: Sequence[Any] = (), named: dict[str, Any] | None = None) -> None:
        try:
            self._queue.put_nowait((sql, tuple(params), named or {}))
        except queue.Full:
            self.dropped += 1

    def telemetry(self, ts: float, device_id: str, topic: str, payload: dict[str, Any]) -> None:
        value = payload.get("value")
        self.submit(
            "INSERT INTO telemetry(ts, device_id, topic, value, payload) VALUES (?,?,?,?,?)",
            (ts, device_id, topic, float(value) if isinstance(value, (int, float)) else None,
             json.dumps(payload, separators=(",", ":"))),
        )

    def plot_reading(self, row: dict[str, Any]) -> None:
        self.submit(
            "INSERT OR REPLACE INTO plot_readings(ts, plot_id, moisture, temp, ec, health, pest, stage)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row["ts"], row["plot_id"], row["moisture"], row["temp"], row["ec"],
             row["health"], row["pest"], row["stage"]),
        )

    def upsert_device(self, device_id: str, **fields_in: Any) -> None:
        """Insert or refresh a device row; ``first_seen`` is never overwritten."""
        now = time.time()
        first_seen = fields_in.pop("first_seen", now)
        last_seen = fields_in.pop("last_seen", now)
        cols = ["device_id", *fields_in.keys()]
        vals: list[Any] = [device_id]
        for col, value in fields_in.items():
            vals.append(json.dumps(value, separators=(",", ":")) if isinstance(value, dict) else value)
        placeholders = ",".join("?" * (len(cols) + 2))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        sql = (
            f"INSERT INTO devices({','.join([*cols, 'first_seen', 'last_seen'])})"
            f" VALUES ({placeholders})"
            f" ON CONFLICT(device_id) DO UPDATE SET {updates}, last_seen=excluded.last_seen"
        )
        self.submit(sql, [*vals, first_seen, last_seen])

    # ---- writer thread ------------------------------------------------
    def _writer(self) -> None:
        assert self._writer_conn is not None
        pending: list[tuple[str, tuple]] = []
        first_queued: float | None = None
        while not self._stop.is_set() or not self._queue.empty():
            try:
                sql, params, _ = self._queue.get(timeout=self._flush_interval)
                first_queued = first_queued if first_queued is not None else time.monotonic()
                pending.append((sql, params))
            except queue.Empty:
                pass
            stale = first_queued is not None and time.monotonic() - first_queued >= self._flush_interval
            if pending and (len(pending) >= self._batch_size or stale or self._stop.is_set()):
                self._flush(pending)
                pending = []
                first_queued = None
        if pending:
            self._flush(pending)

    def _flush(self, pending: list[tuple[str, tuple]]) -> None:
        """Commit a batch, grouping identical statements so executemany applies."""
        if not pending or self._writer_conn is None:
            return
        grouped: dict[str, list[tuple]] = {}
        for sql, params in pending:
            grouped.setdefault(sql, []).append(params)
        for sql, rows in grouped.items():
            try:
                with self._writer_conn:
                    self._writer_conn.executemany(sql, rows)
            except sqlite3.Error:
                self.dropped += len(rows)

    def flush(self) -> None:
        """Synchronously drain the queue (used by tests and on shutdown)."""
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            grouped: dict[str, list[tuple]] = {}
            while True:
                try:
                    sql, params, _ = self._queue.get_nowait()
                except queue.Empty:
                    break
                grouped.setdefault(sql, []).append(params)
            with conn:
                for sql, rows in grouped.items():
                    try:
                        conn.executemany(sql, rows)
                    except sqlite3.Error:
                        self.dropped += len(rows)
        finally:
            conn.close()

    # ---- read path (fresh connection so it is safe from any thread) ---
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = _row_factory
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        rows = self.query(sql, params)
        return rows[0][list(rows[0].keys())[0]] if rows else default

    def prune(self, older_than: float) -> int:
        conn = sqlite3.connect(self.path, timeout=30)
        removed = 0
        try:
            with conn:
                removed = conn.execute("DELETE FROM telemetry WHERE ts < ?", (older_than,)).rowcount
                conn.execute("DELETE FROM plot_readings WHERE ts < ?", (older_than,))
        except sqlite3.Error:
            return removed
        finally:
            conn.close()
        return removed

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "queued": self._queue.qsize(),
            "dropped": self.dropped,
            "devices": self.scalar("SELECT COUNT(*) FROM devices"),
            "telemetry_rows": self.scalar("SELECT COUNT(*) FROM telemetry"),
            "alerts": self.scalar("SELECT COUNT(*) FROM alerts"),
            "missions": self.scalar("SELECT COUNT(*) FROM missions"),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }