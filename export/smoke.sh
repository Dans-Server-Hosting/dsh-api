#!/usr/bin/env bash
# Run the world-export image against the committed fixture server, the way
# the pod runs it, and check what comes out. CI's export-image job runs this.
#
#   docker build -t dsh-world-export:ci export/ && export/smoke.sh dsh-world-export:ci
set -euo pipefail
image="${1:?usage: smoke.sh IMAGE}"
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
chmod -R a+rX "$here/fixtures/server"
before="$(cd "$here/fixtures/server" && find . -type f -exec sha256sum {} + | sort)"

docker run --rm --network none --read-only --user 1000:1000 \
  --tmpfs /work:uid=1000,gid=1000 --tmpfs /tmp \
  -v "$here/fixtures/server:/mcserver:ro" \
  "$image" dsh-world-export --server-state asleep --title fixture > "$out/export.tgz"

tar -xzf "$out/export.tgz" -C "$out"
test -s "$out/world.zip" && test -s "$out/preview/index.json"
ls "$out"/preview/t.*.uwt > /dev/null
python3 - "$out" <<'PY'
import json, sys, zipfile
out = sys.argv[1]
s = json.load(open(f"{out}/summary.json"))
names = sorted(zipfile.ZipFile(f"{out}/world.zip").namelist())
assert names == ["README.txt", "terrain.usdc", "world.usda"], names
assert s["snapshot"] == "at_rest", s
assert s["stats"]["instances"] > 30000, s
assert s["exporter_commit"] and s["usd_web_commit"], s
idx = json.load(open(f"{out}/preview/index.json"))
assert idx["format"] == "usd-web-index" and idx["tiles"], idx.keys()
print("smoke: ok", s["area"], s["stats"])
PY
after="$(cd "$here/fixtures/server" && find . -type f -exec sha256sum {} + | sort)"
[ "$before" = "$after" ] || { echo "smoke: the world changed" >&2; exit 1; }
