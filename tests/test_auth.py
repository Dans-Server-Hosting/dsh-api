import time

import jwt
import pytest

from dsh_api.auth import FakeValidator, Hs256Validator, InvalidToken, bearer_token

SECRET = "test-secret-not-real-" + "x" * 32


def token(**claims) -> str:
    return jwt.encode({"sub": "alice", **claims}, SECRET, algorithm="HS256")


def test_missing_header_is_401(client):
    resp = client.get("/api/v1/servers")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_token_is_401(client):
    resp = client.get("/api/v1/servers", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_non_bearer_scheme_is_401(client):
    resp = client.get("/api/v1/servers", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_first_call_creates_the_tenant_row(client, db):
    assert (
        client.get("/api/v1/servers", headers={"Authorization": "Bearer alice-token"}).json() == []
    )
    assert db.count_servers("alice") == 0
    db.ensure_tenant("alice")  # idempotent


def test_hs256_validator_accepts_a_signed_token():
    assert Hs256Validator(SECRET).validate(token()) == "alice"


def test_hs256_validator_rejects_wrong_secret():
    with pytest.raises(InvalidToken):
        Hs256Validator("other-" + "y" * 32).validate(token())


def test_hs256_validator_rejects_expired_token():
    with pytest.raises(InvalidToken):
        Hs256Validator(SECRET).validate(token(exp=int(time.time()) - 60))


def test_hs256_validator_requires_sub():
    no_sub = jwt.encode({"name": "x"}, SECRET, algorithm="HS256")
    with pytest.raises(InvalidToken):
        Hs256Validator(SECRET).validate(no_sub)


def test_hs256_validator_rejects_none_algorithm():
    unsigned = jwt.encode({"sub": "alice"}, "", algorithm="none")
    with pytest.raises(InvalidToken):
        Hs256Validator(SECRET).validate(unsigned)


def test_hs256_validator_needs_a_secret():
    with pytest.raises(ValueError):
        Hs256Validator("")


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc ") == "abc"
    for bad in (None, "", "Bearer", "Bearer ", "Token abc"):
        with pytest.raises(InvalidToken):
            bearer_token(bad)


def test_fake_validator():
    v = FakeValidator({"t": "tenant"})
    assert v.validate("t") == "tenant"
    with pytest.raises(InvalidToken):
        v.validate("u")
