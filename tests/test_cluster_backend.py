"""The real backend's command lines, checked against the operator's script.

No command is executed: a recording runner stands in for subprocess.
"""

import json
import socket
import struct
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dsh_api.cluster import (
    ClusterError,
    Credentials,
    KubectlHelmBackend,
    ReleaseSpec,
    WrapperStatus,
    helm_install_argv,
    http_json,
    server_list_ping,
    tenant_namespace_manifests,
    tenant_rolebinding_manifest,
    wrapper_api_url,
)

CREDS = Credentials(
    rcon_password="rcon-x",
    admin_password="admin-x",
    deploy_auth_token="deploy-x",
    deployment_auth_token="deployment-x",
)
SPEC = ReleaseSpec(
    name="alpha",
    hostname="alpha.play.example.com",
    sslip_hostname="alpha.203-0-113-10.sslip.io",
    motd="hi there",
    operator_name="Steve",
    operator_uuid="8667ba71-b85a-4004-af54-457a9734eed7",
    default_plugins=("https://example.com/dpm.jar",),
)


class Runner:
    """Records argv/stdin; answers from a queue of (stdout | exception)."""

    def __init__(self, *answers):
        self.calls = []
        self.answers = list(answers)

    def __call__(self, argv, *, stdin=None, stdout_path=None):
        self.calls.append((argv, stdin, stdout_path))
        answer = self.answers.pop(0) if self.answers else ""
        if isinstance(answer, Exception):
            raise answer
        if stdout_path is not None:
            Path(stdout_path).write_bytes(answer.encode())
            return ""
        return answer


@pytest.fixture
def backend(tmp_path):
    runner = Runner()
    return KubectlHelmBackend("/opt/omcsi", str(tmp_path / "backups"), "5m", run=runner), runner


def test_helm_install_line_matches_the_operator_script():
    argv = helm_install_argv(SPEC, CREDS, "/opt/omcsi")
    assert argv == [
        "helm", "upgrade", "--install", "alpha", "/opt/omcsi/helm/omcsi",
        "-n", "t-alpha",
        "-f", "/opt/omcsi/helm/omcsi/values-colocated.yaml",
        "--set", "ingress.hosts[0].host=alpha.play.example.com",
        "--set", "ingress.hosts[1].host=alpha.203-0-113-10.sslip.io",
        "--set", "ingress.annotations.cert-manager\\.io/cluster-issuer=letsencrypt-prod",
        "--set", "ingress.tls[0].hosts[0]=alpha.play.example.com",
        "--set", "ingress.tls[0].secretName=alpha-tls",
        "--set-string",
        "minecraftWrapper.service.annotations.mc-router\\.itzg\\.me/externalServerName="
        "alpha.play.example.com\\,alpha.203-0-113-10.sslip.io",
        "--set", "minecraftWrapper.env.SERVER_MOTD=hi there",
        "--set", "webapp.env.MC_MOTD=hi there",
        "--set", "webapp.env.DASHBOARD_TITLE=alpha",
        "--set", "minecraftWrapper.env.DEFAULT_PLUGINS=https://example.com/dpm.jar",
        "--set", "minecraftWrapper.env.OPERATOR_NAME=Steve",
        "--set", "minecraftWrapper.env.OPERATOR_UUID=8667ba71-b85a-4004-af54-457a9734eed7",
        "--set", "secrets.rconPassword=rcon-x",
        "--set", "secrets.adminPassword=admin-x",
        "--set", "secrets.deployAuthToken=deploy-x",
        "--set", "secrets.deploymentAuthToken=deployment-x",
    ]  # fmt: skip
    assert "--wait" not in argv  # the webapp waits for the wrapper; helm must not wait for both


def test_helm_install_keeps_an_awake_wrapper_awake():
    argv = helm_install_argv(SPEC, CREDS, "/opt/omcsi", keep_awake=True)
    assert argv[-2:] == ["--set", "minecraftWrapper.replicas=1"]


def test_install_release_reads_the_wrapper_first(tmp_path):
    # Fresh tenant: no StatefulSet, so the profile's replicas: 0 stands.
    runner = Runner(ClusterError('statefulsets.apps "x" not found'), "")
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=runner)
    be.install_release(SPEC, CREDS)
    assert runner.calls[0][0][3:5] == ["get", "statefulset"]
    assert "minecraftWrapper.replicas=1" not in runner.calls[1][0]
    # Re-run under players: the wrapper is at 1, so it is kept there.
    runner = Runner(sts(1, 1), pods(), "")
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=runner)
    be.install_release(SPEC, CREDS)
    assert runner.calls[-1][0][-2:] == ["--set", "minecraftWrapper.replicas=1"]


def test_wait_for_rollout_covers_wrapper_webapp_and_nginx(tmp_path):
    runner = Runner()
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), "7m", run=runner)
    be.wait_for_rollout("alpha")
    assert [c[0] for c in runner.calls] == [
        ["kubectl", "-n", "t-alpha", "rollout", "status", target, "--timeout=7m"]
        for target in (
            "statefulset/alpha-omcsi-minecraft-wrapper",
            "deployment/alpha-omcsi-webapp",
            "deployment/alpha-omcsi-nginx",
        )
    ]


def test_helm_install_without_operator_omits_operator_flags():
    spec = ReleaseSpec(name="a", hostname="h", sslip_hostname="s", motd="m")
    argv = helm_install_argv(spec, CREDS, "/opt/omcsi")
    assert not any("OPERATOR" in a for a in argv)


def test_helm_install_without_default_plugins_omits_the_flag():
    spec = ReleaseSpec(name="a", hostname="h", sslip_hostname="s", motd="m")
    argv = helm_install_argv(spec, CREDS, "/opt/omcsi")
    assert not any("DEFAULT_PLUGINS" in a for a in argv)


def test_helm_install_escapes_commas_between_default_plugins():
    # ``helm --set`` splits a bare comma into a list; the wrapper wants one string.
    spec = ReleaseSpec(
        name="a", hostname="h", sslip_hostname="s", motd="m",
        default_plugins=("https://a/x.jar", "https://b/y.jar"),
    )  # fmt: skip
    argv = helm_install_argv(spec, CREDS, "/opt/omcsi")
    i = argv.index("minecraftWrapper.env.DEFAULT_PLUGINS=https://a/x.jar\\,https://b/y.jar")
    assert argv[i - 1] == "--set"


def test_tenant_namespace_manifests_are_the_hosted_free_profile():
    items = tenant_namespace_manifests("alpha")["items"]
    ns, quota, limits = items
    assert ns["kind"] == "Namespace" and ns["metadata"]["name"] == "t-alpha"
    assert ns["metadata"]["labels"] == {
        "dsh.tenant": "alpha",
        "pod-security.kubernetes.io/enforce": "baseline",
        "pod-security.kubernetes.io/warn": "restricted",
    }
    assert quota["kind"] == "ResourceQuota"
    assert quota["metadata"] == {"name": "hosted-free", "namespace": "t-alpha"}
    assert quota["spec"]["hard"] == {
        "requests.cpu": "1",
        "requests.memory": "3Gi",
        "limits.cpu": "3",
        "limits.memory": "5Gi",
        "requests.storage": "8Gi",
        "pods": "6",
    }
    assert limits["kind"] == "LimitRange"
    assert limits["spec"]["limits"] == [
        {
            "type": "Container",
            "default": {"cpu": "200m", "memory": "128Mi"},
            "defaultRequest": {"cpu": "20m", "memory": "32Mi"},
        }
    ]


def test_tenant_rolebinding_binds_the_tenant_clusterrole_to_the_api():
    binding = tenant_rolebinding_manifest("alpha", "dsh-api", "dsh-api")
    assert binding == {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "dsh-api", "namespace": "t-alpha"},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "dsh-api-tenant",
        },
        "subjects": [{"kind": "ServiceAccount", "name": "dsh-api", "namespace": "dsh-api"}],
    }
    # The ClusterRole name is the one deploy/rbac.yaml grants ``bind`` on.
    rbac = (Path(__file__).parent.parent / "deploy" / "rbac.yaml").read_text()
    assert "resourceNames: [dsh-api-tenant]" in rbac
    assert "name: dsh-api-tenant" in rbac


def test_namespace_binding_and_credentials_are_applied_over_stdin(tmp_path):
    runner = Runner()
    be = KubectlHelmBackend(
        "/opt/omcsi", str(tmp_path), service_account=("platform", "api-sa"), run=runner
    )
    be.create_tenant_namespace("alpha")
    be.grant_tenant_access("alpha")
    be.create_credentials("alpha", CREDS)
    (argv1, stdin1, _), (argv2, stdin2, _), (argv3, stdin3, _) = runner.calls
    assert argv1 == argv2 == argv3 == ["kubectl", "apply", "-f", "-"]
    assert json.loads(stdin1)["kind"] == "List"
    binding = json.loads(stdin2)
    assert binding["kind"] == "RoleBinding"
    assert binding["metadata"]["namespace"] == "t-alpha"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "api-sa", "namespace": "platform"}
    ]
    secret = json.loads(stdin3)
    assert secret["kind"] == "Secret"
    assert secret["metadata"] == {"name": "dsh-credentials", "namespace": "t-alpha"}
    assert secret["stringData"] == {
        "rconPassword": "rcon-x",
        "adminPassword": "admin-x",
        "deployAuthToken": "deploy-x",
        "deploymentAuthToken": "deployment-x",
    }
    # Credentials never travel on a command line for kubectl.
    assert not any("admin-x" in a for a in argv2)


def test_namespace_exists(tmp_path):
    runner = Runner("namespace/t-alpha\n", ClusterError('namespaces "t-b" not found'))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=runner)
    assert be.namespace_exists("alpha") is True
    assert be.namespace_exists("b") is False
    assert runner.calls[0][0] == ["kubectl", "get", "namespace", "t-alpha", "-o", "name"]


def test_namespace_exists_propagates_other_errors(tmp_path):
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=Runner(ClusterError("forbidden")))
    with pytest.raises(ClusterError):
        be.namespace_exists("alpha")


def test_scale_uninstall_and_delete_lines(backend):
    be, runner = backend
    be.scale_wrapper("alpha", 1)
    be.uninstall_release("alpha")
    be.delete_namespace("alpha")
    assert [c[0] for c in runner.calls] == [
        ["kubectl", "-n", "t-alpha", "scale", "statefulset", "alpha-omcsi-minecraft-wrapper",
         "--replicas=1"],
        ["helm", "uninstall", "alpha", "-n", "t-alpha"],
        ["kubectl", "delete", "namespace", "t-alpha", "--wait=false"],
    ]  # fmt: skip


def sts(replicas, ready):
    return json.dumps({"spec": {"replicas": replicas}, "status": {"readyReplicas": ready}})


def pods(*reasons, phase="Running", created="2026-09-13T10:00:00Z"):
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"creationTimestamp": created},
                    "status": {
                        "phase": phase,
                        "containerStatuses": [
                            {"state": {"waiting": {"reason": r}}} for r in reasons
                        ],
                    },
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "answers, expected",
    [
        ([ClusterError('statefulsets.apps "x" not found')], "failed"),
        ([sts(0, 0)], "asleep"),
        ([sts(1, 0), pods("ContainerCreating")], "waking"),
        ([sts(1, 1), pods()], "awake"),
        ([sts(1, 0), pods("CrashLoopBackOff")], "failed"),
        ([sts(1, 1), pods(phase="Failed")], "failed"),
    ],
)
def test_statefulset_status_reads_the_statefulset(tmp_path, answers, expected):
    runner = Runner(*answers)
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=runner)
    assert be.statefulset_status("alpha").state == expected
    assert runner.calls[0][0][:6] == ["kubectl", "-n", "t-alpha", "get", "statefulset",
                                      "alpha-omcsi-minecraft-wrapper"]  # fmt: skip


def test_statefulset_status_carries_the_pod_creation_time_as_scaled_at(tmp_path):
    """Scaling to zero deletes the pod, so its creation time is the last scale-up."""
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=Runner(sts(1, 1), pods()))
    status = be.statefulset_status("alpha")
    assert status.scaled_at == datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
    # Asleep: no pod to read, and no second kubectl call.
    runner = Runner(sts(0, 0))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=runner)
    assert be.statefulset_status("alpha").scaled_at is None
    assert len(runner.calls) == 1
    # A pod without a usable timestamp leaves it unset rather than failing.
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=Runner(sts(1, 1), pods(created="")))
    assert be.statefulset_status("alpha").scaled_at is None


# --- the wrapper's own API ----------------------------------------------------


class Http:
    """Records (url, method, timeout); answers from a queue like ``Runner``."""

    def __init__(self, *answers):
        self.calls = []
        self.answers = list(answers)

    def __call__(self, url, method="GET", timeout=3.0):
        self.calls.append((url, method, timeout))
        return self.answers.pop(0) if self.answers else None


def test_wrapper_api_url_is_the_internal_service_in_the_tenant_namespace():
    assert wrapper_api_url("alpha", "/api/server/status") == (
        "http://alpha-omcsi-minecraft-wrapper-internal.t-alpha.svc.cluster.local:8092"
        "/api/server/status"
    )


def test_wrapper_status_gets_the_status_endpoint(tmp_path):
    http = Http((200, {"running": True, "pid": 42, "uptimeSeconds": 900, "startedAt": "t0"}))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), http=http)
    assert be.wrapper_status("alpha") == WrapperStatus(
        running=True, pid=42, uptime_seconds=900, started_at="t0"
    )
    assert http.calls == [(wrapper_api_url("alpha", "/api/server/status"), "GET", 3.0)]


def test_wrapper_status_reports_a_stopped_game(tmp_path):
    http = Http((200, {"running": False, "pid": None, "uptimeSeconds": 0, "startedAt": None}))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), http=http)
    assert be.wrapper_status("alpha") == WrapperStatus(running=False, uptime_seconds=0)


@pytest.mark.parametrize(
    "answer",
    [
        None,  # unreachable: refused, unresolvable or timed out
        (503, {"status": "DOWN"}),  # not 200
        (200, "not json"),  # 200 but not the status document
        (200, {"pid": 1}),  # no ``running`` field
        (200, {"running": "yes"}),  # wrong type
    ],
)
def test_wrapper_status_is_none_unless_the_wrapper_answers_properly(tmp_path, answer):
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), http=Http(answer))
    assert be.wrapper_status("alpha") is None


def test_start_wrapper_posts_to_the_start_endpoint(tmp_path):
    http = Http((200, {"running": True}))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), http=http)
    be.start_wrapper("alpha")
    assert http.calls == [(wrapper_api_url("alpha", "/api/server/start"), "POST", 3.0)]


@pytest.mark.parametrize("answer, match", [(None, "could not be reached"), ((409, {}), "409")])
def test_start_wrapper_failure_is_a_cluster_error(tmp_path, answer, match):
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), http=Http(answer))
    with pytest.raises(ClusterError, match=match):
        be.start_wrapper("alpha")


def fake_wrapper_api(routes: dict, delay: float = 0.0):
    """A loopback HTTP server answering ``{(method, path): (status, body)}``."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            seen.append((self.command, self.path))
            time.sleep(delay)
            status, body = routes.get((self.command, self.path), (404, {"error": "no route"}))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _answer

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", seen, srv


def test_http_json_against_a_loopback_wrapper():
    base, seen, srv = fake_wrapper_api(
        {
            ("GET", "/api/server/status"): (200, {"running": False, "pid": None}),
            ("POST", "/api/server/start"): (200, {"running": True}),
        }
    )
    try:
        assert http_json(f"{base}/api/server/status") == (200, {"running": False, "pid": None})
        assert http_json(f"{base}/api/server/start", method="POST") == (200, {"running": True})
        assert http_json(f"{base}/nope") == (404, {"error": "no route"})
        assert seen == [
            ("GET", "/api/server/status"),
            ("POST", "/api/server/start"),
            ("GET", "/nope"),
        ]
    finally:
        srv.shutdown()


def test_http_json_is_none_on_timeout_or_refused_connection():
    base, _, srv = fake_wrapper_api({("GET", "/slow"): (200, {})}, delay=1.0)
    try:
        started = time.monotonic()
        assert http_json(f"{base}/slow", timeout=0.2) is None
        assert time.monotonic() - started < 1.0  # the timeout, not the server, decided
    finally:
        srv.shutdown()
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert http_json(f"http://127.0.0.1:{port}/api/server/status", timeout=0.5) is None


def test_backup_of_an_awake_server_execs_tar(tmp_path):
    runner = Runner("tarball-bytes")
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=runner)
    path = be.backup_world("alpha", awake=True)
    assert path.parent == tmp_path / "b"
    assert path.name.startswith("alpha-") and path.name.endswith(".tar.gz")
    assert path.read_bytes() == b"tarball-bytes"
    argv, _, stdout_path = runner.calls[0]
    assert stdout_path == path
    assert argv == ["kubectl", "-n", "t-alpha", "exec", "alpha-omcsi-minecraft-wrapper-0", "--",
                    "tar", "-C", "/mcserver", "-czf", "-", "."]  # fmt: skip


def test_backup_of_an_asleep_server_mounts_the_pvc_in_a_throwaway_pod(tmp_path):
    runner = Runner("persistentvolumeclaim/alpha-omcsi-mcserver\n", "tarball-bytes")
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=runner)
    be.backup_world("alpha", awake=False)
    # The claim is looked up first, so a missing one fails fast instead of
    # leaving a reader pod Pending.
    assert runner.calls[0][0] == ["kubectl", "-n", "t-alpha", "get", "pvc", "alpha-omcsi-mcserver",
                                  "-o", "name"]  # fmt: skip
    argv = runner.calls[1][0]
    assert argv[:9] == ["kubectl", "-n", "t-alpha", "run", "backup-reader", "--rm", "-i",
                        "--restart=Never", "--image=busybox:1.36"]  # fmt: skip
    overrides = json.loads(argv[9].removeprefix("--overrides="))
    container = overrides["spec"]["containers"][0]
    assert container["command"] == ["tar", "-C", "/mcserver", "-czf", "-", "."]
    assert container["stdin"] is True
    assert overrides["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == (
        "alpha-omcsi-mcserver"
    )


def test_backup_of_a_server_without_a_world_volume_fails_fast(tmp_path):
    """A create that failed before helm ran has no PVC; nothing is started for it."""
    runner = Runner(ClusterError('persistentvolumeclaims "alpha-omcsi-mcserver" not found'))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=runner)
    with pytest.raises(ClusterError, match="not found"):
        be.backup_world("alpha", awake=False)
    assert len(runner.calls) == 1  # no ``kubectl run``
    assert not (tmp_path / "b").exists() or list((tmp_path / "b").iterdir()) == []


def test_empty_backup_is_refused_and_removed(tmp_path):
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=Runner(""))
    with pytest.raises(ClusterError, match="empty"):
        be.backup_world("alpha", awake=True)
    assert list((tmp_path / "b").iterdir()) == []


def test_failed_backup_leaves_no_file(tmp_path):
    runner = Runner("persistentvolumeclaim/alpha-omcsi-mcserver\n", ClusterError("boom"))
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path / "b"), run=runner)
    with pytest.raises(ClusterError, match="boom"):
        be.backup_world("alpha", awake=False)
    assert list((tmp_path / "b").iterdir()) == []


# --- server list ping -------------------------------------------------------


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def fake_minecraft_server(status: dict):
    """A loopback socket that answers one status handshake like a game server."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(2)
            conn.recv(4096)  # handshake + status request
            payload = json.dumps(status).encode()
            body = _varint(0x00) + _varint(len(payload)) + payload
            conn.sendall(_varint(len(body)) + body)

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_server_list_ping_reads_the_player_count():
    port = fake_minecraft_server({"players": {"online": 3, "max": 10}, "version": {"name": "x"}})
    status = server_list_ping("127.0.0.1", port, timeout=2)
    assert status["players"]["online"] == 3


def test_handshake_packet_shape():
    """What the client sends is a status handshake followed by a status request."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received = {}

    def record():
        conn, _ = srv.accept()
        with conn:
            received["data"] = conn.recv(4096)  # then close: the client sees EOF

    threading.Thread(target=record, daemon=True).start()
    with pytest.raises(OSError):
        server_list_ping("127.0.0.1", port, timeout=2)
    data = received["data"]
    host = b"127.0.0.1"
    handshake_body = _varint(0) + _varint(0) + _varint(len(host)) + host
    handshake_body += struct.pack(">H", port) + _varint(1)
    assert data.startswith(_varint(len(handshake_body)) + handshake_body)
    assert data.endswith(_varint(1) + _varint(0))  # the status request packet


def test_players_online_is_none_when_the_server_cannot_be_asked(tmp_path, monkeypatch):
    import dsh_api.cluster as cluster

    def refuse(host, port=25565, timeout=2.0):
        assert host == "alpha-omcsi-minecraft-wrapper.t-alpha.svc"
        raise ConnectionRefusedError

    monkeypatch.setattr(cluster, "server_list_ping", refuse)
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=Runner())
    assert be.players_online("alpha") is None


def test_players_online_reads_the_ping(tmp_path, monkeypatch):
    import dsh_api.cluster as cluster

    monkeypatch.setattr(cluster, "server_list_ping", lambda *a, **k: {"players": {"online": 2}})
    be = KubectlHelmBackend("/opt/omcsi", str(tmp_path), run=Runner())
    assert be.players_online("alpha") == 2


def test_run_command_reports_failures(tmp_path):
    from dsh_api.cluster import run_command

    with pytest.raises(ClusterError, match="could not be run"):
        run_command(["/nonexistent/kubectl", "get"])
    with pytest.raises(ClusterError, match="false"):
        run_command(["false", "x"])
    assert run_command(["echo", "hi"]) == "hi\n"
    out = tmp_path / "out"
    run_command(["echo", "hi"], stdout_path=out)
    assert out.read_bytes() == b"hi\n"


def test_generated_credentials_are_distinct_and_shell_safe():
    a, b = Credentials.generate(), Credentials.generate()
    assert a != b
    for value in (a.rcon_password, a.admin_password, a.deploy_auth_token, a.deployment_auth_token):
        assert value.isalnum() and len(value) >= 16
