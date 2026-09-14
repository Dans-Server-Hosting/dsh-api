"""Operator UUID lookup against Mojang's profile API, behind an interface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Protocol

MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/{username}"


class UnknownUsername(Exception):
    pass


class UuidResolver(Protocol):
    def resolve(self, username: str) -> str:
        """Return the dashed UUID for a Minecraft username; raise UnknownUsername."""


class MojangResolver:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def resolve(self, username: str) -> str:
        url = MOJANG_PROFILE_URL.format(username=urllib.request.quote(username))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UnknownUsername(username) from None
            raise UnknownUsername(f"{username}: Mojang returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise UnknownUsername(f"{username}: Mojang could not be reached ({exc})") from exc
        raw = body.get("id") if isinstance(body, dict) else None
        if not raw:
            raise UnknownUsername(username)
        return str(uuid.UUID(raw))


class FakeResolver:
    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.known = dict(known or {})

    def resolve(self, username: str) -> str:
        try:
            return self.known[username]
        except KeyError:
            raise UnknownUsername(username) from None
