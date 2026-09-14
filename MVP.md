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
| `POST` | `/api/v1/servers` | `{name, motd?, operator_username?}` → provisions (namespace + quota + OMCSI release from the co-located profile). 201 with the server. 409 if the name is taken, 403 if the tenant is at cap. |
| `GET` | `/api/v1/servers` | the caller's servers with `state: asleep|waking|awake|failed`, hostname, dashboard URL |
| `GET` | `/api/v1/servers/{name}` | one server, plus last-woken and player count when awake |
| `POST` | `/api/v1/servers/{name}/wake` | scales the wrapper to 1 (the panel's Start button) |
| `DELETE` | `/api/v1/servers/{name}` | backs up the world, uninstalls, removes the namespace. 409 while players are online unless `?force=true` |
| `GET` | `/api/v1/limits` | the free-tier profile as numbers, for the portal to display |
| `GET` | `/healthz` | liveness |

## Done when

1. With a valid UserAuth token, `POST /api/v1/servers` results in a server a player can join by hostname, and `GET` reports it `asleep` → `awake` as that happens.
2. A second tenant cannot see or delete the first tenant's server.
3. `DELETE` produces a backup file before the namespace goes.
4. The API is reachable at `api.<domain>` through Traefik with a real certificate once the domain exists (self-signed before).
5. Unit tests run without a cluster (the k8s/helm calls are behind one interface with a fake).

## Not in the MVP

- Quotas beyond "N servers per tenant"; billing; anything paid.
- Plugin or world upload — the OMCSI dashboard already does both.
- An admin API (the operator uses the cluster scripts).
- Replacing its internals with the fleet control plane's API — that is the intended second step and the interface above is shaped so it can be.

## Depends on

- A running cluster with mc-router and the co-located OMCSI profile (private cluster repo).
- [UserAuth](https://github.com/Preponderous-Software/UserAuth) reachable from the cluster.
