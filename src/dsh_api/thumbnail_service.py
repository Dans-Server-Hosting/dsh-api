"""Rotating world thumbnails (RFC 0034): one small looping picture per server.

A thumbnail run is the world-export pod of RFC 0021 with the thumbnail image
and ``dsh-world-export --thumbnail``: the world volume read-only, no network,
no token, the same deadline and limits. It goes through the same one-worker
executor as exports, so the node never runs more than one export or thumbnail
pod at a time. What it leaves is
``<export_dir>/.thumbnails/<server>/{thumb.webp, poster.png, thumbnail.json}``
(the leading dot keeps the export sweeper away from it), served only to the
owner's Bearer token, and deleted with the server.

``DSH_WORLD_THUMBNAILS``: ``off`` (default; every route answers 503),
``manual`` (a run starts only when the owner asks, at most
``DSH_THUMBNAIL_DAILY_LIMIT`` per server per rolling 24 h) or ``auto``, which
is accepted but runs as ``manual``: the scheduled sweep over asleep servers is
not built until RFC 0034's first open question is decided.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dsh_api.cluster import ClusterBackend, ClusterError
from dsh_api.config import Settings
from dsh_api.db import Database, ThumbnailRow, now_iso
from dsh_api.service import JobRunner, ServerService

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
DAY = timedelta(hours=24)
SWEEP_EVERY = timedelta(minutes=10)
THUMBNAILS_DIR = ".thumbnails"
STAGING_DIR = ".staging"
FILES = {"thumb.webp": "image/webp", "poster.png": "image/png"}
META_FIELDS = ("area", "spawn", "snapshot", "strata", "frames", "size", "ms", "exporter_commit")
ALREADY_RUNNING = "the picture is already being drawn"


class ThumbnailDisabled(Exception):
    pass


class ThumbnailNotFound(Exception):
    pass


class ThumbnailConflict(Exception):
    """409: no run can start now."""


class ThumbnailLimit(Exception):
    def __init__(self, limit: int, retry_after: int) -> None:
        super().__init__(f"at most {limit} refreshes a day")
        self.limit = limit
        self.retry_after = retry_after


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clean_meta(raw: object) -> dict:
    """The fields of the pod's thumbnail.json the API keeps, nothing else."""
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in META_FIELDS if k in raw}


class ThumbnailService:
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
        self.root = Path(settings.export_dir) / THUMBNAILS_DIR
        self._last_sweep: datetime | None = None
        servers.after_delete.append(self.forget_server)
        if settings.world_thumbnails == "auto":
            log.warning(
                "DSH_WORLD_THUMBNAILS=auto runs as manual: the scheduled sweep is not built "
                "(RFC 0034, open question 1)"
            )

    # --- guards -------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """What is in effect: ``off`` or ``manual`` (``auto`` runs as ``manual``)."""
        return "manual" if self.settings.thumbnails_on else "off"

    def _require_enabled(self) -> None:
        if not self.settings.thumbnails_on:
            raise ThumbnailDisabled()

    def _row(self, tenant_id: str, name: str) -> ThumbnailRow:
        self.servers.owned(tenant_id, name)
        row = self.db.get_thumbnail(name)
        if row is None or row.tenant_id != tenant_id:
            raise ThumbnailNotFound(name)
        return row

    def _dir(self, name: str) -> Path:
        return self.root / name

    # --- reads --------------------------------------------------------------------

    def view(self, tenant_id: str, name: str) -> dict:
        self._require_enabled()
        row = self._row(tenant_id, name)
        has_picture = row.rendered_at is not None and all(
            (self._dir(name) / f).is_file() for f in FILES
        )
        return {
            "server": name,
            "status": row.status,
            "phase": row.phase,
            "error": row.error,
            "requested_at": row.requested_at,
            "rendered_at": row.rendered_at if has_picture else None,
            "has_picture": has_picture,
            **clean_meta(row.meta if has_picture else None),
        }

    def file(self, tenant_id: str, name: str, file: str) -> tuple[Path, str, str]:
        """The file, its media type and its ETag (from the render time)."""
        self._require_enabled()
        row = self._row(tenant_id, name)
        if file not in FILES or row.rendered_at is None:
            raise ThumbnailNotFound(name)
        path = self._dir(name) / file
        if not path.is_file():
            raise ThumbnailNotFound(name)
        stamp = row.rendered_at.replace(":", "").replace("-", "")
        return path, FILES[file], f'"{stamp}-{file.split(".")[0]}"'

    # --- writes -------------------------------------------------------------------

    def request(self, tenant_id: str, name: str) -> dict:
        """Queue a run for the server's thumbnail; answers at once (202)."""
        self._require_enabled()
        server = self.servers.owned(tenant_id, name)
        self.sweep_if_due()
        if server.status != "ready":
            raise ThumbnailConflict(f"the server is {server.status}; try again once it is up")
        if self.servers.state_of(name) == "failed":
            raise ThumbnailConflict("the server is failing; its picture cannot be drawn now")
        self._refuse(tenant_id, name)
        run_id = secrets.token_hex(6)
        row = self.db.reserve_thumbnail(
            name, tenant_id, run_id, self.settings.thumbnail_daily_limit, DAY
        )
        if row is None:
            self._refuse(tenant_id, name)
            raise ThumbnailConflict(ALREADY_RUNNING)
        self.jobs.submit(self._run, name, run_id)
        return self.view(tenant_id, name)

    def _refuse(self, tenant_id: str, name: str) -> None:
        row = self.db.get_thumbnail(name)
        if row is not None and row.status in ACTIVE:
            raise ThumbnailConflict(ALREADY_RUNNING)
        count, oldest = self.db.count_thumbnails_since(tenant_id, name, DAY)
        limit = self.settings.thumbnail_daily_limit
        if count >= limit:
            frees_at = _parse_iso(oldest) + DAY if oldest else datetime.now(UTC) + DAY
            retry = max(1, int((frees_at - datetime.now(UTC)).total_seconds()))
            raise ThumbnailLimit(limit, retry)

    # --- the job ------------------------------------------------------------------

    def _run(self, name: str, run_id: str) -> None:
        """Off the request thread, on the export executor. Never raises: a
        failure is recorded on the row, and the previous picture is kept."""
        row = self.db.get_thumbnail(name)
        if row is None or row.run_id != run_id or row.status != "queued":
            return
        self.db.update_thumbnail(name, run_id, status="running", phase="starting")
        staging = self.root / STAGING_DIR / run_id
        try:
            if self.db.get_server(name) is None:
                raise ClusterError("the server was deleted")
            awake = self.servers.state_of(name) in ("awake", "waking", "stopped")
            args = [
                "--server-state", "awake" if awake else "asleep",
                "--thumbnail-size", str(self.settings.thumbnail_size),
            ]  # fmt: skip
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            self.cluster.run_world_thumbnail(
                name,
                run_id,
                image=self.settings.thumbnail_image,
                args=args,
                dest=staging,
                timeout=self.settings.thumbnail_timeout_seconds,
                on_phase=lambda phase: self.db.update_thumbnail(name, run_id, phase=phase),
            )
            meta = clean_meta(json.loads((staging / "thumbnail.json").read_text(encoding="utf-8")))
            if self.db.get_server(name) is None:
                raise ClusterError("the server was deleted")
            final = self._dir(name)
            final.mkdir(parents=True, exist_ok=True)
            # One file at a time, each replaced in one step: a reader sees the
            # old picture or the new one, never half of either.
            for file in (*FILES, "thumbnail.json"):
                os.replace(staging / file, final / file)
            if not self.db.update_thumbnail(
                name, run_id, status="ready", phase=None, error=None,
                rendered_at=now_iso(), meta=meta,
            ):  # fmt: skip
                # The server was deleted while the files moved: nothing may stay.
                shutil.rmtree(final, ignore_errors=True)
                return
            self.db.record(row.tenant_id, name, "thumbnail.ready", run_id)
        except ClusterError as exc:
            self._failed(row, run_id, str(exc))
        except Exception as exc:  # the worker must not die silently
            log.exception("thumbnail run %s of %s failed unexpectedly", run_id, name)
            self._failed(row, run_id, f"the picture could not be drawn ({type(exc).__name__})")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _failed(self, row: ThumbnailRow, run_id: str, reason: str) -> None:
        self.db.update_thumbnail(
            row.server, run_id, status="failed", phase=None, error=reason[:300]
        )
        self.db.record(row.tenant_id, row.server, "thumbnail.failed", f"{run_id}: {reason[:300]}")

    # --- housekeeping -------------------------------------------------------------

    def recover_interrupted(self) -> None:
        """Runs a previous process left queued or running: mark them failed and
        remove their pods (the pod deadline would anyway)."""
        for row in self.db.thumbnails_with_status(*ACTIVE):
            if row.run_id:
                self._failed(row, row.run_id, "interrupted; please try again")
            try:
                self.cluster.delete_world_export_pods(row.server)
            except ClusterError as exc:
                log.warning("could not remove thumbnail pods of %s: %s", row.server, exc)
        shutil.rmtree(self.root / STAGING_DIR, ignore_errors=True)

    def sweep_if_due(self) -> None:
        now = datetime.now(UTC)
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_EVERY:
            self._last_sweep = now
            self.sweep()

    def sweep(self) -> int:
        """Remove thumbnails whose server is gone (or changed hands) and
        folders no row knows about. Returns how many were removed."""
        removed = 0
        known: set[str] = set()
        for row in self.db.all_thumbnails():
            server = self.db.get_server(row.server)
            if server is None or server.tenant_id != row.tenant_id:
                if row.status not in ACTIVE:
                    self.db.delete_thumbnail(row.server)
                    shutil.rmtree(self._dir(row.server), ignore_errors=True)
                    removed += 1
                    continue
            known.add(row.server)
        if self.root.is_dir():
            for entry in self.root.iterdir():
                if entry.name.startswith(".") or entry.name in known:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        return removed

    def forget_server(self, name: str) -> None:
        """A deleted server's thumbnail goes with it (RFC 0034 §5)."""
        self.db.delete_thumbnail(name)
        shutil.rmtree(self._dir(name), ignore_errors=True)

    def limits(self) -> dict:
        return {
            "thumbnail_mode": self.mode,
            "thumbnail_daily_limit": self.settings.thumbnail_daily_limit,
        }


__all__ = [
    "ThumbnailConflict",
    "ThumbnailDisabled",
    "ThumbnailLimit",
    "ThumbnailNotFound",
    "ThumbnailService",
]
