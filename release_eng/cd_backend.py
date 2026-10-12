"""A simulated CD system that honors idempotency keys.

This sample doesn't deploy anything real. The Deploy Workflow's Activities call
this small local store instead of a CD system's API. It keeps one row per deploy
step in a SQLite file under .demo-state/ and ignores a step whose idempotency key
it has already seen.

That last part is the point. Temporal runs Activities at least once: if a Worker
dies after a step took effect but before Temporal recorded it, the step runs
again. The Workflow passes each step a key built from the Nexus request ID and
the step name (for example `<request id>:wave-2`), and the receiving system checks
it, so a repeated step changes nothing. In production, `deploy` would call your
CD system's API with the same key.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deploy_steps (
    idempotency_key TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    version TEXT NOT NULL,
    environment TEXT NOT NULL,
    step TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


@dataclass(frozen=True)
class AppliedStep:
    idempotency_key: str
    service: str
    version: str
    environment: str
    step: str
    applied_at: str


class CDBackend:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def is_applied(self, idempotency_key: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT 1 FROM deploy_steps WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return row is not None

    def apply(self, idempotency_key: str, service: str, version: str, environment: str, step: str) -> bool:
        """Apply one deploy step. Returns False, and changes nothing, for a key already applied."""
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO deploy_steps"
                " (idempotency_key, service, version, environment, step) VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, service, version, environment, step),
            )
        return cursor.rowcount == 1

    def applied_steps(self, key_prefix: str = "") -> list[AppliedStep]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT idempotency_key, service, version, environment, step, applied_at"
                " FROM deploy_steps WHERE idempotency_key LIKE ? ESCAPE '\\' ORDER BY rowid",
                (key_prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
            ).fetchall()
        return [AppliedStep(*row) for row in rows]
