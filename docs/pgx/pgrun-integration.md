# PGRun integration specification (PGX v0.1)

What PGRun needs from a PGX host, and in what form. The engine-level
primitives are in `pgrun-interface.md`. This page defines the operations
PGRun calls, the JSON each one returns, the host agent that serves them,
and the end-to-end benchmark that becomes the next engineering target.

Principles:

- **Structured interfaces only.** PGRun calls a host agent over a
  persistent RPC channel. The agent talks to PGX over SQL on a Unix socket
  and returns JSON. Nothing parses shell or log output.
- **Field names are stable.** Fields may be added, never renamed or
  removed, as with `pgrust_runtime_status()`.
- **Mutating operations are idempotent.** Each takes a client-supplied
  `request_id`, and a retried request returns the original result.
- **Two engines, one contract.** The same operations serve
  `engine = postgres` hosts (the existing branch path), so placement can
  fall back without a second API (`compatibility-gate.md`).

## Architecture

```
PGRun control plane
        │  persistent RPC (gRPC over HTTP/2 or WebSocket), mTLS, one per host
        ▼
PGRun host agent (pgx-agent), one per host
        │  pooled superuser connections over the runtime's Unix socket
        ▼
PGX runtime(s), each in its own cgroup
        │
        ▼
branch databases (ZFS clones of sealed golden templates)
```

The agent replaces one SSH session per request. The earlier PGRun branch
path measured ~0.8 s per SSH handshake and ~3.9 s of dispatch per branch,
against 6–100 ms for the PGX mint itself.

The agent owns:

| Responsibility | Mechanism |
|---|---|
| Runtime lifecycle | start each runtime under `systemd-run --scope` with `MemoryMax`, `MemorySwapMax=0`, and the PGX profile; stop and restart |
| Health | `SELECT 1` and `pgrust_runtime_status()` every few seconds; pushed to PGRun, not polled |
| Mint and delete | SQL over pooled connections (below) |
| Credentials | per-branch roles, grants, revocation |
| Limits | connection limit per branch database; session, database and runtime memory limits |
| Warm pool | pool size from PGRun's demand forecast (one pooled template per runtime in v0.1); watch `pool_handouts` / `cold_mints` |
| Templates | receive golden snapshots from object storage, restore, `pgrust_seal_template()` |
| Capacity reporting | runtime status plus `zpool` free space, host CPU and memory |
| Recycling and draining | policy in `pgrun-interface.md`, "Runtime recycling" |

## Operations

Every response carries `host_id`, `runtime_id`, `request_id`, and either
`ok: true` with a result or `ok: false` with
`error: {code, sqlstate?, message}`.

### Read-only

| Operation | PGX primitive | Result |
|---|---|---|
| `RuntimeHealth()` | `SELECT 1`; `pgrust_runtime_status()` | `{"healthy": true, "latency_ms": 0.4, "uptime_s": 86400}` |
| `RuntimeStatus()` | `pgrust_runtime_status()` | the status JSON below, unchanged |
| `RuntimeCapacity()` | runtime status + `zpool list -Hp` + `/proc` | `{"databases": {...}, "memory": {"pss_bytes", "runtime_limit_mb", "cgroup_max_bytes"}, "disk": {"pool_free_bytes", "pool_size_bytes"}, "cpu": {"load1", "cores"}, "accepting": true}` |
| `ListDatabases(prefix?)` | `SELECT datname, oid, datistemplate, datallowconn FROM pg_database WHERE datname LIKE $1` + session counts from `pg_stat_activity` | `[{"name", "oid", "kind": "branch\|template\|spare", "sessions", "pinned"}]` |
| `DatabaseStatus(name)` | the same, for one name | `{"name", "exists", "kind", "sessions", "pinned", "connection_limit", "size_bytes"}` (`pg_database_size`) |
| `WarmPoolStatus()` | `janitor` block of runtime status | `{"pool_size", "spares", "pool_handouts", "cold_mints", "spares_minted", "mint_failures", "cold_mint_ms_mean", "cold_mint_ms_max"}` |
| `ListTemplates()` | `pg_database WHERE datistemplate` | `[{"name", "sealed": true, "version"}]` (version from the agent's template registry) |

`pgrust_runtime_status()` as of `727f31e49f`:

```json
{"pid": 12345,
 "memory": {"rss_bytes": 0, "pss_bytes": 0, "context_bytes": 0,
            "session_limit_mb": 256, "database_limit_mb": 512, "runtime_limit_mb": 3072,
            "retired_session_roots_pending": 0, "retired_session_roots_reclaimed": 0},
 "threads": 9, "connections": 42,
 "databases": {"total": 1003, "ephemeral": 998, "spares": 8, "pinned": 2},
 "janitor": {"prefix": "tdb_", "pool_size": 8, "pending_mints": 0,
             "connection_limit": 5, "pool_handouts": 0, "cold_mints": 0, "spares_minted": 0,
             "mint_failures": 0, "cold_mint_ms_mean": 0, "cold_mint_ms_max": 0}}
```

### Mutating

| Operation | PGX primitive | Result |
|---|---|---|
| `CreateDatabase(template, branch_id, request_id)` | the agent connects to `<prefix><template>__<branch_id>` (mint-on-connect: warm spare, else cold clone), so the database name carries the branch id; or `CREATE DATABASE <name> TEMPLATE <template>` with `file_copy_method = clone` | `{"database": "b_…", "source": "warm\|cold", "mint_ms": 6.2}` |
| `CreateCredentials(database, request_id)` | `CREATE ROLE <r> LOGIN PASSWORD <generated>`; `REVOKE CONNECT … FROM PUBLIC`; `GRANT CONNECT …`; `ALTER DATABASE … OWNER TO <r>` | `{"role", "password", "database_url"}`; the password is returned once and never logged |
| `RevokeCredentials(role, request_id)` | `ALTER ROLE <r> NOLOGIN`; terminate its sessions; later `DROP ROLE` after `REASSIGN OWNED` | `{"revoked": true, "sessions_terminated": 1}` |
| `SetConnectionLimit(database, n)` | `ALTER DATABASE … CONNECTION LIMIT n` (minted databases get `pgrust.ephemeral_db_connection_limit` by default) | `{"connection_limit": n}` |
| `SetMemoryLimits(session_mb?, database_mb?, runtime_mb?)` | `ALTER SYSTEM SET pgrust.*_memory_limit`; `pg_reload_conf()` | the effective limits |
| `PinDatabase(name)` / `UnpinDatabase(name)` | `pgrust_pin_database()` / `pgrust_unpin_database()` | `{"pinned": true}`; pins live in memory, so the agent re-applies them after a runtime restart |
| `DropDatabase(name, request_id)` | `DROP DATABASE … WITH (FORCE)`; drop its role | `{"dropped": true}` (dropping a missing database also returns success) |
| `EnsureTemplate(golden_id, version)` | restore from object storage onto the host's ZFS cache; `pgrust_seal_template()` | `{"template", "version", "restored": true, "ms"}` |
| `SetWarmPool(size)` | `pgrust.ephemeral_db_pool_size` (spares of `pgrust.ephemeral_db_default_template`); `pg_reload_conf()` | `{"pool_size": n}` |
| `DrainRuntime()` / `RecycleRuntime()` | stop placements; start a replacement; stop the old runtime once nothing is connected or pinned | progress events on the channel |

Error codes map PGX SQLSTATEs onto stable agent codes:

| Agent code | SQLSTATE | Meaning |
|---|---|---|
| `mint_unavailable` | 57P03 | janitor unavailable or mint wait timed out |
| `mint_capacity` | 53400 | per-role mint cap reached |
| `memory_limit` | 53200 | a memory limit refused the work |
| `connection_limit` | 53300 | too many connections to the database |
| `not_found` | 3D000 | no such database |
| `template_missing` | — | the template is not cached on this host |

### Engine gaps PGRun will want later

None of these block integration: the agent composes each from SQL today.

- One per-database status function, to save a `pg_stat_activity` scan per
  call.
- Pins that survive a runtime restart.
- Per-database memory in use. The runtime status reports the limit, not
  per-database usage.
- The warm-pool refill rate as a setting. It is a constant today: 8 per
  500 ms.

## Routing

The gateway already routes by TLS SNI to a branch host (port 5432). With
PGX, a branch is a database inside a shared runtime, so the route is
`(host, runtime socket or port, database name)`. The branch's
`DATABASE_URL` names that database, and pg_hba on the runtime allows each
branch role only into its own database. PGRun stores, per branch:
`engine`, `host_id`, `runtime_id`, `database`, `role`.

## Next benchmark: PGRun end-to-end branch creation

This replaces engine-level mint numbers as the headline.

```
PGRun API request
 → host selected (from pushed capacity, template cached locally)
 → PGX database allocated (warm spare or cold clone)
 → credentials created
 → gateway route ready
 → DATABASE_URL returned
 → client connects through the gateway (TLS) and SELECT 1 succeeds
```

- **Measure** from the API request's arrival to the client's first
  `SELECT 1` result. Report p50, p95 and p99 separately for warm-pool and
  cold paths. Also report a per-stage breakdown: placement, agent RPC,
  mint, credentials, route, connect.
- **Load:** 1, 10 and 50 concurrent requests; at least 200 requests per
  level; client in the same region as the control plane.
- **Targets:** p50 < 500 ms; stretch p50 < 200 ms. The budget in
  `pgrun-interface.md` puts a persistent-agent, warm-pool path at roughly
  30–80 ms of engine and network time.
- **Record** with every result: host, PGX commit, profile, template size,
  pool size, and the gateway and agent versions.
