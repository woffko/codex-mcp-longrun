"""Crash-aware SQLite state for Goal wake leases."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WakeLease:
    job_id: str
    thread_id: str
    objective: str
    goal_created_at: int
    goal_updated_at: int
    state: str
    deadline_at: float
    terminal_state: str | None
    delivery_state: str
    error: str | None


class BridgeState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wake_leases (
                job_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                goal_created_at INTEGER NOT NULL,
                goal_updated_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                deadline_at REAL NOT NULL,
                terminal_state TEXT,
                delivery_state TEXT NOT NULL,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_lease_per_thread
                ON wake_leases(thread_id)
                WHERE state IN ('preparing', 'armed', 'terminal');
            CREATE TABLE IF NOT EXISTS bridge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def create_lease(
        self,
        *,
        job_id: str,
        thread_id: str,
        objective: str,
        goal_created_at: int,
        goal_updated_at: int,
        deadline_at: float,
    ) -> WakeLease:
        now = time.time()
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO wake_leases (
                        job_id, thread_id, objective, goal_created_at,
                        goal_updated_at, state, deadline_at, terminal_state,
                        delivery_state, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'preparing', ?, NULL, 'pending', NULL, ?, ?)
                    """,
                    (
                        job_id,
                        thread_id,
                        objective,
                        goal_created_at,
                        goal_updated_at,
                        deadline_at,
                        now,
                        now,
                    ),
                )
                self._event_locked(job_id, "prepare", {"thread_id": thread_id})
        except sqlite3.IntegrityError as exc:
            existing = self.get(job_id)
            if existing is not None:
                if existing.thread_id != thread_id:
                    raise RuntimeError("job ID is already registered for a different thread") from exc
                return existing
            raise RuntimeError("this Goal already has a live longrun wake lease") from exc
        lease = self.get(job_id)
        assert lease is not None
        return lease

    def update(self, job_id: str, **fields: Any) -> WakeLease:
        allowed = {"state", "terminal_state", "delivery_state", "error", "goal_updated_at"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("invalid wake lease update")
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [fields[name] for name in fields]
        with self._db:
            cursor = self._db.execute(
                f"UPDATE wake_leases SET {assignments} WHERE job_id = ?",  # noqa: S608
                [*values, job_id],
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown wake lease: {job_id}")
            self._event_locked(job_id, "update", fields)
        lease = self.get(job_id)
        assert lease is not None
        return lease

    def get(self, job_id: str) -> WakeLease | None:
        row = self._db.execute("SELECT * FROM wake_leases WHERE job_id = ?", (job_id,)).fetchone()
        return self._lease(row) if row is not None else None

    def pending(self) -> list[WakeLease]:
        rows = self._db.execute(
            """
            SELECT * FROM wake_leases
            WHERE state IN ('preparing', 'armed', 'terminal')
              AND delivery_state NOT IN ('resumed', 'abandoned', 'needs_manual_recovery')
            ORDER BY created_at
            """
        ).fetchall()
        return [self._lease(row) for row in rows]

    def live_for_thread(self, thread_id: str) -> WakeLease | None:
        row = self._db.execute(
            """
            SELECT * FROM wake_leases
            WHERE thread_id = ? AND state IN ('preparing', 'armed', 'terminal')
            ORDER BY created_at DESC LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        return self._lease(row) if row is not None else None

    def event(self, job_id: str, event: str, details: dict[str, Any]) -> None:
        with self._db:
            self._event_locked(job_id, event, details)

    def _event_locked(self, job_id: str, event: str, details: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO bridge_events(job_id, event, details_json, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event, json.dumps(details, sort_keys=True), time.time()),
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> WakeLease:
        return WakeLease(
            job_id=str(row["job_id"]),
            thread_id=str(row["thread_id"]),
            objective=str(row["objective"]),
            goal_created_at=int(row["goal_created_at"]),
            goal_updated_at=int(row["goal_updated_at"]),
            state=str(row["state"]),
            deadline_at=float(row["deadline_at"]),
            terminal_state=(str(row["terminal_state"]) if row["terminal_state"] is not None else None),
            delivery_state=str(row["delivery_state"]),
            error=str(row["error"]) if row["error"] is not None else None,
        )
