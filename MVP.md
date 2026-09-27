# dsh-api — MVP

**A signed-in person can create a server, see whether it is awake, and delete it — without an operator in the loop.**

## Shape

- Python 3.11, FastAPI, stdlib-heavy. Chosen because everything it drives today is `kubectl`/`helm`/bash and the fleet control plane it will defer to later is Python; a JVM service would wrap shell commands from the wrong side of the fence.
- Runs **in the cluster**, in its own namespace, with a ServiceAccount whose Role can create namespaces, ResourceQuotas and Helm releases — and nothing else. It never holds a kubeconfig file.
- Authenticates callers with UserAuth JWTs (`Authorization: Bearer`), validated with the shared HS256 secret (or `GET /session/validate` against the UserAuth service). The JWT `sub` is the tenant id. No local user table beyond a `tenants` row created on first call.
- State: one SQLite file on a small PVC (tenants, servers, events). The cluster is the source of truth for whether a server exists; the database records who owns what and what was asked for.

## Endpoints (v1)

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/v1/servers` | `{name, motd?, operator_username?}` → provisions (namespace + quota + OMCSI release from the co-located profile). 202 with the server in state `provisioning`; the steps run in the background. 409 if the name is taken or the tenant's earlier create is still running, 403 if the tenant is at cap. |
| `GET` | `/api/v1/servers` | the caller's servers with `state: provisioning|asleep|waking|awake|stopped|failed`, hostname, dashboard URL |
| `GET` | `/api/v1/servers/{name}` | one server, plus last-woken and player count when awake |
| `POST` | `/api/v1/servers/{name}/wake` | scales the wrapper to 1, or starts the game in a pod that is up with the game stopped (the panel's Start button); 202 |
| `DELETE` | `/api/v1/servers/{name}` | backs up the world, uninstalls, removes the namespace. 409 while players are online unless `?force=true` |
| `GET` | `/api/v1/limits` | the free-tier profile as numbers, for the portal to display |
| `GET` | `/healthz` | liveness |

## Done when

1. [x] With a valid UserAuth token, `POST /api/v1/servers` results in a server a player can join by hostname, and `GET` reports it `asleep` → `awake` as that happens. *Confirmed on the real cluster on 2026-09-14: a create driven through the public API with a UserAuth-issued token reached `awake` in about 95 s.*
2. [x] A second tenant cannot see or delete the first tenant's server.
3. [x] `DELETE` produces a backup file before the namespace goes. *Ordering verified against the fake; the `kubectl exec` / PVC-reader command lines are asserted, and the real transfer was confirmed on 2026-09-14 (delete in about 16 s with the world backed up first).*
4. [x] The API is reachable at `api.<domain>` through Traefik with a real certificate once the domain exists (self-signed before). *Applied on 2026-09-14; `https://api.<domain>/healthz` answers with a Let's Encrypt certificate.*
5. [x] Unit tests run without a cluster (the k8s/helm calls are behind one interface with a fake).

*Since the MVP:* `state` is no longer read from the StatefulSet alone. The
wrapper pod's readiness probe is the wrapper's own health, so a server whose
owner pressed Stop in the dashboard stayed Ready and was reported `awake` for
hours while nobody could join, and `wake` (replicas already 1) did nothing.
The API now also asks the wrapper's `/api/server/status`; a Ready pod whose
game is not running is `stopped`, and `wake` starts the game in place. See the
state table in the README.

*Also since the MVP:* create is asynchronous. The MVP's `POST` held the
request open for the whole install (about 90 s on the node), and a second
`POST` from the same account in that window was answered with the cap 403 —
misleading, since the first was succeeding. `POST` now reserves the row as
`provisioning` and answers 202; the cluster steps run on a thread pool in the
API; `GET` reports `provisioning` until they finish, then the cluster's
reading, or `failed` (with the error as a `create.failed` event). A second
create while one is running is a 409 naming the pending server. The
namespace and release of a failed create are left for inspection; the slot
is released by `DELETE`, which tolerates a missing pod or volume.

## Not in the MVP

- Quotas beyond "N servers per tenant"; billing; anything paid.
- Plugin or world upload — the OMCSI dashboard already does both.
- An admin API (the operator uses the cluster scripts). *Since the MVP: user
  feedback (`POST /api/v1/feedback`, listed and triaged by the `DSH_ADMIN_USERS`
  logins) and `GET /api/v1/me` were added for the portal; server administration
  is still done with the scripts.*
- Replacing its internals with the fleet control plane's API — that is the intended second step and the interface above is shaped so it can be.

## Depends on

- A running cluster with mc-router and the co-located OMCSI profile (private cluster repo).
- [UserAuth](https://github.com/Preponderous-Software/UserAuth) reachable from the cluster.
