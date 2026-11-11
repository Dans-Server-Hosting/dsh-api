"""Settings.from_env against the README's Configuration table, and the parsing
rules the table does not spell out."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dsh_api.config import Limits, Settings

ROOT = Path(__file__).resolve().parent.parent
CONFIG_SOURCE = (ROOT / "src" / "dsh_api" / "config.py").read_text()
ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \| (.+?) \|", re.MULTILINE)
FLAGS = ("DSH_PLUGIN_SELECTION", "DSH_WORLD_EXPORT")
MODES = {"DSH_WORLD_THUMBNAILS": "manual"}  # a mode, not a flag or a number
LITERAL = re.compile(r"`[^`<>]+`")  # a `<placeholder>` default is a template, not a value


def readme_table() -> dict[str, str]:
    """``{variable: default cell}`` from the README's Configuration table."""
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    return dict(ROW.findall(section))


def test_the_readme_table_lists_exactly_the_variables_config_reads():
    read = set(re.findall(r'env\.get\(\s*"([A-Z][A-Z0-9_]*)"', CONFIG_SOURCE))
    assert read, "no env.get calls found; the pattern no longer matches config.py"
    assert set(readme_table()) == read


@pytest.mark.parametrize("variable", sorted(readme_table()))
def test_every_documented_variable_changes_the_settings(variable):
    value = "true" if variable in FLAGS else MODES.get(variable, "7")
    assert Settings.from_env({variable: value}) != Settings.from_env({})


@pytest.mark.parametrize(
    ("variable", "default"),
    sorted(
        (variable, cell[1:-1])
        for variable, cell in readme_table().items()
        if LITERAL.fullmatch(cell)
    ),
)
def test_setting_a_variable_to_its_documented_default_changes_nothing(variable, default):
    assert Settings.from_env({variable: default}) == Settings.from_env({})


def test_the_literal_defaults_checked_above_cover_most_of_the_table():
    literal = [cell for cell in readme_table().values() if LITERAL.fullmatch(cell)]
    assert len(literal) >= 25  # prose and templated defaults (required, derived, the
    # plugin jars, https://api.<domain>) are skipped


@pytest.mark.parametrize("variable", FLAGS)
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES"])
def test_flags_accept_one_true_and_yes_in_any_case(variable, value):
    s = Settings.from_env({variable: value})
    assert (s.plugin_selection if variable == "DSH_PLUGIN_SELECTION" else s.world_export) is True


@pytest.mark.parametrize("variable", FLAGS)
@pytest.mark.parametrize("value", ["", "0", "false", "no", "on", "enabled", " true"])
def test_any_other_flag_value_is_off(variable, value):
    s = Settings.from_env({variable: value})
    assert (s.plugin_selection if variable == "DSH_PLUGIN_SELECTION" else s.world_export) is False


def test_public_api_url_overrides_the_domain_and_loses_trailing_slashes():
    s = Settings.from_env(
        {"DSH_BASE_DOMAIN": "example.com", "DSH_PUBLIC_API_URL": "https://dl.example.org//"}
    )
    assert s.api_url() == "https://dl.example.org"
    assert Settings.from_env({"DSH_BASE_DOMAIN": "example.com"}).api_url() == (
        "https://api.example.com"
    )


def test_every_limit_is_read_with_its_type():
    limits = Limits.from_env(
        {
            "DSH_LIMIT_HEAP_GB": "2.5",
            "DSH_LIMIT_MEMORY_LIMIT_GIB": "3",
            "DSH_LIMIT_WORLD_QUOTA_GIB": "10.5",
            "DSH_LIMIT_IDLE_MINUTES": "45",
            "DSH_LIMIT_MAX_AWAKE": "6",
            "DSH_LIMIT_MAX_REGISTERED": "80",
            "DSH_LIMIT_ARCHIVE_AFTER_DAYS": "30",
            "DSH_LIMIT_BACKUP_RETENTION_DAYS": "7",
            "DSH_LIMIT_MINECRAFT_VERSION": "26.3",
        }
    )
    assert limits == Limits(
        heap_gb=2.5,
        memory_limit_gib=3.0,
        world_quota_gib=10.5,
        idle_minutes=45,
        max_awake_servers=6,
        max_registered_servers=80,
        archive_after_days=30,
        backup_retention_days=7,
        minecraft_version="26.3",
    )
    assert isinstance(limits.memory_limit_gib, float) and isinstance(limits.idle_minutes, int)
    assert Settings.from_env({"DSH_LIMIT_MAX_AWAKE": "6"}).limits.max_awake_servers == 6


@pytest.mark.parametrize(
    "variable",
    ["DSH_MAX_SERVERS_PER_TENANT", "DSH_EXPORT_MAX_SIZE", "DSH_LIMIT_IDLE_MINUTES"],
)
def test_a_non_integer_count_fails_at_load_rather_than_later(variable):
    with pytest.raises(ValueError):
        Settings.from_env({variable: "2.5"})


def test_without_an_argument_the_process_environment_is_read(monkeypatch):
    monkeypatch.setenv("DSH_BASE_DOMAIN", "example.net")
    monkeypatch.setenv("DSH_EXPORT_RETENTION_HOURS", "12")
    s = Settings.from_env()
    assert s.base_domain == "example.net" and s.export_retention_hours == 12


def test_the_link_secret_and_public_url_default_to_empty_so_they_derive():
    s = Settings.from_env({})
    assert s.export_link_secret == "" and s.public_api_url == ""


@pytest.mark.parametrize(
    ("value", "mode", "on"),
    [
        ("off", "off", False),
        ("manual", "manual", True),
        ("MANUAL ", "manual", True),
        ("auto", "auto", True),
        ("true", "off", False),
        ("on", "off", False),
        ("", "off", False),
    ],
)
def test_thumbnail_mode_is_off_manual_or_auto_and_anything_else_is_off(value, mode, on):
    s = Settings.from_env({"DSH_WORLD_THUMBNAILS": value})
    assert s.world_thumbnails == mode and s.thumbnails_on is on
