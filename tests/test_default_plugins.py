from dsh_api.config import DEFAULT_PLUGINS, split_csv
from dsh_api.plugins import describe_default_plugins, describe_plugin

DPM = (
    "https://github.com/Dans-Plugins/Dans-Plugin-Manager/releases/download/"
    "v0.7.0-SNAPSHOT-8-8-2026/DansPluginManager-0.7.0-SNAPSHOT-8-8-2026.jar"
)
VIAVERSION = (
    "https://github.com/ViaVersion/ViaVersion/releases/download/5.12.0/ViaVersion-5.12.0.jar"
)


def test_known_plugin_gets_display_name_description_and_project_page():
    plugin = describe_plugin(DPM)
    assert plugin.name == "Dan's Plugin Manager"
    assert plugin.version == "0.7.0-SNAPSHOT-8-8-2026"
    assert plugin.description.startswith("Installs and updates plugins")
    assert plugin.download_url == DPM
    assert plugin.project_url == "https://github.com/Dans-Plugins/Dans-Plugin-Manager"


def test_version_starts_at_the_first_dash_followed_by_a_digit():
    plugin = describe_plugin(VIAVERSION)
    assert (plugin.name, plugin.version) == ("ViaVersion", "5.12.0")
    assert plugin.project_url == "https://github.com/ViaVersion/ViaVersion"


def test_unknown_plugin_is_still_described_from_its_filename():
    plugin = describe_plugin("https://cdn.example.com/files/Some-Plugin-1.2.3.jar")
    assert plugin.name == "Some-Plugin"
    assert plugin.version == "1.2.3"
    assert plugin.description == ""
    assert plugin.project_url is None  # not a GitHub release asset


def test_filename_without_a_version_is_the_name_alone():
    plugin = describe_plugin("https://example.com/Thing.jar")
    assert (plugin.name, plugin.version) == ("Thing", "")


def test_every_shipped_default_is_known_and_pinned():
    # A default the portal cannot explain is a gap; the list stays in install order.
    described = describe_default_plugins(split_csv(DEFAULT_PLUGINS))
    assert [p["name"] for p in described] == ["Dan's Plugin Manager", "ViaVersion", "ViaBackwards"]
    assert all(p["description"] and p["version"] and p["project_url"] for p in described)


def test_default_plugin_description_accepts_any_iterable():
    described = describe_default_plugins([DPM, VIAVERSION])
    assert [p["name"] for p in described] == ["Dan's Plugin Manager", "ViaVersion"]


def test_default_plugins_endpoint_is_public_and_ordered(client):
    resp = client.get("/api/v1/default-plugins")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 3
    assert body[0]["name"] == "Dan's Plugin Manager"
    assert body[1]["name"] == "ViaVersion" and body[2]["name"] == "ViaBackwards"
    assert set(body[0]) == {"name", "version", "description", "download_url", "project_url"}
