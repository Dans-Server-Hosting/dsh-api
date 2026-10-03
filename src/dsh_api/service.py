"""The server lifecycle: create, read, wake, delete — composed from the backend."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from dsh_api.cluster import (
    ClusterBackend,
    ClusterError,
    Credentials,
    ReleaseSpec,
    StatefulSetStatus,
    WrapperStatus,
)
from dsh_api.config import Settings
from dsh_api.db import Database, ServerRow
from dsh_api.mojang import UuidResolver

log = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

STATES = ("provisioning", "asleep", "waking", "awake", "stopped", "failed", "deleting")

WAKE_GRACE = timedelta(minutes=3)
"""How long after a scale-up a not-yet-running game still counts as waking."""


def server_state(
    sts: StatefulSetStatus, wrapper: WrapperStatus | None, now: datetime | None = None
) -> str:
    """One of ``STATES`` from the StatefulSet and what the wrapper reports.

    The pod's readiness probe is the wrapper's own health, not the game's, so
    a ready pod whose wrapper says the process is not running is ``stopped``
    (the owner pressed Stop in the dashboard, or the game crashed) unless the
    server was scaled up moments ago and is still booting, which is ``waking``.
    Without an answer from the wrapper the replica-based reading stands.
    """
    if wrapper is None or sts.state in ("failed", "asleep"):
        return sts.state
    if sts.ready_replicas < 1:
        return "waking"
    if wrapper.running:
        return "awake"
    scaled_at = sts.scaled_at
    if scaled_at is None:
        return "stopped"
    if scaled_at.tzinfo is None:
        scaled_at = scaled_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return "waking" if now - scaled_at < WAKE_GRACE else "stopped"


def validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ValueError("name must be lowercase letters, digits and dashes, 2-31 characters")
    if "omcsi" in name:
        raise ValueError("name must not contain 'omcsi'")
    return name


class JobRunner(Protocol):
    """Where the provisioning and delete steps run after the request has answered.

    ``concurrent.futures.ThreadPoolExecutor`` is the real one; the tests pass
    one that queues the job and runs it when they say so.
    """

    def submit(self, fn: Callable[..., object], /, *args: object) -> object: ...


class NameTaken(Exception):
    pass


class CreateInProgress(Exception):
    """The tenant already has a create in flight (or the server asked about is it)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a server is already being created: {name}")
        self.name = name


class TenantAtCap(Exception):
    pass


class ServerNotFound(Exception):
    pass


class PlayersOnline(Exception):
    def __init__(self, count: int) -> None:
        super().__init__(f"{count} player(s) online")
        self.count = count


@dataclass(frozen=True)
class ServerView:
    name: str
    state: str
    hostname: str
    sslip_hostname: str
    dashboard_url: str
    motd: str
    operator_username: str | None
    created_at: str
    last_woken_at: str | None
    players_online: int | None


class ServerService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        cluster: ClusterBackend,
        uuids: UuidResolver,
        jobs: JobRunner,
    ) -> None:
        self.settings = settings
        self.db = db
        self.cluster = cluster
        self.uuids = uuids
        self.jobs = jobs

    def recover_interrupted(self) -> None:
        """Rows left ``provisioning`` or ``deleting`` by a process that died mid-job.

        The job ran in this process, so nothing is still working on them. An
        interrupted create is marked failed (the namespace and release stay for
        inspection) so the tenant can see what happened and DELETE to free the
        slot; an interrupted delete is put back as it was.
        """
        for row in self.db.list_with_status("provisioning"):
            self.db.set_status(row.name, "failed")
            self.db.record(row.tenant_id, row.name, "create.interrupted", "the API restarted")
        # A delete cut short goes back to what the row was before it, so the
        # tenant sees the server again and can DELETE it once more (a retry
        # reuses a backup already taken when the world volume is gone).
        for row in self.db.list_with_status("deleting"):
            self.db.set_status(row.name, self._status_before_delete(row.name))
            self.db.record(row.tenant_id, row.name, "delete.interrupted", "the API restarted")

    # --- reads ---------------------------------------------------------------

    def _state(self, name: str) -> str:
        sts = self.cluster.statefulset_status(name)
        wrapper = self.cluster.wrapper_status(name) if sts.state in ("waking", "awake") else None
        return server_state(sts, wrapper)

    def _view(self, row: ServerRow, with_players: bool) -> ServerView:
        # While the create is in flight, or after it failed, the row is the
        # answer: the release may not exist yet, and a failed one stays failed
        # until it is removed, whatever the cluster would say about it.
        state = row.status if row.status != "ready" else self._state(row.name)
        players = (
            self.cluster.players_online(row.name) if with_players and state == "awake" else None
        )
        return ServerView(
            name=row.name,
            state=state,
            hostname=row.hostname,
            sslip_hostname=self.settings.sslip_hostname(row.name),
            dashboard_url=f"https://{row.hostname}/",
            motd=row.motd,
            operator_username=row.operator_username,
            created_at=row.created_at,
            last_woken_at=row.last_woken_at,
            players_online=players,
        )

    def _owned(self, tenant_id: str, name: str) -> ServerRow:
        row = self.db.get_server(name)
        if row is None or row.tenant_id != tenant_id:
            raise ServerNotFound(name)
        return row

    def list(self, tenant_id: str) -> list[ServerView]:
        return [self._view(row, with_players=False) for row in self.db.list_servers(tenant_id)]

    def get(self, tenant_id: str, name: str) -> ServerView:
        return self._view(self._owned(tenant_id, name), with_players=True)

    # --- writes --------------------------------------------------------------

    def create(
        self, tenant_id: str, name: str, motd: str | None, operator_username: str | None
    ) -> tuple[ServerView, str]:
        """Reserve the server and start provisioning it; returns the view (state
        ``provisioning``) and the one-time admin password.

        The cluster steps take minutes, so they run on ``jobs`` after this
        returns; ``get``/``list`` report ``provisioning`` from the row until
        the job has moved it on.
        """
        pending = self.db.provisioning_server(tenant_id)
        if pending is not None:
            raise CreateInProgress(pending.name)
        if self.db.count_servers(tenant_id) >= self.settings.max_servers_per_tenant:
            raise TenantAtCap(tenant_id)
        if self.db.get_server(name) is not None or self.cluster.namespace_exists(name):
            raise NameTaken(name)
        operator_uuid = self.uuids.resolve(operator_username) if operator_username else None
        motd = motd or f"{name} on Dan's Server Hosting"
        spec = ReleaseSpec(
            name=name,
            hostname=self.settings.hostname(name),
            sslip_hostname=self.settings.sslip_hostname(name),
            motd=motd,
            operator_name=operator_username,
            operator_uuid=operator_uuid,
            default_plugins=self.settings.default_plugins,
        )
        creds = Credentials.generate()
        row = self.db.insert_server(
            name, tenant_id, spec.hostname, motd, operator_username, status="provisioning"
        )
        self.db.record(tenant_id, name, "create.requested")
        self.jobs.submit(self._provision, tenant_id, spec, creds)
        return self._view(row, with_players=False), creds.admin_password

    def _provision(self, tenant_id: str, spec: ReleaseSpec, creds: Credentials) -> None:
        """The cluster steps, off the request thread. Never raises: the outcome
        is written to the row and the event log, which is where the tenant
        (and the operator) read it from."""
        name = spec.name
        try:
            self.cluster.create_tenant_namespace(name)
            # Before anything namespaced: the API's own permissions in t-<name>.
            self.cluster.grant_tenant_access(name)
            self.cluster.create_credentials(name, creds)
            # The profile installs the wrapper asleep and the webapp waits for
            # it, so the release is installed without waiting, the wrapper is
            # woken once, and only then are the rollouts waited for.
            self.cluster.install_release(spec, creds)
            self.cluster.scale_wrapper(name, 1)
            self.cluster.wait_for_rollout(name)
        except ClusterError as exc:
            self._provision_failed(tenant_id, name, str(exc))
            return
        except Exception as exc:  # the thread must not die silently
            log.exception("provisioning %s failed unexpectedly", name)
            self._provision_failed(tenant_id, name, f"{type(exc).__name__}: {exc}")
            return
        self.db.mark_woken(name)
        self.db.set_status(name, "ready")
        self.db.record(tenant_id, name, "create.done")

    def _provision_failed(self, tenant_id: str, name: str, detail: str) -> None:
        # The namespace and release are left as they are for inspection; the
        # tenant's slot is held until they DELETE the failed server.
        self.db.set_status(name, "failed")
        self.db.record(tenant_id, name, "create.failed", detail)

    def wake(self, tenant_id: str, name: str) -> ServerView:
        """Scale an asleep wrapper up, or start the game on a stopped one.

        A waking or awake server is left alone, as is a failed one (scaling a
        crash-looping pod does nothing useful, and a StatefulSet that is gone
        cannot be scaled at all; the failure is reported instead).
        """
        row = self._owned(tenant_id, name)
        if row.status != "ready":
            # Still being created, or the create failed: nothing to scale yet.
            return self._view(row, with_players=False)
        sts = self.cluster.statefulset_status(name)
        if sts.state == "failed":
            # A missing StatefulSet has zero replicas too; the scale below would
            # only turn what GET reports as ``failed`` into a 502 from kubectl.
            return self._view(row, with_players=False)
        if sts.replicas < 1:
            self.cluster.scale_wrapper(name, 1)
            self.db.mark_woken(name)
            self.db.record(tenant_id, name, "wake")
        elif server_state(sts, self.cluster.wrapper_status(name)) == "stopped":
            self.cluster.start_wrapper(name)
            self.db.mark_woken(name)
            self.db.record(tenant_id, name, "wake.start")
        return self._view(self.db.get_server(name) or row, with_players=False)

    def delete(self, tenant_id: str, name: str, force: bool = False) -> ServerView:
        """Accept the delete and start it; returns the view (state ``deleting``).

        Backup, uninstall and namespace removal take minutes for a large world,
        so they run on ``jobs`` after this returns, and ``get``/``list`` report
        ``deleting`` until the row is gone. What can be refused at once is:
        a server whose create is still in flight (the job is still writing to
        it), and one with players online unless ``force``. A second DELETE
        while one is running answers with the server as it is.
        """
        row = self._owned(tenant_id, name)
        if row.status == "provisioning":
            raise CreateInProgress(name)
        if row.status == "deleting":
            return self._view(row, with_players=False)
        # The pod being up is what decides how the world is read (exec versus a
        # PVC reader); a stopped game still has its pod.
        awake = self.cluster.statefulset_status(name).state == "awake"
        if awake and not force:
            online = self.cluster.players_online(name) or 0
            if online > 0:
                raise PlayersOnline(online)
        if self.db.begin_delete(name, row.status):
            # The detail is the status to go back to if the delete fails.
            self.db.record(tenant_id, name, "delete.requested", row.status)
            self.jobs.submit(self._remove, tenant_id, name, row.status, awake, force)
        return self._view(self.db.get_server(name) or row, with_players=False)

    def _remove(self, tenant_id: str, name: str, prior: str, awake: bool, force: bool) -> None:
        """The delete's cluster steps, off the request thread. Never raises:
        a failure puts the row back to ``prior`` with the reason recorded
        (``backup.failed`` or ``delete.failed``), and the tenant may retry."""
        try:
            try:
                self._back_up_for_delete(tenant_id, name, prior, awake, force)
            except ClusterError as exc:
                self._delete_failed(tenant_id, name, prior, "backup.failed", str(exc))
                return
            self.cluster.uninstall_release(name)
            self.cluster.delete_namespace(name)
        except ClusterError as exc:
            self._delete_failed(tenant_id, name, prior, "delete.failed", str(exc))
            return
        except Exception as exc:  # the thread must not die silently
            log.exception("deleting %s failed unexpectedly", name)
            self._delete_failed(
                tenant_id, name, prior, "delete.failed", f"{type(exc).__name__}: {exc}"
            )
            return
        self.db.delete_server(name)
        self.db.record(tenant_id, name, "delete")

    def _delete_failed(self, tenant_id: str, name: str, prior: str, kind: str, detail: str) -> None:
        self.db.set_status(name, prior)
        self.db.record(tenant_id, name, kind, detail)

    def _back_up_for_delete(
        self, tenant_id: str, name: str, prior: str, awake: bool, force: bool
    ) -> str | None:
        """Take the world's backup, or decide it may be done without.

        Raises ``ClusterError`` when the world must not be removed unbacked-up.
        One whose create failed may never have had a pod or a volume, so a
        backup that cannot be taken is skipped rather than fatal — as it is
        with ``force``.
        """
        try:
            backup = str(self.cluster.backup_world(name, awake=awake))
            self.db.record(tenant_id, name, "backup", backup)
            return backup
        except ClusterError as exc:
            prior_backup = self._backup_still_on_disk(name)
            if prior_backup is not None and self._world_backup_source_is_gone(exc):
                # A retry of a delete that failed after its backup: helm took
                # the world's volume (or the pod) with the release on the way
                # down, so there is nothing left to read, and that backup is
                # the backup (example, 2026-09-19: helm could not remove the
                # chart's Role, and every retry 502'd on the missing PVC).
                self.db.record(tenant_id, name, "backup.reused", prior_backup)
                return prior_backup
            if not force and prior != "failed":
                # Without force the world is never removed unbacked-up. With
                # force, or for a failed create (a release that never installed
                # has nothing to read), it may be.
                raise
            self.db.record(tenant_id, name, "backup.skipped", str(exc))
            return None

    def _status_before_delete(self, name: str) -> str:
        for event in reversed(self.db.events(name)):
            if event["kind"] == "delete.requested":
                return event["detail"] if event["detail"] in ("ready", "failed") else "ready"
        return "ready"

    @staticmethod
    def _world_backup_source_is_gone(exc: ClusterError) -> bool:
        text = str(exc).lower()
        return "not found" in text and (
            "persistentvolumeclaims" in text or "persistentvolumeclaim/" in text
        )

    def _backup_still_on_disk(self, name: str) -> str | None:
        """The newest recorded backup path for this server that still exists on disk.

        Names are reused once a server is deleted, by any tenant, and backups
        outlive their server; the scan stops at the last ``delete`` so an
        earlier server's world is never handed out as this one's.
        """
        for event in reversed(self.db.events(name)):
            if event["kind"] == "delete":
                break
            if event["kind"] in {"backup", "backup.reused"}:
                if Path(event["detail"]).exists():
                    return event["detail"]
        return None
