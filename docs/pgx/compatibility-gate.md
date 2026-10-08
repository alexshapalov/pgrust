# PGX compatibility gate (contract)

PGRun decides per source database whether its branches run on PGX or on
standard PostgreSQL. This page fixes the inputs, the decision and its
output format. It does not need to be fully implemented before PGRun
integration starts. Until it is, every database defaults to
`STANDARD_POSTGRES_REQUIRED` unless an operator allowlists it for PGX.

## Why a gate

PGX does not need 100 % PostgreSQL compatibility to be useful. It needs
PGRun to send it only the databases it serves correctly, and to send the
rest to the fallback:

```
                  PGRun
                    │
           compatibility gate
              /            \
          PGX               PostgreSQL
   fast, dense path       fallback path
   (engine = pgx)         (engine = postgres)
```

Both engines expose the same branch contract to the customer: a
`DATABASE_URL`, the same template content, the same credentials model.
Only placement differs (`pgrun-integration.md`).

## Inputs

Collected from the source database by PGRun's existing source inspection,
read-only, at connect and at every Safe Copy refresh:

| Input | How | Why it matters |
|---|---|---|
| Server version | `SHOW server_version_num` | PGX reports PostgreSQL 18.6 (`server_version_num` 180006). Older sources are dumped with pg_dump 18 and the restore (rule 3) decides; features newer than 18 do not exist. |
| Installed extensions | `SELECT extname, extversion FROM pg_extension` | PGX cannot load C extensions (no `dlopen`, by design). Only its built-in modules exist. |
| Schema constructs | `pg_dump --schema-only` restored into a scratch PGX database | The authoritative test: does the schema load at all? |
| Data types | `pg_attribute` → `pg_type` for user tables | Types from unavailable extensions (PostGIS `geometry`, …) |
| Procedural languages | `SELECT lanname FROM pg_language` and functions by language | PL/pgSQL and SQL only |
| Replication / publication objects | `pg_publication`, `pg_subscription` | Logical replication is off in the PGX profile |
| Prepared transactions | `max_prepared_transactions` used by the app | 2PC not offered on branches |
| ORM / client (optional, declared by the customer) | project setting | Tested: Rails 8.1, Django 5.2, Prisma 6, SQLAlchemy 2, node-postgres |
| Migration pattern (optional) | `schema_migrations`, `django_migrations`, `_prisma_migrations` present | Confirms the framework lifecycle the branch will run |

Built-in extensions in v0.1 (`crates/contrib`): amcheck, auto_explain,
bloom, btree_gin, btree_gist, citext, cube, dblink, earthdistance,
file_fdw, fuzzystrmatch, hstore, intarray, isn, lo, ltree, pageinspect,
passwordcheck, pg_buffercache, pg_freespacemap, pg_logicalinspect,
pg_overexplain, pg_prewarm, pg_stat_statements, pg_surgery, pg_trgm,
pg_visibility, pg_walinspect, pgcrypto, pgrowlocks, pgstattuple, pgvector
(with HNSW), postgres_fdw, seg, sslinfo, tablefunc, tcn, tsm_system_rows,
tsm_system_time, unaccent, uuid-ossp. Being built in is not the same as
being tested. Beyond what the regression suite and the framework workloads
use, these modules were not exercised by the v0.1 benchmarks.

## Decision

Evaluate the rules in order. The first rule that fails sets
`STANDARD_POSTGRES_REQUIRED`, and every failing rule is reported, not
only the first.

1. **`extension_unavailable`.** An installed extension is not in the
   built-in list. PostGIS, TimescaleDB, Citus, pg_cron and any custom C
   extension fail here.
2. **`language_unavailable`.** A user function in a language other than
   `plpgsql` or `sql`. `internal` and `c` functions are fine only when they
   belong to a built-in module.
3. **`schema_restore_failed`.** The schema-only dump does not restore
   into a scratch PGX database. Report the first error and its object.
4. **`type_unavailable`.** A column uses a type that the restored schema
   could not create. This is normally caught by rule 3 and is reported
   separately for clarity.
5. **`version_newer_than_pgx`.** The source runs a major version newer
   than PGX's.
6. **`feature_unsupported`.** Publications or subscriptions are needed on
   the branch, or the application declares two-phase commit.

If no rule fails, the result is `PGX_COMPATIBLE`. Optional checks add
warnings, never a refusal:

- **`framework_untested`.** A migration table that is not one of the
  tested frameworks.
- **`large_scan_workload`.** The customer declares analytics-heavy
  branches. Single-threaded large scans are 1.5–2.2× slower on PGX.

## Output

Machine-readable and stored with the source database, so placement never
re-runs it inline:

```json
{
  "gate_version": "2026.10.1",
  "pgx_version": "0.1",
  "source_database_id": "db_123",
  "evaluated_at": "2026-10-08T12:00:00Z",
  "verdict": "STANDARD_POSTGRES_REQUIRED",
  "reasons": [
    {"rule": "extension_unavailable", "object": "postgis", "detail": "extension not built into PGX"},
    {"rule": "type_unavailable", "object": "public.places.location", "detail": "type geometry"}
  ],
  "warnings": [],
  "inputs_fingerprint": "sha256:…"
}
```

- **`verdict`** is `PGX_COMPATIBLE` or `STANDARD_POSTGRES_REQUIRED`.
- **`reasons`** is empty exactly when the verdict is `PGX_COMPATIBLE`.
- **`inputs_fingerprint`** hashes the extension list, the schema dump and
  the source version. PGRun re-evaluates only when the fingerprint
  changes.
- **`gate_version`** changes whenever a rule changes. A new PGX version
  can turn earlier verdicts into `PGX_COMPATIBLE`, so a gate-version
  change re-evaluates everything.

## Placement contract

- `engine = pgx` only with a current `PGX_COMPATIBLE` verdict, or an
  operator allowlist entry, which overrides the gate and is recorded with
  a reason.
- `engine = postgres` otherwise. This is the existing PGRun branch path,
  unchanged.
- A branch keeps its engine for life. A changed verdict applies to new
  branches only.
- If a PGX branch fails at runtime on a feature the gate did not catch,
  PGRun records the failure against the gate (to add a rule) and offers
  the customer a re-create on `engine = postgres`.
