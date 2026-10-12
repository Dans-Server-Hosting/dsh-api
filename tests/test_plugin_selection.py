"""Choosing plugins for a server (RFC 0020): the selection, the ConfigMap the
wrapper reconciles at boot, and applying a change."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.catalog import CatalogPlugin, FakeCatalog, extras_from_defaults
from dsh_api.cluster import WrapperStatus
from dsh_api.config import DEFAULT_PLUGINS, split_csv
from dsh_api.main import create_app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


def dpc(slug, title, tag, filename, requires=()):
    return CatalogPlugin(
        slug=slug,
        title=title,
        description=f"{title}.",
        tags=(),
        requires=tuple(requires),
        tag=tag,
        filename=filename,
        url=f"https://api.dansplugins.com/api/v1/plugins/{slug}/versions/{tag}/assets/{filename}/download",
        icon_url=None,
        project_url=f"https://dansplugins.com/resources/{slug}",
    )


CATALOG = [
    dpc("medieval-factions", "Medieval Factions", "v7.0.0", "medieval-factions-7.0.0-all.jar"),
    dpc("fiefs", "Fiefs", "v0.13.1", "Fiefs-0.13.1.jar", ["medieval-factions"]),
    dpc("currencies", "Currencies", "v2.1.0", "Currencies-2.1.0.jar", ["medieval-factions"]),
    dpc("dans-plugin-manager", "Dan's Plugin Manager", "v0.6.0", "DansPluginManager-0.6.0.jar"),
    *extras_from_defaults(split_csv(DEFAULT_PLUGINS)).values(),
]


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog(CATALOG)


@pytest.fixture
def make_client(settings, db, cluster, resolver, jobs, catalog):
    """An app over the shared db/cluster, with plugin selection on or off; two
    of them model the service before and after it is switched on."""

    def make(enabled: bool = True) -> TestClient:
        app = create_app(
            replace(settings, plugin_selection=enabled),
            db=db,
            cluster=cluster,
            validator=FakeValidator({"alice-token": "alice", "bob-token": "bob"}),
            uuids=resolver,
            jobs=jobs,
            catalog=catalog,
        )
        return TestClient(app)

    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client(True)


@pytest.fixture
def alpha(client, jobs, cluster) -> str:
    """alice's server, created with plugin selection on and booted once."""
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202, resp.text
    assert jobs.run() == 1
    cluster.boot("alpha")
    return "alpha"


def selection(cluster, name="alpha") -> dict:
    return {p["slug"]: p for p in json.loads(cluster.selections[name])["plugins"]}


def plugins(client, name="alpha", headers=ALICE) -> dict:
    resp = client.get(f"/api/v1/servers/{name}/plugins", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def by_slug(view) -> dict:
    return {p["slug"]: p for p in view["plugins"]}


# --- switched off ------------------------------------------------------------


def test_routes_answer_503_until_switched_on(make_client, jobs, cluster):
    off = make_client(False)
    assert off.get("/api/v1/catalog").status_code == 503
    off.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    jobs.run()
    assert off.get("/api/v1/servers/alpha/plugins", headers=ALICE).status_code == 503
    assert off.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE).status_code == 503
    # Created the old way: DEFAULT_PLUGINS on the release, no ConfigMap.
    assert cluster.releases["alpha"].default_plugins == split_csv(DEFAULT_PLUGINS)
    assert "alpha" not in cluster.selections


# --- a new server ------------------------------------------------------------


def test_a_new_server_starts_with_the_defaults_as_its_selection(alpha, client, cluster):
    # The defaults travel in the ConfigMap, not DEFAULT_PLUGINS, which only
    # ever adds: a default the tenant removed would come back at every boot.
    assert cluster.releases["alpha"].default_plugins == ()
    assert set(selection(cluster)) == {"dans-plugin-manager", "viaversion", "viabackwards"}
    ops = [op[0] for op in cluster.ops]
    assert ops.index("write_plugin_selection") < ops.index("install_release")

    view = plugins(client)
    assert view["pending"] is False
    assert {p["status"] for p in view["plugins"]} == {"installed"}
    assert {p["added_by"] for p in view["plugins"]} == {"default"}
    assert by_slug(view)["viaversion"]["required_by"] == ["viabackwards"]


def test_catalog_lists_every_plugin_by_title(client):
    resp = client.get("/api/v1/catalog")
    assert resp.status_code == 200
    titles = [p["title"] for p in resp.json()]
    assert titles == sorted(titles, key=str.lower)
    fiefs = next(p for p in resp.json() if p["slug"] == "fiefs")
    assert fiefs["requires"] == ["medieval-factions"]
    assert fiefs["version"] == "v0.13.1"


# --- adding ------------------------------------------------------------------


def test_adding_a_plugin_brings_its_dependency_pinned_and_pending(alpha, client, cluster):
    resp = client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == ["fiefs", "medieval-factions"]
    assert body["pending"] is True

    view = by_slug(body)
    assert view["fiefs"]["added_by"] == "tenant"
    assert view["medieval-factions"]["added_by"] == "dependency"
    assert view["medieval-factions"]["required_by"] == ["fiefs"]
    assert view["fiefs"]["status"] == "pending"
    # What the last boot put on disk is still installed meanwhile.
    assert view["dans-plugin-manager"]["status"] == "installed"

    written = selection(cluster)["medieval-factions"]
    assert written["filename"] == "medieval-factions-7.0.0-all.jar"
    assert written["tag"] == "v7.0.0"
    assert written["url"].startswith("https://api.dansplugins.com/")


def test_adding_twice_changes_nothing(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    writes = [op for op in cluster.ops if op[0] == "write_plugin_selection"]
    resp = client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.json()["added"] == []
    assert [op for op in cluster.ops if op[0] == "write_plugin_selection"] == writes


def test_a_selected_version_is_kept_when_the_catalog_moves_on(alpha, client, cluster, catalog):
    client.put("/api/v1/servers/alpha/plugins/medieval-factions", headers=ALICE)
    catalog.entries["medieval-factions"] = dpc(
        "medieval-factions", "Medieval Factions", "v7.1.0", "medieval-factions-7.1.0-all.jar"
    )
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert selection(cluster)["medieval-factions"]["tag"] == "v7.0.0"


def test_an_unknown_plugin_is_404_and_changes_nothing(alpha, client, cluster):
    before = cluster.selections["alpha"]
    resp = client.put("/api/v1/servers/alpha/plugins/nope", headers=ALICE)
    assert resp.status_code == 404
    assert cluster.selections["alpha"] == before


def test_a_failed_configmap_write_leaves_the_selection_unchanged(alpha, client, cluster):
    cluster.fail_on.add("write_plugin_selection")
    resp = client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.status_code == 502
    cluster.fail_on.clear()
    assert "fiefs" not in by_slug(plugins(client))


def test_catalog_down_refuses_adds_but_the_view_still_works(alpha, client, catalog):
    catalog.available = False
    assert client.get("/api/v1/catalog").status_code == 503
    assert client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE).status_code == 503
    view = plugins(client)
    assert by_slug(view)["viaversion"]["title"] == "viaversion"  # no title without the catalog


# --- removing ----------------------------------------------------------------


def test_a_required_plugin_cannot_be_removed(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    before = cluster.selections["alpha"]
    resp = client.delete("/api/v1/servers/alpha/plugins/medieval-factions", headers=ALICE)
    assert resp.status_code == 409
    assert resp.json()["required_by"] == ["fiefs"]
    assert cluster.selections["alpha"] == before


def test_removing_the_last_dependent_offers_the_dependency_for_removal(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    resp = client.delete("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unused_dependencies"] == ["medieval-factions"]
    # Offered, not removed.
    assert "medieval-factions" in selection(cluster)
    assert "fiefs" not in selection(cluster)


def test_a_dependency_still_needed_is_not_offered(alpha, client):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    client.put("/api/v1/servers/alpha/plugins/currencies", headers=ALICE)
    resp = client.delete("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.json()["unused_dependencies"] == []


def test_removing_a_plugin_that_is_not_selected_is_404(alpha, client):
    assert client.delete("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE).status_code == 404


def test_a_default_plugin_can_be_removed(alpha, client, cluster):
    resp = client.delete("/api/v1/servers/alpha/plugins/dans-plugin-manager", headers=ALICE)
    assert resp.status_code == 200
    assert "dans-plugin-manager" not in selection(cluster)


# --- applying ----------------------------------------------------------------


def test_an_asleep_server_applies_at_its_next_wake(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    cluster.scale_wrapper("alpha", 0)
    resp = client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE)
    assert resp.status_code == 202
    assert resp.json() == {"applies": "next_start"}
    assert "restart_wrapper_pod" not in [op[0] for op in cluster.ops]
    assert plugins(client)["pending"] is True


def test_players_online_need_confirming_then_get_a_warning(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    cluster.players["alpha"] = 3
    resp = client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE)
    assert resp.status_code == 409
    assert resp.json()["players_online"] == 3
    assert "restart_wrapper_pod" not in [op[0] for op in cluster.ops]

    resp = client.post(
        "/api/v1/servers/alpha/plugins/apply", json={"confirm_players": True}, headers=ALICE
    )
    assert resp.status_code == 202
    assert resp.json() == {"applies": "restarting"}
    assert cluster.commands and cluster.commands[-1][1].startswith("say ")
    assert ("restart_wrapper_pod", "alpha") in cluster.ops


def test_no_players_restarts_without_a_warning(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    cluster.players["alpha"] = 0
    resp = client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE)
    assert resp.status_code == 202
    assert cluster.commands == []
    assert ("restart_wrapper_pod", "alpha") in cluster.ops


def test_after_the_restart_the_change_is_applied(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE)
    cluster.boot("alpha")
    view = plugins(client)
    assert view["pending"] is False
    assert by_slug(view)["fiefs"]["status"] == "installed"


def test_a_failed_install_is_reported(alpha, client, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE)
    cluster.boot("alpha")
    cluster.applied["alpha"]["plugins"]["fiefs"] = "failed: download"
    assert by_slug(plugins(client))["fiefs"]["status"] == "failed: download"


def test_a_server_still_being_created_is_409(client, jobs):
    client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    resp = client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    assert resp.status_code == 409
    jobs.run()


def test_a_waking_server_cannot_be_restarted(alpha, client, cluster):
    cluster.wrappers["alpha"] = replace(cluster.wrappers["alpha"], ready_replicas=0)
    assert client.post("/api/v1/servers/alpha/plugins/apply", headers=ALICE).status_code == 409


# --- ownership ---------------------------------------------------------------


def test_another_tenants_server_is_404(alpha, client):
    assert client.get("/api/v1/servers/alpha/plugins", headers=BOB).status_code == 404
    assert client.put("/api/v1/servers/alpha/plugins/fiefs", headers=BOB).status_code == 404
    assert client.delete("/api/v1/servers/alpha/plugins/viaversion", headers=BOB).status_code == 404
    assert client.post("/api/v1/servers/alpha/plugins/apply", headers=BOB).status_code == 404


# --- servers created before selection existed --------------------------------


def test_an_existing_server_gets_the_defaults_without_touching_the_cluster(
    make_client, jobs, cluster
):
    off = make_client(False)
    off.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    jobs.run()
    on = make_client(True)

    view = plugins(on)
    assert {p["slug"] for p in view["plugins"]} == {
        "dans-plugin-manager",
        "viaversion",
        "viabackwards",
    }
    assert view["pending"] is False
    assert {p["status"] for p in view["plugins"]} == {"installed"}
    assert "alpha" not in cluster.selections  # nothing written until something changes

    # Seeded once: removing everything does not bring the defaults back.
    for slug in ("viabackwards", "viaversion", "dans-plugin-manager"):
        assert on.delete(f"/api/v1/servers/alpha/plugins/{slug}", headers=ALICE).status_code == 200
    assert plugins(on)["plugins"] == []


def test_a_deleted_servers_selection_does_not_pass_to_the_next_one(alpha, client, jobs, cluster):
    client.put("/api/v1/servers/alpha/plugins/fiefs", headers=ALICE)
    cluster.wrapper_statuses["alpha"] = WrapperStatus(running=True)
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 202
    jobs.run()
    client.post("/api/v1/servers", json={"name": "alpha"}, headers=BOB)
    jobs.run()
    cluster.boot("alpha")
    assert "fiefs" not in by_slug(plugins(client, headers=BOB))
