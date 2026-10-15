import hashlib
import json

from dsh_api.config import DEFAULT_PLUGINS, split_csv
from dsh_api.db import SelectedPlugin
from dsh_api.selection import default_selection, selection_json, selection_sha256

FIEFS = SelectedPlugin(
    slug="fiefs",
    tag="v1.4.0",
    filename="Fiefs-1.4.0.jar",
    url="https://example.com/fiefs/1.4.0",
    requires=("medieval-factions",),
    added_by="tenant",
    added_at="2026-10-01T00:00:00+00:00",
)
FACTIONS = SelectedPlugin(
    slug="medieval-factions",
    tag="v5.6.0",
    filename="MedievalFactions-5.6.0.jar",
    url="https://example.com/mf/5.6.0",
    added_by="dependency",
)


def test_selection_json_carries_only_what_the_wrapper_reads():
    # requires, added_by and added_at are the API's bookkeeping, not the boot
    # script's; a change to them must not change the file or its hash.
    doc = json.loads(selection_json([FIEFS]))
    assert doc == {
        "version": 1,
        "plugins": [
            {
                "slug": "fiefs",
                "tag": "v1.4.0",
                "filename": "Fiefs-1.4.0.jar",
                "url": "https://example.com/fiefs/1.4.0",
            }
        ],
    }


def test_selection_json_is_sorted_by_slug_whatever_the_input_order():
    forward = selection_json([FIEFS, FACTIONS])
    assert forward == selection_json([FACTIONS, FIEFS])
    assert [p["slug"] for p in json.loads(forward)["plugins"]] == ["fiefs", "medieval-factions"]


def test_selection_json_is_indented_with_sorted_keys_and_a_trailing_newline():
    assert selection_json([]) == '{\n  "plugins": [],\n  "version": 1\n}\n'


def test_selection_json_accepts_any_iterable():
    assert selection_json(iter([FIEFS])) == selection_json([FIEFS])


def test_bookkeeping_fields_do_not_change_the_hash():
    relabelled = SelectedPlugin(
        slug=FIEFS.slug,
        tag=FIEFS.tag,
        filename=FIEFS.filename,
        url=FIEFS.url,
        added_by="default",
    )
    original = selection_sha256(selection_json([FIEFS]))
    assert selection_sha256(selection_json([relabelled])) == original


def test_selection_sha256_is_the_hex_digest_of_the_utf8_text():
    text = selection_json([FIEFS])
    assert selection_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(selection_sha256(text)) == 64


def test_a_different_pin_changes_the_hash():
    upgraded = SelectedPlugin(
        slug=FIEFS.slug, tag="v1.5.0", filename="Fiefs-1.5.0.jar", url=FIEFS.url
    )
    original = selection_sha256(selection_json([FIEFS]))
    assert selection_sha256(selection_json([upgraded])) != original


def test_default_selection_pins_the_exact_default_jars_in_order():
    urls = split_csv(DEFAULT_PLUGINS)
    rows = default_selection(urls)
    assert [r.slug for r in rows] == ["dans-plugin-manager", "viaversion", "viabackwards"]
    assert [r.url for r in rows] == list(urls)
    assert [r.filename for r in rows] == [
        "DansPluginManager-0.7.0-SNAPSHOT-8-8-2026.jar",
        "ViaVersion-5.12.0.jar",
        "ViaBackwards-5.12.0.jar",
    ]
    assert [r.tag for r in rows] == ["0.7.0-SNAPSHOT-8-8-2026", "5.12.0", "5.12.0"]
    assert all(r.added_by == "default" for r in rows)


def test_default_selection_carries_viabackwards_dependency_on_viaversion():
    rows = {r.slug: r for r in default_selection(split_csv(DEFAULT_PLUGINS))}
    assert rows["viabackwards"].requires == ("viaversion",)
    assert rows["viaversion"].requires == ()
    assert rows["dans-plugin-manager"].requires == ()


def test_default_selection_of_an_unknown_jar_is_slugged_from_its_filename():
    (row,) = default_selection(["https://cdn.example.com/files/Some-Plugin-1.2.3.jar"])
    assert row.slug == "some-plugin"
    assert row.tag == "1.2.3"
    assert row.filename == "Some-Plugin-1.2.3.jar"
    assert row.requires == ()


def test_default_selection_of_nothing_is_empty():
    assert default_selection([]) == []
