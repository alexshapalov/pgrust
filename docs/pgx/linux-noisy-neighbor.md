# Noisy neighbours in one PGX runtime (Linux)

How much a quiet database suffers when another database in the same
runtime misbehaves. Host: `linux-host.md` (4 vCPU). Memory limits and the
cgroup backstop are covered in `linux-limits.md`.

## 1. Failure containment: one database misbehaves

`benchmarks/pgx/noisy-neighbor.py`, pass 2 (build `9727706fe7`), PGX
profile. Database B runs `SELECT 1` plus a small indexed query in a loop
and opens new connections, while database A does one of the scenarios
below.

| A does | B query p50 / p99 / max (ms) | B longest gap | B errors | New connections to B | Runtime peak | What happened to A |
|---|---|---|---|---|---|---|
| nothing (baseline) | 0.31 / 0.52 / 4.7 | 4.7 ms | 0 | ok, 3.7 ms | 136 MB | — |
| large sort / aggregate | 0.32 / 0.65 / 20.8 | 21 ms | 0 | ok | 159 MB | completed |
| long open transaction with a heavy write | 0.33 / 0.61 / 9.4 | 15 ms | 0 | ok | 367 MB | completed |
| `array_agg` with `work_mem = 8GB`, no limit | 0.32 / 0.54 / 3.3 | 3.3 ms | 0 | ok | 1,457 MB | completed |
| sort spilling to temp files | 0.32 / 0.93 / 24.9 | 27 ms | 0 | ok | 184 MB | completed |
| huge cross join | 0.31 / 0.55 / 3.1 | 11 ms | 0 | ok | 202 MB | stopped by `statement_timeout` (57014) |
| failing statements in a loop | 0.33 / 1.28 / 2.4 | 4.8 ms | 0 | ok | 188 MB | 23505 each time |
| rolled-back transactions in a loop | 0.32 / 0.67 / 3.6 | 3.6 ms | 0 | ok | 196 MB | ok |
| long query cancelled (`pg_cancel_backend`) | 0.31 / 0.56 / 24.1 | 24 ms | 0 | ok | 207 MB | 57014 |
| session terminated (`pg_terminate_backend`) | 0.32 / 0.73 / 3.4 | 12 ms | 0 | ok | 206 MB | 57P01, session closed |

With `pgrust.session_memory_limit` set, the same memory scenario was
refused with 53200, the runtime peaked at 400 MB instead of 1,457 MB, and
B's maximum latency was 0.9 ms.

- B saw 0 errors in every scenario. Its median never moved by more than
  0.02 ms, and new connections to B always succeeded.
- The worst single stall for B was 20–27 ms, during a large sort, a spill
  or a cancellation in A.
- The runtime stayed up in every scenario. A cancelled or terminated
  session in A affects only that session.

## 2. CPU saturation: N databases busy

`benchmarks/pgx/linux/cpu-noisy.py`, pass 2. B runs a small indexed query
in a loop. N other databases each run a CPU-bound query
(`sum(...) over generate_series(1, 20M)`) back to back.

| Busy databases | Host CPU | B p50 | B p99 | B max | New connection to B, p50 / p99 | B errors |
|---|---|---|---|---|---|---|
| 0 | 4 % | 0.58 ms | 1.14 ms | 14 ms | 3.8 / 5.8 ms | 0 |
| 1 | 30 % | 0.56 ms | 2.06 ms | 81 ms | 3.8 / 8.9 ms | 0 |
| 2 | 56 % | 0.53 ms | 2.95 ms | 68 ms | 3.9 / 17.8 ms | 0 |
| 4 | 99 % | 0.59 ms | 6.22 ms | 78 ms | 7.7 / 21.1 ms | 0 |
| 6 | 100 % | 0.90 ms | 8.33 ms | 36 ms | 8.1 / 26.8 ms | 0 |

- Once the 4 cores are saturated, B's p99 goes from 1.1 to 6–8 ms. Its
  median barely moves until the host is oversubscribed (6 busy on 4 vCPU).
- There is no per-database CPU scheduling in the runtime. Every session is
  a thread, and the kernel shares the cores among them. B is never
  starved, but it queues.

## 3. One agent, many connections

`cpu-noisy.py --same-db --workload agent`, pass 5 (build `dedfdf9ca5`).
One agent database, owned by an ordinary role (`agent`, so connection
limits apply), runs N connections. Each connection cycles through an
agent test/migration mix against its own scratch table: CREATE TABLE, a
20k-row INSERT…SELECT with md5 and jsonb, CREATE INDEX, a self-join, a
jsonb GROUP BY, ALTER TABLE ADD COLUMN, UPDATE, DROP TABLE.

| Agent connections | B p50 | B p99 | B max | Host CPU | Agent statements done (30 s) |
|---|---|---|---|---|---|
| 0 | 0.59 ms | 1.03 ms | 10 ms | 4 % | — |
| 1 | 0.57 ms | 1.88 ms | 17 ms | 31 % | 594 |
| 4 | 0.79 ms | 3.68 ms | 18 ms | 85 % | 1,583 |
| 8 | 0.75 ms | 4.12 ms | 17 ms | 55 % | 890 |
| 16 | 0.92 ms | 5.36 ms | 28 ms | 62 % | 807 |

Above 4 connections the agent's own throughput falls and host CPU drops.
Its sessions contend with each other inside the one database, probably on
catalog locks from concurrent DDL (not profiled). B is affected less than
by pure CPU load.

### The per-database connection limit

The same agent opens 50 connections, under `ALTER DATABASE … CONNECTION
LIMIT n` (in a PGX runtime, `pgrust.ephemeral_db_connection_limit`, added
in `c9a2a024cc`, sets this for every minted database):

| Limit | Agent connections refused | B p50 | B p99 | B max | Host CPU | Agent statements done (20 s) |
|---|---|---|---|---|---|---|
| 1 | 49 | 0.50 ms | 1.23 ms | 27 ms | 30 % | 434 |
| 2 | 48 | 0.54 ms | 2.09 ms | 47 ms | 53 % | 782 |
| 5 | 45 | 0.76 ms | 3.96 ms | 43 ms | 64 % | 800 |
| 10 | 40 | 0.85 ms | 5.44 ms | 22 ms | 60 % | 633 |
| 20 | 30 | 0.99 ms | 6.23 ms | 27 ms | 66 % | 605 |
| none (50) | 0 | 1.20 ms | 9.76 ms | 30 ms | 71 % | 523 |

- The connection limit is the working knob for CPU fairness between
  databases. At 1–2 connections per agent, B stays near its unloaded
  latency (p99 1.2–2.1 ms vs 1.0 ms).
- Limits of 2–5 also give the agent itself its best throughput. Beyond
  that, its connections contend with each other and complete fewer
  statements.
- Excess connections are refused at connect time, and the agent's
  admitted connections carry on. B and the server saw no errors at any
  limit. The refusal error text was not recorded; PostgreSQL's is
  `too many connections for database` (53300).

## What PGRun should do

- Set `pgrust.ephemeral_db_connection_limit` to 2–5 for agent branches.
  Agents and test runners that use a pool should size it to match.
- Keep `statement_timeout` and `pgrust.session_memory_limit` on
  (`linux-limits.md`). The memory limit is what stops one branch's runaway
  query from growing the shared runtime.
- Expect p99 latency to grow with host CPU saturation, not with how many
  databases exist. Plan capacity by active databases per core
  (`linux-density.md`: ~20 active databases per 4 vCPU with p99 under
  3 ms).
- Isolation between agents is by connection limits, memory limits and
  timeouts within one process. A crash or OOM kill of the runtime affects
  every database in it (`linux-limits.md`). Hosts that need hard isolation
  run several runtimes, at ~35–55 MB base each.
