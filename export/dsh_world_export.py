#!/usr/bin/env python3
"""Export a box of a hosted server's world to USD, plus usd-web preview tiles.

Runs inside the world-export pod (RFC 0021), started by dsh-api with
``kubectl exec <pod> -- dsh-world-export ...``. The pod mounts the server's
world volume **read-only** at ``/mcserver`` and has an ``emptyDir`` at
``/work``. This script:

1. finds the world (``level-name`` in ``server.properties``) and its spawn;
2. resolves the box (default: a square around spawn; or the X/Z corners given);
3. copies ``level.dat`` and only the overworld region files that intersect the
   box into ``/work/snapshot`` -- never ``playerdata/``, ``stats/``,
   ``advancements/``, ``entities/``, ``poi/`` or anything else -- and checks
   that no copied file changed while it was being copied (up to three tries);
4. runs ``mc-usd-export`` on the copy, then ``usd-web tiles`` on its terrain;
5. writes a gzipped tar of ``world.zip``, ``summary.json`` and ``preview/`` to
   stdout. Progress goes to stderr as ``phase=<name>`` lines.

It never writes under ``/mcserver`` and never talks to the network or the game.
Standard library only, so the pure parts are testable without ``usd-core``.

Exit status: 0 success; 2 invalid request; 3 world not found or unreadable
for the snapshot; 1 the exporter or the tiler failed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

REGION_BLOCKS = 512
LEVEL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]{0,63}$")
REGION_FILE_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")
SNAPSHOT_ATTEMPTS = 3
# Short, decorative plants: dropped from the preview only (they float as
# coloured cells over the surface); they stay in the USD.
PREVIEW_EXCLUDE = ("short_grass", "tall_grass", "fern", "large_fern")
README = """\
This archive is a 3D export of part of a Minecraft world, made by Dan's Server
Hosting with Minecraft-USD-Exporter (https://github.com/Dans-Plugins/Minecraft-USD-Exporter).

  world.usda    the root OpenUSD stage: open this one
  terrain.usdc  the blocks, one instanced cube per visible block, coloured by block type

Area: X {x1}..{x2}, Z {z1}..{z2}, every height (inclusive block coordinates).

Open world.usda in Blender (File > Import > Universal Scene Description) or in
usdview from a full OpenUSD install. Blocks have flat colours, not textures.
"""


class RequestError(Exception):
    """The request itself is invalid (exit 2)."""


class WorldError(Exception):
    """The world cannot be found or read for the snapshot (exit 3)."""


class StepError(Exception):
    """The exporter or the tiler failed (exit 1)."""


def phase(name: str) -> None:
    print(f"phase={name}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# server.properties and level.dat
# ---------------------------------------------------------------------------


def read_level_name(server_properties: Path) -> str:
    """``level-name`` from server.properties; ``world`` when absent.

    Only a single plain path segment is accepted, so the world folder can
    never be outside ``/mcserver``.
    """
    name = "world"
    try:
        text = server_properties.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return name
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "level-name":
            name = value.strip() or "world"
    if not LEVEL_NAME_RE.match(name) or name in (".", ".."):
        raise WorldError("the world folder name in server.properties is not a plain folder name")
    return name


class _Nbt:
    """Just enough of a big-endian NBT reader to find the spawn in level.dat."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated NBT")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def _unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self._take(size))[0]

    def _string(self) -> str:
        return self._take(self._unpack(">H")).decode("utf-8", errors="replace")

    def payload(self, tag: int, depth: int = 0):
        if depth > 64:
            raise ValueError("NBT nested too deeply")
        if tag == 1:
            return self._unpack(">b")
        if tag == 2:
            return self._unpack(">h")
        if tag == 3:
            return self._unpack(">i")
        if tag == 4:
            return self._unpack(">q")
        if tag == 5:
            return self._unpack(">f")
        if tag == 6:
            return self._unpack(">d")
        if tag == 7:
            return self._take(self._unpack(">i"))
        if tag == 8:
            return self._string()
        if tag == 9:
            inner = self._unpack(">b")
            count = self._unpack(">i")
            return [self.payload(inner, depth + 1) for _ in range(max(count, 0))]
        if tag == 10:
            out = {}
            while True:
                inner = self._unpack(">b")
                if inner == 0:
                    return out
                key = self._string()
                out[key] = self.payload(inner, depth + 1)
        if tag == 11:
            count = self._unpack(">i")
            return list(struct.unpack(f">{count}i", self._take(4 * count)))
        if tag == 12:
            count = self._unpack(">i")
            return list(struct.unpack(f">{count}q", self._take(8 * count)))
        raise ValueError(f"unknown NBT tag {tag}")

    def root(self) -> dict:
        tag = self._unpack(">b")
        if tag != 10:
            raise ValueError("level.dat does not start with a compound")
        self._string()
        return self.payload(10)


def read_spawn(level_dat: Path) -> tuple[int, int] | None:
    """The world spawn's X and Z, or None when it cannot be read.

    1.21.9 and later store ``Data.spawn.pos`` (an int array); earlier
    versions store ``Data.SpawnX`` / ``Data.SpawnZ``.
    """
    try:
        raw = level_dat.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        data = _Nbt(raw).root().get("Data", {})
    except (OSError, ValueError, EOFError, struct.error):
        return None
    spawn = data.get("spawn")
    if isinstance(spawn, dict):
        pos = spawn.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            return int(pos[0]), int(pos[2])
    if isinstance(data.get("SpawnX"), int) and isinstance(data.get("SpawnZ"), int):
        return data["SpawnX"], data["SpawnZ"]
    return None


# ---------------------------------------------------------------------------
# The box and the region files it needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Area:
    x1: int
    z1: int
    x2: int
    z2: int

    @classmethod
    def from_corners(cls, x1: int, z1: int, x2: int, z2: int) -> Area:
        return cls(min(x1, x2), min(z1, z2), max(x1, x2), max(z1, z2))

    @classmethod
    def around(cls, x: int, z: int, size: int) -> Area:
        half = size // 2
        return cls(x - half, z - half, x - half + size - 1, z - half + size - 1)

    @property
    def width(self) -> int:
        return self.x2 - self.x1 + 1

    @property
    def depth(self) -> int:
        return self.z2 - self.z1 + 1

    def regions(self) -> list[tuple[int, int]]:
        rx1, rx2 = self.x1 // REGION_BLOCKS, self.x2 // REGION_BLOCKS
        rz1, rz2 = self.z1 // REGION_BLOCKS, self.z2 // REGION_BLOCKS
        return [(rx, rz) for rx in range(rx1, rx2 + 1) for rz in range(rz1, rz2 + 1)]

    def as_dict(self) -> dict:
        return {"x1": self.x1, "z1": self.z1, "x2": self.x2, "z2": self.z2}


def region_dir(world: Path) -> tuple[Path, Path]:
    """``(source dir, path relative to the world)`` of the overworld regions.

    26.1+ worlds keep them in ``dimensions/minecraft/overworld/region``;
    earlier ones in ``region``. The modern layout wins when both exist,
    which is how the game itself reads an upgraded world.
    """
    for rel in (Path("dimensions/minecraft/overworld/region"), Path("region")):
        if (world / rel).is_dir():
            return world / rel, rel
    raise WorldError("the world has no overworld region folder yet (has it been started once?)")


def _stat(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def snapshot(world: Path, dest: Path, area: Area, attempts: int = SNAPSHOT_ATTEMPTS) -> dict:
    """Copy level.dat and the region files ``area`` needs into ``dest``.

    Returns ``{"stable": bool, "attempts": n, "regions": [names]}``. A copy is
    stable when no source file's size or mtime changed between just before
    and just after it was copied. Nothing outside the listed files is read.
    """
    level_dat = world / "level.dat"
    if not level_dat.is_file():
        raise WorldError("the world has no level.dat yet (has it been started once?)")
    src_dir, rel = region_dir(world)
    wanted = {f"r.{rx}.{rz}.mca" for rx, rz in area.regions()}
    sources = [level_dat] + sorted(src_dir / name for name in wanted if (src_dir / name).is_file())
    targets = {level_dat: dest / "level.dat"}
    for source in sources[1:]:
        targets[source] = dest / rel / source.name
    (dest / rel).mkdir(parents=True, exist_ok=True)

    stable = False
    tries = 0
    while tries < attempts and not stable:
        tries += 1
        before = {s: _stat(s) for s in sources}
        for source in sources:
            # copyfile: contents only. The read-only mount is never written.
            shutil.copyfile(source, targets[source])
        stable = before == {s: _stat(s) for s in sources}
        if not stable and tries < attempts:
            time.sleep(0.5)
    return {
        "stable": stable,
        "attempts": tries,
        "regions": [s.name for s in sources[1:]],
    }


def snapshot_kind(server_state: str, stable: bool) -> str:
    """``at_rest`` | ``live_stable`` | ``live_changing`` (RFC 0021 §3, §6)."""
    if server_state == "asleep" and stable:
        return "at_rest"
    return "live_stable" if stable else "live_changing"


# ---------------------------------------------------------------------------
# Running the tools, packaging
# ---------------------------------------------------------------------------


def _scrub(text: str, *prefixes: str) -> str:
    for prefix in prefixes:
        text = text.replace(prefix, "<world>")
    return re.sub(r"/(?:work|mcserver)/\S*", "<path>", text)


def run_tool(argv: list[str], what: str, scrub: tuple[str, ...]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout).strip().splitlines() if ln.strip()]
        last = _scrub(lines[-1], *scrub) if lines else f"exit status {proc.returncode}"
        raise StepError(f"{what} failed: {last[:300]}")


def build_summary(
    *,
    area: Area,
    custom_area: bool,
    spawn: tuple[int, int] | None,
    kind: str,
    snap: dict,
    exporter: dict,
    zip_bytes: int,
    preview_bytes: int,
    seconds: float,
    scrub: tuple[str, ...],
) -> dict:
    """The fields that leave the pod, chosen one by one (RFC 0021 §3 step 7)."""
    regions = [
        {k: r.get(k) for k in ("region_x", "region_z", "chunks", "kept", "culled")}
        for r in exporter.get("regions", [])
        if isinstance(r, dict)
    ]
    warnings = [_scrub(str(w), *scrub)[:200] for w in exporter.get("warnings", [])][:20]
    return {
        "version": 1,
        "area": area.as_dict(),
        "custom_area": custom_area,
        "spawn": {"x": spawn[0], "z": spawn[1]} if spawn else None,
        "snapshot": kind,
        "snapshot_attempts": snap["attempts"],
        "exporter_version": exporter.get("exporterVersion"),
        "exporter_commit": os.environ.get("EXPORTER_PIN"),
        "usd_web_commit": os.environ.get("USD_WEB_PIN"),
        "stats": {
            "blocks_scanned": exporter.get("blocksScanned"),
            "instances": exporter.get("instances"),
            "chunks": exporter.get("chunksDecoded"),
            "chunks_skipped_partial": exporter.get("chunksSkippedPartial"),
            "zip_bytes": zip_bytes,
            "preview_bytes": preview_bytes,
            "seconds": round(seconds, 2),
        },
        "regions": regions,
        "warnings": warnings,
    }


def write_zip(usd_dir: Path, zip_path: Path, area: Area) -> int:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in ("world.usda", "terrain.usdc"):
            zf.write(usd_dir / name, arcname=name)
        zf.writestr("README.txt", README.format(**area.as_dict()))
    return zip_path.stat().st_size


def write_tar(out: Path, stream) -> None:
    """A gzipped tar of world.zip, summary.json and preview/ to ``stream``."""
    with tarfile.open(fileobj=stream, mode="w|gz") as tar:
        for name in ("summary.json", "world.zip"):
            tar.add(out / name, arcname=name, recursive=False)
        preview = out / "preview"
        for path in sorted(preview.iterdir()):
            if path.is_file():
                tar.add(path, arcname=f"preview/{path.name}", recursive=False)


def export(args: argparse.Namespace, stream) -> dict:
    started = time.monotonic()
    mcserver, work = Path(args.mcserver), Path(args.work)
    phase("copying")
    level_name = read_level_name(mcserver / "server.properties")
    world = mcserver / level_name
    if not (world / "level.dat").is_file():
        raise WorldError("the world has not been created yet (start the server once)")
    spawn = read_spawn(world / "level.dat")
    if args.area:
        area = Area.from_corners(*args.area)
        custom = True
    else:
        sx, sz = spawn or (0, 0)
        area = Area.around(sx, sz, args.default_size)
        custom = False
    if area.width > args.max_size or area.depth > args.max_size:
        raise RequestError(f"the area is larger than {args.max_size} × {args.max_size} blocks")

    snap_world = work / "snapshot" / level_name
    if snap_world.exists():
        shutil.rmtree(snap_world)
    snap = snapshot(world, snap_world, area)
    kind = snapshot_kind(args.server_state, snap["stable"])

    out = work / "out"
    if out.exists():
        shutil.rmtree(out)
    usd_dir, preview = out / "usd", out / "preview"
    usd_dir.mkdir(parents=True)
    scrub = (str(snap_world), str(world))

    phase("exporting")
    exporter_summary = out / "exporter-summary.json"
    run_tool(
        [
            args.exporter,
            str(snap_world),
            "--bbox",
            str(area.x1),
            "-2048",
            str(area.z1),
            str(area.x2),
            "2047",
            str(area.z2),
            "--out",
            str(usd_dir),
            "--summary-json",
            str(exporter_summary),
            "--quiet",
        ],  # fmt: skip
        "the exporter",
        scrub,
    )
    exporter = json.loads(exporter_summary.read_text(encoding="utf-8"))

    phase("preview")
    tiler = [args.tiler, "tiles", str(usd_dir / "terrain.usdc"), "-o", str(preview), "--replace"]
    tiler += ["--tile-size", "128", "--title", args.title]
    for name in PREVIEW_EXCLUDE:
        tiler += ["--exclude", name]
    run_tool(tiler, "the preview tiler", scrub)
    preview_bytes = sum(p.stat().st_size for p in preview.iterdir() if p.is_file())

    phase("packaging")
    zip_bytes = write_zip(usd_dir, out / "world.zip", area)
    summary = build_summary(
        area=area,
        custom_area=custom,
        spawn=spawn,
        kind=kind,
        snap=snap,
        exporter=exporter,
        zip_bytes=zip_bytes,
        preview_bytes=preview_bytes,
        seconds=time.monotonic() - started,
        scrub=scrub,
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_tar(out, stream)
    phase("done")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dsh-world-export", description=__doc__.splitlines()[0])
    p.add_argument("--mcserver", default="/mcserver", help="read-only server directory")
    p.add_argument("--work", default="/work", help="scratch directory (emptyDir)")
    p.add_argument("--area", type=int, nargs=4, metavar=("X1", "Z1", "X2", "Z2"))
    p.add_argument("--default-size", type=int, default=256)
    p.add_argument("--max-size", type=int, default=512)
    p.add_argument("--server-state", choices=("asleep", "awake"), default="awake")
    p.add_argument("--title", default="world")
    p.add_argument("--exporter", default="mc-usd-export")
    p.add_argument("--tiler", default="usd-web")
    return p


def main(argv: list[str] | None = None, stream=None) -> int:
    args = build_parser().parse_args(argv)
    if args.default_size < 16 or args.default_size > args.max_size:
        print("error: --default-size must be between 16 and --max-size", file=sys.stderr)
        return 2
    if stream is None:
        stream = sys.stdout.buffer
    try:
        export(args, stream)
    except RequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except WorldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "Area",
    "build_summary",
    "main",
    "read_level_name",
    "read_spawn",
    "snapshot",
    "snapshot_kind",
]
