"""Everything that touches the cluster, behind one interface.

``KubectlHelmBackend`` shells out to ``kubectl`` and ``helm`` exactly the way
the operator's provisioning script does; ``FakeClusterBackend`` keeps the same
state in memory so every handler can be tested without a cluster.
"""

from __future__ import annotations

import json
import secrets
import socket
import struct
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

TENANT_CLUSTER_ROLE = "dsh-api-tenant"
"""The ClusterRole with the namespaced rules (deploy/rbac.yaml), bound into
each tenant namespace by a RoleBinding the API creates."""
TENANT_ROLE_BINDING = "dsh-api"
WRAPPER_PVC_SUFFIX = "-omcsi-mcserver"
WRAPPER_STS_SUFFIX = "-omcsi-minecraft-wrapper"
WRAPPER_COMPONENT_LABEL = "app.kubernetes.io/component=minecraft-wrapper"
WRAPPER_API_PORT = 8092
WRAPPER_HTTP_TIMEOUT = 3.0
FAILING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Error", "OOMKilled"}


class ClusterError(Exception):
    """A cluster operation failed; the message is safe to show to the caller."""


def namespace_for(name: str) -> str:
    return f"t-{name}"


@dataclass(frozen=True)
class Credentials:
    rcon_password: str
    admin_password: str
    deploy_auth_token: str
    deployment_auth_token: str

    @classmethod
    def generate(cls) -> Credentials:
        return cls(
            rcon_password=secrets.token_hex(16),
            admin_password=secrets.token_urlsafe(18).replace("-", "").replace("_", ""),
            deploy_auth_token=secrets.token_hex(24),
            deployment_auth_token=secrets.token_hex(24),
        )


@dataclass(frozen=True)
class ReleaseSpec:
    name: str
    hostname: str
    sslip_hostname: str
    motd: str
    operator_name: str | None = None
    operator_uuid: str | None = None
    default_plugins: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatefulSetStatus:
    """What the wrapper StatefulSet says: does the pod exist and is it ready."""

    exists: bool
    replicas: int = 0
    ready_replicas: int = 0
    failing: bool = False
    scaled_at: datetime | None = None
    """When the wrapper pod was created, i.e. when the server was last scaled up."""

    @property
    def state(self) -> str:
        """The replica-based reading alone; ``service.server_state`` refines it
        with what the wrapper process reports."""
        if not self.exists or self.failing:
            return "failed"
        if self.replicas == 0:
            return "asleep"
        return "awake" if self.ready_replicas >= 1 else "waking"


@dataclass(frozen=True)
class WrapperStatus:
    """What the wrapper's own API says about the game process it supervises.

    A ready pod only means the wrapper's Spring application is up; whether the
    Minecraft process is running is a separate question, and this answers it.
    """

    running: bool
    pid: int | None = None
    uptime_seconds: int | None = None
    started_at: str | None = None

    @classmethod
    def from_json(cls, data: object) -> WrapperStatus | None:
        if not isinstance(data, dict) or not isinstance(data.get("running"), bool):
            return None
        pid = data.get("pid")
        uptime = data.get("uptimeSeconds")
        started = data.get("startedAt")
        return cls(
            running=data["running"],
            pid=int(pid) if isinstance(pid, int | float) else None,
            uptime_seconds=int(uptime) if isinstance(uptime, int | float) else None,
            started_at=str(started) if started is not None else None,
        )


class ClusterBackend(Protocol):
    def namespace_exists(self, name: str) -> bool: ...

    def create_tenant_namespace(self, name: str) -> None:
        """Namespace ``t-<name>`` with its labels, ResourceQuota and LimitRange."""

    def grant_tenant_access(self, name: str) -> None:
        """RoleBinding in ``t-<name>`` giving the API's ServiceAccount the
        ``dsh-api-tenant`` ClusterRole there. Everything namespaced that follows
        (the Secret, helm, scale, exec) is allowed by this binding alone."""

    def create_credentials(self, name: str, creds: Credentials) -> None:
        """Secret ``dsh-credentials`` in the tenant namespace."""

    def install_release(self, spec: ReleaseSpec, creds: Credentials) -> None:
        """``helm upgrade --install`` without waiting: the webapp's init container
        blocks until the wrapper is healthy, and the profile installs the wrapper
        asleep, so a wait here could never finish."""

    def scale_wrapper(self, name: str, replicas: int) -> None: ...

    def wait_for_rollout(self, name: str) -> None:
        """Block until the wrapper StatefulSet, webapp and nginx have rolled out."""

    def statefulset_status(self, name: str) -> StatefulSetStatus:
        """Replicas and readiness of the wrapper StatefulSet."""

    def wrapper_status(self, name: str) -> WrapperStatus | None:
        """What the wrapper reports about the game process, or None when the
        wrapper cannot be asked (pod gone, not listening yet, timeout)."""

    def start_wrapper(self, name: str) -> None:
        """Ask a running wrapper to start the game process it supervises."""

    def players_online(self, name: str) -> int | None:
        """Player count from the game server, or None when it cannot be asked."""

    def backup_world(self, name: str, awake: bool) -> Path:
        """Write a tarball of /mcserver to the backup directory; return its path."""

    def uninstall_release(self, name: str) -> None: ...

    def delete_namespace(self, name: str) -> None: ...


# ---------------------------------------------------------------------------
# Real backend
# ---------------------------------------------------------------------------


def run_command(
    argv: list[str], *, stdin: str | None = None, stdout_path: Path | None = None
) -> str:
    """Run a command; raise ClusterError with its stderr on failure."""
    try:
        if stdout_path is None:
            proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
        else:
            with stdout_path.open("wb") as out:
                proc = subprocess.run(
                    argv,
                    input=stdin.encode() if stdin else None,
                    stdout=out,
                    stderr=subprocess.PIPE,
                    check=False,
                )
    except OSError as exc:
        raise ClusterError(f"{argv[0]} could not be run: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise ClusterError(f"{argv[0]} {argv[1]} failed: {err.strip()[:500]}")
    return proc.stdout if isinstance(proc.stdout, str) else ""


def pvc_reader_pod_overrides(claim: str) -> dict:
    """A restricted-profile-compatible pod that idles with a tenant's world
    PVC mounted read-only, for ``kubectl exec ... tar``. uid/gid/fsGroup 1000
    match the wrapper's, so the world's files are readable; the limits fit
    beside an awake release under the hosted-free quota."""
    return {
        "spec": {
            "restartPolicy": "Never",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "r",
                    "image": "busybox:1.36",
                    "command": ["sleep", "3600"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "400m", "memory": "256Mi"},
                    },
                    "volumeMounts": [{"name": "w", "mountPath": "/mcserver", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "w", "persistentVolumeClaim": {"claimName": claim}}],
        }
    }


def tenant_namespace_manifests(name: str) -> dict:
    """The namespace, quota and limit range of the hosted-free profile."""
    ns = namespace_for(name)
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": ns,
                    "labels": {
                        "dsh.tenant": name,
                        "pod-security.kubernetes.io/enforce": "baseline",
                        "pod-security.kubernetes.io/warn": "restricted",
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "hosted-free", "namespace": ns},
                "spec": {
                    "hard": {
                        "requests.cpu": "1",
                        "requests.memory": "3Gi",
                        "limits.cpu": "3",
                        "limits.memory": "5Gi",
                        "requests.storage": "8Gi",
                        "pods": "6",
                    }
                },
            },
            {
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": {"name": "hosted-free", "namespace": ns},
                "spec": {
                    "limits": [
                        {
                            "type": "Container",
                            "default": {"cpu": "200m", "memory": "128Mi"},
                            "defaultRequest": {"cpu": "20m", "memory": "32Mi"},
                        }
                    ]
                },
            },
        ],
    }


def tenant_rolebinding_manifest(name: str, sa_namespace: str, sa_name: str) -> dict:
    """The binding that scopes the API's namespaced permissions to this tenant."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": TENANT_ROLE_BINDING, "namespace": namespace_for(name)},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": TENANT_CLUSTER_ROLE,
        },
        "subjects": [{"kind": "ServiceAccount", "name": sa_name, "namespace": sa_namespace}],
    }


def helm_install_argv(
    spec: ReleaseSpec, creds: Credentials, omcsi_dir: str, keep_awake: bool = False
) -> list[str]:
    """The operator's ``helm upgrade --install`` line, reproduced argument for argument.

    ``keep_awake`` is set on a re-run for a wrapper that is already at one
    replica, so the profile's ``replicas: 0`` is not re-applied under players.
    """
    chart = f"{omcsi_dir}/helm/omcsi"
    router_hosts = f"{spec.hostname}\\,{spec.sslip_hostname}"
    argv = [
        "helm", "upgrade", "--install", spec.name, chart,
        "-n", namespace_for(spec.name),
        "-f", f"{chart}/values-colocated.yaml",
        "--set", f"ingress.hosts[0].host={spec.hostname}",
        "--set", f"ingress.hosts[1].host={spec.sslip_hostname}",
        "--set", "ingress.annotations.cert-manager\\.io/cluster-issuer=letsencrypt-prod",
        "--set", f"ingress.tls[0].hosts[0]={spec.hostname}",
        "--set", f"ingress.tls[0].secretName={spec.name}-tls",
        "--set-string",
        f"minecraftWrapper.service.annotations.mc-router\\.itzg\\.me/externalServerName={router_hosts}",
        "--set", f"minecraftWrapper.env.SERVER_MOTD={spec.motd}",
        "--set", f"webapp.env.MC_MOTD={spec.motd}",
        "--set", f"webapp.env.DASHBOARD_TITLE={spec.name}",
    ]  # fmt: skip
    if spec.default_plugins:
        # A bare comma is a list separator to ``helm --set``; the wrapper wants one string.
        plugins = "\\,".join(spec.default_plugins)
        argv += ["--set", f"minecraftWrapper.env.DEFAULT_PLUGINS={plugins}"]
    if spec.operator_name:
        argv += ["--set", f"minecraftWrapper.env.OPERATOR_NAME={spec.operator_name}"]
    if spec.operator_uuid:
        argv += ["--set", f"minecraftWrapper.env.OPERATOR_UUID={spec.operator_uuid}"]
    argv += [
        "--set", f"secrets.rconPassword={creds.rcon_password}",
        "--set", f"secrets.adminPassword={creds.admin_password}",
        "--set", f"secrets.deployAuthToken={creds.deploy_auth_token}",
        "--set", f"secrets.deploymentAuthToken={creds.deployment_auth_token}",
    ]  # fmt: skip
    if keep_awake:
        argv += ["--set", "minecraftWrapper.replicas=1"]
    return argv


def _read_varint(sock: socket.socket) -> int:
    value = shift = 0
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("connection closed mid-varint")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
        shift += 7
        if shift > 35:
            raise ValueError("varint too long")


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _packet(packet_id: int, payload: bytes) -> bytes:
    body = _varint(packet_id) + payload
    return _varint(len(body)) + body


def server_list_ping(host: str, port: int = 25565, timeout: float = 2.0) -> dict:
    """The Minecraft status handshake; returns the server's status JSON."""
    encoded_host = host.encode()
    handshake = _packet(
        0x00,
        _varint(0)
        + _varint(len(encoded_host))
        + encoded_host
        + struct.pack(">H", port)
        + _varint(1),
    )
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(handshake + _packet(0x00, b""))
        _read_varint(sock)  # packet length
        _read_varint(sock)  # packet id
        length = _read_varint(sock)
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


def wrapper_api_url(name: str, path: str) -> str:
    """The wrapper's internal API, reachable only from inside the cluster."""
    host = f"{name}{WRAPPER_STS_SUFFIX}-internal.{namespace_for(name)}.svc.cluster.local"
    return f"http://{host}:{WRAPPER_API_PORT}{path}"


def http_json(
    url: str, method: str = "GET", timeout: float = WRAPPER_HTTP_TIMEOUT
) -> tuple[int, object] | None:
    """One HTTP round trip; ``(status, parsed body)``, or None when the host
    could not be reached at all (refused, unresolvable, timed out)."""
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        body = json.loads(raw.decode()) if raw else None
    except ValueError:
        body = None
    return status, body


class KubectlHelmBackend:
    def __init__(
        self,
        omcsi_dir: str,
        backup_dir: str,
        rollout_timeout: str = "5m",
        service_account: tuple[str, str] = ("dsh-api", "dsh-api"),
        run=run_command,
        http=http_json,
    ) -> None:
        self.omcsi_dir = omcsi_dir
        self.backup_dir = Path(backup_dir)
        self.rollout_timeout = rollout_timeout
        self.service_account = service_account
        """``(namespace, name)`` of the ServiceAccount the API runs as."""
        self._run = run
        self._http = http

    def namespace_exists(self, name: str) -> bool:
        try:
            self._run(["kubectl", "get", "namespace", namespace_for(name), "-o", "name"])
        except ClusterError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc):
                return False
            raise
        return True

    def create_tenant_namespace(self, name: str) -> None:
        self._run(
            ["kubectl", "apply", "-f", "-"], stdin=json.dumps(tenant_namespace_manifests(name))
        )

    def grant_tenant_access(self, name: str) -> None:
        sa_namespace, sa_name = self.service_account
        manifest = tenant_rolebinding_manifest(name, sa_namespace, sa_name)
        self._run(["kubectl", "apply", "-f", "-"], stdin=json.dumps(manifest))

    def create_credentials(self, name: str, creds: Credentials) -> None:
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "dsh-credentials", "namespace": namespace_for(name)},
            "type": "Opaque",
            "stringData": {
                "rconPassword": creds.rcon_password,
                "adminPassword": creds.admin_password,
                "deployAuthToken": creds.deploy_auth_token,
                "deploymentAuthToken": creds.deployment_auth_token,
            },
        }
        self._run(["kubectl", "apply", "-f", "-"], stdin=json.dumps(secret))

    def install_release(self, spec: ReleaseSpec, creds: Credentials) -> None:
        current = self.statefulset_status(spec.name)
        keep_awake = current.exists and current.replicas >= 1
        self._run(helm_install_argv(spec, creds, self.omcsi_dir, keep_awake=keep_awake))

    def scale_wrapper(self, name: str, replicas: int) -> None:
        self._run([
            "kubectl", "-n", namespace_for(name), "scale", "statefulset",
            f"{name}{WRAPPER_STS_SUFFIX}", f"--replicas={replicas}",
        ])  # fmt: skip

    def wait_for_rollout(self, name: str) -> None:
        ns = namespace_for(name)
        for target in (
            f"statefulset/{name}{WRAPPER_STS_SUFFIX}",
            f"deployment/{name}-omcsi-webapp",
            f"deployment/{name}-omcsi-nginx",
        ):
            self._run([
                "kubectl", "-n", ns, "rollout", "status", target,
                f"--timeout={self.rollout_timeout}",
            ])  # fmt: skip

    def statefulset_status(self, name: str) -> StatefulSetStatus:
        ns = namespace_for(name)
        try:
            raw = self._run(
                [
                    "kubectl",
                    "-n",
                    ns,
                    "get",
                    "statefulset",
                    f"{name}{WRAPPER_STS_SUFFIX}",
                    "-o",
                    "json",
                ]
            )
        except ClusterError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc):
                return StatefulSetStatus(exists=False)
            raise
        sts = json.loads(raw)
        replicas = int(sts.get("spec", {}).get("replicas") or 0)
        ready = int(sts.get("status", {}).get("readyReplicas") or 0)
        failing, scaled_at = (False, None) if replicas == 0 else self._wrapper_pods(ns)
        return StatefulSetStatus(
            exists=True,
            replicas=replicas,
            ready_replicas=ready,
            failing=failing,
            scaled_at=scaled_at,
        )

    def _wrapper_pods(self, ns: str) -> tuple[bool, datetime | None]:
        """Whether any wrapper pod is failing, and when the oldest one was created.

        A StatefulSet keeps no record of when it was last scaled; its pod's
        creation time is that moment, since scaling to zero deletes the pod.
        """
        raw = self._run(
            ["kubectl", "-n", ns, "get", "pods", "-l", WRAPPER_COMPONENT_LABEL, "-o", "json"]
        )
        failing = False
        created: datetime | None = None
        for pod in json.loads(raw).get("items", []):
            stamp = pod.get("metadata", {}).get("creationTimestamp")
            if stamp:
                try:
                    when = datetime.fromisoformat(stamp)
                except ValueError:
                    when = None
                if when is not None and (created is None or when < created):
                    created = when
            if pod.get("status", {}).get("phase") == "Failed":
                failing = True
            statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get(
                "status", {}
            ).get("initContainerStatuses", [])
            for cs in statuses:
                waiting = cs.get("state", {}).get("waiting") or {}
                if waiting.get("reason") in FAILING_REASONS:
                    failing = True
        return failing, created

    def wrapper_status(self, name: str) -> WrapperStatus | None:
        answer = self._http(wrapper_api_url(name, "/api/server/status"))
        if answer is None or answer[0] != 200:
            return None
        return WrapperStatus.from_json(answer[1])

    def start_wrapper(self, name: str) -> None:
        answer = self._http(wrapper_api_url(name, "/api/server/start"), method="POST")
        if answer is None:
            raise ClusterError("the wrapper could not be reached to start the server")
        if answer[0] != 200:
            raise ClusterError(f"the wrapper refused to start the server (HTTP {answer[0]})")

    def players_online(self, name: str) -> int | None:
        host = f"{name}{WRAPPER_STS_SUFFIX}.{namespace_for(name)}.svc"
        try:
            status = server_list_ping(host)
        except (OSError, ValueError):
            return None
        online = status.get("players", {}).get("online")
        return int(online) if online is not None else None

    def backup_world(self, name: str, awake: bool) -> Path:
        ns = namespace_for(name)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        path = self.backup_dir / f"{name}-{stamp}.tar.gz"
        tar = ["tar", "-C", "/mcserver", "-czf", "-", "."]
        if awake:
            # A missing pod makes this fail at once (NotFound), as it should.
            argv = ["kubectl", "-n", ns, "exec", f"{name}{WRAPPER_STS_SUFFIX}-0", "--", *tar]
        else:
            # The reader pod would sit Pending until kubectl gave up if the
            # claim did not exist (a create that failed before helm ran), so
            # the claim is looked up first and its absence is the error.
            self._run(
                ["kubectl", "-n", ns, "get", "pvc", f"{name}{WRAPPER_PVC_SUFFIX}", "-o", "name"]
            )
            # ``kubectl run -i --rm`` cannot stream this reliably: it attaches
            # only once the container is Running, and tar of a small world is
            # finished before that, so kubectl sits in "timed out waiting for
            # the condition" for a minute and the file holds a few bytes. The
            # pod idles instead, and ``kubectl exec`` streams tar from the
            # first byte to the last (the operator scripts do the same).
            pod = f"backup-reader-{stamp.lower()}"
            self._run(
                [
                    "kubectl",
                    "-n",
                    ns,
                    "run",
                    pod,
                    "--restart=Never",
                    "--image=busybox:1.36",
                    f"--overrides={json.dumps(pvc_reader_pod_overrides(f'{name}{WRAPPER_PVC_SUFFIX}'))}",
                ]  # fmt: skip
            )
            try:
                self._run(
                    [
                        "kubectl",
                        "-n",
                        ns,
                        "wait",
                        "--for=condition=Ready",
                        f"pod/{pod}",
                        "--timeout=120s",
                    ]  # fmt: skip
                )
                argv = ["kubectl", "-n", ns, "exec", pod, "--", *tar]
                return self._stream_backup(argv, path)
            finally:
                # Best effort: a pod that will not go away is a leak, not a failure.
                try:
                    self._run(["kubectl", "-n", ns, "delete", "pod", pod, "--wait=false"])
                except ClusterError:
                    pass
        return self._stream_backup(argv, path)

    def _stream_backup(self, argv: list[str], path: Path) -> Path:
        try:
            self._run(argv, stdout_path=path)
        except ClusterError:
            path.unlink(missing_ok=True)
            raise
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            raise ClusterError("backup is empty; refusing to continue")
        return path

    def uninstall_release(self, name: str) -> None:
        """Idempotent: a release that is already gone (an earlier attempt, or
        the operator's script, got that far) is not an error on the way out."""
        try:
            self._run(["helm", "uninstall", name, "-n", namespace_for(name)])
        except ClusterError as exc:
            if "release: not found" not in str(exc):
                raise

    def delete_namespace(self, name: str) -> None:
        try:
            self._run(["kubectl", "delete", "namespace", namespace_for(name), "--wait=false"])
        except ClusterError as exc:
            if "NotFound" not in str(exc) and "not found" not in str(exc):
                raise


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


@dataclass
class FakeClusterBackend:
    """In-memory cluster. Records every operation in ``ops`` in call order."""

    backup_dir: Path
    namespaces: set[str] = field(default_factory=set)
    bound: set[str] = field(default_factory=set)
    """Namespaces holding the tenant RoleBinding; namespaced steps need it."""
    credentials: dict[str, Credentials] = field(default_factory=dict)
    releases: dict[str, ReleaseSpec] = field(default_factory=dict)
    wrappers: dict[str, StatefulSetStatus] = field(default_factory=dict)
    wrapper_statuses: dict[str, WrapperStatus | None] = field(default_factory=dict)
    """Per server, what its wrapper answers; absent or None means unreachable."""
    players: dict[str, int | None] = field(default_factory=dict)
    ops: list[tuple] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    def _op(self, *op: object) -> None:
        self.ops.append(op)
        if op[0] in self.fail_on:
            raise ClusterError(f"{op[0]} failed (simulated)")

    def namespace_exists(self, name: str) -> bool:
        return namespace_for(name) in self.namespaces

    def create_tenant_namespace(self, name: str) -> None:
        self._op("create_tenant_namespace", name)
        self.namespaces.add(namespace_for(name))

    def grant_tenant_access(self, name: str) -> None:
        self._op("grant_tenant_access", name)
        if namespace_for(name) not in self.namespaces:
            raise ClusterError(f"namespaces {namespace_for(name)!r} not found")
        self.bound.add(namespace_for(name))

    def _require_binding(self, name: str) -> None:
        # What the real cluster would answer without the RoleBinding.
        if namespace_for(name) not in self.bound:
            raise ClusterError(f"forbidden: no access to namespace {namespace_for(name)}")

    def create_credentials(self, name: str, creds: Credentials) -> None:
        self._op("create_credentials", name)
        self._require_binding(name)
        self.credentials[name] = creds

    def install_release(self, spec: ReleaseSpec, creds: Credentials) -> None:
        current = self.wrappers.get(spec.name, StatefulSetStatus(exists=False))
        keep_awake = current.exists and current.replicas >= 1
        self._op("install_release", spec.name, keep_awake)
        self._require_binding(spec.name)
        self.releases[spec.name] = spec
        self.wrappers[spec.name] = StatefulSetStatus(exists=True, replicas=1 if keep_awake else 0)

    def scale_wrapper(self, name: str, replicas: int) -> None:
        self._op("scale_wrapper", name, replicas)
        current = self.wrappers.get(name)
        if current is None or not current.exists:
            raise ClusterError(f"statefulset {name}{WRAPPER_STS_SUFFIX} not found")
        scaled_at = datetime.now(UTC) if replicas >= 1 else None
        self.wrappers[name] = replace(
            current, replicas=replicas, ready_replicas=0, scaled_at=scaled_at
        )
        self.wrapper_statuses.pop(name, None)  # a fresh pod is not listening yet

    def wait_for_rollout(self, name: str) -> None:
        self._op("wait_for_rollout", name)
        current = self.wrappers.get(name)
        if current is None or current.replicas < 1:
            raise ClusterError(f"rollout of {name}{WRAPPER_STS_SUFFIX} timed out (simulated)")
        self.wrappers[name] = replace(current, ready_replicas=1)
        # A rolled-out wrapper has started the game, the way the real one does.
        self.wrapper_statuses[name] = WrapperStatus(running=True, pid=1, uptime_seconds=0)

    def statefulset_status(self, name: str) -> StatefulSetStatus:
        return self.wrappers.get(name, StatefulSetStatus(exists=False))

    def wrapper_status(self, name: str) -> WrapperStatus | None:
        return self.wrapper_statuses.get(name)

    def start_wrapper(self, name: str) -> None:
        self._op("start_wrapper", name)
        current = self.wrappers.get(name)
        if current is None or current.ready_replicas < 1:
            raise ClusterError("the wrapper could not be reached to start the server")
        self.wrapper_statuses[name] = WrapperStatus(running=True, pid=1, uptime_seconds=0)

    def players_online(self, name: str) -> int | None:
        return self.players.get(name)

    def backup_world(self, name: str, awake: bool) -> Path:
        self._op("backup_world", name, awake)
        if namespace_for(name) not in self.namespaces:
            raise ClusterError("no such namespace")
        if name not in self.releases:
            # No release, no world volume: the real backend finds no PVC (or no pod).
            raise ClusterError(f"persistentvolumeclaims {name}{WRAPPER_PVC_SUFFIX} not found")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"{name}-{stamp}.tar.gz"
        path.write_bytes(b"fake world of " + name.encode())
        return path

    def uninstall_release(self, name: str) -> None:
        self._op("uninstall_release", name)
        self.releases.pop(name, None)
        self.wrappers.pop(name, None)
        self.wrapper_statuses.pop(name, None)

    def delete_namespace(self, name: str) -> None:
        self._op("delete_namespace", name)
        self.namespaces.discard(namespace_for(name))
        self.bound.discard(namespace_for(name))  # the binding goes with its namespace
        self.credentials.pop(name, None)

    # --- test helpers ------------------------------------------------------

    def become_ready(self, name: str) -> None:
        self.wrappers[name] = replace(self.wrappers[name], ready_replicas=1)

    def fail_pod(self, name: str) -> None:
        self.wrappers[name] = replace(self.wrappers[name], failing=True)

    def stop_game(self, name: str) -> None:
        """The owner pressed Stop in the dashboard some time after the server
        came up: pod still up and Ready, game process gone, grace period over."""
        long_ago = datetime.now(UTC) - timedelta(hours=1)
        self.wrappers[name] = replace(self.wrappers[name], scaled_at=long_ago)
        self.wrapper_statuses[name] = WrapperStatus(running=False)
