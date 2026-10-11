"""Installing and removing plugins on a tenant's server (RFC 0020).

The API owns each server's selection; changing it rewrites the server's
ConfigMap and nothing else. The change takes effect the next time the
wrapper pod starts: for an asleep server that is its next wake, for an awake
one ``apply`` restarts the pod (asking first when players are online).
"""

from __future__ import annotations

from dsh_api.catalog import CatalogPlugin, CatalogUnavailable, PluginCatalog
from dsh_api.cluster import ClusterBackend
from dsh_api.config import Settings
from dsh_api.db import Database, SelectedPlugin
from dsh_api.selection import default_selection, selection_json, selection_sha256
from dsh_api.service import PlayersOnline, ServerService

RESTART_WARNING = "say This server is restarting in a moment to change its plugins."

# What the boot script reports per slug, as the portal shows it.
_ON_DISK = {"installed", "present", "adopted"}


class PluginSelectionDisabled(Exception):
    """The service has not switched plugin selection on yet."""


class UnknownPlugin(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(f"no plugin '{slug}' in the catalog")
        self.slug = slug


class PluginNotSelected(Exception):
    pass


class PluginRequired(Exception):
    def __init__(self, slug: str, dependents: list[str]) -> None:
        super().__init__(f"{slug} is required by {', '.join(dependents)}")
        self.slug = slug
        self.dependents = dependents


class ServerBusy(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(f"the server is {state}")
        self.state = state


class PluginService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        cluster: ClusterBackend,
        servers: ServerService,
        catalog: PluginCatalog,
    ) -> None:
        self.settings = settings
        self.db = db
        self.cluster = cluster
        self.servers = servers
        self.catalog = catalog

    def _require_enabled(self) -> None:
        if not self.settings.plugin_selection:
            raise PluginSelectionDisabled()

    # --- reads ---------------------------------------------------------------

    def list_catalog(self) -> list[dict]:
        self._require_enabled()
        return [p.to_json() for p in sorted(self.catalog.plugins().values(), key=_title_key)]

    def _rows(self, name: str) -> list[SelectedPlugin]:
        """The server's selection; one that has never had one (created before
        selections existed) starts from the default plugins, which is what is
        on its disk."""
        self.db.seed_plugins(name, default_selection(self.settings.default_plugins))
        return self.db.selected_plugins(name)

    def view(self, tenant_id: str, name: str) -> dict:
        self._require_enabled()
        server = self.servers.get(tenant_id, name)
        rows = self._rows(name)
        state = self.db.plugin_state(name)
        written = state.written_sha256 if state else None
        applied_hash = state.applied_sha256 if state else None
        outcomes: dict = {}
        if server.state in ("awake", "stopped"):
            report = self.cluster.plugins_applied(name)
            if report is not None:
                applied_hash = str(report.get("selection_sha256") or "") or applied_hash
                outcomes = report.get("plugins") if isinstance(report.get("plugins"), dict) else {}
                if applied_hash:
                    self.db.set_plugin_hashes(name, applied=applied_hash)
        pending = written is not None and written != applied_hash
        titles = self._titles()
        required_by = {
            r.slug: sorted(o.slug for o in rows if r.slug in o.requires and o.slug != r.slug)
            for r in rows
        }
        plugins = []
        for r in rows:
            # While a change waits for the next start, a plugin the last boot
            # put on disk is still installed; anything else is pending.
            outcome = outcomes.get(r.slug)
            if isinstance(outcome, str) and outcome.startswith("failed") and not pending:
                status = outcome
            elif outcome in _ON_DISK or written is None or (outcome is None and not pending):
                status = "installed"
            else:
                status = "pending"
            plugins.append(
                {
                    "slug": r.slug,
                    "title": titles.get(r.slug, r.slug),
                    "version": r.tag,
                    "added_by": r.added_by,
                    "requires": list(r.requires),
                    "required_by": required_by[r.slug],
                    "status": status,
                }
            )
        return {
            "server": name,
            "state": server.state,
            "players_online": server.players_online,
            "pending": pending,
            "plugins": plugins,
        }

    def _titles(self) -> dict[str, str]:
        try:
            return {slug: p.title for slug, p in self.catalog.plugins().items()}
        except CatalogUnavailable:
            return {}

    # --- writes --------------------------------------------------------------

    def _ready(self, tenant_id: str, name: str) -> None:
        row = self.servers.owned(tenant_id, name)
        if row.status != "ready":
            raise ServerBusy({"provisioning": "being created"}.get(row.status, row.status))

    def _write(self, name: str, rows: list[SelectedPlugin]) -> None:
        """The ConfigMap first: if it cannot be written, the selection is unchanged."""
        text = selection_json(rows)
        self.cluster.write_plugin_selection(name, text)
        self.db.set_plugin_hashes(name, written=selection_sha256(text))

    def add(self, tenant_id: str, name: str, slug: str) -> dict:
        """Select ``slug`` and whatever it requires that is not selected yet,
        each pinned to the catalog's latest release now."""
        self._require_enabled()
        self._ready(tenant_id, name)
        rows = self._rows(name)
        selected = {r.slug for r in rows}
        catalog = self.catalog.plugins()
        if slug not in catalog:
            raise UnknownPlugin(slug)
        new: list[SelectedPlugin] = []
        queue: list[tuple[str, str]] = [(slug, "tenant")]
        while queue:
            current, added_by = queue.pop(0)
            if current in selected:
                continue
            entry = catalog.get(current)
            if entry is None:
                raise UnknownPlugin(current)
            new.append(_selected_from(entry, added_by))
            selected.add(current)
            queue.extend((dep, "dependency") for dep in entry.requires)
        if new:
            self._write(name, rows + new)
            self.db.add_plugins(name, new)
            self.db.record(tenant_id, name, "plugins.add", ",".join(r.slug for r in new))
        return {"added": [r.slug for r in new], **self.view(tenant_id, name)}

    def remove(self, tenant_id: str, name: str, slug: str) -> dict:
        """Deselect ``slug``. Refused while another selected plugin requires it.
        The jar goes at the next start; its data folder is always kept."""
        self._require_enabled()
        self._ready(tenant_id, name)
        rows = self._rows(name)
        if slug not in {r.slug for r in rows}:
            raise PluginNotSelected(slug)
        dependents = sorted(r.slug for r in rows if slug in r.requires and r.slug != slug)
        if dependents:
            raise PluginRequired(slug, dependents)
        remaining = [r for r in rows if r.slug != slug]
        self._write(name, remaining)
        self.db.remove_plugin(name, slug)
        self.db.record(tenant_id, name, "plugins.remove", slug)
        # Dependencies nothing selected needs any more are offered for removal,
        # never removed unasked.
        unused = sorted(
            r.slug
            for r in remaining
            if r.added_by == "dependency" and not any(r.slug in o.requires for o in remaining)
        )
        return {"removed": slug, "unused_dependencies": unused, **self.view(tenant_id, name)}

    def apply(self, tenant_id: str, name: str, confirm_players: bool) -> dict:
        """Make the selection take effect. Asleep: nothing to do, it applies at
        the next wake. Awake or stopped: restart the pod, which re-runs the boot
        script; with players online only when ``confirm_players``."""
        self._require_enabled()
        self._ready(tenant_id, name)
        state = self.servers.get(tenant_id, name).state
        if state == "asleep":
            return {"applies": "next_start"}
        if state not in ("awake", "stopped"):
            raise ServerBusy(state)
        online = (self.cluster.players_online(name) or 0) if state == "awake" else 0
        if online > 0 and not confirm_players:
            raise PlayersOnline(online)
        if online > 0:
            self.cluster.send_command(name, RESTART_WARNING)
        self.cluster.restart_wrapper_pod(name)
        self.db.record(tenant_id, name, "plugins.apply", f"players={online}")
        return {"applies": "restarting"}


def _selected_from(entry: CatalogPlugin, added_by: str) -> SelectedPlugin:
    return SelectedPlugin(
        slug=entry.slug,
        tag=entry.tag,
        filename=entry.filename,
        url=entry.url,
        requires=entry.requires,
        added_by=added_by,
    )


def _title_key(plugin: CatalogPlugin) -> str:
    return plugin.title.lower()
