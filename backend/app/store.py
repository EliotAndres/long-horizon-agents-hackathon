"""SQLite persistence for the agent's durable state: intent, active plan, watch conditions, events, metrics.

Everything the agent needs to resume after a restart lives here. There is no chat-history table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ActiveBooking, Event, TravelIntent, WatchCondition

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, status TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conditions (
    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, armed INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL,
    message TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sandbox_bookings (
    booking_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, status TEXT NOT NULL, body TEXT NOT NULL);
"""

TABLES = ["kv", "intents", "bookings", "conditions", "events", "metrics", "sandbox_bookings"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def reset(self) -> None:
        with self._lock:
            for t in TABLES:
                self._db.execute(f"DELETE FROM {t}")  # noqa: S608 - fixed table names

    # --- key/value agent state -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self._q("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # --- intent ------------------------------------------------------------------------------
    def put_intent(self, intent: TravelIntent) -> None:
        self._q(
            "INSERT OR REPLACE INTO intents(id, body, created_at) VALUES(?, ?, ?)",
            (intent.id, intent.model_dump_json(), intent.created_at or now_iso()),
        )

    def intent(self) -> TravelIntent | None:
        rows = self._q("SELECT body FROM intents ORDER BY created_at DESC LIMIT 1")
        return TravelIntent.model_validate_json(rows[0]["body"]) if rows else None

    # --- bookings (the agent's view of its plan) ------------------------------------------------
    def put_booking(self, b: ActiveBooking) -> None:
        self._q(
            "INSERT OR REPLACE INTO bookings(booking_id, intent_id, status, body, created_at) VALUES(?,?,?,?,?)",
            (b.booking_id, b.intent_id, b.status, b.model_dump_json(), b.created_at),
        )

    def bookings(self, intent_id: str) -> list[ActiveBooking]:
        rows = self._q("SELECT body FROM bookings WHERE intent_id=? ORDER BY created_at", (intent_id,))
        return [ActiveBooking.model_validate_json(r["body"]) for r in rows]

    def active_booking(self, intent_id: str) -> ActiveBooking | None:
        live = [b for b in self.bookings(intent_id) if b.status != "CANCELLED"]
        return live[-1] if live else None

    # --- watch conditions ---------------------------------------------------------------------
    def replace_conditions(self, intent_id: str, conds: list[WatchCondition]) -> None:
        with self._lock:
            self._db.execute("UPDATE conditions SET armed=0 WHERE intent_id=?", (intent_id,))
            for c in conds:
                self._db.execute(
                    "INSERT OR REPLACE INTO conditions(id, intent_id, armed, body, created_at) VALUES(?,?,?,?,?)",
                    (c.id, intent_id, int(c.armed), c.model_dump_json(), now_iso()),
                )

    def put_condition(self, c: WatchCondition) -> None:
        self._q("UPDATE conditions SET armed=?, body=? WHERE id=?", (int(c.armed), c.model_dump_json(), c.id))

    def conditions(self, intent_id: str, armed_only: bool = True) -> list[WatchCondition]:
        sql = "SELECT body, armed FROM conditions WHERE intent_id=?" + (" AND armed=1" if armed_only else "")  # noqa: S608 - constant fragments
        out = []
        for r in self._q(sql + " ORDER BY created_at", (intent_id,)):
            c = WatchCondition.model_validate_json(r["body"])
            c.armed = bool(r["armed"])
            out.append(c)
        return out

    # --- activity log ---------------------------------------------------------------------------
    def log(self, type_: str, source: str, message: str, payload: dict | None = None) -> Event:
        ev = Event(timestamp=now_iso(), type=type_, source=source, message=message, payload=payload or {})
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(ts, type, source, message, payload) VALUES(?,?,?,?,?)",
                (ev.timestamp, ev.type, ev.source, ev.message, json.dumps(ev.payload, default=str)),
            )
            ev.id = cur.lastrowid or 0
        return ev

    def events(self, limit: int = 200) -> list[Event]:
        rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [
            Event(
                id=r["id"],
                timestamp=r["ts"],
                type=r["type"],
                source=r["source"],
                message=r["message"],
                payload=json.loads(r["payload"]),
            )
            for r in reversed(rows)
        ]

    # --- metrics ------------------------------------------------------------------------------
    def incr(self, key: str, n: float = 1) -> None:
        self._q(
            "INSERT INTO metrics(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (key, n),
        )

    def metrics(self) -> dict[str, float]:
        return {r["key"]: r["value"] for r in self._q("SELECT key, value FROM metrics")}

    # --- sandbox provider's own records (stands in for an external booking system) -------------
    def sandbox_put(self, booking_id: str, idem: str | None, status: str, body: dict) -> None:
        self._q(
            "INSERT OR REPLACE INTO sandbox_bookings(booking_id, idempotency_key, status, body) VALUES(?,?,?,?)",
            (booking_id, idem, status, json.dumps(body)),
        )

    def sandbox_get(self, booking_id: str | None = None, idem: str | None = None) -> dict | None:
        if booking_id:
            rows = self._q("SELECT body FROM sandbox_bookings WHERE booking_id=?", (booking_id,))
        else:
            rows = self._q("SELECT body FROM sandbox_bookings WHERE idempotency_key=?", (idem,))
        return json.loads(rows[0]["body"]) if rows else None

    def sandbox_all(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self._q("SELECT body FROM sandbox_bookings")]
