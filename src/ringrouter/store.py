"""SQLite state: deliveries (dedupe + log) and pending confirmations."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  transcript TEXT,
  route TEXT,
  summary TEXT,
  first_seen REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trace (
  delivery_id TEXT NOT NULL,
  ts REAL NOT NULL,
  phase TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_delivery ON trace(delivery_id, ts);
CREATE TABLE IF NOT EXISTS pending (
  id TEXT PRIMARY KEY,
  delivery_id TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  state TEXT NOT NULL,          -- waiting | done | cancelled | expired
  created REAL NOT NULL,
  expires REAL NOT NULL
);
"""
# Delivery states: received, discarded, cancelled, refused, dry_run, sending, sent,
# accepted (slow job queued), done, needs_confirm, failed, unknown


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def get_state(self, delivery_id: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT state FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        return row[0] if row else None

    def set_state(self, delivery_id: str, state: str, *, transcript: str | None = None,
                  route: str | None = None, summary: str | None = None) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO deliveries(delivery_id, state, transcript, route, summary, first_seen, updated)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(delivery_id) DO UPDATE SET state=excluded.state,"
                " transcript=COALESCE(excluded.transcript, transcript), route=COALESCE(excluded.route, route),"
                " summary=COALESCE(excluded.summary, summary), updated=excluded.updated",
                (delivery_id, state, transcript, route, summary, now, now))

    def recent(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return self.conn.execute("SELECT delivery_id, state, route, transcript, summary, updated FROM deliveries"
                                     " ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()

    # trace (activity page) --------------------------------------------------
    def add_trace(self, delivery_id: str, phase: str, data: dict) -> None:
        if not delivery_id:
            return
        with self.lock:
            self.conn.execute("INSERT INTO trace VALUES(?,?,?,?)",
                              (delivery_id, time.time(), phase, json.dumps(data, default=str)))

    def get_trace(self, delivery_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT ts, phase, data FROM trace WHERE delivery_id=? ORDER BY ts, rowid",
                                     (delivery_id,)).fetchall()
        return [{"ts": ts, "phase": ph, "data": json.loads(d)} for ts, ph, d in rows]

    def activity(self, limit: int = 100) -> list[dict]:
        """Recent deliveries with their model cost (sum of model_reply costs in the trace)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT d.delivery_id, d.state, d.route, d.transcript, d.summary, d.first_seen, d.updated,"
                " (SELECT SUM(json_extract(t.data, '$.cost')) FROM trace t"
                "   WHERE t.delivery_id = d.delivery_id AND t.phase = 'model_reply') AS cost"
                " FROM deliveries d ORDER BY d.first_seen DESC LIMIT ?", (limit,)).fetchall()
        keys = ("delivery_id", "state", "route", "transcript", "summary", "first_seen", "updated", "cost")
        return [dict(zip(keys, r)) for r in rows]

    def get_delivery(self, delivery_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT delivery_id, state, route, transcript, summary, first_seen, updated"
                                  " FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        keys = ("delivery_id", "state", "route", "transcript", "summary", "first_seen", "updated")
        return dict(zip(keys, r)) if r else None

    def prune(self, days: int = 90) -> None:
        cutoff = time.time() - days * 86400
        with self.lock:
            self.conn.execute("DELETE FROM trace WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM deliveries WHERE updated < ?", (cutoff,))

    def recover_interrupted(self) -> list[str]:
        """A send in progress at shutdown has unknown status; never resend it."""
        with self.lock:
            rows = [r[0] for r in self.conn.execute("SELECT delivery_id FROM deliveries WHERE state='sending'")]
            self.conn.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
        return rows

    # pending confirmations ---------------------------------------------------
    def add_pending(self, delivery_id: str, kind: str, payload: dict, ttl_s: int = 300) -> str:
        pid = secrets.token_urlsafe(12)
        now = time.time()
        with self.lock:
            self.conn.execute("INSERT INTO pending VALUES(?,?,?,?,?,?,?)",
                              (pid, delivery_id, kind, json.dumps(payload), "waiting", now, now + ttl_s))
        return pid

    def take_pending(self, pid: str) -> tuple[str, dict] | None:
        """Atomically claim a waiting, unexpired confirmation."""
        with self.lock:
            row = self.conn.execute("SELECT kind, payload, state, expires FROM pending WHERE id=?", (pid,)).fetchone()
            if not row or row[2] != "waiting":
                return None
            if row[3] < time.time():
                self.conn.execute("UPDATE pending SET state='expired' WHERE id=?", (pid,))
                return None
            self.conn.execute("UPDATE pending SET state='done' WHERE id=?", (pid,))
        return row[0], json.loads(row[1])

    def cancel_pending(self, pid: str) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE pending SET state='cancelled' WHERE id=? AND state='waiting'", (pid,))
        return cur.rowcount > 0
