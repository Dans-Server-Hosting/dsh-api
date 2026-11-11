#!/usr/bin/env bash
# Run the world-export image against the committed fixture server, the way
# the pod runs it, and check what comes out. CI's export-image job runs this.
# With a second image, also draw the fixture's thumbnail with the thumbnail
# image (RFC 0034) the same way, and check the world is still unchanged.
#
#   docker build -t dsh-world-export:ci export/ && export/smoke.sh dsh-world-export:ci
#   export/smoke.sh dsh-world-export:ci dsh-world-thumbnail:ci
set -euo pipefail
image="${1:?usage: smoke.sh IMAGE [THUMBNAIL_IMAGE]}"
thumb_image="${2:-}"
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
if [ -n "$thumb_image" ]; then
  mkdir "$out/thumb"
  # As the pod runs it: no network, read-only root and world, uid 1000,
  # half a CPU and 1 GiB like the pod's limits.
  docker run --rm --network none --read-only --user 1000:1000 --cpus 0.5 --memory 1g \
    --tmpfs /work:uid=1000,gid=1000 --tmpfs /tmp \
    -v "$here/fixtures/server:/mcserver:ro" \
    "$thumb_image" dsh-world-export --server-state asleep --thumbnail > "$out/thumb.tgz"
  tar -xzf "$out/thumb.tgz" -C "$out/thumb"
  python3 - "$out/thumb" <<'PY'
import json, os, sys
out = sys.argv[1]
assert sorted(os.listdir(out)) == ["poster.png", "thumb.webp", "thumbnail.json"], os.listdir(out)
webp = open(f"{out}/thumb.webp", "rb").read()
assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP" and b"ANIM" in webp[:64], webp[:32]
assert open(f"{out}/poster.png", "rb").read(8) == b"\x89PNG\r\n\x1a\n"
m = json.load(open(f"{out}/thumbnail.json"))
assert m["snapshot"] == "at_rest" and m["frames"] == 12 and m["size"] == [200, 132], m
assert m["area"]["x2"] - m["area"]["x1"] == 127, m
assert m["strata"] and m["exporter_commit"], m
assert 1000 < len(webp) < 200_000, len(webp)
print("smoke: thumbnail ok", m["bytes"], m["stats"])
PY
fi
after="$(cd "$here/fixtures/server" && find . -type f -exec sha256sum {} + | sort)"
[ "$before" = "$after" ] || { echo "smoke: the world changed" >&2; exit 1; }
