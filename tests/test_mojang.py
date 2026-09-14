import io
import json
import urllib.error

import pytest

from dsh_api.mojang import FakeResolver, MojangResolver, UnknownUsername


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_mojang_resolver_dashes_the_uuid(monkeypatch):
    seen = {}

    def urlopen(url, timeout):
        seen["url"] = url
        return Response(
            json.dumps({"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve"}).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert MojangResolver().resolve("Steve") == "8667ba71-b85a-4004-af54-457a9734eed7"
    assert seen["url"] == "https://api.mojang.com/users/profiles/minecraft/Steve"


def test_mojang_resolver_404_is_unknown(monkeypatch):
    def urlopen(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(UnknownUsername):
        MojangResolver().resolve("Nobody")


def test_mojang_resolver_network_failure_is_unknown(monkeypatch):
    def urlopen(url, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(UnknownUsername, match="could not be reached"):
        MojangResolver().resolve("Steve")


def test_fake_resolver():
    r = FakeResolver({"Steve": "u"})
    assert r.resolve("Steve") == "u"
    with pytest.raises(UnknownUsername):
        r.resolve("Alex")
