"""Rotating world thumbnails (RFC 0034): the flag, the routes, the shared
export queue, limits, serving, deletion and sweeping, against the in-memory
cluster; the real backend's thumbnail run and unpacking with a recording runner;
and dsh-world-export's --thumbnail mode with stand-in tools."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import (
    ClusterError,
    KubectlHelmBackend,
    StatefulSetStatus,
    thumbnail_failure_message,
)
from dsh_api.export_pod import UnsafeArchive, export_pod_manifest, extract_thumbnail
from dsh_api.main import create_app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8X real enough"
PNG = b"\x89PNG\r\n\x1a\n real enough"


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
def thumb_settings(settings, tmp_path):
    return replace(
        settings,
        world_export=True,
        world_thumbnails="manual",
        export_dir=str(tmp_path / "exports"),
        thumbnail_image="dsh-world-thumbnail:test",
        jwt_secret="a-test-secret-of-at-least-32-characters",
    )


@pytest.fixture
def make_app(thumb_settings, db, cluster, resolver, jobs, catalog, export_jobs):
    def make(**overrides) -> TestClient:
        app = create_app(
            replace(thumb_settings, **overrides),
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
    resp = api.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 202, resp.text
    jobs.run()
    return "alpha"


URL = "/api/v1/servers/alpha/thumbnail"


def refresh(api: TestClient, who=ALICE):
    return api.post(URL, headers=who)


def ready(api: TestClient, export_jobs: Jobs) -> dict:
    resp = refresh(api)
    assert resp.status_code == 202, resp.text
    assert export_jobs.run() == 1
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "ready", view
    return view


def thumbs_dir(tmp_path: Path) -> Path:
    return tmp_path / "exports" / ".thumbnails"


# --- the flag ------------------------------------------------------------------------


def test_everything_is_503_while_the_flag_is_off(make_app, alpha) -> None:
    api = make_app(world_thumbnails="off")
    for resp in (
        api.get(URL, headers=ALICE),
        api.post(URL, headers=ALICE),
        api.get(f"{URL}/thumb.webp", headers=ALICE),
    ):
        assert resp.status_code == 503
        assert resp.json() == {"detail": "world thumbnails are not enabled"}
    limits = api.get("/api/v1/limits").json()
    assert limits["thumbnail_mode"] == "off" and limits["thumbnail_daily_limit"] == 3


def test_auto_is_accepted_but_runs_as_manual(make_app, alpha, export_jobs) -> None:
    api = make_app(world_thumbnails="auto")
    assert api.get("/api/v1/limits").json()["thumbnail_mode"] == "manual"
    assert export_jobs.pending == []  # nothing is queued on its own
    assert api.get(URL, headers=ALICE).status_code == 404
    assert refresh(api).status_code == 202


def test_thumbnails_do_not_need_world_export_on(make_app, alpha, export_jobs) -> None:
    api = make_app(world_export=False)
    assert refresh(api).status_code == 202
    assert export_jobs.run() == 1
    assert api.get(URL, headers=ALICE).json()["status"] == "ready"


# --- refreshing and running -------------------------------------------------------------


def test_no_picture_until_one_is_asked_for(api, alpha) -> None:
    resp = api.get(URL, headers=ALICE)
    assert resp.status_code == 404 and resp.json() == {"detail": "no picture yet"}
    assert api.get(f"{URL}/thumb.webp", headers=ALICE).status_code == 404


def test_refresh_queues_on_the_export_executor_and_becomes_ready(
    api, alpha, export_jobs, cluster, tmp_path
) -> None:
    resp = refresh(api)
    assert resp.status_code == 202
    queued = resp.json()
    assert queued["status"] == "queued" and queued["has_picture"] is False
    assert queued["rendered_at"] is None
    assert len(export_jobs.pending) == 1  # the one-worker export queue, shared

    assert export_jobs.run() == 1
    name, run_id, args, image = cluster.thumbnails_run[0]
    assert name == "alpha" and image == "dsh-world-thumbnail:test"
    assert list(args) == ["--server-state", "awake", "--thumbnail-size", "128"]
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "ready" and view["has_picture"] is True
    assert view["error"] is None and view["rendered_at"]
    assert view["frames"] == 12 and view["size"] == [200, 132] and view["strata"] == "0.6.0"
    assert view["area"] == {"x1": -64, "z1": -64, "x2": 63, "z2": 63}
    files = sorted(p.name for p in (thumbs_dir(tmp_path) / "alpha").iterdir())
    assert files == ["poster.png", "thumb.webp", "thumbnail.json"]
    assert not (thumbs_dir(tmp_path) / ".staging" / run_id).exists()


def test_exports_and_thumbnails_wait_in_one_queue(api, alpha, export_jobs, cluster) -> None:
    assert api.post("/api/v1/servers/alpha/exports", json={}, headers=ALICE).status_code == 202
    assert refresh(api).status_code == 202
    assert len(export_jobs.pending) == 2
    export_jobs.run()
    ran = [op[0] for op in cluster.ops if op[0] in ("run_world_export", "run_world_thumbnail")]
    assert ran == ["run_world_export", "run_world_thumbnail"]


def test_an_asleep_server_gives_an_at_rest_picture(api, alpha, export_jobs, cluster) -> None:
    cluster.wrappers["alpha"] = StatefulSetStatus(exists=True, replicas=0)
    view = ready(api, export_jobs)
    assert cluster.thumbnails_run[0][2][1] == "asleep"
    assert view["snapshot"] == "at_rest"


def test_the_box_size_comes_from_configuration(make_app, alpha, export_jobs, cluster) -> None:
    api = make_app(thumbnail_size=64)
    assert refresh(api).status_code == 202
    export_jobs.run()
    assert cluster.thumbnails_run[0][2][-2:] == ("--thumbnail-size", "64")


def test_one_run_at_a_time_per_server(api, alpha, export_jobs) -> None:
    assert refresh(api).status_code == 202
    resp = refresh(api)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "the picture is already being drawn"}
    assert len(export_jobs.pending) == 1


def test_the_daily_limit_is_per_server_with_retry_after(api, alpha, export_jobs, db) -> None:
    for _ in range(3):
        ready(api, export_jobs)
    resp = refresh(api)
    assert resp.status_code == 429
    body = resp.json()
    assert body["detail"] == "the picture can be refreshed 3 times a day; try again later"
    assert 0 < body["retry_after"] <= 24 * 3600
    assert resp.headers["Retry-After"] == str(body["retry_after"])
    assert db.count_thumbnails_since("alice", "alpha", timedelta(days=1))[0] == 3


def test_not_while_the_server_is_being_created_or_failing(api, jobs, cluster, export_jobs):
    assert api.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE).status_code == 202
    resp = refresh(api)
    assert resp.status_code == 409 and "provisioning" in resp.json()["detail"]
    jobs.run()
    cluster.fail_pod("alpha")
    resp = refresh(api)
    assert resp.status_code == 409 and "failing" in resp.json()["detail"]
    assert export_jobs.pending == []


def test_a_failed_run_says_why_and_keeps_the_previous_picture(
    api, alpha, export_jobs, cluster, tmp_path
) -> None:
    first = ready(api, export_jobs)
    before = (thumbs_dir(tmp_path) / "alpha" / "thumb.webp").read_bytes()
    cluster.thumbnail_failures["alpha"] = "the picture came out blank"
    assert refresh(api).status_code == 202
    export_jobs.run()
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "failed" and view["error"] == "the picture came out blank"
    assert view["has_picture"] is True and view["rendered_at"] == first["rendered_at"]
    assert (thumbs_dir(tmp_path) / "alpha" / "thumb.webp").read_bytes() == before
    assert api.get(f"{URL}/thumb.webp", headers=ALICE).status_code == 200


def test_a_first_run_that_fails_leaves_no_picture(api, alpha, export_jobs, cluster) -> None:
    cluster.thumbnail_failures["alpha"] = "the picture could not be drawn"
    assert refresh(api).status_code == 202
    export_jobs.run()
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "failed" and view["has_picture"] is False
    assert api.get(f"{URL}/poster.png", headers=ALICE).status_code == 404


def test_an_unexpected_error_fails_the_run_without_details(
    api, alpha, export_jobs, cluster, monkeypatch
) -> None:
    def boom(*a, **k):
        raise RuntimeError("/secret/path exploded")

    monkeypatch.setattr(cluster, "run_world_thumbnail", boom)
    assert refresh(api).status_code == 202
    export_jobs.run()
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "failed"
    assert view["error"] == "the picture could not be drawn (RuntimeError)"


def test_phases_are_recorded_while_running(api, alpha, export_jobs, cluster, db) -> None:
    seen: list[str | None] = []
    real = cluster.run_world_thumbnail

    def spy(name, run_id, **kw):
        on_phase = kw["on_phase"]

        def record(phase):
            on_phase(phase)
            seen.append(db.get_thumbnail(name).phase)

        return real(name, run_id, **{**kw, "on_phase": record})

    cluster.run_world_thumbnail = spy
    assert refresh(api).status_code == 202
    export_jobs.run()
    assert seen == ["copying", "exporting", "thumbnail", "packaging"]
    assert db.get_thumbnail("alpha").phase is None


# --- serving -------------------------------------------------------------------------------


def test_files_are_served_to_the_owner_only_with_private_caching(api, alpha, export_jobs):
    ready(api, export_jobs)
    webp = api.get(f"{URL}/thumb.webp", headers=ALICE)
    assert webp.status_code == 200 and webp.headers["content-type"] == "image/webp"
    assert webp.content.startswith(b"RIFF")
    assert webp.headers["cache-control"] == "private, max-age=300"
    poster = api.get(f"{URL}/poster.png", headers=ALICE)
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/png"
    assert poster.headers["etag"] != webp.headers["etag"]
    again = api.get(f"{URL}/thumb.webp", headers={**ALICE, "If-None-Match": webp.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    for file in ("thumbnail.json", "..%2Fthumbnail.json", "thumb.gif", "strip.webp"):
        assert api.get(f"{URL}/{file}", headers=ALICE).status_code == 404
    assert api.get(f"{URL}/thumb.webp").status_code == 401


def test_another_tenant_sees_nothing_and_cannot_refresh(api, alpha, export_jobs) -> None:
    ready(api, export_jobs)
    for resp in (
        api.get(URL, headers=BOB),
        api.post(URL, headers=BOB),
        api.get(f"{URL}/thumb.webp", headers=BOB),
    ):
        assert resp.status_code == 404
        assert resp.json() == {"detail": "no such server"}
    assert export_jobs.pending == []


def test_a_new_picture_changes_the_etag(api, alpha, export_jobs, db) -> None:
    ready(api, export_jobs)
    first = api.get(f"{URL}/thumb.webp", headers=ALICE).headers["etag"]
    row = db.get_thumbnail("alpha")
    db.update_thumbnail("alpha", row.run_id, rendered_at="2020-01-01T00:00:00Z")
    assert api.get(f"{URL}/thumb.webp", headers=ALICE).headers["etag"] != first


# --- deletion and housekeeping ----------------------------------------------------------------


def test_deleting_the_server_deletes_its_thumbnail(api, alpha, export_jobs, jobs, tmp_path, db):
    ready(api, export_jobs)
    assert api.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 202
    jobs.run()
    assert db.get_thumbnail("alpha") is None
    assert not (thumbs_dir(tmp_path) / "alpha").exists()


def test_a_run_whose_server_went_away_meanwhile_keeps_nothing(
    api, alpha, export_jobs, jobs, db, cluster, tmp_path
) -> None:
    assert refresh(api).status_code == 202
    assert api.delete("/api/v1/servers/alpha", headers=ALICE).status_code == 202
    jobs.run()
    export_jobs.run()  # the queued run finds its row gone with the server
    assert db.get_thumbnail("alpha") is None
    assert cluster.thumbnails_run == []
    assert not (thumbs_dir(tmp_path) / "alpha").exists()


def test_the_export_sweeper_leaves_thumbnails_alone(api, alpha, export_jobs, tmp_path) -> None:
    ready(api, export_jobs)
    api.app.state.exports.sweep()
    assert (thumbs_dir(tmp_path) / "alpha" / "thumb.webp").is_file()


def test_orphaned_folders_and_rows_are_swept(api, alpha, export_jobs, db, tmp_path) -> None:
    ready(api, export_jobs)
    stray = thumbs_dir(tmp_path) / "ghost"
    stray.mkdir()
    (stray / "thumb.webp").write_bytes(WEBP)
    db.delete_server("alpha")  # the row went without the hook (a crash mid-delete)
    removed = api.app.state.thumbnails.sweep()
    assert removed == 2  # both folders: no row knows either any more
    assert not stray.exists()
    assert not (thumbs_dir(tmp_path) / "alpha").exists()


def test_a_picture_whose_server_changed_hands_is_swept(api, alpha, export_jobs, db, tmp_path):
    ready(api, export_jobs)
    db.ensure_tenant("bob")
    with db._connect() as conn:  # the name now belongs to someone else; the row is stale
        conn.execute("UPDATE servers SET tenant_id = 'bob' WHERE name = 'alpha'")
    assert api.app.state.thumbnails.sweep() == 1
    assert db.get_thumbnail("alpha") is None
    assert not (thumbs_dir(tmp_path) / "alpha").exists()


def test_a_run_in_flight_is_not_swept_and_fails_once_its_server_is_gone(
    api, alpha, export_jobs, db, cluster, tmp_path
) -> None:
    ready(api, export_jobs)
    assert refresh(api).status_code == 202  # queued again; the old picture stays meanwhile
    with db._connect() as conn:  # the server row went, the thumbnail row did not
        conn.execute("DELETE FROM servers WHERE name = 'alpha'")
    sweep = api.app.state.thumbnails.sweep
    assert sweep() == 0
    assert db.get_thumbnail("alpha").status == "queued"
    assert (thumbs_dir(tmp_path) / "alpha" / "thumb.webp").is_file()

    assert export_jobs.run() == 1
    row = db.get_thumbnail("alpha")
    assert row.status == "failed" and row.error == "the server was deleted"
    assert len(cluster.thumbnails_run) == 1  # no pod for the second run
    assert sweep() == 1  # finished now, so it goes
    assert not (thumbs_dir(tmp_path) / "alpha").exists()


def test_a_stray_file_is_swept_and_dot_folders_are_left(api, alpha, export_jobs, tmp_path):
    ready(api, export_jobs)
    root = thumbs_dir(tmp_path)
    (root / "stray.webp").write_bytes(WEBP)
    (root / ".staging").mkdir(exist_ok=True)
    assert api.app.state.thumbnails.sweep() == 1
    assert not (root / "stray.webp").exists()
    assert (root / ".staging").is_dir()
    assert (root / "alpha" / "thumb.webp").is_file()


def test_a_picture_missing_from_disk_is_not_served(api, alpha, export_jobs, tmp_path) -> None:
    ready(api, export_jobs)
    (thumbs_dir(tmp_path) / "alpha" / "poster.png").unlink()
    assert api.get(f"{URL}/poster.png", headers=ALICE).status_code == 404
    assert api.get(f"{URL}/thumb.webp", headers=ALICE).status_code == 200
    view = api.get(URL, headers=ALICE).json()
    assert view["has_picture"] is False and view["rendered_at"] is None


def test_a_server_deleted_while_its_picture_is_drawn_keeps_nothing(
    api, alpha, export_jobs, cluster, db, tmp_path
) -> None:
    real = cluster.run_world_thumbnail

    def then_delete(name, run_id, **kw):
        real(name, run_id, **kw)
        db.delete_server(name)

    cluster.run_world_thumbnail = then_delete
    assert refresh(api).status_code == 202
    export_jobs.run()
    assert db.get_thumbnail("alpha") is None
    assert not (thumbs_dir(tmp_path) / "alpha").exists()
    events = db.events("alpha")
    failed = [e["detail"] for e in events if e["kind"] == "thumbnail.failed"]
    assert len(failed) == 1 and failed[0].endswith(": the server was deleted")
    assert "thumbnail.ready" not in [e["kind"] for e in events]


def test_a_row_gone_while_the_files_moved_takes_the_files_with_it(
    api, alpha, export_jobs, cluster, db, tmp_path
) -> None:
    real = cluster.run_world_thumbnail

    def then_forget(name, run_id, **kw):
        real(name, run_id, **kw)
        db.delete_thumbnail(name)  # what the delete hook does to the row

    cluster.run_world_thumbnail = then_forget
    assert refresh(api).status_code == 202
    export_jobs.run()
    assert db.get_thumbnail("alpha") is None
    assert not (thumbs_dir(tmp_path) / "alpha").exists()
    kinds = [e["kind"] for e in db.events("alpha")]
    assert "thumbnail.ready" not in kinds and "thumbnail.failed" not in kinds


def test_a_restart_fails_interrupted_runs_and_removes_their_pods(make_app, alpha, cluster):
    api = make_app()
    assert refresh(api).status_code == 202  # queued, never run: the process "died"
    make_app()  # the next process starts
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "failed" and "interrupted" in view["error"]
    assert ("delete_world_export_pods", "alpha") in cluster.ops


def test_a_restart_still_starts_when_the_pods_cannot_be_removed(make_app, alpha, cluster):
    api = make_app()
    assert refresh(api).status_code == 202

    def refuse(name):
        raise ClusterError("the cluster cannot be reached")

    cluster.delete_world_export_pods = refuse
    make_app()  # the next process starts regardless
    view = api.get(URL, headers=ALICE).json()
    assert view["status"] == "failed" and "interrupted" in view["error"]


def test_settings_come_from_the_environment() -> None:
    from dsh_api.config import Settings

    s = Settings.from_env(
        {
            "DSH_WORLD_THUMBNAILS": "manual",
            "DSH_THUMBNAIL_IMAGE": "img:1",
            "DSH_THUMBNAIL_SIZE": "96",
            "DSH_THUMBNAIL_DAILY_LIMIT": "5",
            "DSH_THUMBNAIL_TIMEOUT_SECONDS": "60",
        }
    )
    assert (s.world_thumbnails, s.thumbnail_image, s.thumbnail_size) == ("manual", "img:1", 96)
    assert (s.thumbnail_daily_limit, s.thumbnail_timeout_seconds) == (5, 60)
    d = Settings.from_env({})
    assert (d.world_thumbnails, d.thumbnail_image, d.thumbnail_size) == (
        "off",
        "dsh-world-thumbnail:local",
        128,
    )
    assert (d.thumbnail_daily_limit, d.thumbnail_timeout_seconds) == (3, 120)


# --- the real backend ---------------------------------------------------------------------------


def make_tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = {"thumb.webp": WEBP, "poster.png": PNG, "thumbnail.json": b'{"frames": 12}'}


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
    def __init__(self, payload: bytes, code: int = 0, stderr=("phase=thumbnail",)):
        self.payload, self.code, self.stderr = payload, code, list(stderr)
        self.argv: list[str] | None = None

    def __call__(self, argv, stdout_path, on_line, timeout):
        self.argv, self.timeout = argv, timeout
        stdout_path.write_bytes(self.payload)
        for line in self.stderr:
            on_line(line)
        return self.code, self.stderr


def go(run: Runner, stream: Stream, tmp_path: Path, phases: list | None = None) -> Path:
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=run, stream=stream)
    dest = tmp_path / "dest"
    be.run_world_thumbnail(
        "alpha",
        "0123456789ab",
        image="dsh-world-thumbnail:abc",
        args=["--server-state", "asleep", "--thumbnail-size", "128"],
        dest=dest,
        timeout=120,
        on_phase=(phases.append if phases is not None else (lambda p: None)),
    )
    return dest


def test_real_backend_runs_the_export_pod_with_the_thumbnail_image(tmp_path) -> None:
    run, stream = Runner(), Stream(make_tar(GOOD))
    phases: list[str] = []
    dest = go(run, stream, tmp_path, phases)
    pod = json.loads(run.calls[2][1])
    assert pod == export_pod_manifest(
        "t-alpha", "0123456789ab", "alpha-omcsi-mcserver", "dsh-world-thumbnail:abc", "thumbnail"
    )
    assert pod["metadata"]["labels"]["dsh.export"] == "true"  # the NetworkPolicy applies
    assert pod["metadata"]["labels"]["dsh.export/kind"] == "thumbnail"
    container = pod["spec"]["containers"][0]
    assert container["resources"]["limits"] == {"cpu": "500m", "memory": "1Gi"}
    assert {"name": "LP_NUM_THREADS", "value": "1"} in container["env"]
    assert stream.argv == [
        "kubectl", "-n", "t-alpha", "exec", "world-export-0123456789ab", "--",
        "dsh-world-export", "--thumbnail", "--server-state", "asleep", "--thumbnail-size", "128",
    ]  # fmt: skip
    assert stream.timeout == 120 and phases == ["thumbnail"]
    assert sorted(p.name for p in dest.iterdir()) == ["poster.png", "thumb.webp", "thumbnail.json"]
    assert " ".join(run.calls[-1][0]) == (
        "kubectl -n t-alpha delete pod world-export-0123456789ab --wait=false"
    )


def test_a_full_quota_is_explained_for_a_picture(tmp_path) -> None:
    calls = {"n": 0}

    def flaky(argv, *, stdin=None, stdout_path=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ClusterError('pods "x" is forbidden: exceeded quota: hosted-free')
        return ""

    with pytest.raises(ClusterError, match="no room to draw its picture"):
        go(flaky, Stream(b""), tmp_path)


@pytest.mark.parametrize(
    ("code", "stderr", "message"),
    [
        (1, ["detail: libEGL warning", "error: the picture could not be drawn"], "could not be"),
        (-9, [], "took too long"),
        (137, [], "ran out of memory"),
        (5, ["noise"], r"could not be drawn \(exit status 5\)"),
    ],
)
def test_a_failing_run_is_explained_and_the_pod_deleted(tmp_path, code, stderr, message) -> None:
    run = Runner()
    with pytest.raises(ClusterError, match=message):
        go(run, Stream(b"", code=code, stderr=stderr), tmp_path)
    assert "delete pod world-export-0123456789ab" in " ".join(run.calls[-1][0])


def test_the_detail_line_never_reaches_the_owner() -> None:
    msg = thumbnail_failure_message(1, ["detail: cannot load /opt/x.so", "error: nope"])
    assert msg == "nope"


@pytest.mark.parametrize(
    "members",
    [
        {**GOOD, "extra.txt": b"x"},
        {"thumb.webp": WEBP, "poster.png": PNG},
        {"thumb.webp": PNG, "poster.png": PNG, "thumbnail.json": b"{}"},
        {"thumb.webp": WEBP, "poster.png": b"GIF89a", "thumbnail.json": b"{}"},
        {"thumb.webp": WEBP, "poster.png": PNG, "thumbnail.json": b"not json"},
        {"../thumb.webp": WEBP, "poster.png": PNG, "thumbnail.json": b"{}"},
        {"thumb.webp": WEBP + b"x" * (4 * 1024 * 1024), "poster.png": PNG, "thumbnail.json": b"{}"},
    ],
)
def test_anything_but_the_three_files_is_refused(tmp_path, members) -> None:
    archive = tmp_path / "t.tgz"
    archive.write_bytes(make_tar(members))
    with pytest.raises(UnsafeArchive):
        extract_thumbnail(archive, tmp_path / "out")


def test_a_symlink_is_refused(tmp_path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("thumb.webp")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    archive = tmp_path / "t.tgz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(UnsafeArchive):
        extract_thumbnail(archive, tmp_path / "out")
    assert not (tmp_path / "out" / "thumb.webp").exists()


# --- dsh-world-export --thumbnail, with stand-in tools ------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "export" / "dsh_world_export.py"
if "dsh_world_export" in sys.modules:
    runner = sys.modules["dsh_world_export"]
else:
    spec = importlib.util.spec_from_file_location("dsh_world_export", SCRIPT)
    runner = importlib.util.module_from_spec(spec)
    sys.modules["dsh_world_export"] = runner
    spec.loader.exec_module(runner)

from test_export_runner import FAKE_EXPORTER, make_world, read_only, tool, tree  # noqa: E402

FAKE_THUMBNAILER = """#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
stage, out = pathlib.Path(args[0]), pathlib.Path(args[1])
if "{mode}" == "strata":
    print("mc-usd-thumbnail: cannot load /opt/strata/libstrata.so: libEGL.so.1", file=sys.stderr)
    sys.exit(3)
if "{mode}" == "blank":
    print("mc-usd-thumbnail: frame 1 at azimuth 135 is blank (0.0% drawn)", file=sys.stderr)
    sys.exit(1)
assert stage.name == "world.usda" and stage.is_file()
out.mkdir(parents=True)
(out / "thumb.webp").write_bytes(b"RIFF\\x10\\x00\\x00\\x00WEBPVP8X")
(out / "poster.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\n")
size = [int(args[args.index("--size") + 1]), int(args[args.index("--size") + 2])]
stats = {"frames": int(args[args.index("--frames") + 1]), "size": size,
         "ms": int(args[args.index("--ms") + 1]), "strata": "0.6.0", "s_total": 2.5,
         "bytes": {"thumb.webp": 16, "poster.png": 8}, "gpu": "llvmpipe", "secret": "/work/x"}
pathlib.Path(args[args.index("--stats-json") + 1]).write_text(json.dumps(stats))
"""


def run_thumb(tmp_path: Path, *extra: str, mode: str = "ok") -> tuple[int, bytes]:
    exporter = tool(tmp_path / "exp", FAKE_EXPORTER.replace("{fail}", "no"))
    thumbnailer = tool(tmp_path / "thumb", FAKE_THUMBNAILER.replace("{mode}", mode))
    out = io.BytesIO()
    code = runner.main(
        [
            "--mcserver",
            str(tmp_path / "mc"),
            "--work",
            str(tmp_path / "work"),
            "--exporter",
            exporter,
            "--thumbnailer",
            thumbnailer,
            "--thumbnail",
            *extra,
        ],  # fmt: skip
        stream=out,
    )
    return code, out.getvalue()


def test_thumbnail_mode_streams_only_the_picture_and_its_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXPORTER_PIN", "abc123")
    make_world(tmp_path / "mc")
    read_only(tmp_path / "mc")
    before = tree(tmp_path / "mc")

    code, data = run_thumb(tmp_path, "--server-state", "asleep")

    assert code == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["poster.png", "thumb.webp", "thumbnail.json"]
        meta = json.load(tar.extractfile("thumbnail.json"))
    # 128 around spawn (100, -300); the exporter got the full height.
    assert meta["area"] == {"x1": 36, "z1": -364, "x2": 163, "z2": -237}
    assert meta["snapshot"] == "at_rest" and meta["spawn"] == {"x": 100, "z": -300}
    assert (meta["frames"], meta["size"], meta["ms"]) == (12, [200, 132], 250)
    assert meta["strata"] == "0.6.0" and meta["exporter_commit"] == "abc123"
    assert "gpu" not in meta and "/work" not in json.dumps(meta)
    assert tree(tmp_path / "mc") == before


def test_thumbnail_options_reach_the_renderer(tmp_path) -> None:
    make_world(tmp_path / "mc")
    code, data = run_thumb(
        tmp_path, "--thumbnail-size", "64", "--thumbnail-frames", "6", "--thumbnail-px", "96", "64"
    )
    assert code == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        meta = json.load(tar.extractfile("thumbnail.json"))
    assert meta["area"]["x2"] - meta["area"]["x1"] == 63
    assert (meta["frames"], meta["size"]) == (6, [96, 64])


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("strata", "error: the picture could not be drawn"),
        ("blank", "error: the picture came out blank; is there anything built around spawn?"),
    ],
)
def test_renderer_failures_are_owner_safe_with_a_detail_line(tmp_path, capsys, mode, error):
    make_world(tmp_path / "mc")
    code, data = run_thumb(tmp_path, mode=mode)
    assert code == 1 and data == b""
    err = capsys.readouterr().err
    assert error in err
    assert "detail: mc-usd-thumbnail:" in err


@pytest.mark.parametrize(
    "extra",
    [
        ("--area", "0", "0", "10", "10"),
        ("--thumbnail-size", "8"),
        ("--thumbnail-size", "512"),
        ("--thumbnail-frames", "0"),
    ],
)
def test_bad_thumbnail_requests_are_refused(tmp_path, extra) -> None:
    make_world(tmp_path / "mc")
    assert run_thumb(tmp_path, *extra)[0] == 2


def test_no_world_yet_is_exit_3(tmp_path) -> None:
    (tmp_path / "mc").mkdir()
    assert run_thumb(tmp_path)[0] == 3
