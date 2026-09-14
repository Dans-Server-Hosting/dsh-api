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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

WRAPPER_PVC_SUFFIX = "-omcsi-mcserver"
WRAPPER_STS_SUFFIX = "-omcsi-minecraft-wrapper"
WRAPPER_COMPONENT_LABEL = "app.kubernetes.io/component=minecraft-wrapper"
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
class WrapperStatus:
    exists: bool
    replicas: int = 0
    ready_replicas: int = 0
    failing: bool = False

    @property
    def state(self) -> str:
        if not self.exists or self.failing:
            return "failed"
        if self.replicas == 0:
            return "asleep"
        return "awake" if self.ready_replicas >= 1 else "waking"


class ClusterBackend(Protocol):
    def namespace_exists(self, name: str) -> bool: ...

    def create_tenant_namespace(self, name: str) -> None:
        """Namespace ``t-<name>`` with its labels, ResourceQuota and LimitRange."""

    def create_credentials(self, name: str, creds: Credentials) -> None:
        """Secret ``dsh-credentials`` in the tenant namespace."""

    def install_release(self, spec: ReleaseSpec, creds: Credentials) -> None:
        """``helm upgrade --install`` without waiting: the webapp's init container
        blocks until the wrapper is healthy, and the profile installs the wrapper
        asleep, so a wait here could never finish."""

    def scale_wrapper(self, name: str, replicas: int) -> None: ...

    def wait_for_rollout(self, name: str) -> None:
        """Block until the wrapper StatefulSet, webapp and nginx have rolled out."""

    def wrapper_status(self, name: str) -> WrapperStatus: ...

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


class KubectlHelmBackend:
    def __init__(
        self, omcsi_dir: str, backup_dir: str, rollout_timeout: str = "5m", run=run_command
    ) -> None:
        self.omcsi_dir = omcsi_dir
        self.backup_dir = Path(backup_dir)
        self.rollout_timeout = rollout_timeout
        self._run = run

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
        current = self.wrapper_status(spec.name)
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

    def wrapper_status(self, name: str) -> WrapperStatus:
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
                return WrapperStatus(exists=False)
            raise
        sts = json.loads(raw)
        replicas = int(sts.get("spec", {}).get("replicas") or 0)
        ready = int(sts.get("status", {}).get("readyReplicas") or 0)
        failing = replicas > 0 and self._wrapper_pod_failing(ns)
        return WrapperStatus(exists=True, replicas=replicas, ready_replicas=ready, failing=failing)

    def _wrapper_pod_failing(self, ns: str) -> bool:
        raw = self._run(
            ["kubectl", "-n", ns, "get", "pods", "-l", WRAPPER_COMPONENT_LABEL, "-o", "json"]
        )
        for pod in json.loads(raw).get("items", []):
            if pod.get("status", {}).get("phase") == "Failed":
                return True
            statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get(
                "status", {}
            ).get("initContainerStatuses", [])
            for cs in statuses:
                waiting = cs.get("state", {}).get("waiting") or {}
                if waiting.get("reason") in FAILING_REASONS:
                    return True
        return False

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
            argv = ["kubectl", "-n", ns, "exec", f"{name}{WRAPPER_STS_SUFFIX}-0", "--", *tar]
        else:
            overrides = {
                "spec": {
                    "containers": [
                        {
                            "name": "r",
                            "image": "busybox:1.36",
                            "command": tar,
                            "stdin": True,
                            "volumeMounts": [{"name": "w", "mountPath": "/mcserver"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "w",
                            "persistentVolumeClaim": {"claimName": f"{name}{WRAPPER_PVC_SUFFIX}"},
                        }
                    ],
                }
            }
            argv = [
                "kubectl", "-n", ns, "run", "backup-reader", "--rm", "-i", "--restart=Never",
                "--image=busybox:1.36", f"--overrides={json.dumps(overrides)}",
            ]  # fmt: skip
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
        self._run(["helm", "uninstall", name, "-n", namespace_for(name)])

    def delete_namespace(self, name: str) -> None:
        self._run(["kubectl", "delete", "namespace", namespace_for(name), "--wait=false"])


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


@dataclass
class FakeClusterBackend:
    """In-memory cluster. Records every operation in ``ops`` in call order."""

    backup_dir: Path
    namespaces: set[str] = field(default_factory=set)
    credentials: dict[str, Credentials] = field(default_factory=dict)
    releases: dict[str, ReleaseSpec] = field(default_factory=dict)
    wrappers: dict[str, WrapperStatus] = field(default_factory=dict)
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

    def create_credentials(self, name: str, creds: Credentials) -> None:
        self._op("create_credentials", name)
        self.credentials[name] = creds

    def install_release(self, spec: ReleaseSpec, creds: Credentials) -> None:
        current = self.wrappers.get(spec.name, WrapperStatus(exists=False))
        keep_awake = current.exists and current.replicas >= 1
        self._op("install_release", spec.name, keep_awake)
        self.releases[spec.name] = spec
        self.wrappers[spec.name] = WrapperStatus(exists=True, replicas=1 if keep_awake else 0)

    def scale_wrapper(self, name: str, replicas: int) -> None:
        self._op("scale_wrapper", name, replicas)
        current = self.wrappers.get(name)
        if current is None or not current.exists:
            raise ClusterError(f"statefulset {name}{WRAPPER_STS_SUFFIX} not found")
        self.wrappers[name] = replace(current, replicas=replicas, ready_replicas=0)

    def wait_for_rollout(self, name: str) -> None:
        self._op("wait_for_rollout", name)
        current = self.wrappers.get(name)
        if current is None or current.replicas < 1:
            raise ClusterError(f"rollout of {name}{WRAPPER_STS_SUFFIX} timed out (simulated)")
        self.wrappers[name] = replace(current, ready_replicas=1)

    def wrapper_status(self, name: str) -> WrapperStatus:
        return self.wrappers.get(name, WrapperStatus(exists=False))

    def players_online(self, name: str) -> int | None:
        return self.players.get(name)

    def backup_world(self, name: str, awake: bool) -> Path:
        self._op("backup_world", name, awake)
        if namespace_for(name) not in self.namespaces:
            raise ClusterError("no such namespace")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"{name}-{stamp}.tar.gz"
        path.write_bytes(b"fake world of " + name.encode())
        return path

    def uninstall_release(self, name: str) -> None:
        self._op("uninstall_release", name)
        self.releases.pop(name, None)
        self.wrappers.pop(name, None)

    def delete_namespace(self, name: str) -> None:
        self._op("delete_namespace", name)
        self.namespaces.discard(namespace_for(name))
        self.credentials.pop(name, None)

    # --- test helpers ------------------------------------------------------

    def become_ready(self, name: str) -> None:
        self.wrappers[name] = replace(self.wrappers[name], ready_replicas=1)

    def fail_pod(self, name: str) -> None:
        self.wrappers[name] = replace(self.wrappers[name], failing=True)
