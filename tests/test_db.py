"""The SQLite layer on its own: what each ``Database`` method reads and writes.

The HTTP tests reach most of this through the app; these pin the scoping,
ordering and cascades directly, so a change to a query shows up here first.
"""

import sqlite3
from datetime import timedelta

import pytest

from dsh_api.db import EXPORT_REQUESTED, Database, SelectedPlugin

DAY = timedelta(days=1)


def server(db: Database, name: str, tenant: str, status: str = "ready") -> None:
    db.ensure_tenant(tenant)
    db.insert_server(name, tenant, f"{name}.play.example.com", "motd", None, status=status)


def set_created_at(db: Database, table: str, key: str, value: str, at: str) -> None:
    """Rows are stamped to the second; tests that pin ordering set it outright."""
    with db._connect() as conn:
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE {key} = ?", (at, value))


def plugin(slug: str, tag: str = "v1", **kwargs) -> SelectedPlugin:
    return SelectedPlugin(
        slug=slug,
        tag=tag,
        filename=f"{slug}-{tag}.jar",
        url=f"https://example.com/{slug}",
        **kwargs,
    )


# --- schema ----------------------------------------------------------------


def test_a_database_from_before_the_status_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE servers (name TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,"
            " hostname TEXT NOT NULL, motd TEXT NOT NULL, operator_username TEXT,"
            " created_at TEXT NOT NULL, last_woken_at TEXT)"
        )
        conn.execute(
            "INSERT INTO servers VALUES ('old', 'alice', 'old.play.example.com', 'm', NULL,"
            " '2026-01-01T00:00:00Z', NULL)"
        )
    conn.close()

    db = Database(path)

    assert db.get_server("old").status == "ready"


def test_opening_the_same_file_twice_keeps_its_rows(db, settings):
    server(db, "alpha", "alice")
    again = Database(settings.db_path)
    assert again.get_server("alpha").tenant_id == "alice"


# --- servers -----------------------------------------------------------------


def test_list_servers_is_the_tenants_own_oldest_first_then_by_name(db):
    server(db, "zulu", "alice")
    server(db, "bravo", "alice")
    server(db, "alpha", "alice")
    server(db, "other", "bob")
    set_created_at(db, "servers", "name", "zulu", "2026-01-01T00:00:00Z")
    set_created_at(db, "servers", "name", "bravo", "2026-01-02T00:00:00Z")
    set_created_at(db, "servers", "name", "alpha", "2026-01-02T00:00:00Z")

    assert [s.name for s in db.list_servers("alice")] == ["zulu", "alpha", "bravo"]
    assert [s.name for s in db.list_servers("bob")] == ["other"]
    assert db.list_servers("carol") == []


def test_provisioning_server_is_only_the_tenants_create_in_flight(db):
    server(db, "alpha", "alice", status="ready")
    server(db, "beta", "alice", status="provisioning")
    server(db, "gamma", "bob", status="provisioning")

    assert db.provisioning_server("alice").name == "beta"
    db.set_status("beta", "ready")
    assert db.provisioning_server("alice") is None
    assert db.provisioning_server("bob").name == "gamma"


def test_list_with_status_spans_tenants(db):
    server(db, "alpha", "alice", status="failed")
    server(db, "beta", "bob", status="failed")
    server(db, "gamma", "bob", status="ready")

    assert [s.name for s in db.list_with_status("failed")] == ["alpha", "beta"]
    assert db.list_with_status("deleting") == []


def test_set_status_refuses_an_unknown_status_and_changes_nothing(db):
    server(db, "alpha", "alice", status="ready")
    with pytest.raises(ValueError, match="unknown server status"):
        db.set_status("alpha", "asleep")
    assert db.get_server("alpha").status == "ready"


def test_begin_delete_only_moves_the_row_from_the_status_it_expects(db):
    server(db, "alpha", "alice", status="ready")
    assert db.begin_delete("alpha", "failed") is False
    assert db.get_server("alpha").status == "ready"
    assert db.begin_delete("alpha", "ready") is True
    assert db.get_server("alpha").status == "deleting"
    assert db.begin_delete("alpha", "ready") is False


def test_mark_woken_stamps_the_server(db):
    server(db, "alpha", "alice")
    assert db.get_server("alpha").last_woken_at is None
    db.mark_woken("alpha")
    assert db.get_server("alpha").last_woken_at.endswith("Z")


def test_delete_server_takes_its_plugin_selection_but_no_one_elses(db):
    server(db, "alpha", "alice")
    server(db, "beta", "bob")
    db.seed_plugins("alpha", [plugin("dpm")])
    db.seed_plugins("beta", [plugin("dpm")])

    db.delete_server("alpha")

    assert db.get_server("alpha") is None
    assert db.selected_plugins("alpha") == []
    assert db.plugin_state("alpha") is None
    assert [p.slug for p in db.selected_plugins("beta")] == ["dpm"]
    assert db.plugin_state("beta") is not None
    # The name is free again, and a new server under it starts from scratch.
    assert db.seed_plugins("alpha", [plugin("via")]) is True
    assert [p.slug for p in db.selected_plugins("alpha")] == ["via"]


# --- plugin selection ----------------------------------------------------------


def test_seed_plugins_happens_once_even_after_everything_is_removed(db):
    assert db.seed_plugins("alpha", [plugin("dpm")]) is True
    db.remove_plugin("alpha", "dpm")
    assert db.selected_plugins("alpha") == []

    assert db.seed_plugins("alpha", [plugin("via")]) is False
    assert db.selected_plugins("alpha") == []


def test_selected_plugins_round_trip_requires_and_fill_in_added_at(db):
    db.seed_plugins(
        "alpha",
        [plugin("viabackwards", requires=("viaversion",), added_by="default")],
    )
    (row,) = db.selected_plugins("alpha")
    assert row.requires == ("viaversion",)
    assert row.added_by == "default"
    assert row.added_at.endswith("Z")
    assert row.filename == "viabackwards-v1.jar"


def test_selected_plugins_are_in_the_order_added_then_by_slug(db):
    db.seed_plugins("alpha", [])
    db.add_plugins(
        "alpha",
        [
            plugin("zeta", added_at="2026-01-01T00:00:00Z"),
            plugin("beta", added_at="2026-01-02T00:00:00Z"),
            plugin("alpha", added_at="2026-01-02T00:00:00Z"),
        ],
    )
    assert [p.slug for p in db.selected_plugins("alpha")] == ["zeta", "alpha", "beta"]


def test_adding_a_plugin_already_selected_keeps_its_pinned_release(db):
    db.seed_plugins("alpha", [plugin("dpm", tag="v1")])
    db.add_plugins("alpha", [plugin("dpm", tag="v2")])
    assert [(p.slug, p.tag) for p in db.selected_plugins("alpha")] == [("dpm", "v1")]


def test_remove_plugin_is_per_server(db):
    db.seed_plugins("alpha", [plugin("dpm")])
    db.seed_plugins("beta", [plugin("dpm")])
    db.remove_plugin("alpha", "dpm")
    assert db.selected_plugins("alpha") == []
    assert [p.slug for p in db.selected_plugins("beta")] == ["dpm"]


def test_set_plugin_hashes_leaves_the_hash_it_is_not_given(db):
    db.seed_plugins("alpha", [])
    state = db.plugin_state("alpha")
    assert (state.written_sha256, state.applied_sha256) == (None, None)

    db.set_plugin_hashes("alpha", written="w1")
    state = db.plugin_state("alpha")
    assert (state.written_sha256, state.applied_sha256) == ("w1", None)

    db.set_plugin_hashes("alpha", applied="a1")
    state = db.plugin_state("alpha")
    assert (state.written_sha256, state.applied_sha256) == ("w1", "a1")


def test_set_plugin_hashes_on_an_unseeded_server_writes_nothing(db):
    db.set_plugin_hashes("alpha", written="w1", applied="a1")
    assert db.plugin_state("alpha") is None


# --- events --------------------------------------------------------------------


def test_events_are_per_server_in_the_order_recorded(db):
    db.record("alice", "alpha", "server.created", "first")
    db.record("bob", "beta", "server.created")
    db.record("alice", "alpha", "server.woken")
    db.record("alice", None, "tenant.thing")

    events = db.events("alpha")
    assert [(e["kind"], e["detail"]) for e in events] == [
        ("server.created", "first"),
        ("server.woken", ""),
    ]
    assert set(events[0]) == {"at", "kind", "detail"}


# --- feedback ------------------------------------------------------------------


def test_list_feedback_is_newest_first_and_narrows_by_status(db):
    first = db.reserve_feedback("alice", "one", "/a", limit=10, window=DAY)
    second = db.reserve_feedback("bob", "two", None, limit=10, window=DAY)
    db.set_feedback_status(first.id, "done")

    assert [f.message for f in db.list_feedback()] == ["two", "one"]
    assert [f.message for f in db.list_feedback("done")] == ["one"]
    assert [f.id for f in db.list_feedback("new")] == [second.id]
    assert db.list_feedback("archived") == []


def test_set_feedback_status_returns_the_updated_row_or_none(db):
    item = db.reserve_feedback("alice", "hello", "/servers", limit=10, window=DAY)
    assert item.status == "new"
    updated = db.set_feedback_status(item.id, "seen")
    assert (updated.id, updated.status, updated.message) == (item.id, "seen", "hello")
    assert db.set_feedback_status(9999, "seen") is None


# --- world exports ---------------------------------------------------------------


def test_insert_export_marks_whether_an_area_was_given(db):
    around_spawn = db.insert_export("e1", "alpha", "alice", None)
    boxed = db.insert_export("e2", "alpha", "alice", (0, 0, 10, 20))

    assert around_spawn.status == "queued"
    assert around_spawn.custom_area is False
    assert (around_spawn.x1, around_spawn.z2) == (None, None)
    assert boxed.custom_area is True
    assert (boxed.x1, boxed.z1, boxed.x2, boxed.z2) == (0, 0, 10, 20)


def test_list_exports_is_the_tenants_own_for_that_server_newest_first(db):
    db.insert_export("old", "alpha", "alice", None)
    db.insert_export("new", "alpha", "alice", None)
    db.insert_export("same-second", "alpha", "alice", None)
    db.insert_export("other-server", "beta", "alice", None)
    db.insert_export("other-tenant", "alpha", "bob", None)
    set_created_at(db, "world_exports", "id", "old", "2026-01-01T00:00:00Z")
    set_created_at(db, "world_exports", "id", "new", "2026-01-02T00:00:00Z")
    set_created_at(db, "world_exports", "id", "same-second", "2026-01-02T00:00:00Z")

    # Equal timestamps fall back to insertion order, latest first.
    assert [e.id for e in db.list_exports("alpha", "alice")] == ["same-second", "new", "old"]
    assert [e.id for e in db.list_exports("alpha", "bob")] == ["other-tenant"]
    assert db.list_exports("alpha", "carol") == []


def test_exports_with_status_matches_any_of_the_statuses_given(db):
    db.insert_export("q", "alpha", "alice", None)
    db.insert_export("r", "beta", "bob", None)
    db.insert_export("d", "gamma", "carol", None)
    db.update_export("r", status="running")
    db.update_export("d", status="done")

    assert sorted(e.id for e in db.exports_with_status("queued", "running")) == ["q", "r"]
    assert [e.id for e in db.exports_with_status("done")] == ["d"]
    assert db.exports_with_status("failed") == []


def test_update_export_stores_stats_as_json_and_reads_them_back(db):
    db.insert_export("e1", "alpha", "alice", None)
    db.update_export("e1", status="done", stats={"chunks": 4, "blocks": [1, 2]}, phase="render")
    row = db.get_export("e1")
    assert row.stats == {"chunks": 4, "blocks": [1, 2]}
    assert (row.status, row.phase) == ("done", "render")

    db.update_export("e1", stats=None)
    assert db.get_export("e1").stats is None


@pytest.mark.parametrize("fields", [{}, {"tenant_id": "bob"}, {"status": "done", "server": "x"}])
def test_update_export_refuses_fields_outside_the_allow_list(db, fields):
    db.insert_export("e1", "alpha", "alice", None)
    with pytest.raises(ValueError, match="cannot update"):
        db.update_export("e1", **fields)
    row = db.get_export("e1")
    assert (row.tenant_id, row.server, row.status) == ("alice", "alpha", "queued")


def test_delete_export_removes_only_that_export(db):
    db.insert_export("e1", "alpha", "alice", None)
    db.insert_export("e2", "alpha", "alice", None)
    db.delete_export("e1")
    assert [e.id for e in db.all_exports()] == ["e2"]


def test_delete_exports_of_returns_the_ids_it_removed_for_that_server_only(db):
    db.insert_export("a1", "alpha", "alice", None)
    db.insert_export("a2", "alpha", "alice", None)
    db.insert_export("b1", "beta", "bob", None)

    assert sorted(db.delete_exports_of("alpha")) == ["a1", "a2"]
    assert [e.id for e in db.all_exports()] == ["b1"]
    assert db.delete_exports_of("alpha") == []


def test_count_exports_since_is_per_tenant_and_survives_deleting_the_export(db):
    assert db.count_exports_since("alice", DAY) == (0, None)
    first = db.reserve_export("e1", "alpha", "alice", None, daily_limit=5, window=DAY)
    db.update_export(first.id, status="done")
    db.reserve_export("e2", "alpha", "alice", None, daily_limit=5, window=DAY)
    db.reserve_export("e3", "beta", "bob", None, daily_limit=5, window=DAY)

    db.delete_exports_of("alpha")

    count, oldest = db.count_exports_since("alice", DAY)
    assert count == 2
    assert oldest == db.events("alpha")[0]["at"]
    assert db.count_exports_since("bob", DAY)[0] == 1
    assert [e["kind"] for e in db.events("alpha")] == [EXPORT_REQUESTED, EXPORT_REQUESTED]
