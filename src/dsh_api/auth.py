"""Caller authentication: UserAuth JWTs, validated with the shared HS256 secret.

The JWT ``sub`` is the tenant id. No passwords, no registration, no local user
table beyond the ``tenants`` row the first authenticated call creates.
"""

from __future__ import annotations

from typing import Protocol

import jwt


class InvalidToken(Exception):
    pass


class TokenValidator(Protocol):
    def validate(self, token: str) -> str:
        """Return the tenant id for a valid token; raise InvalidToken otherwise."""


class Hs256Validator:
    """Validates UserAuth-issued tokens with the shared secret."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("USERAUTH_JWT_SECRET is required")
        self._secret = secret

    def validate(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token, self._secret, algorithms=["HS256"], options={"require": ["sub"]}
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise InvalidToken("sub claim is missing")
        return sub


class FakeValidator:
    """Maps literal bearer strings to tenant ids; for tests."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.tokens = dict(tokens or {})

    def validate(self, token: str) -> str:
        try:
            return self.tokens[token]
        except KeyError:
            raise InvalidToken("unknown token") from None


def bearer_token(authorization: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header value."""
    if not authorization:
        raise InvalidToken("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise InvalidToken("expected a Bearer token")
    return token.strip()
