"""Every handler against the fake cluster."""

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import Credentials, ReleaseSpec, WrapperStatus
from dsh_api.config import DEFAULT_PLUGINS, Settings
from dsh_api.main import create_app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}

# --- create ----------------------------------------------------------------


def test_create_provisions_like_the_operator_script(client, cluster, created):
    assert created["name"] == "alpha"
    assert created["hostname"] == "alpha.play.example.com"
    assert created["sslip_hostname"] == "alpha.203-0-113-10.sslip.io"
    assert created["dashboard_url"] == "https://alpha.play.example.com/"
    assert created["motd"] == "alpha on Dan's Server Hosting"
    assert created["state"] == "awake"  # woken after install, rollouts waited for
    assert created["last_woken_at"]
    assert created["admin_username"] == "admin"
    assert created["admin_password"] == cluster.credentials["alpha"].admin_password

    assert cluster.ops == [
        ("create_tenant_namespace", "alpha"),
        ("create_credentials", "alpha"),
        ("install_release", "alpha", False),  # the profile's replicas: 0 stands
        ("scale_wrapper", "alpha", 1),  # woken once, so the webapp can start
        ("wait_for_rollout", "alpha"),  # only now: helm itself must not wait
    ]
    assert "t-alpha" in cluster.namespaces
    assert cluster.releases["alpha"] == ReleaseSpec(
        name="alpha",
        hostname="alpha.play.example.com",
        sslip_hostname="alpha.203-0-113-10.sslip.io",
        motd="alpha on Dan's Server Hosting",
        default_plugins=(DEFAULT_PLUGINS,),  # Dan's Plugin Manager, like the operator's script
    )


def test_create_passes_the_configured_default_plugins(settings, db, cluster, resolver):
    settings = Settings(
        **{**vars(settings), "default_plugins": ("https://a/x.jar", "https://b/y.jar")}
    )
    app = create_app(
        settings,
        db=db,
        cluster=cluster,
        validator=FakeValidator({"alice-token": "alice"}),
        uuids=resolver,
    )
    resp = TestClient(app).post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 201, resp.text
    assert cluster.releases["alpha"].default_plugins == ("https://a/x.jar", "https://b/y.jar")


def test_create_honours_motd_and_operator(client, cluster):
    resp = client.post(
        "/api/v1/servers",
        json={"name": "beta", "motd": "hello", "operator_username": "Steve"},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    spec = cluster.releases["beta"]
    assert spec.motd == "hello"
    assert spec.operator_name == "Steve"
    assert spec.operator_uuid == "8667ba71-b85a-4004-af54-457a9734eed7"
    assert resp.json()["operator_username"] == "Steve"


def test_create_with_unknown_operator_is_422_and_provisions_nothing(client, cluster):
    resp = client.post(
        "/api/v1/servers", json={"name": "beta", "operator_username": "Nobody"}, headers=ALICE
    )
    assert resp.status_code == 422
    assert cluster.ops == []
    assert client.get("/api/v1/servers", headers=ALICE).json() == []


@pytest.mark.parametrize(
    "name", ["A", "Alpha", "1abc", "a", "has_underscore", "my-omcsi", "omcsi", "a" * 32, "ünï"]
)
def test_create_rejects_bad_names(client, cluster, name):
    resp = client.post("/api/v1/servers", json={"name": name}, headers=ALICE)
    assert resp.status_code == 422, resp.text
    assert cluster.ops == []


def test_create_same_name_twice_is_409(client, settings, created):
    settings_cap = settings.max_servers_per_tenant
    assert settings_cap == 1
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=BOB)
    assert resp.status_code == 409


def test_create_name_already_in_cluster_is_409(client, cluster):
    cluster.namespaces.add("t-taken")
    resp = client.post("/api/v1/servers", json={"name": "taken"}, headers=ALICE)
    assert resp.status_code == 409
    assert cluster.ops == []


def test_tenant_at_cap_is_403(client, created):
    resp = client.post("/api/v1/servers", json={"name": "second"}, headers=ALICE)
    assert resp.status_code == 403
    assert "cap" in resp.json()["detail"]


def test_admin_password_is_returned_exactly_once(client, created):
    assert "admin_password" in created
    one = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    many = client.get("/api/v1/servers", headers=ALICE).json()
    assert "admin_password" not in one
    assert "admin_password" not in many[0]
    dumped = (one | many[0]).values()
    creds = Credentials.generate()
    assert creds.rcon_password not in dumped


@pytest.mark.parametrize(
    "step, state_after", [("install_release", "failed"), ("wait_for_rollout", "waking")]
)
def test_create_failure_is_502_and_recorded(client, cluster, db, step, state_after):
    cluster.fail_on.add(step)
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 502
    assert step in resp.json()["detail"]
    kinds = [e["kind"] for e in db.events("alpha")]
    assert kinds == ["create.requested", "create.failed"]
    # The row stays so the tenant can see what became of it and remove it.
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == state_after


# --- read ------------------------------------------------------------------


def test_list_and_get_report_state_transitions(client, cluster, created):
    def state():
        return client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"]

    assert state() == "awake"
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=1, ready_replicas=0)
    assert state() == "waking"
    # The router puts the server to sleep when idle.
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=0)
    assert state() == "asleep"
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=1, failing=True)
    assert state() == "failed"
    cluster.wrappers.pop("alpha")
    assert state() == "failed"

    listed = client.get("/api/v1/servers", headers=ALICE).json()
    assert [s["name"] for s in listed] == ["alpha"]
    assert listed[0]["state"] == "failed"


def test_get_includes_player_count_when_awake(client, cluster, created):
    cluster.players["alpha"] = 3
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=1, ready_replicas=0)
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["players_online"] is None
    cluster.become_ready("alpha")
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["players_online"] == 3
    # The list is cheap: no ping per server.
    assert client.get("/api/v1/servers", headers=ALICE).json()[0]["players_online"] is None


def test_wrapper_status_state_table():
    assert WrapperStatus(exists=False).state == "failed"
    assert WrapperStatus(exists=True, replicas=0).state == "asleep"
    assert WrapperStatus(exists=True, replicas=1, ready_replicas=0).state == "waking"
    assert WrapperStatus(exists=True, replicas=1, ready_replicas=1).state == "awake"
    assert WrapperStatus(exists=True, replicas=1, ready_replicas=1, failing=True).state == "failed"


# --- two tenants -------------------------------------------------------------


def test_a_second_tenant_cannot_see_wake_or_delete_the_first_tenants_server(
    client, cluster, created
):
    assert client.get("/api/v1/servers", headers=BOB).json() == []
    assert client.get("/api/v1/servers/alpha", headers=BOB).status_code == 404
    assert client.post("/api/v1/servers/alpha/wake", headers=BOB).status_code == 404
    assert client.delete("/api/v1/servers/alpha", headers=BOB).status_code == 404
    assert client.delete("/api/v1/servers/alpha?force=true", headers=BOB).status_code == 404
    # Nothing happened to alice's server.
    assert "t-alpha" in cluster.namespaces
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200
    assert [op[0] for op in cluster.ops if op[0] in ("backup_world", "uninstall_release")] == []


def test_each_tenant_sees_only_their_own(client, settings, created):
    resp = client.post("/api/v1/servers", json={"name": "bobs"}, headers=BOB)
    assert resp.status_code == 201
    assert [s["name"] for s in client.get("/api/v1/servers", headers=ALICE).json()] == ["alpha"]
    assert [s["name"] for s in client.get("/api/v1/servers", headers=BOB).json()] == ["bobs"]


# --- wake --------------------------------------------------------------------


def test_wake_scales_the_wrapper_to_one(client, cluster, created):
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=0)
    before = created["last_woken_at"]
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["state"] == "waking"
    assert cluster.ops[-1] == ("scale_wrapper", "alpha", 1)
    assert resp.json()["last_woken_at"] >= before


def test_wake_when_already_up_does_nothing(client, cluster, created):
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["state"] == "awake"
    assert len(cluster.ops) == n_ops


def test_wake_unknown_server_is_404(client):
    assert client.post("/api/v1/servers/ghost/wake", headers=ALICE).status_code == 404


# --- delete ------------------------------------------------------------------


def test_delete_backs_up_before_the_namespace_goes(client, cluster, created, db):
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=0)  # asleep
    resp = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    backup = cluster.backup_dir / body["backup"].rsplit("/", 1)[1]
    assert backup.exists() and backup.stat().st_size > 0
    assert backup.name.startswith("alpha-") and backup.name.endswith(".tar.gz")

    ops = [op[0] for op in cluster.ops]
    assert (
        ops.index("backup_world") < ops.index("uninstall_release") < ops.index("delete_namespace")
    )
    assert ("backup_world", "alpha", False) in cluster.ops  # asleep: PVC reader path
    assert "t-alpha" not in cluster.namespaces
    assert "alpha" not in cluster.releases
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 404
    assert client.get("/api/v1/servers", headers=ALICE).json() == []
    assert [e["kind"] for e in db.events("alpha")][-2:] == ["backup", "delete"]


def test_delete_of_an_awake_server_uses_exec(client, cluster, created):
    cluster.players["alpha"] = 0
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 200
    assert ("backup_world", "alpha", True) in cluster.ops


def test_delete_refuses_while_players_are_online_unless_forced(client, cluster, created):
    cluster.players["alpha"] = 2
    resp = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert resp.status_code == 409
    assert "2 player" in resp.json()["detail"]
    assert "t-alpha" in cluster.namespaces
    assert not any(op[0] == "backup_world" for op in cluster.ops)

    resp = client.delete("/api/v1/servers/alpha?force=true", headers=ALICE)
    assert resp.status_code == 200
    assert "t-alpha" not in cluster.namespaces


def test_delete_without_a_backup_removes_nothing(client, cluster, created):
    cluster.fail_on.add("backup_world")
    resp = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert resp.status_code == 502
    assert "t-alpha" in cluster.namespaces
    assert "alpha" in cluster.releases
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200


def test_force_delete_tolerates_an_impossible_backup(client, cluster, created, db):
    cluster.fail_on.add("backup_world")
    resp = client.delete("/api/v1/servers/alpha?force=true", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["backup"] is None
    assert "t-alpha" not in cluster.namespaces
    assert "backup.skipped" in [e["kind"] for e in db.events("alpha")]


def test_after_delete_the_name_can_be_reused(client, cluster, created):
    cluster.wrappers["alpha"] = WrapperStatus(exists=True, replicas=0)
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 200
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 201


@pytest.mark.parametrize("motd", ["a,b", "k=v", "x[0]", "{y}", "back\\slash", "tab\there"])
def test_create_rejects_motd_that_would_confuse_helm(client, cluster, motd):
    resp = client.post("/api/v1/servers", json={"name": "alpha", "motd": motd}, headers=ALICE)
    assert resp.status_code == 422
    assert cluster.ops == []


@pytest.mark.parametrize("username", ["ab", "a" * 17, "bad name", "dash-y"])
def test_create_rejects_impossible_operator_usernames(client, cluster, username):
    resp = client.post(
        "/api/v1/servers", json={"name": "alpha", "operator_username": username}, headers=ALICE
    )
    assert resp.status_code == 422
    assert cluster.ops == []


def test_fake_install_keeps_an_awake_wrapper_awake(cluster):
    """Mirrors the real backend: a re-run over a wrapper at 1 replica must not sleep it."""
    spec = ReleaseSpec(name="alpha", hostname="h", sslip_hostname="s", motd="m")
    cluster.install_release(spec, Credentials.generate())
    assert cluster.ops[-1] == ("install_release", "alpha", False)
    assert cluster.wrapper_status("alpha").replicas == 0
    cluster.scale_wrapper("alpha", 1)
    cluster.install_release(spec, Credentials.generate())
    assert cluster.ops[-1] == ("install_release", "alpha", True)
    assert cluster.wrapper_status("alpha").replicas == 1
