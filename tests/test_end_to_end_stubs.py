"""The real backend driven through stub ``kubectl``/``helm`` executables.

This exercises the subprocess plumbing (argv, stdin, stdout capture) end to
end through the HTTP layer, still with no cluster: the stubs log what they were
asked and answer like the cluster would.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_api.auth import FakeValidator
from dsh_api.cluster import KubectlHelmBackend
from dsh_api.config import Settings
from dsh_api.db import Database
from dsh_api.main import create_app
from dsh_api.mojang import FakeResolver

STUB = """#!{python}
import json, os, sys
log = os.environ["STUB_LOG"]
argv = sys.argv
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
with open(log, "a") as f:
    f.write(json.dumps({{"argv": argv, "stdin": stdin}}) + "\\n")
name = os.path.basename(argv[0])
if name == "kubectl" and argv[1:3] == ["get", "namespace"]:
    sys.stderr.write('Error from server (NotFound): namespaces "x" not found\\n'); sys.exit(1)
if name == "kubectl" and "statefulset" in argv and "-o" in argv:
    print(json.dumps({{"spec": {{"replicas": 1}}, "status": {{"readyReplicas": 1}}}}))
elif name == "kubectl" and "pods" in argv:
    print(json.dumps({{"items": []}}))
elif name == "kubectl" and "exec" in argv:
    sys.stdout.write("pretend-tarball")
"""


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("kubectl", "helm"):
        path = bindir / tool
        path.write_text(STUB.format(python=sys.executable))
        path.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    settings = Settings(
        db_path=str(tmp_path / "s.db"),
        base_domain="example.com",
        node_ip="203.0.113.10",
        omcsi_dir=str(tmp_path / "omcsi"),
        backup_dir=str(tmp_path / "backups"),
    )
    app = create_app(
        settings,
        db=Database(settings.db_path),
        cluster=KubectlHelmBackend(settings.omcsi_dir, settings.backup_dir),
        validator=FakeValidator({"t": "alice"}),
        uuids=FakeResolver(),
    )
    return TestClient(app), log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_create_get_and_delete_through_real_subprocesses(stubbed):
    client, log = stubbed
    auth = {"Authorization": "Bearer t"}

    resp = client.post("/api/v1/servers", json={"name": "alpha", "motd": "hey"}, headers=auth)
    assert resp.status_code == 201, resp.text
    seen = calls(log)
    tools = [os.path.basename(c["argv"][0]) + " " + " ".join(c["argv"][1:3]) for c in seen]
    assert tools == [
        "kubectl get namespace",
        "kubectl apply -f",
        "kubectl apply -f",
        "helm upgrade --install",
        "kubectl -n t-alpha",  # scale
        "kubectl -n t-alpha",  # statefulset read for the response
        "kubectl -n t-alpha",  # pods read
    ]
    assert json.loads(seen[1]["stdin"])["kind"] == "List"
    assert json.loads(seen[2]["stdin"])["metadata"]["name"] == "dsh-credentials"
    helm = seen[3]["argv"]
    assert "--set" in helm and "minecraftWrapper.env.SERVER_MOTD=hey" in helm
    assert helm[-3:] == ["--wait", "--timeout", "5m"]
    assert "scale" in seen[4]["argv"]

    resp = client.get("/api/v1/servers/alpha", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["state"] == "awake"
    assert resp.json()["players_online"] is None  # no game server to ping

    resp = client.delete("/api/v1/servers/alpha", headers=auth)
    assert resp.status_code == 200, resp.text
    backup = Path(resp.json()["backup"])
    assert backup.read_bytes() == b"pretend-tarball"
    tail = [[os.path.basename(c["argv"][0]), *c["argv"][1:]] for c in calls(log)[-3:]]
    assert "exec" in tail[0]
    assert tail[1][:2] == ["helm", "uninstall"]
    assert tail[2][:3] == ["kubectl", "delete", "namespace"]
