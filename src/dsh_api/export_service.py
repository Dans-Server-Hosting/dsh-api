"""Download your world as 3D (RFC 0021): start, track, serve and expire exports.

An export runs on its own one-worker executor, so there is never more than one
export pod on the node; each runs ``dsh-world-export`` in a pod in the owner's
namespace (``ClusterBackend.run_world_export``) and leaves
``<export_dir>/<server>/<id>/{world.zip, summary.json, preview/}``. Downloads
go through short-lived signed links, so a browser can fetch the zip without
the session token; preview files are served to the owner's Bearer token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dsh_api.cluster import ClusterBackend, ClusterError
from dsh_api.config import Settings
from dsh_api.db import Database, ExportRow, now_iso
from dsh_api.export_pod import EXPORT_ID_RE, PREVIEW_FILE_RE
from dsh_api.service import JobRunner, ServerNotFound, ServerService

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
LINK_TTL = timedelta(minutes=15)
DAY = timedelta(hours=24)
COORDINATE_LIMIT = 29_999_984  # the world border
SWEEP_EVERY = timedelta(minutes=10)


class ExportDisabled(Exception):
    pass


class ExportNotFound(Exception):
    pass


class ExportConflict(Exception):
    """409: the server cannot be exported now, or the export cannot be changed now."""


class ExportLimit(Exception):
    def __init__(self, limit: int, retry_after: int) -> None:
        super().__init__(f"at most {limit} exports a day")
        self.limit = limit
        self.retry_after = retry_after


class InvalidArea(ValueError):
    pass


@dataclass(frozen=True)
class ExportView:
    id: str
    server: str
    status: str
    phase: str | None
    area: dict | None
    custom_area: bool
    snapshot: str | None
    created_at: str
    finished_at: str | None
    expires_at: str | None
    error: str | None
    stats: dict | None


def _view(row: ExportRow) -> ExportView:
    area = {"x1": row.x1, "z1": row.z1, "x2": row.x2, "z2": row.z2} if row.x1 is not None else None
    return ExportView(
        id=row.id,
        server=row.server,
        status=row.status,
        phase=row.phase,
        area=area,
        custom_area=row.custom_area,
        snapshot=row.snapshot,
        created_at=row.created_at,
        finished_at=row.finished_at,
        expires_at=row.expires_at,
        error=row.error,
        stats=row.stats,
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ExportService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        cluster: ClusterBackend,
        servers: ServerService,
        jobs: JobRunner,
    ) -> None:
        self.settings = settings
        self.db = db
        self.cluster = cluster
        self.servers = servers
        self.jobs = jobs
        self.root = Path(settings.export_dir)
        secret = settings.export_link_secret or settings.jwt_secret
        # A dedicated key per purpose: a link key never validates a JWT, or
        # the reverse, even when both come from the same configured secret.
        self._link_key = hmac.new(secret.encode(), b"dsh-export-links", hashlib.sha256).digest()
        self._last_sweep: datetime | None = None
        servers.after_delete.append(self.forget_server)

    # --- guards ----------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.settings.world_export:
            raise ExportDisabled()

    def _owned_export(self, tenant_id: str, name: str, export_id: str) -> ExportRow:
        self.servers.owned(tenant_id, name)
        row = self.db.get_export(export_id) if EXPORT_ID_RE.match(export_id) else None
        if row is None or row.server != name or row.tenant_id != tenant_id:
            raise ExportNotFound(export_id)
        return row

    def validate_area(self, x1: int, z1: int, x2: int, z2: int) -> tuple[int, int, int, int]:
        for v in (x1, z1, x2, z2):
            if abs(v) > COORDINATE_LIMIT:
                raise InvalidArea("coordinates must be inside the world border")
        x1, x2 = sorted((x1, x2))
        z1, z2 = sorted((z1, z2))
        cap = self.settings.export_max_size
        if x2 - x1 + 1 > cap or z2 - z1 + 1 > cap:
            raise InvalidArea(f"the area can be at most {cap} × {cap} blocks")
        return x1, z1, x2, z2

    # --- reads -----------------------------------------------------------------

    def list(self, tenant_id: str, name: str) -> list[dict]:
        self._require_enabled()
        self.servers.owned(tenant_id, name)
        self.sweep_if_due()
        return [asdict(_view(r)) for r in self.db.list_exports(name, tenant_id)]

    def get(self, tenant_id: str, name: str, export_id: str) -> dict:
        self._require_enabled()
        return asdict(_view(self._owned_export(tenant_id, name, export_id)))

    def preview_file(self, tenant_id: str, name: str, export_id: str, file: str) -> Path:
        self._require_enabled()
        row = self._owned_export(tenant_id, name, export_id)
        if row.status != "ready" or not PREVIEW_FILE_RE.match(file):
            raise ExportNotFound(export_id)
        path = self._dir(name, export_id) / "preview" / file
        if not path.is_file():
            raise ExportNotFound(export_id)
        return path

    # --- writes ----------------------------------------------------------------

    def create(self, tenant_id: str, name: str, area: tuple[int, int, int, int] | None) -> dict:
        self._require_enabled()
        row = self.servers.owned(tenant_id, name)
        if area is not None:
            area = self.validate_area(*area)
        if row.status != "ready":
            raise ExportConflict(f"the server is {row.status}; try again once it is up")
        if self.servers.state_of(name) == "failed":
            raise ExportConflict("the server is failing; it cannot be exported until it is fixed")
        if any(e.status in ACTIVE for e in self.db.list_exports(name, tenant_id)):
            raise ExportConflict("an export of this server is already running")
        count, oldest = self.db.count_exports_since(tenant_id, DAY)
        if count >= self.settings.export_daily_limit:
            frees_at = _parse_iso(oldest) + DAY if oldest else datetime.now(UTC) + DAY
            retry = max(1, int((frees_at - datetime.now(UTC)).total_seconds()))
            raise ExportLimit(self.settings.export_daily_limit, retry)
        export_id = secrets.token_hex(6)
        export = self.db.insert_export(export_id, name, tenant_id, area)
        self.db.record(tenant_id, name, "export.requested", export_id)
        self.jobs.submit(self._run, export_id)
        return asdict(_view(export))

    def delete(self, tenant_id: str, name: str, export_id: str) -> None:
        self._require_enabled()
        row = self._owned_export(tenant_id, name, export_id)
        if row.status in ACTIVE:
            raise ExportConflict("the export is still running")
        self._remove(row.server, row.id)

    def link(self, tenant_id: str, name: str, export_id: str) -> dict:
        self._require_enabled()
        row = self._owned_export(tenant_id, name, export_id)
        if row.status != "ready":
            raise ExportConflict("the export is not ready")
        expires = datetime.now(UTC).replace(microsecond=0) + LINK_TTL
        token = self.sign(name, export_id, int(expires.timestamp()))
        return {
            "url": f"{self.settings.api_url()}/api/v1/export-downloads/{token}",
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    # --- signed links ----------------------------------------------------------

    def sign(self, server: str, export_id: str, expires: int) -> str:
        payload = f"{server}.{export_id}.{expires}".encode()
        mac = hmac.new(self._link_key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(mac)}"

    def download(self, token: str) -> tuple[Path, str]:
        """The zip a signed link names, and its download filename.
        ``ExportNotFound`` for anything wrong, without saying what."""
        self._require_enabled()
        try:
            payload_b64, mac_b64 = token.split(".", 1)
            payload = _unb64(payload_b64)
            mac = _unb64(mac_b64)
            server, export_id, expires = payload.decode().split(".")
            expires_at = int(expires)
        except (ValueError, UnicodeDecodeError):
            raise ExportNotFound("link") from None
        expected = hmac.new(self._link_key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected) or expires_at < datetime.now(UTC).timestamp():
            raise ExportNotFound("link")
        row = self.db.get_export(export_id) if EXPORT_ID_RE.match(export_id) else None
        if row is None or row.server != server or row.status != "ready":
            raise ExportNotFound("link")
        path = self._dir(row.server, row.id) / "world.zip"
        if not path.is_file():
            raise ExportNotFound("link")
        return path, f"{row.server}-world-{row.id}.zip"

    # --- the job ---------------------------------------------------------------

    def _dir(self, server: str, export_id: str) -> Path:
        return self.root / server / export_id

    def _run(self, export_id: str) -> None:
        """Off the request thread, one at a time. Never raises: any failure
        is recorded on the row with a reason the owner can read."""
        row = self.db.get_export(export_id)
        if row is None or row.status != "queued":
            return
        name = row.server
        self.db.update_export(export_id, status="running", phase="starting")
        staging = self.root / ".staging" / export_id
        try:
            if self.db.get_server(name) is None:
                raise ClusterError("the server was deleted")
            awake = self.servers.state_of(name) in ("awake", "waking", "stopped")
            args = [
                "--server-state", "awake" if awake else "asleep",
                "--default-size", str(self.settings.export_default_size),
                "--max-size", str(self.settings.export_max_size),
                "--title", name,
            ]  # fmt: skip
            if row.custom_area:
                args += ["--area", str(row.x1), str(row.z1), str(row.x2), str(row.z2)]
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            self.cluster.run_world_export(
                name,
                export_id,
                image=self.settings.export_image,
                args=args,
                dest=staging,
                timeout=self.settings.export_timeout_seconds,
                on_phase=lambda phase: self.db.update_export(export_id, phase=phase),
            )
            summary = json.loads((staging / "summary.json").read_text(encoding="utf-8"))
            if self.db.get_server(name) is None:
                raise ClusterError("the server was deleted")
            final = self._dir(name, export_id)
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(final, ignore_errors=True)
            staging.rename(final)
            area = summary.get("area") or {}
            stats = dict(summary.get("stats") or {})
            finished = datetime.now(UTC).replace(microsecond=0)
            expires = finished + timedelta(hours=self.settings.export_retention_hours)
            self.db.update_export(
                export_id,
                status="ready",
                phase=None,
                x1=area.get("x1"),
                z1=area.get("z1"),
                x2=area.get("x2"),
                z2=area.get("z2"),
                snapshot=summary.get("snapshot"),
                stats={
                    k: stats.get(k)
                    for k in ("blocks_scanned", "instances", "zip_bytes", "preview_bytes")
                },
                finished_at=finished.isoformat().replace("+00:00", "Z"),
                expires_at=expires.isoformat().replace("+00:00", "Z"),
            )
            self.db.record(row.tenant_id, name, "export.ready", export_id)
        except ClusterError as exc:
            self._failed(row, str(exc))
        except Exception as exc:  # the worker must not die silently
            log.exception("export %s of %s failed unexpectedly", export_id, name)
            self._failed(row, f"the export failed unexpectedly ({type(exc).__name__})")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _failed(self, row: ExportRow, reason: str) -> None:
        finished = datetime.now(UTC).replace(microsecond=0)
        expires = finished + timedelta(hours=self.settings.export_retention_hours)
        self.db.update_export(
            row.id,
            status="failed",
            phase=None,
            error=reason[:300],
            finished_at=finished.isoformat().replace("+00:00", "Z"),
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
        self.db.record(row.tenant_id, row.server, "export.failed", f"{row.id}: {reason[:300]}")

    # --- housekeeping ------------------------------------------------------------

    def recover_interrupted(self) -> None:
        """Exports a previous process was running when it stopped: mark them
        failed, and remove their pods (activeDeadlineSeconds would anyway)."""
        servers: set[str] = set()
        for row in self.db.exports_with_status(*ACTIVE):
            self._failed(row, "interrupted; please start it again")
            servers.add(row.server)
        for name in sorted(servers):
            try:
                self.cluster.delete_world_export_pods(name)
            except ClusterError as exc:
                log.warning("could not remove export pods of %s: %s", name, exc)
        shutil.rmtree(self.root / ".staging", ignore_errors=True)

    def sweep_if_due(self) -> None:
        now = datetime.now(UTC)
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_EVERY:
            self._last_sweep = now
            self.sweep()

    def sweep(self) -> int:
        """Remove expired exports, exports whose server is gone, and directories
        no row knows about. Returns how many exports were removed."""
        now = datetime.now(UTC)
        removed = 0
        known: set[tuple[str, str]] = set()
        for row in self.db.all_exports():
            server = self.db.get_server(row.server)
            expired = row.expires_at is not None and _parse_iso(row.expires_at) <= now
            orphaned = server is None or server.tenant_id != row.tenant_id
            if row.status not in ACTIVE and (expired or orphaned):
                self._remove(row.server, row.id)
                removed += 1
            else:
                known.add((row.server, row.id))
        if self.root.is_dir():
            for server_dir in self.root.iterdir():
                if server_dir.name.startswith(".") or not server_dir.is_dir():
                    continue
                for export_dir in server_dir.iterdir():
                    if (server_dir.name, export_dir.name) not in known:
                        shutil.rmtree(export_dir, ignore_errors=True)
                if not any(server_dir.iterdir()):
                    server_dir.rmdir()
        return removed

    def _remove(self, server: str, export_id: str) -> None:
        shutil.rmtree(self._dir(server, export_id), ignore_errors=True)
        self.db.delete_export(export_id)

    def forget_server(self, name: str) -> None:
        """A deleted server's exports go with it (RFC 0021 §9)."""
        for export_id in self.db.delete_exports_of(name):
            shutil.rmtree(self._dir(name, export_id), ignore_errors=True)
        shutil.rmtree(self.root / name, ignore_errors=True)

    def limits(self) -> dict:
        return {
            "export_default_size": self.settings.export_default_size,
            "export_max_size": self.settings.export_max_size,
            "export_daily_limit": self.settings.export_daily_limit,
            "export_retention_hours": self.settings.export_retention_hours,
        }


__all__ = [
    "ExportConflict",
    "ExportDisabled",
    "ExportLimit",
    "ExportNotFound",
    "ExportService",
    "InvalidArea",
    "ServerNotFound",
    "now_iso",
]
