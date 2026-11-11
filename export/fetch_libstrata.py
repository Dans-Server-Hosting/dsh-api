#!/usr/bin/env python3
"""Build-time only (export/Dockerfile.thumbnail): download the libstrata release
pinned in the installed exporter's mcusd/strata.lock.json to DEST and check its
SHA-256. The thumbnail pod has no network, so nothing is fetched at run time."""

import hashlib
import sys
import urllib.request
from pathlib import Path

from mcusd.preview import read_lock


def main(dest: str) -> int:
    lock = read_lock()
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(lock["url"], path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != lock["sha256"]:
        path.unlink()
        print(f"libstrata {digest} does not match the pinned {lock['sha256']}", file=sys.stderr)
        return 1
    path.chmod(0o644)
    print(f"libstrata {lock['version']} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
