"""Download your world as 3D (RFC 0021): the routes, the export job, signed
links, limits and housekeeping, against the in-memory cluster; and the real
backend's command lines and unpacking, with a recording runner."""

from __future__ import annotations

import io
import json
import tarfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import (
    ClusterError,
    KubectlHelmBackend,
    StatefulSetStatus,
    export_failure_message,
)
from dsh_api.export_pod import (
    MAX_MEMBERS,
    UnsafeArchive,
    export_network_policy,
    export_pod_manifest,
    extract_export,
    stream_command,
)
from dsh_api.main import create_app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


class Jobs:
    def __init__(self) -> None:
        self.pending: list = []

    def submit(self, fn, /, *args):
        self.pending.append((fn, args))

    def run(self) -> int:
        n = 0
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)
            n += 1
        return n


@pytest.fixture
def export_jobs() -> Jobs:
    return Jobs()


@pytest.fixture
def export_settings(settings, tmp_path):
    return replace(
        settings,
        world_export=True,
        export_dir=str(tmp_path / "exports"),
        export_image="dsh-world-export:test",
        public_api_url="https://api.example.com",
        jwt_secret="a-test-secret-of-at-least-32-characters",
    )


@pytest.fixture
def make_app(export_settings, db, cluster, resolver, jobs, catalog, export_jobs):
    def make(**overrides) -> TestClient:
        app = create_app(
            replace(export_settings, **overrides),
            db=db,
            cluster=cluster,
            validator=FakeValidator({"alice-token": "alice", "bob-token": "bob"}),
            uuids=resolver,
            jobs=jobs,
            catalog=catalog,
            export_jobs=export_jobs,
        )
        return TestClient(app)

    return make


@pytest.fixture
def api(make_app) -> TestClient:
    return make_app()


@pytest.fixture
def alpha(api: TestClient, jobs) -> str:
    """alice's server ``alpha``, provisioned and awake."""
    resp = api.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202, resp.text
    jobs.run()
    return "alpha"


def start(api: TestClient, body: dict | None = None, who=ALICE, name="alpha"):
    return api.post(f"/api/v1/servers/{name}/exports", json=body or {}, headers=who)


def ready_export(api: TestClient, export_jobs: Jobs, body: dict | None = None) -> dict:
    resp = start(api, body)
    assert resp.status_code == 202, resp.text
    assert export_jobs.run() == 1
    export = api.get(f"/api/v1/servers/alpha/exports/{resp.json()['id']}", headers=ALICE).json()
    assert export["status"] == "ready", export
    return export


# --- the flag ------------------------------------------------------------------


def test_everything_is_503_while_the_flag_is_off(make_app, alpha) -> None:
    api = make_app(world_export=False)
    assert api.get("/api/v1/servers/alpha/exports", headers=ALICE).status_code == 503
    resp = start(api)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "world export is not enabled"}
    for path in ("/api/v1/servers/alpha/exports/0123456789ab", "/api/v1/export-downloads/x.y"):
        assert api.get(path, headers=ALICE).status_code == 503
    # The limits the portal shows are there either way.
    limits = api.get("/api/v1/limits").json()
    assert limits["export_max_size"] == 512 and limits["export_default_size"] == 256
    assert limits["export_daily_limit"] == 5 and limits["export_retention_hours"] == 72


# --- starting and running ---------------------------------------------------------


def test_default_export_runs_around_spawn_and_becomes_ready(api, alpha, export_jobs, cluster):
    resp = start(api)
    assert resp.status_code == 202
    queued = resp.json()
    assert queued["status"] == "queued" and queued["area"] is None
    assert queued["custom_area"] is False and queued["stats"] is None

    assert export_jobs.run() == 1
    (name, export_id, args) = cluster.exports_run[0]
    assert (name, export_id) == ("alpha", queued["id"])
    assert "--area" not in args
    assert args[args.index("--server-state") + 1] == "awake"
    assert args[args.index("--default-size") + 1] == "256"
    assert args[args.index("--max-size") + 1] == "512"
    run_op = [op for op in cluster.ops if op[0] == "run_world_export"]
    assert run_op == [("run_world_export", "alpha", queued["id"])]

    export = api.get(f"/api/v1/servers/alpha/exports/{queued['id']}", headers=ALICE).json()
    assert export["status"] == "ready" and export["phase"] is None and export["error"] is None
    assert export["area"] == {"x1": -128, "z1": -128, "x2": 127, "z2": 127}
    assert export["snapshot"] == "live_stable"
    assert export["stats"]["instances"] == 100 and export["stats"]["zip_bytes"] > 0
    finished = datetime.fromisoformat(export["finished_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(export["expires_at"].replace("Z", "+00:00"))
    assert expires - finished == timedelta(hours=72)
    listed = api.get("/api/v1/servers/alpha/exports", headers=ALICE).json()
    assert [e["id"] for e in listed["exports"]] == [queued["id"]]


def test_an_asleep_server_gives_an_at_rest_snapshot(api, alpha, export_jobs, cluster):
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    export = ready_export(api, export_jobs)
    assert cluster.exports_run[0][2][1] == "asleep"
    assert export["snapshot"] == "at_rest"


def test_custom_area_is_normalised_and_passed(api, alpha, export_jobs, cluster):
    body = {"area": {"x1": 100, "z1": 50, "x2": -100, "z2": -50}}
    resp = start(api, body)
    assert resp.status_code == 202
    assert resp.json()["area"] == {"x1": -100, "z1": -50, "x2": 100, "z2": 50}
    assert resp.json()["custom_area"] is True
    export_jobs.run()
    args = cluster.exports_run[0][2]
    assert args[args.index("--area") + 1 : args.index("--area") + 5] == ("-100", "-50", "100", "50")


@pytest.mark.parametrize(
    "area",
    [
        {"x1": 0, "z1": 0, "x2": 512, "z2": 10},  # 513 wide
        {"x1": 0, "z1": 0, "x2": 10, "z2": -512},
        {"x1": 30_000_000, "z1": 0, "x2": 30_000_010, "z2": 10},
        {"x1": "a", "z1": 0, "x2": 1, "z2": 1},
        {"x1": 0, "z1": 0},
    ],
)
def test_bad_areas_are_422(api, alpha, area) -> None:
    assert start(api, {"area": area}).status_code == 422


def test_the_largest_area_is_accepted(api, alpha) -> None:
    assert start(api, {"area": {"x1": 0, "z1": 0, "x2": 511, "z2": 511}}).status_code == 202


@pytest.mark.parametrize(
    "area",
    [
        {"x1": 29_999_974, "z1": 0, "x2": 29_999_984, "z2": 10},
        {"x1": -29_999_984, "z1": -29_999_984, "x2": -29_999_974, "z2": -29_999_974},
    ],
)
def test_an_area_reaching_the_world_border_is_accepted(api, alpha, area) -> None:
    assert start(api, {"area": area}).status_code == 202


@pytest.mark.parametrize(
    ("area", "detail"),
    [
        (
            {"x1": 29_999_975, "z1": 0, "x2": 29_999_985, "z2": 10},
            "coordinates must be inside the world border",
        ),
        (
            {"x1": 0, "z1": -29_999_985, "x2": 10, "z2": -29_999_975},
            "coordinates must be inside the world border",
        ),
        ({"x1": 0, "z1": 0, "x2": 512, "z2": 10}, "the area can be at most 512 × 512 blocks"),
    ],
)
def test_a_refused_area_says_why(api, alpha, area, detail) -> None:
    resp = start(api, {"area": area})
    assert resp.status_code == 422
    assert resp.json() == {"detail": detail}


def test_one_export_at_a_time_per_server(api, alpha, export_jobs) -> None:
    assert start(api).status_code == 202
    resp = start(api)
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]
    export_jobs.run()
    assert start(api).status_code == 202


def test_the_daily_limit_is_429_with_retry_after(make_app, alpha, export_jobs) -> None:
    api = make_app(export_daily_limit=2)
    for _ in range(2):
        assert start(api).status_code == 202
        export_jobs.run()
    resp = start(api)
    assert resp.status_code == 429
    assert 0 < resp.json()["retry_after"] <= 24 * 3600
    assert resp.headers["Retry-After"] == str(resp.json()["retry_after"])


def test_not_while_the_server_is_being_created_or_failing(api, jobs, cluster, export_jobs):
    assert api.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202
    resp = start(api)
    assert resp.status_code == 409 and "provisioning" in resp.json()["detail"]
    jobs.run()
    cluster.fail_pod("alpha")
    resp = start(api)
    assert resp.status_code == 409 and "failing" in resp.json()["detail"]
    assert export_jobs.pending == []


def test_a_failed_export_says_why_and_keeps_nothing(api, alpha, export_jobs, cluster, tmp_path):
    cluster.export_failures["alpha"] = "the export ran out of memory; try a smaller area"
    export_id = start(api).json()["id"]
    export_jobs.run()
    export = api.get(f"/api/v1/servers/alpha/exports/{export_id}", headers=ALICE).json()
    assert export["status"] == "failed"
    assert export["error"] == "the export ran out of memory; try a smaller area"
    assert not (tmp_path / "exports" / "alpha" / export_id).exists()
    assert not any((tmp_path / "exports" / ".staging").glob("*"))
    # Another try is allowed straight away.
    assert start(api).status_code == 202


def test_an_unexpected_error_fails_the_export_without_details(
    api, alpha, export_jobs, cluster, monkeypatch
) -> None:
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(cluster, "run_world_export", boom)
    export_id = start(api).json()["id"]
    export_jobs.run()
    export = api.get(f"/api/v1/servers/alpha/exports/{export_id}", headers=ALICE).json()
    assert export["status"] == "failed"
    assert "secret" not in export["error"] and "RuntimeError" in export["error"]


def test_phases_are_recorded_while_running(api, alpha, export_jobs, cluster, db) -> None:
    seen: list[str | None] = []
    real = cluster.run_world_export

    def spy(name, export_id, **kw):
        on_phase = kw["on_phase"]

        def record(phase):
            on_phase(phase)
            seen.append(db.get_export(export_id).phase)

        return real(name, export_id, **{**kw, "on_phase": record})

    cluster.run_world_export = spy
    start(api)
    export_jobs.run()
    assert seen == ["copying", "exporting", "preview", "packaging"]


# --- ownership ---------------------------------------------------------------------


def test_another_tenant_sees_nothing(api, alpha, export_jobs) -> None:
    export = ready_export(api, export_jobs)
    base = f"/api/v1/servers/alpha/exports/{export['id']}"
    assert api.get("/api/v1/servers/alpha/exports", headers=BOB).status_code == 404
    assert start(api, who=BOB).status_code == 404
    assert api.get(base, headers=BOB).status_code == 404
    assert api.get(f"{base}/preview/index.json", headers=BOB).status_code == 404
    assert api.post(f"{base}/link", headers=BOB).status_code == 404
    assert api.delete(base, headers=BOB).status_code == 404
    assert api.get(base).status_code == 401


def test_an_export_id_under_another_server_is_404(api, alpha, export_jobs, db) -> None:
    export = ready_export(api, export_jobs)
    db.insert_server("beta", "alice", "beta.play.example.com", "hi", None)
    assert api.get(f"/api/v1/servers/beta/exports/{export['id']}", headers=ALICE).status_code == 404
    assert api.get("/api/v1/servers/alpha/exports/not-an-id", headers=ALICE).status_code == 404


# --- preview and download -------------------------------------------------------------


def test_preview_files_are_served_to_the_owner(api, alpha, export_jobs) -> None:
    export = ready_export(api, export_jobs)
    base = f"/api/v1/servers/alpha/exports/{export['id']}/preview"
    index = api.get(f"{base}/index.json", headers=ALICE)
    assert index.status_code == 200 and index.headers["content-type"] == "application/json"
    assert index.json() == {"format": "usd-web-index"}
    tile = api.get(f"{base}/t.0.0.0123456789ab.uwt", headers=ALICE)
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "application/octet-stream"
    assert tile.content.startswith(b"\x1f\x8b")
    for bad in ("summary.json", "world.zip", "t.0.0.zz.uwt", "..%2Fsummary.json", "x.json"):
        assert api.get(f"{base}/{bad}", headers=ALICE).status_code == 404


def test_no_preview_until_ready(api, alpha) -> None:
    export_id = start(api).json()["id"]
    resp = api.get(f"/api/v1/servers/alpha/exports/{export_id}/preview/index.json", headers=ALICE)
    assert resp.status_code == 404


def test_a_signed_link_downloads_the_zip_without_a_token(api, alpha, export_jobs) -> None:
    export = ready_export(api, export_jobs)
    link = api.post(f"/api/v1/servers/alpha/exports/{export['id']}/link", headers=ALICE).json()
    assert link["url"].startswith("https://api.example.com/api/v1/export-downloads/")
    expires = datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)
    path = link["url"].removeprefix("https://api.example.com")
    resp = api.get(path)  # no Authorization header
    assert resp.status_code == 200
    assert resp.content == b"PK fake zip of alpha"
    assert resp.headers["content-type"] == "application/zip"
    assert f'filename="alpha-world-{export["id"]}.zip"' in resp.headers["content-disposition"]


def test_bad_expired_or_stale_links_are_404(api, alpha, export_jobs) -> None:
    export = ready_export(api, export_jobs)
    service = api.app.state.exports
    good = service.sign("alpha", export["id"], int(time.time()) + 60)
    payload, mac = good.split(".")
    for token in (
        service.sign("alpha", export["id"], int(time.time()) - 1),  # expired
        f"{payload}.{mac[:-2]}AA",  # tampered mac
        service.sign("beta", export["id"], int(time.time()) + 60),  # wrong server
        service.sign("alpha", "ffffffffffff", int(time.time()) + 60),  # no such export
        "not-a-token",
        "....",
    ):
        assert api.get(f"/api/v1/export-downloads/{token}").status_code == 404, token
    assert api.get(f"/api/v1/export-downloads/{good}").status_code == 200
    api.delete(f"/api/v1/servers/alpha/exports/{export['id']}", headers=ALICE)
    assert api.get(f"/api/v1/export-downloads/{good}").status_code == 404


def test_no_link_until_ready(api, alpha) -> None:
    export_id = start(api).json()["id"]
    resp = api.post(f"/api/v1/servers/alpha/exports/{export_id}/link", headers=ALICE)
    assert resp.status_code == 409


def test_a_link_signed_with_another_key_is_refused(make_app, alpha, export_jobs) -> None:
    api = make_app()
    export = ready_export(api, export_jobs)
    other = make_app(export_link_secret="a-different-link-secret-entirely-xx")
    token = other.app.state.exports.sign("alpha", export["id"], int(time.time()) + 60)
    assert api.get(f"/api/v1/export-downloads/{token}").status_code == 404


# --- deleting and housekeeping --------------------------------------------------------------


def test_delete_removes_the_files(api, alpha, export_jobs, tmp_path) -> None:
    export = ready_export(api, export_jobs)
    folder = tmp_path / "exports" / "alpha" / export["id"]
    assert (folder / "world.zip").is_file()
    resp = api.delete(f"/api/v1/servers/alpha/exports/{export['id']}", headers=ALICE)
    assert resp.status_code == 204
    assert not folder.exists()
    assert api.get("/api/v1/servers/alpha/exports", headers=ALICE).json() == {"exports": []}


def test_a_running_export_cannot_be_deleted(api, alpha) -> None:
    export_id = start(api).json()["id"]
    resp = api.delete(f"/api/v1/servers/alpha/exports/{export_id}", headers=ALICE)
    assert resp.status_code == 409


def test_deleting_the_server_deletes_its_exports(api, alpha, export_jobs, jobs, tmp_path, db):
    export = ready_export(api, export_jobs)
    assert api.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 202
    jobs.run()
    assert db.get_export(export["id"]) is None
    assert not (tmp_path / "exports" / "alpha").exists()


def test_an_export_whose_server_went_away_meanwhile_fails(api, alpha, export_jobs, jobs, db):
    export_id = start(api).json()["id"]
    assert api.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 202
    jobs.run()
    export_jobs.run()  # the queued run finds the row gone with its server
    assert db.get_export(export_id) is None


def test_expired_and_orphaned_exports_are_swept(api, alpha, export_jobs, db, tmp_path) -> None:
    keep = ready_export(api, export_jobs)
    old = ready_export(api, export_jobs)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    db.update_export(old["id"], expires_at=past)
    stray = tmp_path / "exports" / "alpha" / "abcabcabcabc"
    stray.mkdir(parents=True)
    # A row from an earlier server of the same name, owned by someone else.
    db.insert_export("0000aaaa1111", "alpha", "bob", None)
    db.update_export("0000aaaa1111", status="failed")

    assert api.app.state.exports.sweep() == 2
    assert db.get_export(old["id"]) is None and db.get_export("0000aaaa1111") is None
    assert db.get_export(keep["id"]) is not None
    assert not (tmp_path / "exports" / "alpha" / old["id"]).exists()
    assert not stray.exists()
    assert (tmp_path / "exports" / "alpha" / keep["id"] / "world.zip").is_file()


def test_a_restart_fails_interrupted_exports_and_removes_their_pods(make_app, alpha, cluster):
    api = make_app()
    export_id = start(api).json()["id"]  # queued, never run: the process "died"
    make_app()  # the next process starts
    export = api.get(f"/api/v1/servers/alpha/exports/{export_id}", headers=ALICE).json()
    assert export["status"] == "failed" and "interrupted" in export["error"]
    assert ("delete_world_export_pods", "alpha") in cluster.ops


# --- the real backend ------------------------------------------------------------------------


def make_tar(members: dict[str, bytes], *, extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra:
            extra(tar)
    return buf.getvalue()


GOOD = {
    "summary.json": json.dumps({"area": {"x1": 0, "z1": 0, "x2": 1, "z2": 1}}).encode(),
    "world.zip": b"PK",
    "preview/index.json": b"{}",
    "preview/t.-1.0.abcdef012345.uwt": b"\x1f\x8b",
}


class Runner:
    def __init__(self, answers: dict[str, object] | None = None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.answers = answers or {}

    def __call__(self, argv, *, stdin=None, stdout_path=None):
        self.calls.append((argv, stdin))
        for key, answer in self.answers.items():
            if key in " ".join(argv):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""


class Stream:
    def __init__(self, payload: bytes, code: int = 0, stderr=("phase=copying",)):
        self.payload, self.code, self.stderr = payload, code, list(stderr)
        self.argv: list[str] | None = None

    def __call__(self, argv, stdout_path, on_line, timeout):
        self.argv, self.timeout = argv, timeout
        stdout_path.write_bytes(self.payload)
        for line in self.stderr:
            on_line(line)
        return self.code, self.stderr


def backend(run: Runner, stream: Stream, tmp_path: Path) -> KubectlHelmBackend:
    return KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=run, stream=stream)


def go(be: KubectlHelmBackend, dest: Path, phases: list | None = None) -> None:
    be.run_world_export(
        "alpha",
        "0123456789ab",
        image="dsh-world-export:abc",
        args=["--server-state", "asleep"],
        dest=dest,
        timeout=600,
        on_phase=(phases.append if phases is not None else (lambda p: None)),
    )


def test_real_backend_runs_the_pod_read_only_and_always_deletes_it(tmp_path) -> None:
    run, stream = Runner(), Stream(make_tar(GOOD), stderr=("phase=copying", "phase=done", "x"))
    phases: list[str] = []
    go(backend(run, stream, tmp_path), tmp_path / "dest", phases)

    argvs = [" ".join(a) for a, _ in run.calls]
    assert argvs == [
        "kubectl apply -f -",
        "kubectl -n t-alpha get pvc alpha-omcsi-mcserver -o name",
        "kubectl apply -f -",
        "kubectl -n t-alpha wait --for=condition=Ready pod/world-export-0123456789ab"
        " --timeout=120s",
        "kubectl -n t-alpha delete pod world-export-0123456789ab --wait=false",
    ]
    assert json.loads(run.calls[0][1]) == export_network_policy("t-alpha")
    pod = json.loads(run.calls[2][1])
    assert pod == export_pod_manifest(
        "t-alpha", "0123456789ab", "alpha-omcsi-mcserver", "dsh-world-export:abc"
    )
    assert stream.argv == [
        "kubectl", "-n", "t-alpha", "exec", "world-export-0123456789ab", "--",
        "dsh-world-export", "--server-state", "asleep",
    ]  # fmt: skip
    assert stream.timeout == 600
    assert phases == ["copying", "done"]
    assert sorted(p.name for p in (tmp_path / "dest").rglob("*") if p.is_file()) == [
        "index.json",
        "summary.json",
        "t.-1.0.abcdef012345.uwt",
        "world.zip",
    ]


def test_the_pod_mounts_the_world_read_only_with_no_token_or_network() -> None:
    pod = export_pod_manifest("t-alpha", "0123456789ab", "alpha-omcsi-mcserver", "img")
    spec = pod["spec"]
    world = next(v for v in spec["volumes"] if v["name"] == "world")
    assert world["persistentVolumeClaim"] == {"claimName": "alpha-omcsi-mcserver", "readOnly": True}
    mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "world")
    assert mount["readOnly"] is True and mount["mountPath"] == "/mcserver"
    assert spec["automountServiceAccountToken"] is False
    assert spec["activeDeadlineSeconds"] == 900 and spec["restartPolicy"] == "Never"
    assert spec["terminationGracePeriodSeconds"] == 1
    sc = spec["containers"][0]["securityContext"]
    assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"]
    assert spec["securityContext"]["runAsUser"] == 1000
    limits = spec["containers"][0]["resources"]["limits"]
    assert limits == {"cpu": "500m", "memory": "1Gi"}
    policy = export_network_policy("t-alpha")
    assert policy["spec"]["podSelector"] == {"matchLabels": {"dsh.export": "true"}}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in policy["spec"] and "egress" not in policy["spec"]
    assert pod["metadata"]["labels"]["dsh.export"] == "true"


def test_no_world_volume_means_no_pod(tmp_path) -> None:
    run = Runner({"get pvc": ClusterError('persistentvolumeclaims "x" not found')})
    with pytest.raises(ClusterError, match="no world yet"):
        go(backend(run, Stream(b""), tmp_path), tmp_path / "d")
    assert not any("world-export" in " ".join(a) for a, _ in run.calls)


def test_a_full_quota_is_explained(tmp_path) -> None:
    def apply_pod(argv, *, stdin=None, stdout_path=None):
        if stdin and '"kind": "Pod"' in stdin:
            raise ClusterError("pods is forbidden: exceeded quota: hosted-free")
        return ""

    with pytest.raises(ClusterError, match="no room for an export"):
        go(KubectlHelmBackend("/o", str(tmp_path), run=apply_pod), tmp_path / "d")


def test_a_pod_that_never_starts_is_explained_and_deleted(tmp_path) -> None:
    waiting = {
        "status": {"containerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull"}}}]}
    }
    run = Runner(
        {
            "wait --for": ClusterError("timed out waiting for the condition"),
            "get pod world-export": json.dumps(waiting),
        }
    )
    with pytest.raises(ClusterError, match="image is not available"):
        go(backend(run, Stream(b""), tmp_path), tmp_path / "d")
    assert "delete pod world-export-0123456789ab" in " ".join(run.calls[-1][0])


@pytest.mark.parametrize(
    ("code", "stderr", "message"),
    [
        (3, ["phase=copying", "error: the world has not been created yet"], "not been created"),
        (137, ["phase=exporting"], "ran out of memory"),
        (-9, ["phase=exporting"], "took too long"),
        (1, ["something odd"], "exit status 1"),
    ],
)
def test_a_failing_run_is_explained_and_the_pod_deleted(tmp_path, code, stderr, message) -> None:
    run = Runner()
    with pytest.raises(ClusterError, match=message):
        go(backend(run, Stream(b"", code=code, stderr=stderr), tmp_path), tmp_path / "d")
    assert "delete pod world-export" in " ".join(run.calls[-1][0])
    assert not (tmp_path / "d" / "export.tgz").exists()


def test_leftover_export_pods_are_deleted_by_label_in_the_tenant_namespace(tmp_path) -> None:
    run = Runner()
    backend(run, Stream(b""), tmp_path).delete_world_export_pods("alpha")
    assert [argv for argv, _ in run.calls] == [
        [
            "kubectl", "-n", "t-alpha", "delete", "pod", "-l", "dsh.export=true",
            "--wait=false", "--ignore-not-found",
        ]
    ]  # fmt: skip
    pod = export_pod_manifest("t-alpha", "0123456789ab", "alpha-omcsi-mcserver", "img")
    assert pod["metadata"]["labels"]["dsh.export"] == "true"


def test_the_last_error_line_is_the_reason() -> None:
    tail = ["error: first", "phase=exporting", "error: second", "Traceback (most recent call last)"]
    assert export_failure_message(1, tail) == "second"


def test_a_long_error_line_is_cut_to_300_characters() -> None:
    assert export_failure_message(2, ["error: " + "x" * 400]) == "x" * 300


@pytest.mark.parametrize(
    ("code", "start_of"), [(137, "the export ran out"), (-9, "the export took")]
)
def test_a_kill_is_reported_over_an_error_line(code, start_of) -> None:
    assert export_failure_message(code, ["error: something else"]).startswith(start_of)


def test_an_error_line_must_start_the_line() -> None:
    assert export_failure_message(4, ["  error: indented"]) == "the export failed (exit status 4)"


def test_unexpected_archive_entries_are_refused(tmp_path) -> None:
    for members in (
        {**GOOD, "playerdata/abc.dat": b"x"},
        {**GOOD, "../escape": b"x"},
        {**GOOD, "preview/../../x.json": b"x"},
        {**GOOD, "preview/index.json\n": b"x"},
        {k: v for k, v in GOOD.items() if k != "world.zip"},
    ):
        run = Runner()
        dest = tmp_path / f"d{len(run.calls)}{hash(tuple(members))}"
        with pytest.raises(ClusterError):
            go(backend(run, Stream(make_tar(members)), tmp_path), dest)
        assert not (tmp_path / "escape").exists() and not (tmp_path / "x.json").exists()


def test_links_and_too_many_members_are_refused(tmp_path) -> None:
    def symlink(tar):
        info = tarfile.TarInfo("preview/index.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    archive = tmp_path / "a.tgz"
    archive.write_bytes(make_tar({"summary.json": b"{}"}, extra=symlink))
    with pytest.raises(UnsafeArchive):
        extract_export(archive, tmp_path / "out")
    many = {f"preview/t.{i}.0.abcdef.uwt": b"" for i in range(MAX_MEMBERS + 1)}
    archive.write_bytes(make_tar(many))
    with pytest.raises(UnsafeArchive, match="too many"):
        extract_export(archive, tmp_path / "out2")
    archive.write_bytes(b"not a tar")
    with pytest.raises(UnsafeArchive):
        extract_export(archive, tmp_path / "out3")


def test_stream_command_passes_stdout_and_stderr_lines(tmp_path) -> None:
    lines: list[str] = []
    code, tail = stream_command(
        ["sh", "-c", "echo phase=copying >&2; printf payload; echo error: nope >&2; exit 3"],
        tmp_path / "out",
        lines.append,
        timeout=10,
    )
    assert code == 3 and lines == ["phase=copying", "error: nope"] and tail == lines
    assert (tmp_path / "out").read_bytes() == b"payload"


def test_stream_command_kills_on_timeout(tmp_path) -> None:
    started = time.monotonic()
    code, _ = stream_command(["sleep", "5"], tmp_path / "out", lambda line: None, timeout=0.3)
    assert code == -9 and time.monotonic() - started < 4


def test_export_settings_come_from_the_environment() -> None:
    from dsh_api.config import Settings

    s = Settings.from_env(
        {
            "DSH_WORLD_EXPORT": "true",
            "DSH_EXPORT_MAX_SIZE": "300",
            "DSH_EXPORT_DAILY_LIMIT": "2",
            "DSH_EXPORT_IMAGE": "dsh-world-export:abc",
            "DSH_BASE_DOMAIN": "example.org",
        }
    )
    assert s.world_export and s.export_max_size == 300 and s.export_daily_limit == 2
    assert s.export_image == "dsh-world-export:abc"
    assert s.api_url() == "https://api.example.org"
    assert not Settings.from_env({}).world_export
