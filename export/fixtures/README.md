`server/world` is the `tests/fixtures/world-26.3` fixture of
[Minecraft-USD-Exporter](https://github.com/Dans-Plugins/Minecraft-USD-Exporter)
(MIT) at commit `ab4d4bdc838e25f18c4a0c98dbe3992c1a91efd3`: four chunks of a fresh
vanilla 26.3 world, recipe in that repository's `tests/fixtures/RECIPE.md`.
`server/server.properties` is the one line the export reads.

`smoke.sh` runs the export image against it the way the pod does: world
mounted read-only, read-only root filesystem, no network, uid 1000.
