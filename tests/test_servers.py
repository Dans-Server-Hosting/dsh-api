"""Every handler against the fake cluster."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import (
    ClusterError,
    Credentials,
    ReleaseSpec,
    StatefulSetStatus,
    WrapperStatus,
)
from dsh_api.config import DEFAULT_PLUGINS, Settings, split_csv
from dsh_api.db import Database
from dsh_api.main import create_app
from dsh_api.service import STATES, WAKE_GRACE, ServerService, server_state

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}

# --- create ----------------------------------------------------------------


def test_create_provisions_like_the_operator_script(client, cluster, created):
    assert created["name"] == "alpha"
    assert created["hostname"] == "alpha.play.example.com"
    assert created["sslip_hostname"] == "alpha.203-0-113-10.sslip.io"
    assert created["dashboard_url"] == "https://alpha.play.example.com/"
    assert created["motd"] == "alpha on Dan's Server Hosting"
    assert created["state"] == "provisioning"  # the 202: the cluster work is still ahead
    assert created["last_woken_at"] is None
    assert created["players_online"] is None
    assert created["admin_username"] == "admin"
    assert created["admin_password"] == cluster.credentials["alpha"].admin_password

    # The job has run (the fixture ran it): woken after install, rollouts waited for.
    after = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    assert after["state"] == "awake"
    assert after["last_woken_at"]

    assert cluster.ops == [
        ("create_tenant_namespace", "alpha"),
        ("grant_tenant_access", "alpha"),  # the API's own RoleBinding, before anything namespaced
        ("create_credentials", "alpha"),
        ("install_release", "alpha", False),  # the profile's replicas: 0 stands
        ("scale_wrapper", "alpha", 1),  # woken once, so the webapp can start
        ("wait_for_rollout", "alpha"),  # only now: helm itself must not wait
    ]
    assert "t-alpha" in cluster.namespaces
    assert "t-alpha" in cluster.bound
    assert cluster.releases["alpha"] == ReleaseSpec(
        name="alpha",
        hostname="alpha.play.example.com",
        sslip_hostname="alpha.203-0-113-10.sslip.io",
        motd="alpha on Dan's Server Hosting",
        default_plugins=split_csv(DEFAULT_PLUGINS),  # DPM + Via pair, like the operator script
    )


def test_create_passes_the_configured_default_plugins(settings, db, cluster, resolver, jobs):
    settings = Settings(
        **{**vars(settings), "default_plugins": ("https://a/x.jar", "https://b/y.jar")}
    )
    app = create_app(
        settings,
        db=db,
        cluster=cluster,
        validator=FakeValidator({"alice-token": "alice"}),
        uuids=resolver,
        jobs=jobs,
    )
    resp = TestClient(app).post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202, resp.text
    assert cluster.ops == []  # nothing has happened yet: the job is queued
    assert jobs.run() == 1
    assert cluster.releases["alpha"].default_plugins == ("https://a/x.jar", "https://b/y.jar")


def test_create_honours_motd_and_operator(client, cluster, jobs):
    resp = client.post(
        "/api/v1/servers",
        json={"name": "beta", "motd": "hello", "operator_username": "Steve"},
        headers=ALICE,
    )
    assert resp.status_code == 202, resp.text
    jobs.run()
    spec = cluster.releases["beta"]
    assert spec.motd == "hello"
    assert spec.operator_name == "Steve"
    assert spec.operator_uuid == "8667ba71-b85a-4004-af54-457a9734eed7"
    assert resp.json()["operator_username"] == "Steve"


def test_create_with_unknown_operator_is_422_and_provisions_nothing(client, cluster, jobs):
    resp = client.post(
        "/api/v1/servers", json={"name": "beta", "operator_username": "Nobody"}, headers=ALICE
    )
    assert resp.status_code == 422
    assert jobs.pending == [] and cluster.ops == []
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


# --- create, asynchronously ----------------------------------------------------


def test_create_answers_202_and_provisions_in_the_background(client, cluster, jobs, db):
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "provisioning"
    assert resp.json()["admin_password"]
    # Nothing has touched the cluster yet; the row is reserved and the job queued.
    assert cluster.ops == []
    assert "t-alpha" not in cluster.namespaces
    assert len(jobs.pending) == 1
    assert [e["kind"] for e in db.events("alpha")] == ["create.requested"]
    # Reads report the row's state without asking a cluster that has nothing yet.
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "provisioning"
    listed = client.get("/api/v1/servers", headers=ALICE).json()
    assert [(s["name"], s["state"]) for s in listed] == [("alpha", "provisioning")]

    assert jobs.run() == 1
    assert [op[0] for op in cluster.ops] == [
        "create_tenant_namespace",
        "grant_tenant_access",
        "create_credentials",
        "install_release",
        "scale_wrapper",
        "wait_for_rollout",
    ]
    body = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    assert body["state"] == "awake"
    assert body["last_woken_at"]
    assert [e["kind"] for e in db.events("alpha")] == ["create.requested", "create.done"]


def test_second_create_while_provisioning_is_409_not_the_cap(client, cluster, jobs):
    first = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert first.status_code == 202
    resp = client.post("/api/v1/servers", json={"name": "second"}, headers=ALICE)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": "a server is already being created for this account",
        "server": "alpha",
    }
    # The same name again is the same answer: the create in flight is the one that counts.
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 409
    assert len(jobs.pending) == 1  # nothing extra was queued
    # Another tenant is unaffected.
    assert client.post("/api/v1/servers", json={"name": "bobs"}, headers=BOB).status_code == 202
    # Once the job has run the answer is the ordinary cap.
    jobs.run()
    resp = client.post("/api/v1/servers", json={"name": "second"}, headers=ALICE)
    assert resp.status_code == 403
    assert "cap" in resp.json()["detail"]


def test_wake_and_delete_while_provisioning(client, cluster, jobs):
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202
    # Wake has nothing to scale yet: it answers with the row and touches nothing.
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202 and resp.json()["state"] == "provisioning"
    # Delete would race the job that is still writing to the server.
    resp = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert resp.status_code == 409
    assert "still being created" in resp.json()["detail"]
    assert cluster.ops == []
    jobs.run()
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "awake"


@pytest.mark.parametrize("step", ["install_release", "wait_for_rollout"])
def test_create_failure_is_reported_as_failed_and_recorded(client, cluster, jobs, db, step):
    cluster.fail_on.add(step)
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202  # the failure is still ahead
    jobs.run()
    events = db.events("alpha")
    assert [e["kind"] for e in events] == ["create.requested", "create.failed"]
    assert step in events[-1]["detail"]
    # The row stays (holding the slot) so the tenant can see what became of it
    # and remove it; whatever the cluster says, the create failed.
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "failed"
    assert client.get("/api/v1/servers", headers=ALICE).json()[0]["state"] == "failed"
    assert client.post("/api/v1/servers", json={"name": "second"}, headers=ALICE).status_code == 403
    # Nothing was torn down: the namespace is there for the operator to inspect.
    assert "t-alpha" in cluster.namespaces
    assert "uninstall_release" not in [op[0] for op in cluster.ops]


def test_an_unexpected_error_in_the_job_is_a_failed_create_too(client, cluster, jobs, db):
    def boom(name):
        raise RuntimeError("kubectl vanished")

    cluster.wait_for_rollout = boom  # type: ignore[method-assign]
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202
    jobs.run()  # does not raise: the thread must not die with the row left provisioning
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "failed"
    assert db.events("alpha")[-1]["detail"] == "RuntimeError: kubectl vanished"


def test_wake_of_a_failed_create_touches_nothing(client, cluster, jobs):
    cluster.fail_on.add("install_release")
    client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    jobs.run()
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202 and resp.json()["state"] == "failed"
    assert len(cluster.ops) == n_ops


@pytest.mark.parametrize("step", ["install_release", "wait_for_rollout"])
def test_delete_of_a_failed_create_frees_the_slot(client, cluster, jobs, db, step):
    cluster.fail_on.add(step)
    client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    jobs.run()
    resp = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert resp.status_code == 200, resp.text
    kinds = [e["kind"] for e in db.events("alpha")]
    if step == "install_release":
        # No release, so no world volume: the backup is skipped, not fatal.
        assert resp.json()["backup"] is None
        assert "backup.skipped" in kinds
    else:
        assert resp.json()["backup"]
        assert "backup" in kinds
    assert [op[0] for op in cluster.ops[-2:]] == ["uninstall_release", "delete_namespace"]
    assert "t-alpha" not in cluster.namespaces
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 404
    # The slot is free again, and the name too.
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202


def test_provisioning_left_over_by_a_restart_is_marked_failed(settings, cluster, resolver, jobs):
    db = Database(settings.db_path)
    db.ensure_tenant("alice")
    db.insert_server("alpha", "alice", "alpha.play.example.com", "m", None, status="provisioning")
    validator = FakeValidator({"alice-token": "alice"})
    app = create_app(
        settings, db=db, cluster=cluster, validator=validator, uuids=resolver, jobs=jobs
    )
    client = TestClient(app)
    body = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    assert body["state"] == "failed"
    assert db.events("alpha")[-1]["kind"] == "create.interrupted"
    # ... and the tenant is not stuck: the failed server can be removed.
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 200


def test_create_runs_on_a_real_thread_pool_by_default(settings, db, cluster, resolver):
    """Without an injected runner the app's own executor does the work."""
    app = create_app(
        settings,
        db=db,
        cluster=cluster,
        validator=FakeValidator({"alice-token": "alice"}),
        uuids=resolver,
    )
    client = TestClient(app)
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202
    deadline = time.monotonic() + 10
    while client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "provisioning":
        assert time.monotonic() < deadline, "the background job never finished"
        time.sleep(0.02)
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "awake"
    app.state.jobs.shutdown(wait=True)


def test_an_old_database_gains_the_status_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE servers (name TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,"
            " hostname TEXT NOT NULL, motd TEXT NOT NULL, operator_username TEXT,"
            " created_at TEXT NOT NULL, last_woken_at TEXT);"
            "INSERT INTO servers VALUES ('alpha', 'alice', 'h', 'm', NULL, 't', NULL);"
        )
    db = Database(str(path))
    assert db.get_server("alpha").status == "ready"  # existing servers are done
    Database(str(path))  # idempotent


def test_admin_password_is_returned_exactly_once(client, created):
    assert "admin_password" in created
    one = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    many = client.get("/api/v1/servers", headers=ALICE).json()
    assert "admin_password" not in one
    assert "admin_password" not in many[0]
    dumped = (one | many[0]).values()
    creds = Credentials.generate()
    assert creds.rcon_password not in dumped


# --- read ------------------------------------------------------------------


def test_list_and_get_report_state_transitions(client, cluster, created):
    def state():
        return client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"]

    assert state() == "awake"
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=1, ready_replicas=0)
    assert state() == "waking"
    # The router puts the server to sleep when idle.
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    assert state() == "asleep"
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=1, failing=True)
    assert state() == "failed"
    cluster.wrappers.pop("alpha")
    assert state() == "failed"

    listed = client.get("/api/v1/servers", headers=ALICE).json()
    assert [s["name"] for s in listed] == ["alpha"]
    assert listed[0]["state"] == "failed"


def test_get_includes_player_count_when_awake(client, cluster, created):
    cluster.players["alpha"] = 3
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=1, ready_replicas=0)
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["players_online"] is None
    cluster.become_ready("alpha")
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["players_online"] == 3
    # The list is cheap: no ping per server.
    assert client.get("/api/v1/servers", headers=ALICE).json()[0]["players_online"] is None


def test_stopped_server_reports_no_player_count(client, cluster, created):
    cluster.players["alpha"] = 3  # a stale ping answer must not leak through
    cluster.stop_game("alpha")
    body = client.get("/api/v1/servers/alpha", headers=ALICE).json()
    assert body["state"] == "stopped"
    assert body["players_online"] is None


def test_statefulset_status_state_table():
    assert StatefulSetStatus(exists=False).state == "failed"
    assert StatefulSetStatus(exists=True, replicas=0).state == "asleep"
    assert StatefulSetStatus(exists=True, replicas=1, ready_replicas=0).state == "waking"
    assert StatefulSetStatus(exists=True, replicas=1, ready_replicas=1).state == "awake"
    failing = StatefulSetStatus(exists=True, replicas=1, ready_replicas=1, failing=True)
    assert failing.state == "failed"


# --- state from the wrapper ---------------------------------------------------

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=2)
JUST_NOW = NOW - WAKE_GRACE + timedelta(seconds=30)
READY = StatefulSetStatus(exists=True, replicas=1, ready_replicas=1, scaled_at=LONG_AGO)
NOT_READY = StatefulSetStatus(exists=True, replicas=1, ready_replicas=0, scaled_at=JUST_NOW)
RUNNING = WrapperStatus(running=True, pid=7, uptime_seconds=60)
STOPPED = WrapperStatus(running=False)


@pytest.mark.parametrize(
    "sts, wrapper, expected",
    [
        (READY, RUNNING, "awake"),
        (READY, STOPPED, "stopped"),  # the observed case: pod Ready, game gone
        (NOT_READY, RUNNING, "waking"),
        (NOT_READY, STOPPED, "waking"),
        (NOT_READY, None, "waking"),
        (READY, None, "awake"),  # wrapper unreachable: replica-based reading stands
        (StatefulSetStatus(exists=True, replicas=0), None, "asleep"),
        (StatefulSetStatus(exists=True, replicas=0), STOPPED, "asleep"),
        (StatefulSetStatus(exists=False), None, "failed"),
        (StatefulSetStatus(exists=True, replicas=1, ready_replicas=1, failing=True), RUNNING,
         "failed"),
    ],
)  # fmt: skip
def test_server_state_table(sts, wrapper, expected):
    assert server_state(sts, wrapper, now=NOW) == expected
    assert expected in STATES


def test_a_ready_pod_whose_game_is_still_booting_is_waking_for_a_grace_period():
    fresh = StatefulSetStatus(exists=True, replicas=1, ready_replicas=1, scaled_at=JUST_NOW)
    assert server_state(fresh, STOPPED, now=NOW) == "waking"
    assert server_state(fresh, STOPPED, now=NOW + WAKE_GRACE) == "stopped"
    assert server_state(fresh, RUNNING, now=NOW) == "awake"
    # Without a scale-up time nothing excuses a not-running game.
    unknown = StatefulSetStatus(exists=True, replicas=1, ready_replicas=1)
    assert server_state(unknown, STOPPED, now=NOW) == "stopped"


def test_state_over_http_for_each_wrapper_answer(client, cluster, created):
    def state():
        return client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"]

    cluster.wrappers["alpha"] = READY
    cluster.wrapper_statuses["alpha"] = RUNNING
    assert state() == "awake"
    cluster.wrapper_statuses["alpha"] = STOPPED
    assert state() == "stopped"
    cluster.wrapper_statuses["alpha"] = None  # timed out
    assert state() == "awake"
    cluster.wrappers["alpha"] = NOT_READY
    cluster.wrapper_statuses["alpha"] = STOPPED
    assert state() == "waking"
    cluster.wrappers["alpha"] = StatefulSetStatus(
        exists=True, replicas=1, ready_replicas=1, scaled_at=datetime.now(UTC)
    )
    assert state() == "waking"  # booting: within the grace period after scale-up


def test_a_wrapper_that_cannot_be_asked_never_breaks_the_list(client, cluster, created):
    cluster.wrapper_statuses["alpha"] = None
    resp = client.get("/api/v1/servers", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()[0]["state"] == "awake"
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200


def test_asleep_servers_are_not_asked(client, cluster, created):
    """No pod, nothing to ask: the wrapper call is skipped rather than timed out."""
    calls = []
    cluster.wrapper_status = lambda name: calls.append(name)  # type: ignore[method-assign]
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    assert client.get("/api/v1/servers", headers=ALICE).json()[0]["state"] == "asleep"
    assert calls == []


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


def test_each_tenant_sees_only_their_own(client, settings, created, jobs):
    resp = client.post("/api/v1/servers", json={"name": "bobs"}, headers=BOB)
    assert resp.status_code == 202
    jobs.run()
    assert [s["name"] for s in client.get("/api/v1/servers", headers=ALICE).json()] == ["alpha"]
    assert [s["name"] for s in client.get("/api/v1/servers", headers=BOB).json()] == ["bobs"]


# --- wake --------------------------------------------------------------------


def test_wake_scales_the_wrapper_to_one(client, cluster, created):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    before = client.get("/api/v1/servers/alpha", headers=ALICE).json()["last_woken_at"]
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202
    assert resp.json()["state"] == "waking"
    assert cluster.ops[-1] == ("scale_wrapper", "alpha", 1)
    assert resp.json()["last_woken_at"] >= before


def test_wake_when_already_up_does_nothing(client, cluster, created):
    assert cluster.wrapper_status("alpha").running  # the fake's rollout started the game
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202
    assert resp.json()["state"] == "awake"
    assert len(cluster.ops) == n_ops


def test_wake_of_a_stopped_server_starts_the_game_in_place(client, cluster, created, db):
    """The observed case: Stop was pressed in the dashboard, the pod stayed Ready."""
    cluster.stop_game("alpha")
    before = client.get("/api/v1/servers/alpha", headers=ALICE).json()["last_woken_at"]
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "awake"
    assert cluster.ops[n_ops:] == [("start_wrapper", "alpha")]  # not a scale: replicas is 1
    assert resp.json()["last_woken_at"] >= before
    assert [e["kind"] for e in db.events("alpha")][-1] == "wake.start"


def test_wake_while_waking_leaves_it_alone(client, cluster, created):
    n_ops = len(cluster.ops)
    # Pod not ready yet.
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=1, ready_replicas=0)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202 and resp.json()["state"] == "waking"
    # Pod ready, game still booting after a fresh scale-up.
    cluster.wrappers["alpha"] = StatefulSetStatus(
        exists=True, replicas=1, ready_replicas=1, scaled_at=datetime.now(UTC)
    )
    cluster.wrapper_statuses["alpha"] = STOPPED
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202 and resp.json()["state"] == "waking"
    assert len(cluster.ops) == n_ops  # no start call: it is booting on its own


def test_wake_when_the_wrapper_cannot_be_asked_falls_back_to_replicas(client, cluster, created):
    cluster.wrapper_statuses["alpha"] = None
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202
    assert resp.json()["state"] == "awake"
    assert len(cluster.ops) == n_ops


def test_wake_of_a_failed_server_reports_the_failure(client, cluster, created):
    cluster.fail_pod("alpha")
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202
    assert resp.json()["state"] == "failed"
    assert len(cluster.ops) == n_ops


def test_wake_of_a_server_whose_statefulset_is_gone_reports_the_failure(client, cluster, created):
    """The release was removed behind the API's back: ``GET`` says ``failed``,
    and ``wake`` must say the same rather than scale a StatefulSet that is not
    there (a 502 from kubectl)."""
    del cluster.wrappers["alpha"]
    n_ops = len(cluster.ops)
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "failed"
    assert cluster.ops[n_ops:] == []  # no scale attempted


def test_wake_start_failure_is_502(client, cluster, created):
    cluster.stop_game("alpha")
    cluster.fail_on.add("start_wrapper")
    resp = client.post("/api/v1/servers/alpha/wake", headers=ALICE)
    assert resp.status_code == 502
    assert "start_wrapper" in resp.json()["detail"]


def test_wake_unknown_server_is_404(client):
    assert client.post("/api/v1/servers/ghost/wake", headers=ALICE).status_code == 404


# --- delete ------------------------------------------------------------------


def test_delete_backs_up_before_the_namespace_goes(client, cluster, created, db):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)  # asleep
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


def test_delete_of_a_stopped_server_still_execs_into_its_pod(client, cluster, created):
    cluster.stop_game("alpha")
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


def test_delete_retry_after_a_failure_past_the_backup_reuses_that_backup(
    client, cluster, created, db
):
    """Seen live 2026-09-19: helm removed the world volume, then failed on an
    object the API could not delete; every retry then 502'd on the missing PVC
    while a good backup sat on disk. The retry must use it and finish."""
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)  # asleep
    cluster.fail_on.add("uninstall_release")
    first = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert first.status_code == 502
    taken = [e["detail"] for e in db.events("alpha") if e["kind"] == "backup"]
    assert len(taken) == 1 and (cluster.backup_dir / taken[0].rsplit("/", 1)[1]).exists()

    # helm's partial uninstall took the release (and with it the PVC) even
    # though it reported failure; the API's row still says ready.
    cluster.fail_on.discard("uninstall_release")
    cluster.releases.pop("alpha")
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200

    retry = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert retry.status_code == 200, retry.text
    assert retry.json()["backup"] == taken[0]
    assert "t-alpha" not in cluster.namespaces
    kinds = [e["kind"] for e in db.events("alpha")]
    assert kinds[-2:] == ["backup.reused", "delete"]
    assert kinds.count("backup") == 1  # no second tarball was attempted successfully


def test_delete_retry_reuses_a_backup_when_kubectl_reports_the_missing_pvc_singularly(
    client, cluster, created, db
):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    cluster.fail_on.add("uninstall_release")
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 502
    taken = [e["detail"] for e in db.events("alpha") if e["kind"] == "backup"]
    cluster.fail_on.discard("uninstall_release")

    def backup_world(name: str, awake: bool):
        raise ClusterError(f"persistentvolumeclaim/{name}-omcsi-mcserver not found")

    cluster.backup_world = backup_world
    retry = client.delete("/api/v1/servers/alpha", headers=ALICE)
    assert retry.status_code == 200, retry.text
    assert retry.json()["backup"] == taken[0]
    assert [e["kind"] for e in db.events("alpha")][-2:] == ["backup.reused", "delete"]


def test_latest_reused_backup_is_considered_reusable(
    settings, db, cluster, resolver, jobs, created
):
    cluster.backup_dir.mkdir(parents=True, exist_ok=True)
    original = cluster.backup_dir / "old.tar.gz"
    original.write_bytes(b"old")
    db.record("alice", "alpha", "backup", str(original))
    original.unlink()
    reused = cluster.backup_dir / "new.tar.gz"
    reused.write_bytes(b"new")
    db.record("alice", "alpha", "backup.reused", str(reused))
    service = ServerService(settings, db, cluster, resolver, jobs)
    assert service._backup_still_on_disk("alpha") == str(reused)


def test_older_existing_backup_is_used_when_the_latest_recorded_one_is_gone(
    settings, db, cluster, resolver, jobs, created
):
    cluster.backup_dir.mkdir(parents=True, exist_ok=True)
    older = cluster.backup_dir / "older.tar.gz"
    older.write_bytes(b"older")
    missing = cluster.backup_dir / "missing.tar.gz"
    db.record("alice", "alpha", "backup", str(older))
    db.record("alice", "alpha", "backup.reused", str(missing))
    service = ServerService(settings, db, cluster, resolver, jobs)
    assert service._backup_still_on_disk("alpha") == str(older)


def test_delete_retry_does_not_reuse_a_backup_that_is_gone_from_disk(client, cluster, created, db):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    cluster.fail_on.add("uninstall_release")
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 502
    for path in cluster.backup_dir.iterdir():
        path.unlink()  # retention swept it, say
    cluster.fail_on.discard("uninstall_release")
    cluster.releases.pop("alpha")
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 502
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200  # still there


def test_delete_retry_does_not_reuse_a_backup_for_an_unrelated_not_found(
    client, cluster, created, db
):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    cluster.fail_on.add("uninstall_release")
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 502
    cluster.fail_on.discard("uninstall_release")

    def backup_world(name: str, awake: bool):
        raise ClusterError("exec failed: container not found")

    cluster.backup_world = backup_world
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 502
    assert client.get("/api/v1/servers/alpha", headers=ALICE).status_code == 200
    assert [e["kind"] for e in db.events("alpha")].count("backup.reused") == 0


def test_after_delete_the_name_can_be_reused(client, cluster, created):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    assert client.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 200
    assert client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202


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


def test_fake_refuses_namespaced_steps_without_the_tenant_binding(cluster):
    """Mirrors the cluster: with the scoped RBAC, nothing namespaced works in
    t-<name> until the RoleBinding is there, so the order of steps is checked."""
    spec = ReleaseSpec(name="alpha", hostname="h", sslip_hostname="s", motd="m")
    cluster.create_tenant_namespace("alpha")
    with pytest.raises(ClusterError, match="forbidden"):
        cluster.create_credentials("alpha", Credentials.generate())
    with pytest.raises(ClusterError, match="forbidden"):
        cluster.install_release(spec, Credentials.generate())
    cluster.grant_tenant_access("alpha")
    cluster.create_credentials("alpha", Credentials.generate())
    cluster.install_release(spec, Credentials.generate())
    # A binding cannot be created in a namespace that does not exist.
    with pytest.raises(ClusterError, match="not found"):
        cluster.grant_tenant_access("ghost")


def test_a_binding_failure_fails_the_create_before_the_secret(client, cluster, db, jobs):
    cluster.fail_on.add("grant_tenant_access")
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202
    assert jobs.run() == 1
    assert [op[0] for op in cluster.ops] == ["create_tenant_namespace", "grant_tenant_access"]
    assert "alpha" not in cluster.credentials
    assert db.events("alpha")[-1]["kind"] == "create.failed"
    assert client.get("/api/v1/servers/alpha", headers=ALICE).json()["state"] == "failed"


def test_fake_install_keeps_an_awake_wrapper_awake(cluster):
    """Mirrors the real backend: a re-run over a wrapper at 1 replica must not sleep it."""
    spec = ReleaseSpec(name="alpha", hostname="h", sslip_hostname="s", motd="m")
    cluster.create_tenant_namespace("alpha")
    cluster.grant_tenant_access("alpha")
    cluster.install_release(spec, Credentials.generate())
    assert cluster.ops[-1] == ("install_release", "alpha", False)
    assert cluster.statefulset_status("alpha").replicas == 0
    cluster.scale_wrapper("alpha", 1)
    cluster.install_release(spec, Credentials.generate())
    assert cluster.ops[-1] == ("install_release", "alpha", True)
    assert cluster.statefulset_status("alpha").replicas == 1
