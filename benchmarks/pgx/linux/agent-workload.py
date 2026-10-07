#!/usr/bin/env python3
"""Agent workload corpus: whole database lifecycles of real framework runs.

For each engine (PostgreSQL 18, PGX profile) one server is started; then
each workload runs --repeats times, each in a fresh database:

  django    a generated Django project (--models models with FKs, unique
            and composite indexes, JSONField, M2M) plus Django's own
            auth/contenttypes/admin/sessions apps:
              time_to_db   create the branch database (PGX: mint-on-connect
                           from a sealed empty template; PG18: CREATE DATABASE)
              migrate      manage.py migrate
              test         manage.py test (Django creates test_<db>, migrates
                           it, runs --tests TestCase tests in transactions with
                           savepoints, destroys it)
              schema_change  a generated migration adding a column + index to
                           every model, then manage.py migrate
              test_again   manage.py test once more
              cleanup      DROP DATABASE
  node_pg   node-postgres: connect, create schema, 500 parameterized inserts
            (prepared statements over the extended protocol), a transaction
            with SAVEPOINT/ROLLBACK TO, COPY-free bulk insert, queries.
  rails_sql the statement sequence ActiveRecord issues for db:schema:load
            and a migration (schema_migrations, ar_internal_metadata,
            bigserial PKs, FKs, indexes, advisory lock), replayed as SQL.

Per run: phase timings, pass/fail with the failing output, the runtime's
peak PSS during the run, and the ZFS pool allocation delta. Output:
<out>/agent-workload.json. Requires python3-django, python3-psycopg,
nodejs, node-pg and python3-sqlalchemy (Debian/Ubuntu packages).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

M = 1048576.0
PREFIX = "tdb_"


def pool_alloc():
    subprocess.run(["sudo", "-n", "zpool", "sync", "tank"], capture_output=True)
    out = subprocess.run(["zpool", "list", "-Hp", "-o", "allocated", "tank"], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def gen_django_project(root, models, tests):
    os.makedirs(os.path.join(root, "proj"), exist_ok=True)
    os.makedirs(os.path.join(root, "app", "migrations"), exist_ok=True)
    open(os.path.join(root, "proj", "__init__.py"), "w").close()
    open(os.path.join(root, "app", "__init__.py"), "w").close()
    open(os.path.join(root, "app", "migrations", "__init__.py"), "w").close()
    with open(os.path.join(root, "proj", "settings.py"), "w") as f:
        f.write('''import os
SECRET_KEY = "bench"
DEBUG = False
USE_TZ = True
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "app"]
DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": os.environ["BENCH_DB"],
             "USER": "postgres", "HOST": os.environ["BENCH_HOST"], "PORT": os.environ["BENCH_PORT"],
             "TEST": {"NAME": "test_" + os.environ["BENCH_DB"]}}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "proj.urls"
MIDDLEWARE = []
TEMPLATES = []
''')
    with open(os.path.join(root, "proj", "urls.py"), "w") as f:
        f.write("urlpatterns = []\n")
    with open(os.path.join(root, "manage.py"), "w") as f:
        f.write('''import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
''')
    lines = ["from django.db import models\n\n"]
    for i in range(models):
        lines.append("class M%d(models.Model):\n" % i)
        lines.append("    name = models.CharField(max_length=100, db_index=True)\n")
        lines.append("    code = models.CharField(max_length=40, unique=True)\n")
        lines.append("    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)\n")
        lines.append("    data = models.JSONField(default=dict)\n")
        lines.append("    created = models.DateTimeField(auto_now_add=True)\n")
        if i > 0:
            lines.append("    parent = models.ForeignKey('M%d', on_delete=models.CASCADE, null=True, related_name='children')\n" % (i - 1))
        if i % 5 == 4:
            lines.append("    tags = models.ManyToManyField('M%d', related_name='tagged_by_%d')\n" % (i - 4, i))
        lines.append("    class Meta:\n        indexes = [models.Index(fields=['name', 'created'])]\n\n")
    with open(os.path.join(root, "app", "models.py"), "w") as f:
        f.write("".join(lines))
    t = ["from decimal import Decimal\nfrom django.test import TestCase\nfrom django.db import transaction, IntegrityError\nfrom app.models import *\n\n",
         "class T(TestCase):\n"]
    for j in range(tests):
        i = j % models
        t.append("    def test_%d(self):\n" % j)
        if i > 0:
            t.append("        p = M%d.objects.create(name='p%d', code='pc%d')\n" % (i - 1, j, j))
            t.append("        o = M%d.objects.create(name='n%d', code='c%d', amount=Decimal('1.50'), data={'k': %d}, parent=p)\n" % (i, j, j, j))
            t.append("        self.assertEqual(p.children.count(), 1)\n")
        else:
            t.append("        o = M0.objects.create(name='n%d', code='c%d', data={'k': %d})\n" % (j, j, j))
        t.append("        self.assertEqual(M%d.objects.filter(data__k=%d).count(), 1)\n" % (i, j))
        t.append("        with self.assertRaises(IntegrityError):\n            with transaction.atomic():\n")
        t.append("                M%d.objects.create(name='dup', code='c%d')\n" % (i, j))
        t.append("        M%d.objects.filter(pk=o.pk).update(amount=Decimal('2.00'))\n" % i)
        t.append("        self.assertEqual(M%d.objects.get(pk=o.pk).amount, Decimal('2.00'))\n\n" % i)
    with open(os.path.join(root, "app", "tests.py"), "w") as f:
        f.write("".join(t))


def add_schema_change(root, models):
    lines = open(os.path.join(root, "app", "models.py")).read()
    lines = lines.replace("    created = models.DateTimeField(auto_now_add=True)\n",
                          "    created = models.DateTimeField(auto_now_add=True)\n"
                          "    status = models.CharField(max_length=20, default='new', db_index=True)\n")
    with open(os.path.join(root, "app", "models.py"), "w") as f:
        f.write(lines)


def run_cmd(cmd, cwd, env, timeout=1800):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return time.perf_counter() - t0, p.returncode, (p.stdout + p.stderr)[-1500:]


NODE_SCRIPT = r'''
const { Client } = require('pg');
(async () => {
  const c = new Client({ host: process.env.BENCH_HOST, port: +process.env.BENCH_PORT, user: 'postgres', database: process.env.BENCH_DB });
  await c.connect();
  await c.query('CREATE TABLE t(id bigserial PRIMARY KEY, n int NOT NULL, s text, j jsonb)');
  await c.query('CREATE INDEX t_n ON t(n)');
  for (let i = 0; i < 500; i++) await c.query({ name: 'ins', text: 'INSERT INTO t(n, s, j) VALUES ($1, $2, $3)', values: [i, 's' + i, { i }] });
  await c.query('BEGIN'); await c.query('INSERT INTO t(n) VALUES (-1)'); await c.query('SAVEPOINT a');
  try { await c.query('INSERT INTO t(id, n) VALUES (1, 0)'); } catch (e) { await c.query('ROLLBACK TO SAVEPOINT a'); }
  await c.query('COMMIT');
  const r = await c.query({ name: 'sel', text: 'SELECT count(*)::int AS n FROM t WHERE n >= $1', values: [0] });
  if (r.rows[0].n !== 500) throw new Error('count ' + r.rows[0].n);
  const r2 = await c.query("SELECT j->>'i' AS v FROM t WHERE n = $1", [42]);
  if (r2.rows[0].v !== '42') throw new Error('jsonb');
  await c.query('INSERT INTO t(n) SELECT g FROM generate_series(1000, 20999) g');
  await c.end();
})().catch(e => { console.error(e); process.exit(1); });
'''

SQLALCHEMY_SCRIPT = r'''
import os, sys
from sqlalchemy import create_engine, ForeignKey, String, Integer, select, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session
from sqlalchemy.dialects.postgresql import JSONB
url = "postgresql+psycopg://postgres@/%s?host=%s&port=%s" % (os.environ["BENCH_DB"], os.environ["BENCH_HOST"], os.environ["BENCH_PORT"])
eng = create_engine(url)
class Base(DeclarativeBase): pass
class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    pets = relationship("Pet", back_populates="owner", cascade="all, delete-orphan")
class Pet(Base):
    __tablename__ = "pet"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    data = mapped_column(JSONB, default=dict)
    owner = relationship("Owner", back_populates="pets")
Base.metadata.create_all(eng)
with Session(eng) as s, s.begin():
    for i in range(300):
        o = Owner(email="o%d@x.io" % i)
        o.pets = [Pet(name="p%d_%d" % (i, k), data={"k": k}) for k in range(5)]
        s.add(o)
from sqlalchemy.exc import IntegrityError
with Session(eng) as s:
    n = s.scalar(select(func.count()).select_from(Pet).join(Owner).where(Pet.data["k"].astext == "3"))
    assert n == 300, n
with Session(eng) as s:
    try:
        with s.begin():
            s.add(Owner(email="o1@x.io"))
        raise SystemExit("expected a unique violation")
    except IntegrityError:
        pass
with eng.begin() as conn:
    conn.execute(text("ALTER TABLE pet ADD COLUMN status text DEFAULT 'new'"))
    conn.execute(text("CREATE INDEX pet_status ON pet(status)"))
with Session(eng) as s:
    assert s.scalar(select(func.count()).select_from(Owner)) == 300
Base.metadata.drop_all(eng)
print("ok")
'''

PRISMA_SCHEMA = """
generator client {
  provider = "prisma-client-js"
}
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}
model User {
  id       Int      @id @default(autoincrement())
  email    String   @unique
  profile  Json     @default("{}")
  posts    Post[]
  created  DateTime @default(now())
}
model Post {
  id        Int     @id @default(autoincrement())
  title     String
  published Boolean @default(false)
  author    User    @relation(fields: [authorId], references: [id], onDelete: Cascade)
  authorId  Int
  @@index([authorId, published])
}
"""

PRISMA_CHANGE = PRISMA_SCHEMA.replace("  published Boolean @default(false)\n",
                                      "  published Boolean @default(false)\n  views     Int     @default(0)\n")

PRISMA_CLIENT = r'''
const { PrismaClient } = require("@prisma/client");
const p = new PrismaClient();
(async () => {
  for (let i = 0; i < 100; i++) {
    await p.user.create({ data: { email: `u${i}@x.io`, profile: { i }, posts: { create: [{ title: "a" }, { title: "b", published: true }] } } });
  }
  const n = await p.post.count({ where: { published: true } });
  if (n !== 100) throw new Error("count " + n);
  await p.$transaction(async (tx) => {
    const u = await tx.user.findUnique({ where: { email: "u7@x.io" }, include: { posts: true } });
    await tx.post.updateMany({ where: { authorId: u.id }, data: { published: true } });
  });
  try { await p.user.create({ data: { email: "u1@x.io" } }); throw new Error("no unique violation"); }
  catch (e) { if (e.code !== "P2002") throw e; }
  const r = await p.$queryRaw`SELECT count(*)::int AS c FROM "User" WHERE (profile->>'i')::int < 10`;
  if (r[0].c !== 10) throw new Error("raw " + JSON.stringify(r));
  await p.$disconnect();
})().catch(async (e) => { console.error(e); await p.$disconnect(); process.exit(1); });
'''

RAILS_SQL = [
    "SELECT pg_try_advisory_lock(7123456789)",
    'CREATE TABLE IF NOT EXISTS "schema_migrations" ("version" character varying NOT NULL PRIMARY KEY)',
    'CREATE TABLE IF NOT EXISTS "ar_internal_metadata" ("key" character varying NOT NULL PRIMARY KEY, "value" character varying, "created_at" timestamp(6) NOT NULL, "updated_at" timestamp(6) NOT NULL)',
    'CREATE TABLE "users" ("id" bigserial primary key, "email" character varying DEFAULT \'\' NOT NULL, "encrypted_password" character varying DEFAULT \'\' NOT NULL, "created_at" timestamp(6) NOT NULL, "updated_at" timestamp(6) NOT NULL)',
    'CREATE UNIQUE INDEX "index_users_on_email" ON "users" ("email")',
    'CREATE TABLE "projects" ("id" bigserial primary key, "user_id" bigint NOT NULL, "name" character varying, "settings" jsonb DEFAULT \'{}\' NOT NULL, "created_at" timestamp(6) NOT NULL, "updated_at" timestamp(6) NOT NULL, CONSTRAINT "fk_rails_projects_users" FOREIGN KEY ("user_id") REFERENCES "users" ("id"))',
    'CREATE INDEX "index_projects_on_user_id" ON "projects" ("user_id")',
    'CREATE TABLE "tasks" ("id" bigserial primary key, "project_id" bigint NOT NULL, "title" character varying, "done" boolean DEFAULT FALSE NOT NULL, "position" integer, "created_at" timestamp(6) NOT NULL, "updated_at" timestamp(6) NOT NULL, CONSTRAINT "fk_rails_tasks_projects" FOREIGN KEY ("project_id") REFERENCES "projects" ("id") ON DELETE CASCADE)',
    'CREATE INDEX "index_tasks_on_project_id_and_position" ON "tasks" ("project_id", "position")',
    "INSERT INTO \"schema_migrations\" (version) VALUES ('20260101000001'), ('20260101000002'), ('20260101000003')",
    "INSERT INTO \"ar_internal_metadata\" (\"key\", \"value\", \"created_at\", \"updated_at\") VALUES ('environment', 'test', now(), now())",
    # a migration
    "BEGIN",
    'ALTER TABLE "tasks" ADD "due_on" date',
    'ALTER TABLE "tasks" ADD "priority" integer DEFAULT 0 NOT NULL',
    'CREATE INDEX "index_tasks_on_due_on" ON "tasks" ("due_on")',
    "INSERT INTO \"schema_migrations\" (\"version\") VALUES ('20260101000004')",
    "COMMIT",
    # transactional-fixture test shape
    "BEGIN", "SAVEPOINT active_record_1",
    "INSERT INTO \"users\" (\"email\", \"created_at\", \"updated_at\") VALUES ('a@x.io', now(), now())",
    "RELEASE SAVEPOINT active_record_1",
    "INSERT INTO \"projects\" (\"user_id\", \"name\", \"created_at\", \"updated_at\") SELECT id, 'p', now(), now() FROM users",
    "INSERT INTO \"tasks\" (\"project_id\", \"title\", \"position\", \"created_at\", \"updated_at\") SELECT p.id, 't' || g, g, now(), now() FROM projects p, generate_series(1, 200) g",
    "SELECT COUNT(*) FROM \"tasks\" WHERE \"tasks\".\"project_id\" IN (SELECT id FROM projects) AND \"tasks\".\"done\" = FALSE",
    "SAVEPOINT active_record_2",
    "EXPECT-ERROR:INSERT INTO \"users\" (\"email\", \"created_at\", \"updated_at\") VALUES ('a@x.io', now(), now())",
    "ROLLBACK TO SAVEPOINT active_record_2",
    "ROLLBACK",
    "SELECT pg_advisory_unlock(7123456789)",
]


def peak_watch(pid):
    st = {"peak": 0, "stop": False}

    def run():
        while not st["stop"]:
            st["peak"] = max(st["peak"], pb.memory_sample(pid).get(pb.MEM_KEY, 0) or 0)
            time.sleep(0.25)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return st, t


def one_engine(args, label, engine_key, port):
    pgx = engine_key == "pgx"
    gucs = ["max_connections=60"]
    if pgx:
        gucs += ["pgrust.ephemeral_db_prefix=" + PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
                 "pgrust.ephemeral_db_grace=86400"]
    sargs = []
    for g in gucs:
        sargs += ["-c", g]
    if pgx:
        engine = pb.Engine("pgrust", args.binary, sargs, args.conf)
    else:
        engine = pb.Engine("postgres", os.path.join(pb.pg18_prefix(), "bin", "postgres"), sargs)
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, port).launch()
    out = {"label": label, "runs": []}
    try:
        srv.wait_select1(timeout=300)
        pid = srv.proc.pid
        admin = pb.PgConn(ws.sockdir, port, timeout=600)
        if pgx:
            admin.query("CREATE DATABASE tpl_empty")
            pb.PgConn(ws.sockdir, port, database="tpl_empty").close()
            admin.query("SELECT pgrust_seal_template('tpl_empty')")
            time.sleep(2)
        env = dict(os.environ, BENCH_HOST=ws.sockdir, BENCH_PORT=str(port), PGHOST=ws.sockdir)
        for wl in args.workloads.split(","):
            for rep in range(args.repeats):
                db = "%stpl_empty__%s%d" % (PREFIX, wl, rep) if pgx else "%s%d" % (wl, rep)
                rec = {"workload": wl, "repeat": rep, "phases": {}, "ok": True}
                alloc0 = pool_alloc()
                st, wt = peak_watch(pid)
                t0 = time.perf_counter()
                if pgx:
                    pb.PgConn(ws.sockdir, port, database=db, timeout=300).close()
                else:
                    admin.query("CREATE DATABASE %s" % db)
                rec["phases"]["time_to_db_s"] = round(time.perf_counter() - t0, 3)
                env["BENCH_DB"] = db
                if wl == "django":
                    root = tempfile.mkdtemp(prefix="dj-")
                    gen_django_project(root, args.models, args.tests)
                    steps = [("makemigrations", [sys.executable, "manage.py", "makemigrations", "app"]),
                             ("migrate", [sys.executable, "manage.py", "migrate", "--noinput"]),
                             ("test", [sys.executable, "manage.py", "test", "--noinput", "app"]),
                             ("schema_change", None),
                             ("test_again", [sys.executable, "manage.py", "test", "--noinput", "app"])]
                    for name, cmd in steps:
                        if name == "schema_change":
                            add_schema_change(root, args.models)
                            d1, rc1, o1 = run_cmd([sys.executable, "manage.py", "makemigrations", "app"], root, env)
                            d2, rc2, o2 = run_cmd([sys.executable, "manage.py", "migrate", "--noinput"], root, env)
                            d, rc, o = d1 + d2, rc1 or rc2, o1 + o2
                        else:
                            d, rc, o = run_cmd(cmd, root, env)
                        rec["phases"][name + "_s"] = round(d, 3)
                        if rc != 0:
                            rec["ok"] = False
                            rec["failed_phase"] = name
                            rec["output_tail"] = o[-1200:]
                            break
                    shutil.rmtree(root, ignore_errors=True)
                elif wl == "node_pg":
                    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                        f.write(NODE_SCRIPT)
                    d, rc, o = run_cmd(["node", f.name], "/tmp", dict(env, NODE_PATH="/usr/share/nodejs:/usr/lib/nodejs"))
                    rec["phases"]["run_s"] = round(d, 3)
                    if rc != 0:
                        rec.update(ok=False, failed_phase="run", output_tail=o[-1200:])
                    os.unlink(f.name)
                elif wl == "prisma":
                    pdir = tempfile.mkdtemp(prefix="prisma-")
                    os.symlink(os.path.join(args.prisma_dir, "node_modules"), os.path.join(pdir, "node_modules"))
                    os.makedirs(os.path.join(pdir, "prisma"))
                    url = "postgresql://postgres@localhost:%d/%s?host=%s" % (port, db, ws.sockdir)
                    penv = dict(env, DATABASE_URL=url, PRISMA_HIDE_UPDATE_MESSAGE="1", CHECKPOINT_DISABLE="1")
                    npx = os.path.join(pdir, "node_modules", ".bin", "prisma")
                    steps = [("migrate_init", PRISMA_SCHEMA, [npx, "migrate", "dev", "--name", "init", "--skip-generate"]),
                             ("generate", None, [npx, "generate"]),
                             ("client", None, ["node", "client.js"]),
                             ("migrate_change", PRISMA_CHANGE, [npx, "migrate", "dev", "--name", "views", "--skip-generate"]),
                             ("generate_again", None, [npx, "generate"]),
                             ("client_again", None, ["node", "client.js"])]
                    with open(os.path.join(pdir, "client.js"), "w") as f:
                        f.write(PRISMA_CLIENT)
                    for name, schema, cmd in steps:
                        if schema is not None:
                            with open(os.path.join(pdir, "prisma", "schema.prisma"), "w") as f:
                                f.write(schema)
                        if name.startswith("client"):
                            # each client run starts from an empty table set
                            cc = pb.PgConn(ws.sockdir, port, database=db)
                            cc.query('TRUNCATE "User", "Post" CASCADE')
                            cc.close()
                        d, rc, o = run_cmd(cmd, pdir, penv)
                        rec["phases"][name + "_s"] = round(d, 3)
                        if rc != 0:
                            rec.update(ok=False, failed_phase=name, output_tail=o[-1200:])
                            break
                    shutil.rmtree(pdir, ignore_errors=True)
                elif wl == "sqlalchemy":
                    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                        f.write(SQLALCHEMY_SCRIPT)
                    d, rc, o = run_cmd([sys.executable, f.name], "/tmp", env)
                    rec["phases"]["run_s"] = round(d, 3)
                    if rc != 0:
                        rec.update(ok=False, failed_phase="run", output_tail=o[-1200:])
                    os.unlink(f.name)
                elif wl == "rails_sql":
                    c = pb.PgConn(ws.sockdir, port, database=db, timeout=600)
                    t1 = time.perf_counter()
                    try:
                        for sql in RAILS_SQL:
                            if sql.startswith("EXPECT-ERROR:"):
                                try:
                                    c.query(sql[len("EXPECT-ERROR:"):])
                                    raise RuntimeError("expected a unique violation: " + sql)
                                except pb.ServerNotReady as e:
                                    if "23505" not in str(e):
                                        raise
                            else:
                                c.query(sql)
                    except Exception as e:  # noqa: BLE001
                        rec.update(ok=False, failed_phase="sql", output_tail=str(e)[:600])
                    rec["phases"]["schema_load_migrate_test_s"] = round(time.perf_counter() - t1, 3)
                    c.close()
                rec["peak_pss_mb"] = round(st["peak"] / M, 1)
                os.sync()
                alloc1 = pool_alloc()
                rec["pool_alloc_delta_mb"] = round((alloc1 - alloc0) / M, 1) if alloc0 and alloc1 else None
                t2 = time.perf_counter()
                admin.query("DROP DATABASE IF EXISTS test_%s" % db)
                admin.query("DROP DATABASE %s WITH (FORCE)" % db)
                rec["phases"]["cleanup_s"] = round(time.perf_counter() - t2, 3)
                st["stop"] = True
                wt.join()
                rec["total_s"] = round(sum(v for k, v in rec["phases"].items()), 3)
                out["runs"].append(rec)
                print("%-10s %-9s #%d %-4s %s peak=%.0f MB pool+%s MB" % (
                    label, wl, rep, "ok" if rec["ok"] else "FAIL",
                    " ".join("%s=%s" % (k.replace("_s", ""), v) for k, v in rec["phases"].items()),
                    rec["peak_pss_mb"], rec["pool_alloc_delta_mb"]), flush=True)
                if not rec["ok"]:
                    print(rec.get("output_tail", "")[-600:], flush=True)
        admin.close()
    finally:
        srv.stop()
        ws.cleanup()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--engines", default="postgres,pgx")
    ap.add_argument("--workloads", default="django,node_pg,sqlalchemy,prisma,rails_sql")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--models", type=int, default=20)
    ap.add_argument("--tests", type=int, default=60)
    ap.add_argument("--port", type=int, default=54800)
    ap.add_argument("--prisma-dir", default=os.path.expanduser("~/prisma-bench"),
                    help="a directory with prisma and @prisma/client installed (npm)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    doc = {"benchmark": "agent-workload", "git_commit": pb.git("rev-parse", "HEAD"),
           "models": args.models, "tests": args.tests, "engines": []}
    labels = {"postgres": "postgres18", "pgx": "pgx-profile"}
    for i, e in enumerate(args.engines.split(",")):
        doc["engines"].append(one_engine(args, labels[e], e, args.port + i))
        with open(os.path.join(args.out, "agent-workload.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")


if __name__ == "__main__":
    main()
