# dsh-api

Tenant-facing API for **Dan's Server Hosting**: create, list, wake and remove
your Minecraft servers. See [MVP.md](MVP.md) for scope and endpoints.

Status: **MVP implemented, not yet deployed** — every handler is covered by
tests against an in-memory cluster; the first deploy to the real cluster is
what remains (see the MVP done-when list).

## How it works

- FastAPI on Python 3.11. Callers present a UserAuth JWT
  (`Authorization: Bearer …`), validated with the shared HS256 secret; the
  token's `sub` is the tenant id.
- Provisioning shells out to `kubectl` and `helm` exactly the way the
  operator's script does: namespace `t-<name>` with the hosted-free quota and
  limit range, a `dsh-credentials` Secret, then `helm upgrade --install` of the
  OMCSI chart from its co-located values profile. The chart is baked into the
  image at a pinned commit (`OMCSI_PIN` build arg).
- Every cluster interaction goes through one interface, `ClusterBackend`
  (`src/dsh_api/cluster.py`), with a real `KubectlHelmBackend` and a
  `FakeClusterBackend` used by the tests. Nothing in the test suite needs a
  cluster or the network.
- State is one SQLite file: `tenants`, `servers` (who owns what) and `events`.
  The cluster stays the source of truth for whether a server exists and what
  state it is in (`asleep | waking | awake | failed`, read from the wrapper
  StatefulSet).
- The player count comes from the game server's own status handshake
  (server list ping) against the wrapper Service inside the cluster.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/healthz` | liveness |
| `GET` | `/api/v1/limits` | free-tier profile as numbers; no token needed |
| `GET` | `/api/v1/servers` | the caller's servers |
| `POST` | `/api/v1/servers` | `{name, motd?, operator_username?}` → 201; the admin password is in this response **only** |
| `GET` | `/api/v1/servers/{name}` | one server, with `players_online` when awake |
| `POST` | `/api/v1/servers/{name}/wake` | scales the wrapper to 1 |
| `DELETE` | `/api/v1/servers/{name}` | backup, `helm uninstall`, namespace delete; 409 while players are online unless `?force=true` |

`POST /api/v1/servers` blocks until the Helm release is installed
(`helm --wait`, up to `DSH_HELM_TIMEOUT`), so expect it to take a few minutes.

## Running locally

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && pytest            # no cluster, no network

USERAUTH_JWT_SECRET=dev-secret-of-at-least-32-characters DSH_NODE_IP=203.0.113.10 \
  uvicorn --factory dsh_api.main:create_app --reload
```

The interactive docs are at `http://127.0.0.1:8000/docs`. Provisioning against a
cluster additionally needs `kubectl`/`helm` on `PATH` pointed at it, and an
OMCSI checkout at `OMCSI_CHART_DIR` (the image provides all three).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `USERAUTH_JWT_SECRET` | *(required)* | HS256 secret shared with UserAuth |
| `DSH_DB_PATH` | `dsh-api.db` | SQLite file |
| `DSH_BASE_DOMAIN` | `dansserverhosting.com` | servers are `<name>.play.<domain>` |
| `DSH_NODE_IP` | *(required)* | node address for the `<name>.<ip-dashed>.sslip.io` fallback hostname |
| `OMCSI_CHART_DIR` | `/opt/omcsi` | OMCSI checkout; the chart is `helm/omcsi` inside it |
| `DSH_BACKUP_DIR` | `/backups` | where `DELETE` writes `<name>-<timestamp>.tar.gz` |
| `DSH_HELM_TIMEOUT` | `5m` | `helm --wait` timeout |
| `DSH_MAX_SERVERS_PER_TENANT` | `1` | the cap behind the 403 |
| `DSH_LIMIT_HEAP_GB` | `3` | free-tier profile served by `GET /api/v1/limits` |
| `DSH_LIMIT_MEMORY_LIMIT_GIB` | `3.5` | " |
| `DSH_LIMIT_WORLD_QUOTA_GIB` | `5` | " |
| `DSH_LIMIT_IDLE_MINUTES` | `20` | " |
| `DSH_LIMIT_MAX_AWAKE` | `12` | " |
| `DSH_LIMIT_MAX_REGISTERED` | `40` | " |
| `DSH_LIMIT_ARCHIVE_AFTER_DAYS` | `60` | " |
| `DSH_LIMIT_BACKUP_RETENTION_DAYS` | `14` | " |
| `DSH_LIMIT_MINECRAFT_VERSION` | `26.2` | " |

The limits are configuration rather than constants so the portal shows the
same numbers the cluster enforces.

## Deploying

`deploy/` holds plain manifests (`kubectl kustomize deploy/` renders them):
namespace, ServiceAccount + ClusterRole limited to what provisioning touches,
ConfigMap, Secret stub, two PVCs (state, backups), Deployment, Service and the
`api.<domain>` Ingress with the cert-manager annotation. Before applying: build
and push the image somewhere the cluster can pull from and set it in
`deployment.yaml`, put the real node address in `config.yaml`, and create the
`dsh-api` Secret out of band rather than from the committed stub.

## Layout

```
src/dsh_api/
  main.py      FastAPI app factory and routes
  auth.py      JWT validation (real + fake)
  cluster.py   ClusterBackend: kubectl/helm backend + in-memory fake
  service.py   create / list / get / wake / delete
  db.py        SQLite schema and queries
  mojang.py    operator UUID lookup (real + fake)
  config.py    settings and the limits profile
tests/         pytest, no cluster required
deploy/        Kubernetes manifests
```
