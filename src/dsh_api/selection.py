"""A server's plugin selection as the wrapper reads it.

The selection is written to the ``<name>-plugins`` ConfigMap as
``selection.json``; OMCSI's boot script reconciles plugins/ against it and
reports the SHA-256 of the file it read. The API hashes the exact text it
writes, so the two hashes agree when the server has applied the latest
selection, and differ while a change waits for the next start.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from dsh_api.catalog import EXTRA_REQUIRES, default_plugin_slug
from dsh_api.db import SelectedPlugin
from dsh_api.plugins import jar_name_and_version


def selection_json(rows: Iterable[SelectedPlugin]) -> str:
    """The ``selection.json`` text for these rows; deterministic, so the same
    selection always hashes the same."""
    plugins = [
        {"slug": r.slug, "tag": r.tag, "filename": r.filename, "url": r.url}
        for r in sorted(rows, key=lambda r: r.slug)
    ]
    return json.dumps({"version": 1, "plugins": plugins}, indent=2, sort_keys=True) + "\n"


def selection_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def default_selection(default_urls: Iterable[str]) -> list[SelectedPlugin]:
    """The selection a server starts with: the default plugins, pinned to the
    exact jars the default list names (the ones already on an existing
    server's disk, so its first reconcile adopts them without a download)."""
    rows = []
    for url in default_urls:
        slug = default_plugin_slug(url)
        rows.append(
            SelectedPlugin(
                slug=slug,
                tag=jar_name_and_version(url)[1],
                filename=url.rsplit("/", 1)[-1],
                url=url,
                requires=EXTRA_REQUIRES.get(slug, ()),
                added_by="default",
            )
        )
    return rows
