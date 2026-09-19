# syntax=docker/dockerfile:1
FROM python:3.11-slim

# A commit on OMCSI main. The previous pin (b7653aa) was a merge commit on the
# stacked feat/colocated-values-profile branch, unreachable since that branch
# was merged and deleted on 2026-09-14, so `git clone` could not check it out.
# e944a82 is what dsh-cluster/scripts/lib.sh pins and what the tenants run.
ARG OMCSI_PIN=e944a82426582abfc4e793ffffc27054d3c603c3
ARG KUBECTL_VERSION=v1.31.4
ARG HELM_VERSION=v3.16.4

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMCSI_CHART_DIR=/opt/omcsi DSH_DB_PATH=/data/dsh-api.db DSH_BACKUP_DIR=/backups

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tar \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" | tar -xzO "linux-${arch}/helm" > /usr/local/bin/helm \
    && chmod 0755 /usr/local/bin/helm \
    && git clone https://github.com/Stephenson-Software/open-mc-server-infrastructure /opt/omcsi \
    && git -C /opt/omcsi checkout --quiet "${OMCSI_PIN}" \
    && rm -rf /opt/omcsi/.git \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home dsh \
    && mkdir -p /data /backups && chown dsh:dsh /data /backups
USER 10001

EXPOSE 8000
CMD ["uvicorn", "--factory", "dsh_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
