# Query performance: PGX vs PostgreSQL 18 (Linux)

`benchmarks/pgx/linux/query-bench.py`, pass 5 (build `dedfdf9ca5`), host
`linux-host.md` (4 vCPU, data on ZFS). Each engine gets one server and the
same schema and data:

- `accounts` (100k rows), `orders` (500k, FK to accounts, indexed on
  `account_id`), `items`, `products` (1k), `events` (100k rows of jsonb).

Each operation runs warm, then 500 sequential executions from one client
(fewer for the heavy ones). Latency is measured at the client and includes
the Python client's own overhead, which is the same for every engine.
Throughput is point SELECTs from 4 concurrent clients. Client and server
share the host.

The four configurations:

| Label | Settings |
|---|---|
| PG 18 | stock PostgreSQL 18 with `fsync`, `synchronous_commit` and `full_page_writes` off, the same durability as the PGX profile. Stock otherwise: `shared_buffers = 128MB`, parallel query on. |
| PGX | the PGX profile: `shared_buffers = 16MB`, parallel query off, `pgrust.runtime = off` |
| PGX sb128 | the PGX profile with `shared_buffers = 128MB` |
| PGX runtime | the PGX profile with `shared_buffers = 128MB`, `pgrust.runtime = on` and parallel query on (2 per gather) |

## Latency, p50 (p99) in ms

| Operation | PG 18 | PGX | PGX sb128 | PGX runtime |
|---|---|---|---|---|
| point SELECT by primary key | 0.13 (0.42) | 0.25 (0.42) | 0.18 (0.25) | 0.19 (0.34) |
| indexed lookup | 0.20 (0.53) | 0.35 (0.57) | 0.29 (0.46) | 0.30 (0.47) |
| INSERT one row | 0.11 (0.28) | 0.16 (0.41) | 0.16 (0.30) | 0.16 (0.27) |
| UPDATE one row | 0.13 (0.23) | 0.27 (0.42) | 0.26 (0.48) | 0.23 (0.36) |
| DELETE one row | 0.20 (0.34) | 0.30 (0.47) | 0.34 (0.60) | 0.31 (0.47) |
| two-table join | 0.31 (0.45) | 0.48 (0.83) | 0.37 (0.65) | 0.39 (0.62) |
| four-table join + aggregate | 1.57 (2.04) | 1.99 (3.22) | 1.91 (2.38) | 1.98 (2.42) |
| GROUP BY over 500k rows | 58 (80) | 100 (117) | 93 (111) | **36 (50)** |
| ORDER BY over 500k rows, OFFSET 100k | 83 (89) | 181 (202) | 168 (207) | **71 (78)** |
| jsonb containment + GROUP BY, 100k rows | 41 (58) | 61 (73) | 61 (73) | 58 (69) |
| short transaction (UPDATE + INSERT + COMMIT) | 0.36 (0.63) | 0.67 (1.71) | 0.62 (0.85) | 0.64 (0.93) |
| COPY 10k rows in | 8.8 (13.3) | 8.7 (15.8) | 9.7 (18.3) | 10.3 (15.7) |
| CREATE INDEX on 500k rows | 324 (350) | 415 (441) | 368 (385) | 378 (414) |
| migration (add column with default, index, FK, rename, undo) | 2.4 (3.4) | 3.6 (4.6) | 3.6 (4.1) | 4.1 (5.0) |

## Throughput, memory, load time

| | PG 18 | PGX | PGX sb128 | PGX runtime |
|---|---|---|---|---|
| point SELECTs/s, 4 clients | 7,662 | 7,878 | 8,148 | 8,200 |
| schema + data load | 13.3 s | 27.2 s | 29.0 s | 28.3 s |
| peak PSS | 149 MB | 134 MB | 213 MB | 229 MB |
| idle PSS after the run | 126 MB | 105 MB | 192 MB | 200 MB |

## Reading

- **Short statements:** PGX is about 0.05–0.15 ms slower per statement
  (1.3–2× at this scale). The profile's 16 MB buffer pool accounts for part
  of the point-SELECT gap (0.25 → 0.18 ms at 128 MB). The rest is
  per-statement overhead in the engine. Throughput with 4 clients is equal
  to or above PostgreSQL's, so the gap is latency, not capacity.
- **Large scans:** single-threaded PGX is 1.5–2.2× slower on the 500k-row
  aggregate and sort. PostgreSQL ran with parallel query available (its
  default; whether it chose parallel plans was not recorded). With
  the PgRust runtime and parallel query on, PGX is faster than PostgreSQL
  on both (36 vs 58 ms, 71 vs 83 ms), at +95 MB idle memory. The PGX
  profile keeps both off to save memory per runtime.
- **DDL and bulk:** COPY is equal. CREATE INDEX is 1.1–1.3× slower and the
  migration sequence 1.5×. Loading the schema and data (bulk INSERT…SELECT
  plus index builds) takes about twice as long.
- **For agent branches:** framework lifecycles are dominated by the
  framework itself. The end-to-end cost is 0–15 % (`agent-workloads.md`).
- **Memory:** in this run the PGX profile runtime peaked lower than
  PostgreSQL 18 with its default `shared_buffers`. That compares one
  database per server; density is in `linux-density.md`.

This build predates the DML per-row memory fixes (`ea6d055cb2`,
`daf827c0ff`, `67269453b2`, `d48f587da0`). Those change the memory of bulk
INSERT…SELECT and UPDATE, not the latency of these single-row operations.
Not re-run since.
