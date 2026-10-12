import urllib.error

import pytest

from dsh_api.catalog import CatalogUnavailable, DpcCatalog, default_plugin_slug, parse_dpc
from dsh_api.config import DEFAULT_PLUGINS, split_csv

PLUGINS = [
    {
        "slug": "medieval-factions",
        "title": "Medieval Factions",
        "description": "Factions.",
        "iconPath": "/icons/mf.png",
        "tags": ["factions"],
        "requires": [],
    },
    {
        "slug": "fiefs",
        "title": "Fiefs",
        "description": "Fiefs.",
        "iconPath": None,
        "tags": [],
        "requires": ["medieval-factions"],
    },
    {"slug": "unreleased", "title": "Unreleased", "requires": []},
]
LATEST = [
    {
        "slug": "medieval-factions",
        "tag": "v7.0.0",
        "downloadUrl": "https://github.com/Dans-Plugins/Medieval-Factions/releases/download/v7.0.0/medieval-factions-7.0.0-all.jar",
        "downloadPath": (
            "/api/v1/plugins/medieval-factions/versions/v7.0.0/assets/"
            "medieval-factions-7.0.0-all.jar/download"
        ),
    },
    {
        "slug": "fiefs",
        "tag": "v0.13.1",
        "downloadUrl": "https://github.com/Dans-Plugins/Fiefs/releases/download/v0.13.1/Fiefs-0.13.1.jar",
        "downloadPath": "/api/v1/plugins/fiefs/versions/v0.13.1/assets/Fiefs-0.13.1.jar/download",
    },
]


def test_joins_plugins_with_their_latest_release_through_the_counting_url():
    catalog = parse_dpc(PLUGINS, LATEST, api="https://api.example")
    mf = catalog["medieval-factions"]
    assert mf.tag == "v7.0.0"
    # The filename is the asset's, not the counting URL's last segment ("download").
    assert mf.filename == "medieval-factions-7.0.0-all.jar"
    assert mf.url.startswith("https://api.example/api/v1/plugins/medieval-factions/")
    assert mf.url.endswith("/download")
    assert mf.icon_url == "https://dansplugins.com/icons/mf.png"
    assert mf.project_url == "https://dansplugins.com/resources/medieval-factions"
    assert catalog["fiefs"].requires == ("medieval-factions",)
    assert catalog["fiefs"].icon_url is None


def test_a_plugin_with_no_release_is_left_out():
    assert "unreleased" not in parse_dpc(PLUGINS, LATEST)


def test_default_plugins_map_to_catalog_and_extra_slugs():
    assert [default_plugin_slug(u) for u in split_csv(DEFAULT_PLUGINS)] == [
        "dans-plugin-manager",
        "viaversion",
        "viabackwards",
    ]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_adds_non_dpc_defaults_and_caches_until_the_ttl():
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return PLUGINS if url.endswith("/plugins") else LATEST

    clock = Clock()
    catalog = DpcCatalog(split_csv(DEFAULT_PLUGINS), ttl=600, fetch=fetch, clock=clock)
    plugins = catalog.plugins()
    assert {"medieval-factions", "fiefs", "viaversion", "viabackwards"} <= plugins.keys()
    assert plugins["viabackwards"].requires == ("viaversion",)
    assert plugins["viaversion"].filename == "ViaVersion-5.12.0.jar"
    assert len(calls) == 2

    clock.now = 599
    catalog.plugins()
    assert len(calls) == 2
    clock.now = 601
    catalog.plugins()
    assert len(calls) == 4


def test_serves_the_last_good_copy_when_dpc_is_down():
    state = {"up": True}

    def fetch(url, timeout):
        if not state["up"]:
            raise urllib.error.URLError("refused")
        return PLUGINS if url.endswith("/plugins") else LATEST

    clock = Clock()
    catalog = DpcCatalog(fetch=fetch, ttl=10, clock=clock)
    catalog.plugins()
    state["up"] = False
    clock.now = 100
    assert "fiefs" in catalog.plugins()


def test_raises_when_dpc_is_down_and_nothing_is_cached():
    def fetch(url, timeout):
        raise urllib.error.URLError("refused")

    with pytest.raises(CatalogUnavailable):
        DpcCatalog(fetch=fetch).plugins()
