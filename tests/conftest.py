from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import FakeClusterBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.main import create_app
from dsh_api.mojang import FakeResolver


class ManualJobs:
    """A ``JobRunner`` that queues provisioning jobs and runs them on demand.

    The app's real one is a thread pool; here the test decides when the
    background work happens, so the in-between (``provisioning``) can be
    observed and the outcome asserted without sleeping.
    """

    def __init__(self) -> None:
        self.pending: list[tuple[Callable[..., object], tuple[object, ...]]] = []
        self.ran = 0

    def submit(self, fn: Callable[..., object], /, *args: object) -> None:
        self.pending.append((fn, args))

    def run(self) -> int:
        """Run everything queued (in order); returns how many jobs ran."""
        ran = 0
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)
            ran += 1
        self.ran += ran
        return ran


@pytest.fixture
def jobs() -> ManualJobs:
    return ManualJobs()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "state.db"),
        base_domain="example.com",
        node_ip="203.0.113.10",
        omcsi_dir="/opt/omcsi",
        backup_dir=str(tmp_path / "backups"),
        max_servers_per_tenant=1,
        admin_users=frozenset({"admin"}),
    )


@pytest.fixture
def cluster(tmp_path: Path) -> FakeClusterBackend:
    return FakeClusterBackend(backup_dir=tmp_path / "backups")


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings.db_path)


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver({"Steve": "8667ba71-b85a-4004-af54-457a9734eed7"})


@pytest.fixture
def client(settings, db, cluster, resolver, jobs) -> TestClient:
    app = create_app(
        settings,
        db=db,
        cluster=cluster,
        validator=FakeValidator(
            {"alice-token": "alice", "bob-token": "bob", "admin-token": "admin"}
        ),
        uuids=resolver,
        jobs=jobs,
    )
    return TestClient(app)


@pytest.fixture
def created(client: TestClient, jobs: ManualJobs) -> dict:
    """A server named ``alpha`` owned by alice, provisioned to completion.

    The body is the 202 (state ``provisioning``, with the one-time admin
    password); the provisioning job has then been run, so a ``GET`` sees it
    ``awake``.
    """
    resp = client.post(
        "/api/v1/servers", json={"name": "alpha"}, headers={"Authorization": "Bearer alice-token"}
    )
    assert resp.status_code == 202, resp.text
    assert jobs.run() == 1
    return resp.json()
