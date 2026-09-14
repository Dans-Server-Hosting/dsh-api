"""Feedback and /me, against the fake validator: ``admin`` is the one admin."""

from datetime import timedelta

import pytest

from dsh_api.feedback import RATE_LIMIT

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
ADMIN = {"Authorization": "Bearer admin-token"}


def submit(client, headers, message="hello", page="/servers"):
    return client.post("/api/v1/feedback", json={"message": message, "page": page}, headers=headers)


# --- /me -------------------------------------------------------------------


def test_me_for_a_non_admin(client):
    resp = client.get("/api/v1/me", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice", "is_admin": False}


def test_me_for_an_admin(client):
    assert client.get("/api/v1/me", headers=ADMIN).json() == {"username": "admin", "is_admin": True}


def test_me_needs_a_token(client):
    assert client.get("/api/v1/me").status_code == 401


# --- submit ----------------------------------------------------------------


def test_submit_returns_the_new_item_and_records_an_event(client, db):
    resp = submit(client, ALICE, message="the wake button is hard to find", page="/servers/alpha")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body == {
        "id": 1,
        "username": "alice",
        "message": "the wake button is hard to find",
        "page": "/servers/alpha",
        "created_at": body["created_at"],
        "status": "new",
    }
    assert body["created_at"].endswith("Z")
    with db._connect() as conn:
        events = conn.execute("SELECT tenant_id, kind, detail FROM events").fetchall()
    assert [tuple(e) for e in events] == [("alice", "feedback", "1")]


def test_submit_without_a_page(client):
    resp = client.post("/api/v1/feedback", json={"message": "hi"}, headers=ALICE)
    assert resp.status_code == 201
    assert resp.json()["page"] is None


def test_submit_needs_a_token(client):
    assert client.post("/api/v1/feedback", json={"message": "hi"}).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "x" * 4001},
        {"message": "ok", "page": "/" + "p" * 200},
        {"message": None},
        {"page": "/servers"},
    ],
)
def test_submit_validation(client, body):
    assert client.post("/api/v1/feedback", json=body, headers=ALICE).status_code == 422


def test_submit_accepts_the_limits_exactly(client):
    resp = client.post(
        "/api/v1/feedback", json={"message": "x" * 4000, "page": "/" + "p" * 199}, headers=ALICE
    )
    assert resp.status_code == 201, resp.text


def test_submit_is_rate_limited_per_user(client, db):
    for _ in range(RATE_LIMIT):
        assert submit(client, ALICE).status_code == 201
    resp = submit(client, ALICE)
    assert resp.status_code == 429
    assert "per hour" in resp.json()["detail"]
    assert submit(client, BOB).status_code == 201  # another user is unaffected
    assert db.count_feedback_since("alice", timedelta(hours=1)) == RATE_LIMIT
    # Once alice's messages fall outside the hour, she may submit again.
    with db._connect() as conn:
        conn.execute("UPDATE feedback SET created_at = '2000-01-01T00:00:00Z'")
    assert submit(client, ALICE).status_code == 201


# --- list ------------------------------------------------------------------


def test_list_is_admin_only(client):
    assert client.get("/api/v1/feedback", headers=ALICE).status_code == 403
    assert client.get("/api/v1/feedback").status_code == 401


def test_list_newest_first_and_filtered_by_status(client):
    first = submit(client, ALICE, message="first").json()["id"]
    second = submit(client, BOB, message="second").json()["id"]
    third = submit(client, ALICE, message="third", page=None).json()["id"]
    marked = client.patch(f"/api/v1/feedback/{second}", json={"status": "read"}, headers=ADMIN)
    assert marked.status_code == 200

    new = client.get("/api/v1/feedback", headers=ADMIN).json()
    assert [(f["id"], f["username"], f["message"]) for f in new] == [
        (third, "alice", "third"),
        (first, "alice", "first"),
    ]
    assert new[0]["page"] is None and new[0]["status"] == "new"

    read = client.get("/api/v1/feedback", params={"status": "read"}, headers=ADMIN).json()
    assert [f["id"] for f in read] == [second]
    assert read[0]["status"] == "read"

    everything = client.get("/api/v1/feedback", params={"status": "all"}, headers=ADMIN).json()
    assert [f["id"] for f in everything] == [third, second, first]
    assert client.get("/api/v1/feedback", params={"status": "new"}, headers=ADMIN).json() == new


def test_list_rejects_an_unknown_status(client):
    resp = client.get("/api/v1/feedback", params={"status": "archived"}, headers=ADMIN)
    assert resp.status_code == 422


# --- patch -----------------------------------------------------------------


def test_patch_is_admin_only(client):
    item = submit(client, ALICE).json()
    resp = client.patch(f"/api/v1/feedback/{item['id']}", json={"status": "read"}, headers=ALICE)
    assert resp.status_code == 403
    assert client.get("/api/v1/feedback", headers=ADMIN).json()[0]["status"] == "new"


def test_patch_updates_the_status_both_ways(client):
    item = submit(client, ALICE).json()
    url = f"/api/v1/feedback/{item['id']}"
    read = client.patch(url, json={"status": "read"}, headers=ADMIN)
    assert read.status_code == 200
    assert read.json() == {**item, "status": "read"}
    back = client.patch(url, json={"status": "new"}, headers=ADMIN)
    assert back.json() == item


def test_patch_unknown_id_is_404(client):
    resp = client.patch("/api/v1/feedback/999", json={"status": "read"}, headers=ADMIN)
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [{}, {"status": "archived"}, {"status": None}])
def test_patch_validation(client, body):
    item = submit(client, ALICE).json()
    resp = client.patch(f"/api/v1/feedback/{item['id']}", json=body, headers=ADMIN)
    assert resp.status_code == 422


# --- schema ----------------------------------------------------------------


def test_existing_databases_gain_the_feedback_table(tmp_path):
    """The bootstrap is ``CREATE TABLE IF NOT EXISTS``, so an MVP database upgrades in place."""
    import sqlite3

    from dsh_api.db import Database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO tenants VALUES ('alice', '2026-01-01T00:00:00Z')")
    db = Database(str(path))
    assert db.insert_feedback("alice", "still here", None).id == 1
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
