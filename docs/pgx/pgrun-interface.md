# PGX ↔ PGRun interface and the host daemon

PGX is the database runtime; PGRun is the control plane. This page fixes
what PGX exposes, and recommends how PGRun should reach it.

## What PGX exposes today

Everything below is plain PostgreSQL protocol against the runtime's own
socket. No shell, no log parsing.

| Need | Primitive | Notes |
|---|---|---|
| Create a database from a template | connect to `<prefix><template>__<token>` | Mint-on-connect: the connection that names it creates it (warm spare or cold clone) and is then connected to it. Roles allowed by `pgrust.ephemeral_db_mint_roles`. Errors: 57P03 when the janitor is unavailable or the wait times out, 53400 at the per-role cap. |
| Create explicitly | `CREATE DATABASE d TEMPLATE t` | Ordinary SQL; `file_copy_method = clone` gives ZFS block cloning. |
| Delete | `DROP DATABASE d` (`WITH (FORCE)` to end sessions) | Or let the janitor reap it after `pgrust.ephemeral_db_grace` seconds with no sessions. |
| Keep a database past the grace period | `SELECT pgrust_pin_database('d')` / `pgrust_unpin_database('d')` | Pins are in memory: they do not survive a runtime restart. |
| Make a template | `SELECT pgrust_seal_template('t')` | Freeze-vacuums and marks it a template; minting requires a sealed template. |
| Exists / status of one database | `SELECT oid, datistemplate FROM pg_database WHERE datname = $1` and `SELECT count(*) FROM pg_stat_activity WHERE datname = $1` | |
| List databases | `SELECT datname FROM pg_database WHERE datname LIKE '<prefix>%'` | |
| Branch credentials | `CREATE ROLE b_x LOGIN PASSWORD '…'`; `GRANT CONNECT ON DATABASE d TO b_x`; `ALTER DATABASE d OWNER TO b_x` | Ordinary roles are cluster-wide in PostgreSQL. Revoke = `DROP ROLE` after `REASSIGN OWNED`/`DROP DATABASE`. pg_hba decides who can connect to which database. |
| Runtime health | `SELECT 1`; `SELECT pgrust_runtime_status()` | |
| Runtime capacity and memory | `SELECT pgrust_runtime_status()` | JSON, below. |
| Active queries | `SELECT datname, pid, state, query_start, wait_event FROM pg_stat_activity` | Standard view. |
| Template status | `SELECT datistemplate, datallowconn FROM pg_database WHERE datname = $1` | Sealed = `datistemplate AND NOT datallowconn`. |
| Per-database memory cap | `ALTER SYSTEM SET pgrust.database_memory_limit = …; SELECT pg_reload_conf()` | Runtime-wide setting applied per database; see `memory-limits.md`. |

### `pgrust_runtime_status()`

Superuser only. One catalog scan, one procarray pass, two `/proc` reads. Example output (illustrative values):
Field names are a stable interface: fields may be added, never renamed.

```json
{"pid":12345,
 "memory":{"rss_bytes":702545920,"pss_bytes":698351616,"context_bytes":41943040,
           "session_limit_mb":256,"database_limit_mb":512,"runtime_limit_mb":3072,
           "retired_session_roots_pending":3,"retired_session_roots_reclaimed":24151},
 "threads":9,"connections":42,
 "databases":{"total":1003,"ephemeral":998,"spares":8,"pinned":2},
 "janitor":{"prefix":"tdb_","pool_size":8,"pending_mints":0}}
```

- `rss_bytes`, `pss_bytes`: the runtime process. PSS is the number to sum
  across runtimes on one host.
- `context_bytes`: memory held in memory-context blocks (query and cache
  memory), the part the memory limits govern.
- `retired_session_roots_*`: per-thread context shells waiting for their
  thread's join / freed so far (`lifecycle-memory.md`). `pending` should
  stay near the number of live sessions.
- `databases.ephemeral` excludes warm-pool spares and templates.

## Recommended host architecture

```
PGRun API ──(persistent gRPC/HTTP2 or WebSocket, mTLS)──> pgx-agent (one per host)
                                                           │  Unix socket, pooled
                                                           ▼  superuser connections
                                                        PGX runtime(s)
                                                           │
                                                        ZFS pool (golden cache + branches)
```

**pgx-agent** is a small long-running daemon on each PGX host. It holds a
few superuser connections per runtime and a persistent, authenticated
channel to the control plane. A branch request becomes one RPC and a few SQL
statements over already-open connections:

1. `CreateBranch(template, branch_id)` → connect (or reuse an admin
   connection and run `CREATE DATABASE … TEMPLATE …`), create the branch
   role, grant, return `DATABASE_URL` (host gateway address + database +
   role).
2. `DeleteBranch(branch_id)` → `DROP DATABASE … WITH (FORCE)`, `DROP ROLE`.
3. `Status()` → `pgrust_runtime_status()` for every runtime plus `zpool`
   and host figures, pushed every few seconds so placement decisions never
   wait on a round trip.
4. `EnsureTemplate(golden_id, version)` → restore/receive the golden onto
   the host's ZFS cache if missing, `pgrust_seal_template`.

Why not SSH per branch: the earlier PgRust benchmark measured ~0.8 s for a
fresh SSH handshake and ~3.9 s of dispatch per branch (four executor calls),
against 7–113 ms for the mint itself here. A persistent agent removes the
handshake, the process spawn and the per-call sudo entirely.

Placement: PGRun keeps, per host, the last `Status()` and picks the host
with the template cached locally and the lowest `pss_bytes` /
`connections`, below the host's configured ceilings.

### Product latency budget (target `API request → DATABASE_URL → SELECT 1`)

| Step | Budget |
|---|---|
| PGRun API → agent (persistent channel, same region) | 1 RTT, 1–20 ms |
| Mint: warm spare / cold clone | 7 ms / 70–115 ms |
| Role + grant | 2–5 ms |
| Client connect through the gateway (TLS) | 1 RTT + TLS, 10–40 ms |
| First `SELECT 1` | < 1 ms |

That is roughly 30–80 ms with a warm pool and 100–180 ms cold, inside the
200 ms stretch target, provided the agent channel is persistent and the
template is cached on the chosen host. These are budgets, not measurements:
the PGX part is measured (`linux-results.md`), the network parts are not.

## Runtime recycling

Until lifecycle retention is zero (`lifecycle-memory.md`), runtimes should
be recycled by policy rather than by surprise:

- Triggers: `pss_bytes` above a share of the runtime's cgroup limit, a
  count of database lifecycles since start, or age; checked by the agent
  from `pgrust_runtime_status()`.
- Procedure: mark the runtime draining in PGRun (no new placements); start
  a replacement runtime; new branches go to the replacement; existing
  branches finish on the old runtime (or, by policy, are re-created from
  their template on the new one); when the old runtime has no pinned or
  connected databases, stop it. No live database is dropped by recycling.
- A restart costs ~80 ms of runtime startup plus the template being sealed
  on the new runtime's data directory (or shared via the same ZFS dataset).
