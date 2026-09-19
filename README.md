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
  its create is still in flight), `events` and `feedback` (what signed-in
  people said about the portal, for the admins). Once a server is created the
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

`provisioning` and a failed create are read from the row, not the cluster:
during the create the release may not exist yet, and after a failure the
namespace and release are left as they are for inspection (the error is in
the `events` table as `create.failed`). If the API restarts mid-create the
row is marked failed on startup (`create.interrupted`); nothing is retried on
its own.

The pod's readiness probe is the wrapper's Spring health, not the game's, so
readiness alone cannot tell `awake` from `stopped`. When the wrapper cannot be
asked (connection refused, timeout, non-200) the replica-based reading stands:
Ready is `awake`, not Ready is `waking`. A slow or missing wrapper answer
never fails a list or get.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/healthz` | liveness |
| `GET` | `/api/v1/limits` | free-tier profile as numbers; no token needed |
| `GET` | `/api/v1/servers` | the caller's servers |
| `POST` | `/api/v1/servers` | `{name, motd?, operator_username?}` → **202** with the server in state `provisioning`; the admin password is in this response **only**. 409 `{"detail": "a server is already being created for this account", "server": "<name>"}` while the caller's earlier create is still running; 409 when the name is taken; 403 at the tenant cap |
| `GET` | `/api/v1/servers/{name}` | one server, with `players_online` when `awake` (`null` otherwise) |
| `POST` | `/api/v1/servers/{name}/wake` | 202 with the resulting server: `asleep` → scales the wrapper to 1; `stopped` → `POST /api/server/start` on the wrapper; `waking`/`awake`/`failed` → no-op (a failed server, including one whose StatefulSet is gone, is reported as such rather than scaled) |
| `DELETE` | `/api/v1/servers/{name}` | backup, `helm uninstall`, namespace delete; 409 while players are online unless `?force=true`; 409 while the server is still `provisioning`; a `failed` create is removed even when there is nothing to back up |
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
server starts with Dan's Plugin Manager the way the operator's script installs
it (commas in the list are escaped for `helm --set`, which would otherwise
split them).

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
| `DSH_DEFAULT_PLUGINS` | Dan's Plugin Manager `0.7.0-SNAPSHOT-8-8-2026` release jar | comma-separated plugin download URLs every new server is installed with; empty for none |
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
stub, two PVCs (state, backups), Deployment, Service and the `api.<domain>`
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
| `dsh-api-tenant` | per tenant, by a RoleBinding the API creates in `t-<name>` | the namespaced rules helm and the backend actually use: secrets, configmaps, services, serviceaccounts, persistentvolumeclaims, pods, `pods/exec`, `pods/attach`, `pods/log`, events, deployments, statefulsets, `statefulsets/scale`, horizontalpodautoscalers, ingresses, networkpolicies |

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

## Layout

```
src/dsh_api/
  main.py      FastAPI app factory and routes
  auth.py      JWT validation (real + fake)
  cluster.py   ClusterBackend: kubectl/helm backend + in-memory fake
  service.py   create / list / get / wake / delete
  feedback.py  submit / list / triage user feedback
  db.py        SQLite schema and queries
  mojang.py    operator UUID lookup (real + fake)
  config.py    settings and the limits profile
tests/         pytest, no cluster required
deploy/        Kubernetes manifests
```
