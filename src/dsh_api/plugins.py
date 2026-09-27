"""The default plugins as the portal shows them.

``DSH_DEFAULT_PLUGINS`` stays a plain list of download URLs -- that is the
contract with the wrapper, which installs whatever it is given by filename --
and this module turns each URL into something a person can read: the plugin's
name and version from the jar's filename, the project page from a GitHub
release URL, and a one-line description for the plugins the service knows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

# Filenames look like ``ViaVersion-5.12.0.jar`` or
# ``DansPluginManager-0.7.0-SNAPSHOT-8-8-2026.jar``: the name runs up to the
# first dash that is followed by a digit, the rest is the version.
_NAME_VERSION = re.compile(r"^(?P<name>.+?)-(?P<version>\d.*)$")
_GITHUB_RELEASE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/")

# Display names and descriptions for the plugins the service ships by default.
# Keyed by the filename's name part; anything not listed here is shown by
# that name with no description, so an addition to DSH_DEFAULT_PLUGINS never
# breaks the endpoint.
KNOWN_PLUGINS: dict[str, tuple[str, str]] = {
    "DansPluginManager": (
        "Dan's Plugin Manager",
        "Installs and updates plugins from Dan's Plugins in-game with /dpm.",
    ),
    "ViaVersion": (
        "ViaVersion",
        "Lets players on newer Minecraft versions join your server.",
    ),
    "ViaBackwards": (
        "ViaBackwards",
        "Lets players on older Minecraft versions join your server.",
    ),
}


@dataclass(frozen=True)
class DefaultPlugin:
    name: str
    version: str
    description: str
    download_url: str
    project_url: str | None


def describe_plugin(url: str) -> DefaultPlugin:
    parsed = urlparse(url)
    filename = parsed.path.rsplit("/", 1)[-1]
    stem = filename[:-4] if filename.endswith(".jar") else filename
    match = _NAME_VERSION.match(stem)
    key, version = (match["name"], match["version"]) if match else (stem, "")
    name, description = KNOWN_PLUGINS.get(key, (key, ""))
    project_url = None
    if parsed.netloc == "github.com":
        release = _GITHUB_RELEASE.match(parsed.path)
        if release:
            project_url = f"https://github.com/{release['owner']}/{release['repo']}"
    return DefaultPlugin(
        name=name,
        version=version,
        description=description,
        download_url=url,
        project_url=project_url,
    )


def describe_default_plugins(urls: Iterable[str]) -> list[dict]:
    return [asdict(describe_plugin(url)) for url in urls]
