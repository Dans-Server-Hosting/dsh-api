from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import FakeClusterBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.main import create_app
from dsh_api.mojang import FakeResolver

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "state.db"),
        base_domain="example.com",
        node_ip="203.0.113.10",
        omcsi_dir="/opt/omcsi",
        backup_dir=str(tmp_path / "backups"),
        max_servers_per_tenant=1,
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
def client(settings, db, cluster, resolver) -> TestClient:
    app = create_app(
        settings,
        db=db,
        cluster=cluster,
        validator=FakeValidator({"alice-token": "alice", "bob-token": "bob"}),
        uuids=resolver,
    )
    return TestClient(app)


@pytest.fixture
def created(client: TestClient) -> dict:
    """A server named ``alpha`` owned by alice, freshly provisioned."""
    resp = client.post("/api/v1/servers", json={"name": "alpha"}, headers=ALICE)
    assert resp.status_code == 201, resp.text
    return resp.json()
