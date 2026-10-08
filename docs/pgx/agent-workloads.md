# Agent workloads: real framework lifecycles on PGX vs PostgreSQL 18

`benchmarks/pgx/linux/agent-workload.py`, pass 4 (build `83fbf8d06e`), re-run
with real Rails on the final build `60fca8427f` (engine `6fe0b1ad65`, profile
with skip-idle autovacuum), host `linux-host.md`. Each run is a whole branch lifecycle in a fresh
database; 3 repeats per workload per engine; every phase is wall time of the
real tool (Django management commands, the Prisma CLI, node, python).

- PGX: the database is minted from a sealed empty template (mint-on-connect)
  in a runtime with the PGX profile.
- PostgreSQL 18: `CREATE DATABASE` in a stock server.
- Toolchain: Django 5.2.9 + psycopg 3.3.2, SQLAlchemy 2.0.45, Prisma 6.19.3,
  node 22 + node-postgres (Ubuntu packages / npm), Ruby 3.3.8 + Rails 8.1.4 +
  pg 1.7.0.

## Compatibility

All six workloads pass on both engines, every repeat: 30/30 in pass 4 and
36/36 on the final build.

| Workload | What it exercises | PG 18 | PGX |
|---|---|---|---|
| Django | makemigrations, migrate (auth/contenttypes/sessions + 20 models: FKs, unique, composite and JSON indexes, M2M), `manage.py test` (Django creates/migrates/destroys `test_<db>`, 60 TestCase tests in transactions with savepoints, IntegrityError inside `atomic`), a generated schema migration adding a column + index to every model, tests again | pass | pass |
| node-postgres | 500 parameterized inserts as named prepared statements (extended protocol), BEGIN/SAVEPOINT/ROLLBACK TO, JSONB access, 20k-row INSERT…SELECT | pass | pass |
| SQLAlchemy 2 | ORM `create_all` (FK, unique, JSONB), 1,800-row unit of work, join with a JSONB predicate, IntegrityError rollback, DDL in a transaction, `drop_all` | pass | pass |
| Prisma 6 | `migrate dev` (creates and drops a shadow database), `generate`, client: nested creates, `count`, interactive `$transaction`, P2002 unique violation, raw JSON query; schema change + `migrate dev` + client again | pass | pass |
| Rails-shaped SQL | the statement sequence ActiveRecord issues for `db:schema:load` + a migration + transactional fixtures (advisory lock, schema_migrations, ar_internal_metadata, bigserial, FKs, savepoints) | pass | pass |
| Rails 8.1 (real app, `rails-app-setup.sh`) | `db:prepare` runs 3 migrations (FK, unique and composite unique indexes, jsonb with a GIN index); `bin/rails test` with fixtures (20 users, 20 projects, 200 tasks): jsonb queries, joins with GROUP BY, `RecordNotUnique` inside a transaction, `InvalidForeignKey` from the database, nested transaction rolled back to a savepoint, `insert_all` + `update_all` of 500 rows, cascade delete; a schema-change migration (columns, a composite index, a unique index); tests again including the new columns; `db:schema:load` from the dumped `schema.rb`; tests again | pass | pass |

## Time (median of 3)

| Phase | PG 18 | PGX | PGX vs PG |
|---|---|---|---|
| time to database | 38–41 ms | 43–52 ms | +10 ms |
| Django makemigrations | 0.70 s | 0.67 s | ≈ |
| Django migrate | 1.43 s | 1.57 s | +10% |
| Django test (incl. test DB create/migrate/destroy) | 2.22 s | 2.37 s | +7% |
| Django schema change (makemigrations + migrate) | 1.52 s | 1.64 s | +8% |
| Django test again | 2.28 s | 2.62 s | +15% |
| node-postgres run | 0.71 s | 0.59 s | −17% |
| SQLAlchemy run | 1.00 s | 1.08 s | +8% |
| Prisma migrate dev (init) | 2.62 s | 2.81 s | +7% |
| Prisma generate | 2.48 s | 2.49 s | ≈ (no database) |
| Prisma client | 0.52 s | 0.57 s | +9% |
| Rails-shaped SQL | 31 ms | 60 ms | +29 ms |
| cleanup (DROP DATABASE) | 17–32 ms | 15–35 ms | ≈ |

Real Rails, final build (median of 3):

| Phase | PG 18 | PGX | PGX vs PG |
|---|---|---|---|
| time to database | 43 ms | 47 ms | +4 ms |
| `db:prepare` (migrations) | 2.14 s | 2.13 s | ≈ |
| `bin/rails test` | 4.46 s | 4.40 s | ≈ |
| schema-change `db:migrate` | 2.01 s | 2.11 s | +5 % |
| tests again | 4.58 s | 4.78 s | +4 % |
| `db:schema:load` | 2.00 s | 2.07 s | +4 % |
| tests after the load | 2.38 s | 2.44 s | +2 % |

The other workloads on the final build are within a few percent of the
pass 4 figures above: Django migrate +12 %, tests +8–15 %, node-postgres
−16 %, SQLAlchemy +15 %, Prisma +2–7 %.

Framework work dominates every lifecycle; the database is 0–15% slower under
PGX for these, consistent with the per-statement gap in
`linux-query-performance.md`, and faster for node-postgres.

## Resources

| | PG 18 | PGX |
|---|---|---|
| Peak memory of the server during a run | 47–61 MB | 93–105 MB |
| Pool space added per run (after the lifecycle, before DROP) | Django 12–36 MB, node 8 MB, SQLAlchemy 6 MB, Prisma 8.5 MB, Rails 6.3 MB | Django 5.6–33 MB, node 2.5 MB, SQLAlchemy 0.5 MB, Prisma 2.2 MB, Rails 0.9 MB |

- PGX's peak includes the shared runtime (~86 MB with the template and
  janitor) that every database in it shares; PostgreSQL's is one server per
  run. The density comparison is in `linux-density.md`.
- PGX branches start as block clones, so the space a short agent lifecycle
  adds is mostly what it writes.
