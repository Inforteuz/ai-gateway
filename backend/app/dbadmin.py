"""Database administration (with the migration role, as root on the server):

  python -m app.dbadmin migrate                                   creates/updates the schema, grants the app role
  python -m app.dbadmin import-sqlite FILE.db [--dry-run] [--orphans fail|null] [--replace]
                                                                  copies SQLite data into PostgreSQL
  python -m app.dbadmin verify-sqlite FILE.db [--orphans ...]     compares row counts and checksums
  python -m app.dbadmin export-sqlite OLD.db NEW.db               rollback: writes PG data into an old-schema SQLite file

Connection: SF_MIGRATE_DATABASE_URL (URL without password) + SF_MIGRATE_DATABASE_PASSWORD (or PGPASSWORD / ~/.pgpass).
App role: SF_DB_APP_ROLE (default ai_gateway_app). Passwords are never printed or logged.

import-sqlite:
  * everything in ONE transaction: on error nothing is left in PG;
  * --dry-run: schema + copy + verification run in full, then ROLLBACK;
  * idempotent: if PG data is identical to SQLite — "already imported", nothing is done;
    if PG is not empty and differs — it stops (--replace overwrites);
  * identity sequences are moved past the larger of max(id) and sqlite_sequence (ids are never reused);
  * "orphan" rows violating FKs (pointing at rows deleted while SQLite ran without FK checks) stop the
    import by default; --orphans null sets the referencing column to NULL (the count is reported).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field

import psycopg
from psycopg import sql as pgsql

from .db import WRITE_LOCK_KEY, apply_schema, configure_connection, schema_version

SKIP_SQLITE = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}
SKIP_PG = {"schema_meta"}
# Columns silently dropped in a PG -> SQLite export (rollback) when the old schema lacks them.
# Referral (v2): every new account has a code and device mark, but the old code works fine without them.
# Referral LINKS and REWARDS live in separate tables — if those are not empty the export still stops
# (the old schema also rejects ledger entries of kind 'referral').
# Telegram message (v4): only used to update the message, not needed by the old code.
# Encrypted key copy (v6): losing it only costs the owner the "show my key" button -
# `key_hash` still authenticates every key, so nothing stops working after a rollback.
# Stand-in model (v8): losing it means a failing model stops falling back to another,
# which is how the old code behaved anyway.
DROPPABLE_ON_EXPORT = {("users", "ref_code"), ("users", "signup_device"), ("users", "referral_paused"),
                       ("topup_requests", "tg_chat_id"), ("topup_requests", "tg_message_id"),
                       ("api_keys", "secret_enc"), ("models", "fallback_to")}


class Abort(Exception):
    pass


class _Rollback(Exception):
    pass


def connect() -> psycopg.Connection:
    dsn = os.getenv("SF_MIGRATE_DATABASE_URL")
    if not dsn:
        raise Abort("SF_MIGRATE_DATABASE_URL is not set")
    kw = {}
    if os.getenv("SF_MIGRATE_DATABASE_PASSWORD"):
        kw["password"] = os.environ["SF_MIGRATE_DATABASE_PASSWORD"]
    conn = psycopg.connect(dsn, autocommit=True, application_name="ai-gateway-dbadmin", **kw)
    configure_connection(conn)
    conn.execute("SET statement_timeout = 0")
    return conn


def app_role() -> str:
    return os.getenv("SF_DB_APP_ROLE", "ai_gateway_app")


def where(conn: psycopg.Connection) -> str:
    i = conn.info
    return f"{i.user}@{i.host}:{i.port}/{i.dbname}"


# -- table structure ---------------------------------------------------------------
def pg_tables(conn) -> list[str]:
    """Parent tables first (FK order)."""
    tables = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() ORDER BY tablename")]
    parents = {t: set() for t in tables}
    for child, parent in conn.execute(
            "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
            "WHERE contype='f' AND connamespace=current_schema()::regnamespace"):
        if child != parent and child in parents:
            parents[child].add(parent)
    order: list[str] = []
    seen: set[str] = set()

    def visit(t):
        if t not in seen:
            seen.add(t)
            for p in sorted(parents[t]):
                visit(p)
            order.append(t)
    for t in tables:
        visit(t)
    return [t for t in order if t not in SKIP_PG]


def pg_columns(conn, table) -> dict[str, bool]:
    """column -> is nullable."""
    return {r[0]: r[1] == "YES" for r in conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position", (table,))}


def pg_identity(conn, table) -> str | None:
    row = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema()"
                       " AND table_name=%s AND is_identity='YES'", (table,)).fetchone()
    return row[0] if row else None


def sqlite_tables(lite) -> list[str]:
    return [r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if r[0] not in SKIP_SQLITE]


def sqlite_columns(lite, table) -> list[str]:
    return [r[1] for r in lite.execute(f'PRAGMA table_info("{table}")')]


def sqlite_seq(lite) -> dict[str, int]:
    try:
        return {r[0]: r[1] for r in lite.execute("SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.OperationalError:
        return {}


def open_sqlite(path: str, readonly: bool = True) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise Abort(f"SQLite file not found: {path}")
    uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    return sqlite3.connect(uri, uri=True)


# -- comparison --------------------------------------------------------------------
def norm(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, memoryview):
        return bytes(v)
    return v


def digest(rows) -> str:
    """Order-independent checksum (rows are sorted)."""
    lines = sorted(repr(tuple(norm(v) for v in r)) for r in rows)
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln.encode("utf-8", "surrogatepass"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class TablePlan:
    name: str
    columns: list[str]
    rows: list[tuple]
    orphans: int = 0
    seq_floor: int = 0
    notes: list[str] = field(default_factory=list)


def read_sqlite(lite, conn, orphans: str) -> list[TablePlan]:
    """Reads SQLite and prepares it for the PG schema (orphans handled per policy)."""
    lite_tables = set(sqlite_tables(lite))
    targets = pg_tables(conn)
    missing = sorted(lite_tables - set(targets))
    if missing:
        raise Abort(f"Tables missing from the PG schema (data would be lost): {', '.join(missing)}")

    violations: dict[str, dict[int, set[str]]] = {}
    for table, rowid, parent, fkid in lite.execute("PRAGMA foreign_key_check"):
        fk = {r[0]: r[3] for r in lite.execute(f'PRAGMA foreign_key_list("{table}")')}
        violations.setdefault(table, {}).setdefault(rowid, set()).add(fk[fkid])
    if violations and orphans == "fail":
        detail = ", ".join(f"{t}: {len(v)} qator" for t, v in violations.items())
        raise Abort(f"FK'ga zid yetim yozuvlar bor ({detail}). Ko'rib chiqing va --orphans null bilan qayta ishga tushiring.")

    seqs = sqlite_seq(lite)
    plans = []
    for table in targets:
        if table not in lite_tables:
            plans.append(TablePlan(table, [], []))
            continue
        cols_pg = pg_columns(conn, table)
        cols = sqlite_columns(lite, table)
        extra = [c for c in cols if c not in cols_pg]
        if extra:
            raise Abort(f"{table}: columns missing in PG (data would be lost): {', '.join(extra)}")
        bad = violations.get(table, {})
        idx = {c: i for i, c in enumerate(cols)}
        rows = []
        n_orph = 0
        col_sql = ", ".join(f'"{c}"' for c in cols)
        for r in lite.execute(f'SELECT rowid, {col_sql} FROM "{table}"'):
            rowid, vals = r[0], [norm(v) for v in r[1:]]
            if rowid in bad:
                for col in bad[rowid]:
                    if not cols_pg[col]:
                        raise Abort(f"{table}.{col}: orphan reference, but the column is NOT NULL (rowid {rowid})")
                    vals[idx[col]] = None
                n_orph += 1
            rows.append(tuple(vals))
        plan = TablePlan(table, cols, rows, n_orph, seqs.get(table, 0))
        plans.append(plan)
    return plans


def pg_digest(conn, table, cols) -> tuple[int, str]:
    if not cols:
        n = conn.execute(pgsql.SQL("SELECT COUNT(*) FROM {}").format(pgsql.Identifier(table))).fetchone()[0]
        return n, digest([]) if n == 0 else "?"
    q = pgsql.SQL("SELECT {} FROM {}").format(pgsql.SQL(", ").join(map(pgsql.Identifier, cols)), pgsql.Identifier(table))
    with conn.cursor() as cur:
        rows = cur.execute(q).fetchall()
    return len(rows), digest(rows)


def compare(conn, plans: list[TablePlan], out=print) -> bool:
    ok = True
    out(f"  {'table':<20}{'sqlite':>10}{'pg':>10}  result")
    for p in plans:
        n, d = pg_digest(conn, p.name, p.columns or list(pg_columns(conn, p.name)))
        good = n == len(p.rows) and d == digest(p.rows)
        ok &= good
        extra = f"  (orphan refs set to NULL: {p.orphans})" if p.orphans else ""
        out(f"  {p.name:<20}{len(p.rows):>10}{n:>10}  {'OK' if good else 'FARQ BOR'}{extra}")
    return ok


def pg_is_empty(conn, plans) -> bool:
    return all(conn.execute(pgsql.SQL("SELECT NOT EXISTS (SELECT 1 FROM {})").format(pgsql.Identifier(p.name))).fetchone()[0]
               for p in plans)


def copy_in(conn, plan: TablePlan) -> None:
    if not plan.rows:
        return
    q = pgsql.SQL("COPY {} ({}) FROM STDIN").format(
        pgsql.Identifier(plan.name), pgsql.SQL(", ").join(map(pgsql.Identifier, plan.columns)))
    with conn.cursor() as cur, cur.copy(q) as cp:
        for r in plan.rows:
            cp.write_row(r)


def fix_sequence(conn, plan: TablePlan) -> None:
    col = pg_identity(conn, plan.name)
    if not col:
        return
    ident = pgsql.Identifier(col)
    top = conn.execute(pgsql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(ident, pgsql.Identifier(plan.name))).fetchone()[0]
    top = max(top, plan.seq_floor)
    seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (plan.name, col)).fetchone()[0]
    if top > 0:
        conn.execute("SELECT setval(%s, %s, true)", (seq, top))
    else:
        conn.execute("SELECT setval(%s, 1, false)", (seq,))


# -- commands ----------------------------------------------------------------------
def cmd_migrate(args) -> int:
    with connect() as conn:
        before = schema_version(conn)
        after = apply_schema(conn, app_role())
        print(f"{where(conn)}: schema v{before} -> v{after}, privileges of '{app_role()}' updated")
    return 0


def cmd_import(args) -> int:
    lite = open_sqlite(args.sqlite)
    if lite.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise Abort("SQLite integrity_check failed")
    with connect() as conn:
        print(f"Source: {args.sqlite}\nTarget: {where(conn)}{'  [DRY-RUN]' if args.dry_run else ''}")
        try:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))   # the app cannot write meanwhile
                if schema_version(conn) == 0:
                    if not args.dry_run:
                        raise Abort("No schema: run `python -m app.dbadmin migrate` first")
                    apply_schema(conn, app_role())                                    # dry-run: for the test only, rolled back
                plans = read_sqlite(lite, conn, args.orphans)
                if not pg_is_empty(conn, plans):
                    print("PG is not empty, comparing existing data...")
                    if compare(conn, plans):
                        print("Ma'lumot allaqachon ko'chirilgan va bir xil — hech narsa qilinmadi.")
                        raise _Rollback
                    if not args.replace:
                        raise Abort("PG data differs from SQLite. Use --replace to overwrite.")
                    for p in reversed(plans):
                        conn.execute(pgsql.SQL("DELETE FROM {}").format(pgsql.Identifier(p.name)))
                    print("--replace: existing rows deleted")
                for p in plans:
                    copy_in(conn, p)
                    fix_sequence(conn, p)
                print("Verification (row counts + sha256):")
                if not compare(conn, plans):
                    raise Abort("Imported data does not match — ROLLBACK")
                orphans = sum(p.orphans for p in plans)
                if orphans:
                    print(f"Orphan references set to NULL: {orphans}")
                if args.dry_run:
                    print("DRY-RUN: everything succeeded, changes rolled back (ROLLBACK).")
                    raise _Rollback
        except _Rollback:
            return 0
    print("Import finished (COMMIT).")
    return 0


def cmd_verify(args) -> int:
    lite = open_sqlite(args.sqlite)
    with connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        plans = read_sqlite(lite, conn, args.orphans)
        ok = compare(conn, plans)
    print("MOS" if ok else "FARQ BOR")
    return 0 if ok else 1


def cmd_export(args) -> int:
    """PG -> SQLite: the old file is copied (keeping its exact schema) and its tables are filled with PG data."""
    if os.path.exists(args.out):
        raise Abort(f"{args.out} already exists — not overwriting")
    open_sqlite(args.template).close()
    shutil.copyfile(args.template, args.out)
    lite = sqlite3.connect(args.out, isolation_level=None)
    lite.execute("PRAGMA foreign_keys=OFF")
    with connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        lite_tables = set(sqlite_tables(lite))
        counts = {t: conn.execute(pgsql.SQL("SELECT COUNT(*) FROM {}").format(pgsql.Identifier(t))).fetchone()[0]
                  for t in pg_tables(conn)}
        # Check every table first: no half-written export.
        for table, n_pg in counts.items():
            if table not in lite_tables and n_pg:
                raise Abort(f"{table}: missing from the old SQLite schema, but PG has {n_pg} rows")
        lite.execute("BEGIN")
        plans = []
        for table, n_pg in counts.items():
            if table not in lite_tables:
                continue
            cols = sqlite_columns(lite, table)
            missing = [c for c in pg_columns(conn, table) if c not in cols and (table, c) not in DROPPABLE_ON_EXPORT]
            if missing and n_pg:
                raise Abort(f"{table}: columns missing from the old schema: {', '.join(missing)}")
            q = pgsql.SQL("SELECT {} FROM {}").format(pgsql.SQL(", ").join(map(pgsql.Identifier, cols)), pgsql.Identifier(table))
            with conn.cursor() as cur:
                rows = [tuple(norm(v) for v in r) for r in cur.execute(q).fetchall()]
            lite.execute(f'DELETE FROM "{table}"')
            marks = ",".join("?" * len(cols))
            lite.executemany(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({marks})', rows)
            plans.append(TablePlan(table, cols, rows))
            if "id" in cols and pg_identity(conn, table):
                seq = conn.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,)).fetchone()[0]
                last = conn.execute(pgsql.SQL("SELECT last_value FROM {}").format(pgsql.SQL(seq))).fetchone()[0]
                lite.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (last, table))
        lite.execute("COMMIT")
        if lite.execute("PRAGMA foreign_key_check").fetchone() is not None:
            print("Warning: the export has rows violating FKs (PRAGMA foreign_key_check)")
        bad = [p.name for p in plans
               if digest(lite.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in p.columns)} FROM "{p.name}"')) != digest(p.rows)]
    lite.close()
    if bad:
        raise Abort(f"Export failed verification: {', '.join(bad)}")
    print(f"Export ready: {args.out} ({sum(len(p.rows) for p in plans)} rows, {len(plans)} tables, verified)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.dbadmin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    imp = sub.add_parser("import-sqlite")
    imp.add_argument("sqlite")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--replace", action="store_true")
    imp.add_argument("--orphans", choices=("fail", "null"), default="fail")
    ver = sub.add_parser("verify-sqlite")
    ver.add_argument("sqlite")
    ver.add_argument("--orphans", choices=("fail", "null"), default="fail")
    exp = sub.add_parser("export-sqlite")
    exp.add_argument("template")
    exp.add_argument("out")
    args = p.parse_args(argv)
    try:
        return {"migrate": cmd_migrate, "import-sqlite": cmd_import, "verify-sqlite": cmd_verify,
                "export-sqlite": cmd_export}[args.cmd](args)
    except Abort as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
