"""SQLite state: who owns which server, and what was asked for.

The cluster remains the source of truth for whether a server exists.
"""

from __future__ import annotations

import json
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
    last_woken_at TEXT,
    status TEXT NOT NULL DEFAULT 'ready'
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
CREATE TABLE IF NOT EXISTS server_plugins (
    server TEXT NOT NULL,
    slug TEXT NOT NULL,
    tag TEXT NOT NULL,
    filename TEXT NOT NULL,
    url TEXT NOT NULL,
    requires TEXT NOT NULL DEFAULT '[]',
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (server, slug)
);
CREATE TABLE IF NOT EXISTS server_plugin_state (
    server TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL,
    written_sha256 TEXT,
    applied_sha256 TEXT
);
"""

MIGRATIONS = {
    # (table, column): the ALTER that adds it to a database created before it existed.
    ("servers", "status"): "ALTER TABLE servers ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'",
}

ROW_STATUSES = ("provisioning", "ready", "failed", "deleting")
"""What the row says about the create: in flight, done (the cluster decides the
state from here on), or failed (the row keeps the tenant's slot until DELETE);
or that a DELETE was accepted and its steps are still running."""


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
    status: str = "ready"


@dataclass(frozen=True)
class SelectedPlugin:
    """One plugin in a server's selection, pinned to the release chosen when it
    was added (a boot never changes its version)."""

    slug: str
    tag: str
    filename: str
    url: str
    requires: tuple[str, ...] = ()
    added_by: str = "tenant"
    """``tenant`` (chosen), ``dependency`` (pulled in by a ``requires``), or
    ``default`` (what every server starts with)."""
    added_at: str = ""


@dataclass(frozen=True)
class PluginState:
    server: str
    seeded_at: str
    written_sha256: str | None
    """Hash of the selection last written to the server's ConfigMap."""
    applied_sha256: str | None
    """Hash of the selection the server last reported applying at boot."""


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
            for (table, column), alter in MIGRATIONS.items():
                present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in present:
                    conn.execute(alter)

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
        self,
        name: str,
        tenant_id: str,
        hostname: str,
        motd: str,
        operator_username: str | None,
        status: str = "provisioning",
    ) -> ServerRow:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO servers (name, tenant_id, hostname, motd, operator_username,"
                " created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, tenant_id, hostname, motd, operator_username, now_iso(), status),
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

    def provisioning_server(self, tenant_id: str) -> ServerRow | None:
        """The tenant's server whose create is still in flight, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM servers WHERE tenant_id = ? AND status = 'provisioning'"
                " ORDER BY created_at, name LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return ServerRow(**row) if row else None

    def list_with_status(self, status: str) -> list[ServerRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM servers WHERE status = ? ORDER BY created_at, name", (status,)
            ).fetchall()
        return [ServerRow(**r) for r in rows]

    def set_status(self, name: str, status: str) -> None:
        if status not in ROW_STATUSES:
            raise ValueError(f"unknown server status {status!r}")
        with self._connect() as conn:
            conn.execute("UPDATE servers SET status = ? WHERE name = ?", (status, name))

    def begin_delete(self, name: str, current: str) -> bool:
        """Move the row from ``current`` to ``deleting``; False when another
        request got there first (or the row changed under it)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET status = 'deleting' WHERE name = ? AND status = ?",
                (name, current),
            )
        return cur.rowcount == 1

    def mark_woken(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE servers SET last_woken_at = ? WHERE name = ?", (now_iso(), name))

    def delete_server(self, name: str) -> None:
        # Names are reused once a server is gone, so its plugin selection goes with it.
        with self._connect() as conn:
            conn.execute("DELETE FROM servers WHERE name = ?", (name,))
            conn.execute("DELETE FROM server_plugins WHERE server = ?", (name,))
            conn.execute("DELETE FROM server_plugin_state WHERE server = ?", (name,))

    # --- plugin selection ----------------------------------------------------

    def plugin_state(self, server: str) -> PluginState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM server_plugin_state WHERE server = ?", (server,)
            ).fetchone()
        return PluginState(**row) if row else None

    def seed_plugins(self, server: str, rows: list[SelectedPlugin]) -> bool:
        """Give a server its first selection; False (and no change) when it has
        had one before, even if the tenant has since removed everything."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO server_plugin_state (server, seeded_at) VALUES (?, ?)",
                (server, now_iso()),
            )
            if cur.rowcount != 1:
                return False
            for row in rows:
                self._insert_plugin(conn, server, row)
        return True

    def selected_plugins(self, server: str) -> list[SelectedPlugin]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, tag, filename, url, requires, added_by, added_at"
                " FROM server_plugins WHERE server = ? ORDER BY added_at, slug",
                (server,),
            ).fetchall()
        return [
            SelectedPlugin(**{**dict(r), "requires": tuple(json.loads(r["requires"]))})
            for r in rows
        ]

    def add_plugins(self, server: str, rows: list[SelectedPlugin]) -> None:
        with self._connect() as conn:
            for row in rows:
                self._insert_plugin(conn, server, row)

    def remove_plugin(self, server: str, slug: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM server_plugins WHERE server = ? AND slug = ?", (server, slug))

    def set_plugin_hashes(
        self, server: str, *, written: str | None = None, applied: str | None = None
    ) -> None:
        with self._connect() as conn:
            if written is not None:
                conn.execute(
                    "UPDATE server_plugin_state SET written_sha256 = ? WHERE server = ?",
                    (written, server),
                )
            if applied is not None:
                conn.execute(
                    "UPDATE server_plugin_state SET applied_sha256 = ? WHERE server = ?",
                    (applied, server),
                )

    @staticmethod
    def _insert_plugin(conn: sqlite3.Connection, server: str, row: SelectedPlugin) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO server_plugins"
            " (server, slug, tag, filename, url, requires, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server,
                row.slug,
                row.tag,
                row.filename,
                row.url,
                json.dumps(list(row.requires)),
                row.added_by,
                row.added_at or now_iso(),
            ),
        )

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
