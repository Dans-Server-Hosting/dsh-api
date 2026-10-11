"""The plugins a tenant can install: the Dan's Plugins catalog, plus the
non-DPC plugins every server ships with by default.

The catalog is read from dansplugins.com's public API (``/api/v1/plugins``
for the plugins, ``/versions/latest`` for what to download) and cached, so
the portal does not depend on DPC being up for every page view. Downloads
go through DPC's counting ``downloadPath``, which redirects to the GitHub
asset, so installs made from DSH show up in the site's download counts.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from dsh_api.plugins import describe_plugin, jar_name_and_version

DPC_API = "https://api.dansplugins.com"
DPC_SITE = "https://dansplugins.com"
CATALOG_TTL_SECONDS = 600.0

# The plugins the default list carries that are not in the DPC catalog, by
# the name part of their jar's filename. ViaBackwards ``depend``s on
# ViaVersion, so it requires it here too.
EXTRA_SLUGS: dict[str, str] = {"ViaVersion": "viaversion", "ViaBackwards": "viabackwards"}
EXTRA_REQUIRES: dict[str, tuple[str, ...]] = {"viabackwards": ("viaversion",)}
# A default plugin that *is* in the catalog, by filename name part; its slug
# is the catalog's, so a tenant sees one entry, not two.
CATALOG_SLUGS: dict[str, str] = {"DansPluginManager": "dans-plugin-manager"}


class CatalogUnavailable(Exception):
    """The catalog could not be read and no earlier copy is cached."""


@dataclass(frozen=True)
class CatalogPlugin:
    slug: str
    title: str
    description: str
    tags: tuple[str, ...]
    requires: tuple[str, ...]
    tag: str
    """The release to install; pinned into a selection when it is added."""
    filename: str
    url: str
    icon_url: str | None
    project_url: str | None

    def to_json(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "requires": list(self.requires),
            "version": self.tag,
            "icon_url": self.icon_url,
            "project_url": self.project_url,
        }


class PluginCatalog(Protocol):
    def plugins(self) -> dict[str, CatalogPlugin]:
        """Every installable plugin by slug; raises ``CatalogUnavailable``."""


def default_plugin_slug(url: str) -> str:
    """The slug a default plugin's URL is selected under."""
    key = _filename_key(url)
    return CATALOG_SLUGS.get(key) or EXTRA_SLUGS.get(key) or key.lower()


def _filename_key(url: str) -> str:
    return jar_name_and_version(url)[0]


def extras_from_defaults(default_urls: Iterable[str]) -> dict[str, CatalogPlugin]:
    """The default plugins that are not DPC's, as catalog entries pinned to the
    exact jar the default list names."""
    extras: dict[str, CatalogPlugin] = {}
    for url in default_urls:
        key = _filename_key(url)
        if key not in EXTRA_SLUGS:
            continue
        slug = EXTRA_SLUGS[key]
        described = describe_plugin(url)
        extras[slug] = CatalogPlugin(
            slug=slug,
            title=described.name,
            description=described.description,
            tags=("compatibility",),
            requires=EXTRA_REQUIRES.get(slug, ()),
            tag=described.version,
            filename=url.rsplit("/", 1)[-1],
            url=url,
            icon_url=None,
            project_url=described.project_url,
        )
    return extras


def parse_dpc(plugins: object, latest: object, api: str = DPC_API) -> dict[str, CatalogPlugin]:
    """Join DPC's plugin list with its latest versions. A plugin with no
    release (nothing to download) is left out."""
    if not isinstance(plugins, list) or not isinstance(latest, list):
        raise ValueError("unexpected catalog shape")
    releases = {r.get("slug"): r for r in latest if isinstance(r, dict)}
    catalog: dict[str, CatalogPlugin] = {}
    for p in plugins:
        if not isinstance(p, dict) or not isinstance(p.get("slug"), str):
            continue
        release = releases.get(p["slug"])
        if not release or not release.get("downloadPath") or not release.get("downloadUrl"):
            continue
        icon = p.get("iconPath")
        icon_ok = isinstance(icon, str) and icon.startswith("/")
        catalog[p["slug"]] = CatalogPlugin(
            slug=p["slug"],
            title=str(p.get("title") or p["slug"]),
            description=str(p.get("description") or ""),
            tags=tuple(str(t) for t in p.get("tags") or ()),
            requires=tuple(str(r) for r in p.get("requires") or ()),
            tag=str(release["tag"]),
            filename=str(release["downloadUrl"]).rsplit("/", 1)[-1],
            url=api + str(release["downloadPath"]),
            icon_url=f"{DPC_SITE}{icon}" if icon_ok else None,
            project_url=f"{DPC_SITE}/resources/{p['slug']}",
        )
    return catalog


def _fetch_json(url: str, timeout: float) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.load(resp)


class DpcCatalog:
    """The DPC catalog, cached for ``ttl`` seconds; on a failed refresh the
    last good copy is served, and with none the error is raised."""

    def __init__(
        self,
        default_urls: Iterable[str] = (),
        api: str = DPC_API,
        ttl: float = CATALOG_TTL_SECONDS,
        timeout: float = 5.0,
        fetch: Callable[[str, float], object] = _fetch_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.extras = extras_from_defaults(default_urls)
        self.api = api
        self.ttl = ttl
        self.timeout = timeout
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: dict[str, CatalogPlugin] | None = None
        self._fetched_at = 0.0

    def plugins(self) -> dict[str, CatalogPlugin]:
        with self._lock:
            if self._cached is not None and self._clock() - self._fetched_at < self.ttl:
                return self._cached
            try:
                dpc = parse_dpc(
                    self._fetch(f"{self.api}/api/v1/plugins", self.timeout),
                    self._fetch(f"{self.api}/api/v1/plugins/versions/latest", self.timeout),
                    self.api,
                )
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if self._cached is not None:
                    return self._cached
                raise CatalogUnavailable(f"the plugin catalog could not be read ({exc})") from exc
            self._cached = {**dpc, **self.extras}
            self._fetched_at = self._clock()
            return self._cached


class FakeCatalog:
    def __init__(self, plugins: Iterable[CatalogPlugin] = (), available: bool = True) -> None:
        self.entries = {p.slug: p for p in plugins}
        self.available = available

    def plugins(self) -> dict[str, CatalogPlugin]:
        if not self.available:
            raise CatalogUnavailable("the plugin catalog could not be read (simulated)")
        return dict(self.entries)
