"""The world-export pod's entrypoint (export/dsh_world_export.py, RFC 0021).

Standard library only, like the script: the exporter and the tiler are
replaced by small stand-in scripts, so nothing here needs usd-core. The real
tools run against a committed fixture world in CI's ``export-image`` job.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import stat
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "export" / "dsh_world_export.py"
spec = importlib.util.spec_from_file_location("dsh_world_export", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules["dsh_world_export"] = runner
spec.loader.exec_module(runner)


# --- a minimal NBT writer for level.dat fixtures ----------------------------


def _name(s: str) -> bytes:
    raw = s.encode()
    return struct.pack(">H", len(raw)) + raw


def _compound(entries: list[bytes]) -> bytes:
    return b"".join(entries) + b"\x00"


def _int(key: str, value: int) -> bytes:
    return b"\x03" + _name(key) + struct.pack(">i", value)


def _int_array(key: str, values: list[int]) -> bytes:
    return (
        b"\x0b"
        + _name(key)
        + struct.pack(">i", len(values))
        + struct.pack(f">{len(values)}i", *values)
    )


def _string(key: str, value: str) -> bytes:
    return b"\x08" + _name(key) + _name(value)


def _nested(key: str, entries: list[bytes]) -> bytes:
    return b"\x0a" + _name(key) + _compound(entries)


def level_dat(data_entries: list[bytes]) -> bytes:
    return gzip.compress(b"\x0a" + _name("") + _compound([_nested("Data", data_entries)]))


MODERN = level_dat(
    [
        _string("LevelName", "world"),
        _nested("spawn", [_int_array("pos", [100, 70, -300]), _string("dimension", "o")]),
    ]
)
LEGACY = level_dat([_int("SpawnX", -40), _int("SpawnY", 64), _int("SpawnZ", 12)])


def make_world(root: Path, *, modern: bool = True, regions=((0, 0), (-1, 0), (5, 5))) -> Path:
    """A server directory with one world, player data and entities beside it."""
    world = root / "world"
    rel = Path("dimensions/minecraft/overworld/region") if modern else Path("region")
    (world / rel).mkdir(parents=True)
    (world / "level.dat").write_bytes(MODERN if modern else LEGACY)
    for rx, rz in regions:
        (world / rel / f"r.{rx}.{rz}.mca").write_bytes(f"region {rx} {rz}".encode() * 100)
    for private in ("playerdata", "stats", "advancements", "entities", "poi"):
        (world / private).mkdir()
        (world / private / "secret.dat").write_text("not for export")
    (root / "server.properties").write_text("motd=hi\nlevel-name=world\n")
    return world


def read_only(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        mode = p.stat().st_mode
        p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()
    }


# --- server.properties / level.dat -------------------------------------------


def test_level_name_defaults_to_world(tmp_path: Path) -> None:
    assert runner.read_level_name(tmp_path / "missing") == "world"
    (tmp_path / "s").write_text("# comment\nlevel-name=\n")
    assert runner.read_level_name(tmp_path / "s") == "world"


def test_level_name_is_read(tmp_path: Path) -> None:
    (tmp_path / "s").write_text("level-name = Big World_2\n")
    assert runner.read_level_name(tmp_path / "s") == "Big World_2"


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "/abs", "x" * 80])
def test_level_name_outside_the_server_is_refused(tmp_path: Path, bad: str) -> None:
    (tmp_path / "s").write_text(f"level-name={bad}\n")
    with pytest.raises(runner.WorldError):
        runner.read_level_name(tmp_path / "s")


def test_spawn_modern_and_legacy(tmp_path: Path) -> None:
    (tmp_path / "m.dat").write_bytes(MODERN)
    (tmp_path / "l.dat").write_bytes(LEGACY)
    assert runner.read_spawn(tmp_path / "m.dat") == (100, -300)
    assert runner.read_spawn(tmp_path / "l.dat") == (-40, 12)


def test_spawn_uncompressed_and_unreadable(tmp_path: Path) -> None:
    (tmp_path / "raw.dat").write_bytes(gzip.decompress(LEGACY))
    assert runner.read_spawn(tmp_path / "raw.dat") == (-40, 12)
    (tmp_path / "junk.dat").write_bytes(b"\x1f\x8bnot gzip")
    assert runner.read_spawn(tmp_path / "junk.dat") is None
    (tmp_path / "short.dat").write_bytes(gzip.compress(b"\x0a\x00\x00\x0a"))
    assert runner.read_spawn(tmp_path / "short.dat") is None
    (tmp_path / "nospawn.dat").write_bytes(level_dat([_string("LevelName", "w")]))
    assert runner.read_spawn(tmp_path / "nospawn.dat") is None
    assert runner.read_spawn(tmp_path / "missing.dat") is None


# --- the box ---------------------------------------------------------------


def test_area_around_spawn_is_size_by_size() -> None:
    area = runner.Area.around(100, -300, 256)
    assert (area.x1, area.z1, area.x2, area.z2) == (-28, -428, 227, -173)
    assert area.width == area.depth == 256


def test_area_from_any_two_corners() -> None:
    assert runner.Area.from_corners(10, -5, -10, 5) == runner.Area(-10, -5, 10, 5)


def test_regions_cover_negative_coordinates() -> None:
    assert runner.Area(-1, -1, 0, 0).regions() == [(-1, -1), (-1, 0), (0, -1), (0, 0)]
    assert runner.Area(0, 0, 511, 511).regions() == [(0, 0)]
    assert runner.Area(-512, 0, -1, 600).regions() == [(-1, 0), (-1, 1)]


# --- the snapshot ------------------------------------------------------------


@pytest.mark.parametrize("modern", [True, False])
def test_snapshot_copies_only_level_dat_and_the_regions_needed(
    tmp_path: Path, modern: bool
) -> None:
    world = make_world(tmp_path / "mc", modern=modern)
    before = tree(tmp_path / "mc")
    read_only(tmp_path / "mc")
    dest = tmp_path / "snap" / "world"

    result = runner.snapshot(world, dest, runner.Area(-10, -10, 10, 10))

    rel = "dimensions/minecraft/overworld/region" if modern else "region"
    assert result == {"stable": True, "attempts": 1, "regions": ["r.-1.0.mca", "r.0.0.mca"]}
    assert sorted(tree(dest)) == sorted(["level.dat", f"{rel}/r.-1.0.mca", f"{rel}/r.0.0.mca"])
    # The source is byte-for-byte what it was, and nothing private was read out.
    assert tree(tmp_path / "mc") == before
    assert not any("secret" in name for name in tree(dest))


def test_snapshot_needs_a_started_world(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    with pytest.raises(runner.WorldError):
        runner.snapshot(tmp_path / "w", tmp_path / "d", runner.Area(0, 0, 1, 1))
    (tmp_path / "w" / "level.dat").write_bytes(MODERN)
    with pytest.raises(runner.WorldError):
        runner.snapshot(tmp_path / "w", tmp_path / "d", runner.Area(0, 0, 1, 1))


def test_snapshot_retries_while_files_change_and_says_so(tmp_path: Path, monkeypatch) -> None:
    world = make_world(tmp_path / "mc")
    calls = {"n": 0}
    real = runner._stat

    def changing(path: Path):
        calls["n"] += 1
        size, mtime = real(path)
        return size, mtime + calls["n"]  # a write landed between every look

    monkeypatch.setattr(runner, "_stat", changing)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    result = runner.snapshot(world, tmp_path / "d", runner.Area(0, 0, 1, 1))
    assert result["stable"] is False and result["attempts"] == runner.SNAPSHOT_ATTEMPTS


def test_snapshot_stabilises_after_a_retry(tmp_path: Path, monkeypatch) -> None:
    world = make_world(tmp_path / "mc")
    seen: list[Path] = []
    real = runner._stat

    def once(path: Path):
        seen.append(path)
        size, mtime = real(path)
        # The first "after" look at level.dat (third stat of it) differs.
        return size, mtime + (1 if seen.count(path) == 2 and path.name == "level.dat" else 0)

    monkeypatch.setattr(runner, "_stat", once)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    result = runner.snapshot(world, tmp_path / "d", runner.Area(0, 0, 1, 1))
    assert result["stable"] is True and result["attempts"] == 2


@pytest.mark.parametrize(
    ("state", "stable", "kind"),
    [
        ("asleep", True, "at_rest"),
        ("asleep", False, "live_changing"),
        ("awake", True, "live_stable"),
        ("awake", False, "live_changing"),
    ],
)
def test_snapshot_kind(state: str, stable: bool, kind: str) -> None:
    assert runner.snapshot_kind(state, stable) == kind


# --- summary -------------------------------------------------------------------


def test_summary_keeps_only_named_fields_and_scrubs_paths() -> None:
    exporter = {
        "exporterVersion": "0.1.0",
        "instances": 5,
        "blocksScanned": 10,
        "chunksDecoded": 1,
        "chunksSkippedPartial": 0,
        "claims": 3,
        "files": {"terrain.usdc": 1},
        "regions": [{"region_x": 0, "region_z": 0, "chunks": 1, "kept": 5, "culled": 2, "x": 1}],
        "warnings": ["chunk at /work/snapshot/world/region/r.0.0.mca is partial", "x" * 500],
        "playerPositions": [[1, 2, 3]],
    }
    summary = runner.build_summary(
        area=runner.Area(0, 0, 15, 15),
        custom_area=True,
        spawn=None,
        kind="at_rest",
        snap={"attempts": 1},
        exporter=exporter,
        zip_bytes=100,
        preview_bytes=50,
        seconds=1.234,
        scrub=("/work/snapshot/world",),
    )
    text = json.dumps(summary)
    assert "/work" not in text and "playerPositions" not in text and "claims" not in text
    assert summary["warnings"][0] == "chunk at <world>/region/r.0.0.mca is partial"
    assert len(summary["warnings"][1]) == 200
    assert summary["regions"] == [
        {"region_x": 0, "region_z": 0, "chunks": 1, "kept": 5, "culled": 2}
    ]
    assert summary["stats"]["instances"] == 5 and summary["stats"]["zip_bytes"] == 100


# --- the whole run, with stand-in tools ---------------------------------------

FAKE_EXPORTER = """#!/usr/bin/env python3
import json, sys, pathlib
args = sys.argv[1:]
out = pathlib.Path(args[args.index("--out") + 1])
summary = pathlib.Path(args[args.index("--summary-json") + 1])
bbox = args[args.index("--bbox") + 1: args.index("--bbox") + 7]
if "{fail}" == "yes":
    print("mc-usd-export: error reading world: bad chunk in " + sys.argv[1] + "/r.0.0.mca",
          file=sys.stderr)
    sys.exit(1)
(out / "world.usda").write_text("#usda 1.0\\n")
(out / "terrain.usdc").write_bytes(b"PXR-USDC" + " ".join(bbox).encode())
summary.write_text(json.dumps({"exporterVersion": "0.1.0", "instances": 7,
    "blocksScanned": 9, "regions": [], "warnings": []}))
"""

FAKE_TILER = """#!/usr/bin/env python3
import sys, pathlib
out = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "index.json").write_text('{"format": "usd-web"}')
(out / "t.0.0.abcdef012345.uwt").write_bytes(b"\\x1f\\x8b tile")
"""


def tool(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def run(tmp_path: Path, *extra: str, fail: str = "no") -> tuple[int, bytes]:
    exporter = tool(tmp_path / "exp", FAKE_EXPORTER.replace("{fail}", fail))
    tiler = tool(tmp_path / "tiler", FAKE_TILER)
    out = io.BytesIO()
    code = runner.main(
        [
            "--mcserver",
            str(tmp_path / "mc"),
            "--work",
            str(tmp_path / "work"),
            "--exporter",
            exporter,
            "--tiler",
            tiler,
            *extra,
        ],  # fmt: skip
        stream=out,
    )
    return code, out.getvalue()


def test_full_run_streams_zip_summary_and_preview(tmp_path: Path) -> None:
    make_world(tmp_path / "mc")
    read_only(tmp_path / "mc")
    before = tree(tmp_path / "mc")

    code, data = run(tmp_path, "--server-state", "asleep", "--default-size", "64")

    assert code == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        assert names == [
            "preview/index.json",
            "preview/t.0.0.abcdef012345.uwt",
            "summary.json",
            "world.zip",
        ]
        summary = json.load(tar.extractfile("summary.json"))
        archive = zipfile.ZipFile(io.BytesIO(tar.extractfile("world.zip").read()))
    assert sorted(archive.namelist()) == ["README.txt", "terrain.usdc", "world.usda"]
    # 64 around spawn (100, -300); full height handed to the exporter.
    assert archive.read("terrain.usdc").decode().endswith("68 -2048 -332 131 2047 -269")
    assert summary["area"] == {"x1": 68, "z1": -332, "x2": 131, "z2": -269}
    assert summary["snapshot"] == "at_rest" and summary["custom_area"] is False
    assert tree(tmp_path / "mc") == before


def test_custom_area_and_cap(tmp_path: Path) -> None:
    make_world(tmp_path / "mc")
    code, data = run(tmp_path, "--area", "0", "0", "-15", "31")
    assert code == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        summary = json.load(tar.extractfile("summary.json"))
    assert summary["area"] == {"x1": -15, "z1": 0, "x2": 0, "z2": 31}
    assert summary["custom_area"] is True and summary["snapshot"] == "live_stable"

    code, data = run(tmp_path, "--area", "0", "0", "600", "10")
    assert code == 2 and data == b""


def test_world_not_created_yet(tmp_path: Path) -> None:
    (tmp_path / "mc").mkdir()
    code, data = run(tmp_path)
    assert code == 3 and data == b""


def test_exporter_failure_is_reported_without_paths(tmp_path: Path, capsys) -> None:
    make_world(tmp_path / "mc")
    code, data = run(tmp_path, fail="yes")
    assert code == 1 and data == b""
    err = capsys.readouterr().err
    assert "error: the exporter failed: mc-usd-export: error reading world: bad chunk" in err
    assert "/work" not in err and str(tmp_path) not in err.split("error:", 1)[1]


def test_bad_default_size(tmp_path: Path) -> None:
    make_world(tmp_path / "mc")
    assert run(tmp_path, "--default-size", "4")[0] == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only tree")
def test_read_only_source_cannot_be_written(tmp_path: Path) -> None:
    world = make_world(tmp_path / "mc")
    read_only(tmp_path / "mc")
    with pytest.raises(PermissionError):
        (world / "level.dat").write_bytes(b"x")
