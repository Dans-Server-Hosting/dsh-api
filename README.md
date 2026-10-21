# dsh-api

Tenant-facing API for **Dan's Server Hosting**: create, list, wake and remove
your Minecraft servers. See [MVP.md](MVP.md) for scope and endpoints.

Status: **deployed** — running in the cluster since 2026-09-14 and reachable
at `api.<domain>` with a real certificate; every handler is covered by tests
against an in-memory cluster, and the create → wake → delete cycle has been
driven through the public API (see the MVP done-when list).

## How it works

- FastAPI on Python 3.11. Callers present a UserAuth JWT
  (`Authorization: Bearer …`), validated with the shared HS256 secret; the
  token's `sub` is the tenant id.
- Provisioning shells out to `kubectl` and `helm` exactly the way the
  operator's script does: namespace `t-<name>` with the hosted-free quota and
  limit range, a RoleBinding giving the API itself access to that namespace
  (see [Permission model](#permission-model)), a `dsh-credentials` Secret,
  then `helm upgrade --install` of the OMCSI chart from its co-located values
  profile. The chart is baked into the image at a pinned commit (`OMCSI_PIN`
  build arg).
- Every cluster interaction goes through one interface, `ClusterBackend`
  (`src/dsh_api/cluster.py`), with a real `KubectlHelmBackend` and a
  `FakeClusterBackend` used by the tests. Nothing in the test suite needs a
  cluster or the network.
- State is one SQLite file: `tenants`, `servers` (who owns what, and whether
  its create is still in flight), `events`, `feedback` (what signed-in
  people said about the portal, for the admins), and `server_plugins` /
  `server_plugin_state` (each server's plugin selection, and the hashes of
  the selection last written to its ConfigMap and the one the wrapper last
  reported applying at boot), and `world_exports` (each world export's
  status, box, snapshot kind and expiry; the files themselves are under
  `DSH_EXPORT_DIR`). Once a server is created the
  cluster is the source of truth for whether it exists and what state it is
  in: the wrapper StatefulSet says whether the pod is up, and the wrapper's
  own API (`/api/server/status` on its internal Service, port 8092) says
  whether the game process is running inside it.
- Creating a server takes minutes (helm install, a wake, three rollouts), so
  `POST /api/v1/servers` answers at once and the cluster steps run on a small
  thread pool inside the API process (two at a time). The row is the record
  of that job: `provisioning` until it finishes, then the cluster's reading,
  or `failed`.
- The player count comes from the game server's own status handshake
  (server list ping) against the wrapper Service inside the cluster.

### Server states

| `state` | StatefulSet | Wrapper says | Meaning |
|---|---|---|---|
| `provisioning` | *(not asked)* | *(not asked)* | the create was accepted and its steps are still running; `wake` is a no-op and `DELETE` is refused (409) until it finishes |
| `asleep` | 0 replicas | *(not asked)* | scaled down by the router or never woken; `wake` scales it up |
| `waking` | 1 replica, pod not Ready — or Ready with `running: false` less than 3 minutes after the scale-up | booting | the pod or the game is still coming up |
| `awake` | 1 replica, pod Ready | `running: true` | joinable; `players_online` is reported on `GET /api/v1/servers/{name}` |
| `stopped` | 1 replica, pod Ready | `running: false` | the game process exited (Stop in the dashboard, or a crash) while the pod stayed up; `wake` starts it in place |
| `failed` | missing, or a pod in `CrashLoopBackOff` / `ImagePullBackOff` / `Failed` — or the create itself failed, whatever the cluster says | *(not asked)* | needs the operator; a failed create keeps the tenant's slot until `DELETE`, which works without a pod or volume to back up |
| `deleting` | *(not asked)* | *(not asked)* | a `DELETE` was accepted and its backup, uninstall and namespace removal are still running; the server keeps its name and the tenant's slot until it is gone (404), `wake` is a no-op and a second `DELETE` answers with it as it is |

`provisioning`, `deleting` and a failed create are read from the row, not
the cluster: during the create the release may not exist yet, and after a
failure the namespace and release are left as they are for inspection (the
error is in the `events` table as `create.failed`). If the API restarts
mid-create the row is marked failed on startup (`create.interrupted`); nothing
is retried on its own. A delete that fails puts the server back as it was
(`ready` or a failed create) with the reason recorded as `backup.failed` or
`delete.failed`, and so does a restart mid-delete (`delete.interrupted`); the
tenant may `DELETE` again.

The pod's readiness probe is the wrapper's Spring health, not the game's, so
readiness alone cannot tell `awake` from `stopped`. When the wrapper cannot be
asked (connection refused, timeout, non-200) the replica-based reading stands:
Ready is `awake`, not Ready is `waking`. A slow or missing wrapper answer
never fails a list or get.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/healthz` | liveness |
| `GET` | `/api/v1/limits` | free-tier profile as numbers, plus the world-export limits (`export_default_size`, `export_max_size`, `export_daily_limit`, `export_retention_hours`); no token needed |
| `GET` | `/api/v1/default-plugins` | the plugins every new server is installed with, in install order (`name`, `version`, `description`, `download_url`, `project_url`); no token needed |
| `GET` | `/api/v1/servers` | the caller's servers |
| `POST` | `/api/v1/servers` | `{name, motd?, operator_username?}` → **202** with the server in state `provisioning`; the admin password is in this response **only**. 409 `{"detail": "a server is already being created for this account", "server": "<name>"}` while the caller's earlier create is still running; 409 when the name is taken; 403 at the tenant cap |
| `GET` | `/api/v1/servers/{name}` | one server, with `players_online` when `awake` (`null` otherwise) |
| `POST` | `/api/v1/servers/{name}/wake` | 202 with the resulting server: `asleep` → scales the wrapper to 1; `stopped` → `POST /api/server/start` on the wrapper; `waking`/`awake`/`failed` → no-op (a failed server, including one whose StatefulSet is gone, is reported as such rather than scaled) |
| `DELETE` | `/api/v1/servers/{name}` | **202** with the server in state `deleting`; backup, `helm uninstall` and namespace delete then run in the background, and `GET` answers 404 once they are done. 409 while players are online unless `?force=true`; 409 while the server is still `provisioning`; a `failed` create is removed even when there is nothing to back up; a retry after a delete that failed past its backup reuses that backup when the world volume is already gone |
| `GET` | `/api/v1/catalog` | the plugins a tenant can install, by title (`slug`, `title`, `description`, `tags`, `requires`, `version`, `icon_url`, `project_url`): the Dan's Plugins catalog plus the non-DPC defaults; no token needed. 503 while plugin selection is off, or when dansplugins.com cannot be read and nothing is cached |
| `GET` | `/api/v1/servers/{name}/plugins` | the server's plugin selection: `{server, state, players_online, pending, plugins: [{slug, title, version, added_by, requires, required_by, status}]}`. `status` is `installed`, `pending` (waits for the next start) or `failed: <reason>` from the last boot; `pending` is true while a change waits |
| `PUT` | `/api/v1/servers/{name}/plugins/{slug}` | select a plugin and anything it `requires` (`added_by: dependency`), pinned to the catalog's current release; idempotent. `{added: [slugs], ...the selection}`. 404 for a slug not in the catalog; 409 while the server is being created or deleted |
| `DELETE` | `/api/v1/servers/{name}/plugins/{slug}` | deselect a plugin; its data folder is kept. `{removed, unused_dependencies: [slugs], ...the selection}`; offer `unused_dependencies` for removal, they are not removed. 409 `{required_by}` while another selected plugin requires it; 404 when it is not selected |
| `POST` | `/api/v1/servers/{name}/plugins/apply` | `{confirm_players?}` → 202. `asleep`: `{applies: "next_start"}`, nothing restarted. `awake`/`stopped`: deletes the wrapper pod so its boot script applies the selection, `{applies: "restarting"}`; with players online, 409 `{players_online}` unless `confirm_players`, then an in-game warning first. 409 while `waking` |
| `GET` | `/api/v1/servers/{name}/exports` | world export (see [World export](#world-export-rfc-0021)); `{exports: [...]}` newest first. Every export route answers 503 while `DSH_WORLD_EXPORT` is off |
| `POST` | `/api/v1/servers/{name}/exports` | `{area?: {x1, z1, x2, z2}}` → **202** with the export `queued`; no `area` means 256 × 256 around spawn. Every height. 409 while another export of the server is queued/running or the server is provisioning/deleting/failing; 422 for an area over `DSH_EXPORT_MAX_SIZE` a side; 429 `{retry_after}` at the daily limit |
| `GET` | `/api/v1/servers/{name}/exports/{id}` | one export: `{id, server, status: queued\|running\|ready\|failed, phase, area, custom_area, snapshot: at_rest\|live_stable\|live_changing, created_at, finished_at, expires_at, error, stats: {blocks_scanned, instances, zip_bytes, preview_bytes}}` |
| `GET` | `/api/v1/servers/{name}/exports/{id}/preview/{file}` | a usd-web preview file (`index.json`, or a `.uwt` tile as gzip bytes); 404 until `ready` |
| `POST` | `/api/v1/servers/{name}/exports/{id}/link` | `{url, expires_at}`: a signed link to the zip, valid for 15 minutes; 409 until `ready` |
| `GET` | `/api/v1/export-downloads/{token}` | **no token needed**: the signed link is the credential; the zip as `<server>-world-<id>.zip`. 404 for a bad, expired or deleted link |
| `DELETE` | `/api/v1/servers/{name}/exports/{id}` | 204; 409 while it is queued/running |
| `GET` | `/api/v1/me` | `{username, is_admin}` for the caller; admins are the `DSH_ADMIN_USERS` logins |
| `POST` | `/api/v1/feedback` | `{message (1–4000 chars), page?}` → 201; any signed-in user, at most 10 per user per hour (429) |
| `GET` | `/api/v1/feedback?status=new\|read\|all` | admin only (403 otherwise); newest first, default `new` |
| `PATCH` | `/api/v1/feedback/{id}` | `{status: "read"\|"new"}` → the updated item; admin only; 404 for an unknown id |

`POST /api/v1/servers` validates the request, reserves the name and the
tenant's slot, and answers 202 before any cluster work. The steps then run in
the background: namespace with quota and limit range, the credentials Secret,
`helm upgrade --install` without waiting (the profile installs the wrapper
asleep and the webapp's init container waits for it, so `helm --wait` could
never finish), one wake, then the wrapper, webapp and nginx rollouts (up to
`DSH_ROLLOUT_TIMEOUT` each). Poll `GET /api/v1/servers/{name}` until `state`
leaves `provisioning`; expect a few minutes. The release is installed with
`minecraftWrapper.env.DEFAULT_PLUGINS` set from `DSH_DEFAULT_PLUGINS`, so a new
server starts with Dan's Plugin Manager plus ViaVersion and ViaBackwards (so
clients on other Minecraft versions can join), the same set the operator's
script installs (commas in the list are escaped for `helm --set`, which would
otherwise split them).

### Contract note for the portal

- Create is `202`, not `201`, and the server it returns is `provisioning`.
  The response is otherwise the same shape as before, `admin_username` and
  `admin_password` included — show the password now; it is never repeated.
- Add `provisioning` to the state pill and keep polling the server while it
  is in that state; `wake` and `delete` are not offered for it.
- A `409` whose body carries `server` means the account's earlier create is
  still running — point at that server rather than reporting a limit. The
  cap `403` is unchanged and only ever means the account is full.
- A `failed` server may be a create that failed: it still counts against the
  cap, and `DELETE` (no `force` needed) is how the account gets its slot back.
- `DELETE` is `202`, not `200`, and answers with the server in state
  `deleting` instead of `{name, deleted, backup}`. Add `deleting` to the state
  pill and keep polling the server until it is a 404 (gone); if it comes back
  in its earlier state instead, the delete failed (most often the backup) and
  nothing was removed. The players-online and still-provisioning 409s are
  unchanged and still answered at once.
- `GET /api/v1/default-plugins` is what to show for "what comes installed":
  it is derived from `DSH_DEFAULT_PLUGINS`, so it is always what a new server
  actually gets. `description` is empty and `project_url` is `null` for a
  plugin the API does not know (see `KNOWN_PLUGINS` in `plugins.py`); show
  the name and version and omit the rest.

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
| `DSH_ROLLOUT_TIMEOUT` | `5m` | per-object `kubectl rollout status` timeout after install |
| `DSH_SERVICE_ACCOUNT_NAMESPACE` | `dsh-api` | where the API's ServiceAccount lives; the Deployment sets it from the pod's own namespace |
| `DSH_SERVICE_ACCOUNT_NAME` | `dsh-api` | the ServiceAccount the per-tenant RoleBinding is made out to; the Deployment sets it from `spec.serviceAccountName` |
| `DSH_MAX_SERVERS_PER_TENANT` | `1` | the cap behind the 403 |
| `DSH_ADMIN_USERS` | `dmccoystephenson` | comma-separated JWT `sub`s that may read and triage feedback |
| `DSH_DEFAULT_PLUGINS` | Dan's Plugin Manager `0.7.0-SNAPSHOT-8-8-2026`, ViaVersion `5.12.0`, ViaBackwards `5.12.0` release jars | comma-separated plugin download URLs every new server is installed with; empty for none. Also what `GET /api/v1/default-plugins` describes |
| `DSH_PLUGIN_SELECTION` | `false` | `true` lets tenants choose their plugins (see Plugin selection); only once the cluster's OMCSI includes the plugin reconcile step |
| `DSH_DPC_API` | `https://api.dansplugins.com` | where the plugin catalog is read from |
| `DSH_WORLD_EXPORT` | `false` | `true` lets owners export their world as 3D (see World export); only once the export image is on the node |
| `DSH_EXPORT_IMAGE` | `dsh-world-export:local` | the image built from `export/` |
| `DSH_EXPORT_DIR` | `/exports` | where finished exports are kept (`<server>/<id>/`) |
| `DSH_EXPORT_DEFAULT_SIZE` | `256` | blocks a side of the default box around spawn |
| `DSH_EXPORT_MAX_SIZE` | `512` | the largest box a side |
| `DSH_EXPORT_DAILY_LIMIT` | `5` | exports per account per rolling 24 hours |
| `DSH_EXPORT_RETENTION_HOURS` | `72` | how long a finished export is kept |
| `DSH_EXPORT_TIMEOUT_SECONDS` | `600` | the API stops a run after this; the pod's own deadline is 900 s |
| `DSH_EXPORT_LINK_SECRET` | *(derived from the JWT secret)* | key for signed download links |
| `DSH_PUBLIC_API_URL` | `https://api.<domain>` | where download links point |
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
namespace, ServiceAccount + the two ClusterRoles below, ConfigMap, Secret
stub, three PVCs (state, backups, world exports), Deployment, Service and the `api.<domain>`
Ingress with the cert-manager annotation. Before applying: build and push the
image somewhere the cluster can pull from and set it in `deployment.yaml`,
put the real node address in `config.yaml`, and create the `dsh-api` Secret
out of band rather than from the committed stub.

### Permission model

The API never holds a kubeconfig; it acts as the `dsh-api` ServiceAccount
with the token Kubernetes mounts into the pod. Tenant namespaces are created
at request time, so a namespaced Role cannot be bound to them in advance —
but that is no reason to hold the namespaced verbs everywhere. The grant is
split in two (`deploy/rbac.yaml`):

| ClusterRole | Bound | Holds |
|---|---|---|
| `dsh-api-cluster` | cluster-wide, by a ClusterRoleBinding | only what has to work before a tenant namespace has a binding: `namespaces` (get/list/create/patch/delete); `resourcequotas` and `limitranges` (create/get/patch/delete — namespaced, but applied in the same step as the namespace); `rolebindings` (create/get/patch); and the `bind` verb on **one** ClusterRole, `dsh-api-tenant`, by `resourceNames` |
| `dsh-api-tenant` | per tenant, by a RoleBinding the API creates in `t-<name>` | the namespaced rules helm and the backend actually use: secrets, configmaps, services, serviceaccounts, persistentvolumeclaims, pods, `pods/exec`, `pods/log`, events, deployments, statefulsets, `statefulsets/scale`, horizontalpodautoscalers, ingresses, networkpolicies, roles, rolebindings (the chart's dashboard Role) |

Creating a server therefore goes: namespace + quota + limit range (cluster
role), then the RoleBinding `dsh-api` in the new namespace binding
`dsh-api-tenant` to the API's ServiceAccount, and only then the Secret, helm,
the scale and the rollout waits — every one of which is allowed by that
binding alone. Deleting the namespace deletes the binding with it. The
result is that a compromise of the API pod reaches the tenant namespaces it
has bound and nothing else: not `dsh-api`'s own Secret, not `kube-system`,
not another platform namespace. Creating a binding to a ClusterRole one does
not already hold every permission of needs `bind` on that role, which is why
`dsh-api-cluster` carries it, restricted to `dsh-api-tenant`; `escalate` is
not granted, so the API cannot write Roles or ClusterRoles at all.

The ServiceAccount the binding is made out to comes from
`DSH_SERVICE_ACCOUNT_NAMESPACE` / `DSH_SERVICE_ACCOUNT_NAME`, which the
Deployment fills from the pod's own namespace and `serviceAccountName`, so
renaming either in the manifests cannot leave the binding pointing at the
wrong subject.

### Plugin selection (RFC 0020)

Off until `DSH_PLUGIN_SELECTION=true`: it needs every server on an OMCSI image
whose boot script reconciles the `<name>-plugins` ConfigMap (OMCSI #308), so
it is switched on only after the cluster's OMCSI pin includes it. While it is
on:

- A new server's defaults are written to its ConfigMap as its first selection,
  and the release gets **no** `DEFAULT_PLUGINS`: that path only ever adds, so a
  default the tenant removed would come back at every boot.
- A server created before has no ConfigMap. Its selection starts as the
  default plugins (what is on its disk) the first time it is read, and nothing
  is written to the cluster until the tenant changes it. Its release still
  carries `DEFAULT_PLUGINS`, so a default plugin removed from it returns at the
  next boot until the release is upgraded without it.
- Changing a selection rewrites the ConfigMap and nothing else. It takes
  effect at the next pod start: the next wake of an asleep server, or `apply`.
- Each plugin is pinned to the release that was current when it was added;
  the boot script downloads through DPC's counting URL.

## World export (RFC 0021)

Off until `DSH_WORLD_EXPORT=true`; every route answers 503 until then, which
the portal takes as "hide the section". While it is on:

- `POST .../exports` checks ownership, the server's state, one export per
  server and the daily limit, records the export `queued` and answers. Exports
  run **one at a time on the whole node** on their own one-worker executor.
- A run applies `NetworkPolicy/dsh-export-isolate` (deny all, export pods
  only) in `t-<name>`, then creates `pod/world-export-<id>`: the export image,
  the world PVC mounted **read-only**, no service-account token, read-only
  root, uid 1000, `activeDeadlineSeconds: 900`, 100m/256Mi requests and
  500m/1Gi limits (room beside an awake release under the hosted-free quota).
  It execs `dsh-world-export` in it and streams the tar back, exactly as a
  backup streams `tar`; the pod is deleted whatever happens. No permission
  beyond `dsh-api-tenant` is needed.
- No command is ever sent to the game. An asleep server gives an `at_rest`
  snapshot; an awake one `live_stable`, or `live_changing` if region files
  kept changing while they were copied.
- The tar is unpacked accepting only `summary.json`, `world.zip` and
  `preview/<usd-web file>`, then moved to `DSH_EXPORT_DIR/<server>/<id>/`.
- Finished and failed exports are removed after `DSH_EXPORT_RETENTION_HOURS`,
  and with their server; a restart marks running ones failed and deletes their
  pods.

## World export image

`export/` builds the image the world-export pod runs: Minecraft-USD-Exporter
and usd-web, each pinned to a commit (`EXPORTER_PIN`, `USD_WEB_PIN`), plus
`export/dsh_world_export.py` installed as `dsh-world-export`. Given the
server directory mounted read-only at `/mcserver` and a scratch `emptyDir` at
`/work`, it copies `level.dat` and only the overworld region files the box
needs (never `playerdata/`, `stats/`, `advancements/`, `entities/` or
`poi/`), checks the copy did not race a write, runs `mc-usd-export` on the
copy and `usd-web tiles` on its terrain, and writes a gzipped tar of
`world.zip` (the two USD layers and a README), `summary.json` (a fixed set of
fields, no paths) and `preview/` (usd-web `index.json` + `.uwt` tiles) to
stdout, with `phase=<name>` progress lines on stderr. The default box is
256 × 256 blocks around spawn at every height. It sends nothing to the game
and has no network.

```sh
docker build -t dsh-world-export:local export/
export/smoke.sh dsh-world-export:local   # the committed fixture, as the pod runs it
```

## Layout

```
src/dsh_api/
  main.py      FastAPI app factory and routes
  auth.py      JWT validation (real + fake)
  cluster.py   ClusterBackend: kubectl/helm backend + in-memory fake
  service.py   create / list / get / wake / delete
  plugin_service.py  a server's plugin selection: add / remove / apply
  export_service.py  world export (RFC 0021): start, run, links, expiry
  export_pod.py      the export pod and policy manifests, the exec stream, unpacking
  catalog.py   the installable plugins: dansplugins.com, cached (real + fake)
  selection.py the selection.json the wrapper reconciles, and its hash
  feedback.py  submit / list / triage user feedback
  db.py        SQLite schema and queries
  mojang.py    operator UUID lookup (real + fake)
  plugins.py   default-plugin URLs described for GET /api/v1/default-plugins
  config.py    settings and the limits profile
tests/         pytest, no cluster required
export/        the world-export image (RFC 0021) and its smoke test
deploy/        Kubernetes manifests
```
