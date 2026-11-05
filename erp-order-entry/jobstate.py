#!/usr/bin/env python3
"""The agent's own record of which jobs it has finished.

A SQLite table, because that is what a worker of this size actually keeps: a
row per job, flipped to `done` once the work is over. It is written with
`PRAGMA synchronous=FULL`, so a row that says `done` survives the machine
losing power. The weakness measured here is not that the record is unreliable.
It is that the record is written *after* the order exists in the ERP, and the
gap between those two moments belongs to neither system.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    status     TEXT NOT NULL,
    order_name TEXT,
    updated    REAL NOT NULL
);
"""


class JobStore:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def is_done(self, job_id: str) -> bool:
        row = self.db.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return bool(row) and row[0] == "done"

    def order_name(self, job_id: str) -> Optional[str]:
        row = self.db.execute(
            "SELECT order_name FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row[0] if row else None

    def mark_done(self, job_id: str, email: str, order_name: str) -> None:
        self.db.execute(
            "INSERT INTO jobs (job_id, email, status, order_name, updated) "
            "VALUES (?, ?, 'done', ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET status='done', order_name=excluded.order_name, "
            "updated=excluded.updated",
            (job_id, email, order_name, time.time()),
        )

    def close(self) -> None:
        self.db.close()
