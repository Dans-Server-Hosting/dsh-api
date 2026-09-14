"""SQLite state: who owns which server, and what was asked for.

The cluster remains the source of truth for whether a server exists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    name TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    hostname TEXT NOT NULL,
    motd TEXT NOT NULL,
    operator_username TEXT,
    created_at TEXT NOT NULL,
    last_woken_at TEXT
);
CREATE INDEX IF NOT EXISTS servers_tenant ON servers(tenant_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    server_name TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    message TEXT NOT NULL,
    page TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
"""


def now_iso() -> str:
    return _iso(datetime.now(UTC))


def _iso(at: datetime) -> str:
    return at.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ServerRow:
    name: str
    tenant_id: str
    hostname: str
    motd: str
    operator_username: str | None
    created_at: str
    last_woken_at: str | None


@dataclass(frozen=True)
class FeedbackRow:
    id: int
    username: str
    message: str
    page: str | None
    created_at: str
    status: str


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- tenants -----------------------------------------------------------

    def ensure_tenant(self, tenant_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants (id, created_at) VALUES (?, ?)",
                (tenant_id, now_iso()),
            )

    # --- servers -----------------------------------------------------------

    def insert_server(
        self, name: str, tenant_id: str, hostname: str, motd: str, operator_username: str | None
    ) -> ServerRow:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO servers (name, tenant_id, hostname, motd, operator_username,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, tenant_id, hostname, motd, operator_username, now_iso()),
            )
        return self.get_server(name)  # type: ignore[return-value]

    def get_server(self, name: str) -> ServerRow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM servers WHERE name = ?", (name,)).fetchone()
        return ServerRow(**row) if row else None

    def list_servers(self, tenant_id: str) -> list[ServerRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM servers WHERE tenant_id = ? ORDER BY created_at, name",
                (tenant_id,),
            ).fetchall()
        return [ServerRow(**r) for r in rows]

    def count_servers(self, tenant_id: str) -> int:
        with self._connect() as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM servers WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return int(n)

    def mark_woken(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE servers SET last_woken_at = ? WHERE name = ?", (now_iso(), name))

    def delete_server(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM servers WHERE name = ?", (name,))

    # --- events ------------------------------------------------------------

    def record(self, tenant_id: str, server_name: str | None, kind: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (at, tenant_id, server_name, kind, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (now_iso(), tenant_id, server_name, kind, detail),
            )

    def events(self, server_name: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, kind, detail FROM events WHERE server_name = ? ORDER BY id",
                (server_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- feedback ----------------------------------------------------------

    def insert_feedback(self, username: str, message: str, page: str | None) -> FeedbackRow:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO feedback (username, message, page, created_at) VALUES (?, ?, ?, ?)",
                (username, message, page, now_iso()),
            )
            feedback_id = cur.lastrowid
        return self.get_feedback(feedback_id)  # type: ignore[arg-type,return-value]

    def get_feedback(self, feedback_id: int) -> FeedbackRow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        return FeedbackRow(**row) if row else None

    def list_feedback(self, status: str | None = None) -> list[FeedbackRow]:
        """Newest first; ``status`` narrows to one status, None returns everything."""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM feedback WHERE status = ? ORDER BY id DESC", (status,)
                ).fetchall()
        return [FeedbackRow(**r) for r in rows]

    def count_feedback_since(self, username: str, window: timedelta) -> int:
        since = _iso(datetime.now(UTC) - window)
        with self._connect() as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE username = ? AND created_at >= ?",
                (username, since),
            ).fetchone()
        return int(n)

    def set_feedback_status(self, feedback_id: int, status: str) -> FeedbackRow | None:
        with self._connect() as conn:
            conn.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))
        return self.get_feedback(feedback_id)
