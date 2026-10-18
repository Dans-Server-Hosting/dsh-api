# syntax=docker/dockerfile:1
# The world-export image (RFC 0021): Minecraft-USD-Exporter and usd-web, each
# pinned to a commit, plus dsh-world-export. dsh-api starts it as an idle pod
# in a tenant namespace with that server's world volume mounted read-only, then
# runs `kubectl exec <pod> -- dsh-world-export ...`, which writes a gzipped tar
# of the results to stdout.
#
#   docker build -t dsh-world-export:local export/
FROM python:3.12-slim

# Both pin usd-core==26.8. Move a pin deliberately, like OMCSI_PIN in ../Dockerfile.
ARG EXPORTER_PIN=ab4d4bdc838e25f18c4a0c98dbe3992c1a91efd3
ARG USD_WEB_PIN=418892dae443f3291a34bf6fc7da0ca53d6b2ec8

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    EXPORTER_PIN=${EXPORTER_PIN} USD_WEB_PIN=${USD_WEB_PIN}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && pip install --no-cache-dir \
        "minecraft-usd-exporter[lz4] @ git+https://github.com/Dans-Plugins/Minecraft-USD-Exporter@${EXPORTER_PIN}" \
        "usd-web @ git+https://github.com/Preponderous-Software/usd-web@${USD_WEB_PIN}" \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --home-dir /home/export export

COPY dsh_world_export.py /usr/local/bin/dsh-world-export
RUN chmod 0755 /usr/local/bin/dsh-world-export \
    && dsh-world-export --help > /dev/null \
    && mc-usd-export --help > /dev/null && usd-web --version

# Same uid/gid as the OMCSI wrapper, so the world's files are readable.
USER 1000:1000
WORKDIR /work
# The pod idles; the work arrives over `kubectl exec`. The pod's own
# activeDeadlineSeconds ends it whatever happens.
CMD ["sleep", "900"]
