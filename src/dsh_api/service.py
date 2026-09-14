"""The server lifecycle: create, read, wake, delete — composed from the backend."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

STATES = ("asleep", "waking", "awake", "stopped", "failed")

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


class NameTaken(Exception):
    pass


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
        self, settings: Settings, db: Database, cluster: ClusterBackend, uuids: UuidResolver
    ) -> None:
        self.settings = settings
        self.db = db
        self.cluster = cluster
        self.uuids = uuids

    # --- reads ---------------------------------------------------------------

    def _state(self, name: str) -> str:
        sts = self.cluster.statefulset_status(name)
        wrapper = self.cluster.wrapper_status(name) if sts.state in ("waking", "awake") else None
        return server_state(sts, wrapper)

    def _view(self, row: ServerRow, with_players: bool) -> ServerView:
        state = self._state(row.name)
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
        """Provision a server; returns the view and the one-time admin password."""
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
        row = self.db.insert_server(name, tenant_id, spec.hostname, motd, operator_username)
        self.db.record(tenant_id, name, "create.requested")
        try:
            self.cluster.create_tenant_namespace(name)
            self.cluster.create_credentials(name, creds)
            # The profile installs the wrapper asleep and the webapp waits for
            # it, so the release is installed without waiting, the wrapper is
            # woken once, and only then are the rollouts waited for.
            self.cluster.install_release(spec, creds)
            self.cluster.scale_wrapper(name, 1)
            self.cluster.wait_for_rollout(name)
        except ClusterError as exc:
            self.db.record(tenant_id, name, "create.failed", str(exc))
            raise
        self.db.mark_woken(name)
        self.db.record(tenant_id, name, "create.done")
        return self._view(self.db.get_server(name) or row, with_players=False), creds.admin_password

    def wake(self, tenant_id: str, name: str) -> ServerView:
        """Scale an asleep wrapper up, or start the game on a stopped one.

        A waking or awake server is left alone, as is a failed one (scaling a
        crash-looping pod does nothing useful; the failure is reported instead).
        """
        row = self._owned(tenant_id, name)
        sts = self.cluster.statefulset_status(name)
        if sts.replicas < 1:
            self.cluster.scale_wrapper(name, 1)
            self.db.mark_woken(name)
            self.db.record(tenant_id, name, "wake")
        elif server_state(sts, self.cluster.wrapper_status(name)) == "stopped":
            self.cluster.start_wrapper(name)
            self.db.mark_woken(name)
            self.db.record(tenant_id, name, "wake.start")
        return self._view(self.db.get_server(name) or row, with_players=False)

    def delete(self, tenant_id: str, name: str, force: bool = False) -> str | None:
        """Back up, uninstall, remove the namespace. Returns the backup path."""
        self._owned(tenant_id, name)
        # The pod being up is what decides how the world is read (exec versus a
        # PVC reader); a stopped game still has its pod.
        awake = self.cluster.statefulset_status(name).state == "awake"
        if awake and not force:
            online = self.cluster.players_online(name) or 0
            if online > 0:
                raise PlayersOnline(online)
        backup: str | None
        try:
            backup = str(self.cluster.backup_world(name, awake=awake))
            self.db.record(tenant_id, name, "backup", backup)
        except ClusterError as exc:
            # Without force the world is never removed unbacked-up. With force
            # (a release that never installed has nothing to read) it may be.
            if not force:
                raise
            backup = None
            self.db.record(tenant_id, name, "backup.skipped", str(exc))
        self.cluster.uninstall_release(name)
        self.cluster.delete_namespace(name)
        self.db.delete_server(name)
        self.db.record(tenant_id, name, "delete")
        return backup
