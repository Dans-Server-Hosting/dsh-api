"""The world-export pod (RFC 0021): its manifests, the exec stream, and
unpacking what it sends back.

The pod mirrors the backup reader (``pvc_reader_pod_overrides``): it mounts the
server's world volume read-only and idles; the work runs over ``kubectl exec``,
whose stdout is a gzipped tar of the results. Unlike the reader it also gets a
deny-all NetworkPolicy, no service-account token and a hard deadline.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
import threading
from collections.abc import Callable
from pathlib import Path

EXPORT_LABEL = "dsh.export"
EXPORT_ID_LABEL = "dsh.export/id"
EXPORT_POLICY = "dsh-export-isolate"
POD_DEADLINE_SECONDS = 900
READY_TIMEOUT = "120s"
EXPORT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PREVIEW_FILE_RE = re.compile(r"^(index\.json|t\.-?\d+\.-?\d+\.[0-9a-f]{6,64}\.uwt)$")
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 4096
PHASES = ("copying", "exporting", "preview", "packaging", "done")


def export_pod_name(export_id: str) -> str:
    return f"world-export-{export_id}"


def export_pod_manifest(namespace: str, export_id: str, claim: str, image: str) -> dict:
    """A restricted-profile pod: uid/gid 1000 like the wrapper (so the world
    is readable), read-only world, read-only root, no token, no network (see
    :func:`export_network_policy`), and a kubelet-enforced deadline. Its limits
    fit beside an awake release under the hosted-free quota (RFC 0021 §2)."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": export_pod_name(export_id),
            "namespace": namespace,
            "labels": {
                EXPORT_LABEL: "true",
                EXPORT_ID_LABEL: export_id,
                "app.kubernetes.io/name": "dsh-world-export",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": POD_DEADLINE_SECONDS,
            # `sleep` as PID 1 ignores SIGTERM, so the default 30 s grace would
            # keep a finished pod (and its share of the namespace quota, which
            # an awake release leaves exactly full) for half a minute.
            "terminationGracePeriodSeconds": 1,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "export",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["sleep", str(POD_DEADLINE_SECONDS)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "500m", "memory": "1Gi"},
                    },
                    "volumeMounts": [
                        {"name": "world", "mountPath": "/mcserver", "readOnly": True},
                        {"name": "work", "mountPath": "/work"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "world",
                    "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
                },
                {"name": "work", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
            ],
        },
    }


def export_network_policy(namespace: str) -> dict:
    """Deny all traffic to and from export pods. ``kubectl exec`` goes through
    the kubelet, not the pod network, so it is unaffected."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": EXPORT_POLICY, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {EXPORT_LABEL: "true"}},
            "policyTypes": ["Ingress", "Egress"],
        },
    }


def stream_command(
    argv: list[str],
    stdout_path: Path,
    on_stderr_line: Callable[[str], None],
    timeout: float,
) -> tuple[int, list[str]]:
    """Run ``argv`` with stdout to a file and stderr line by line to
    ``on_stderr_line``. Kills it after ``timeout`` seconds (returncode -9).
    Returns the exit status and the last stderr lines."""
    tail: list[str] = []
    with stdout_path.open("wb") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.PIPE)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                tail = (tail + [line])[-20:]
                on_stderr_line(line)
            proc.wait()
        finally:
            timer.cancel()
    return proc.returncode, tail


class UnsafeArchive(Exception):
    pass


def _allowed(name: str) -> bool:
    if name in ("summary.json", "world.zip"):
        return True
    head, _, file = name.partition("/")
    return head == "preview" and bool(PREVIEW_FILE_RE.match(file))


def extract_export(archive: Path, dest: Path) -> list[str]:
    """Unpack the pod's tar into ``dest``, accepting only regular files named
    ``summary.json``, ``world.zip`` and ``preview/<usd-web file>``, within a
    size budget. Anything else is refused, never written."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "preview").mkdir(exist_ok=True)
    written: list[str] = []
    total = 0
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for count, member in enumerate(tar, start=1):
                if count > MAX_MEMBERS:
                    raise UnsafeArchive("too many files")
                if member.isdir() and member.name in ("preview", "./preview"):
                    continue
                name = member.name.removeprefix("./")
                if not member.isfile() or not _allowed(name):
                    raise UnsafeArchive(f"unexpected entry {member.name!r}")
                total += member.size
                if total > MAX_UNPACKED_BYTES:
                    raise UnsafeArchive("the export is larger than allowed")
                src = tar.extractfile(member)
                assert src is not None
                with (dest / name).open("wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                written.append(name)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UnsafeArchive(f"the export could not be read: {exc}") from exc
    for required in ("summary.json", "world.zip", "preview/index.json"):
        if required not in written:
            raise UnsafeArchive(f"the export is missing {required}")
    return written
