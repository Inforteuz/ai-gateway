"""PostgreSQL layer: psycopg 3 + a connection pool, write transactions serialised.

The code is written in the style of its SQLite days (`?` parameters, `row["column"]`,
`cur.lastrowid`, `db.tx()`); this layer adapts that to PostgreSQL:

* `?` and `:name` parameters are converted to psycopg's `%s` / `%(name)s`
  (text inside quotes and comments is left alone). Values are ALWAYS sent
  separately — queries are never built by concatenating strings into SQL.
* Rows behave like `sqlite3.Row`: `row["name"]`, `row[0]`, `dict(row)`.
* NUMERIC returned by `SUM()`/`AVG()` becomes int when integral, float
  otherwise; bool results are 0/1 (same as SQLite, so API responses do not change).
* `db.tx()` — `BEGIN` + `pg_advisory_xact_lock`: all write transactions run
  one at a time (the same guarantee as SQLite's `BEGIN IMMEDIATE` — the
  read-then-write sequences in billing rely on it). `db.one()`/`db.all()`
  inside a transaction read from the same connection, so they see
  writes that are not committed yet.

The app role cannot run DDL: the schema is created only by `python -m app.dbadmin migrate`
(with a separate migration role). On startup the app only checks the schema
version.
"""
from __future__ import annotations

import os
import contextvars
import re
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import sql as pgsql
from psycopg.adapt import Dumper, Loader
from psycopg.pq import Format
from psycopg_pool import ConnectionPool

from .config import DEFAULT_SETTINGS

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Bump when `schema.sql` or MIGRATIONS change. The app refuses to start when the
# database version is lower (better than failing silently on an old schema).
SCHEMA_VERSION = 4

# Schema changes: {version: [SQL, ...]}. Every statement must be idempotent
# (`ADD COLUMN IF NOT EXISTS` etc.) — add it to `schema.sql` too.
# `schema.sql` always runs FIRST: new tables are created there, while this
# holds columns added to existing tables and their indexes.
MIGRATIONS: dict[int, list[str]] = {
    # v2: referral program (referral.py). The new tables (referrals, referral_rewards) live in schema.sql.
    2: [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_code TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_paused BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_device TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ref_code ON users(ref_code) WHERE ref_code IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_users_signup_ip ON users(signup_ip) WHERE signup_ip IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_users_signup_device ON users(signup_device) WHERE signup_device IS NOT NULL",
        # Adds 'referral' to ledger.kind. We do not rely on the constraint name: every CHECK on `kind`
        # is dropped and a single one is re-added (if the old constraint survived, the reward
        # entry — and with it the deposit itself — would be rejected).
        """DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT conname FROM pg_constraint
                   WHERE conrelid = 'ledger'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) ILIKE '%kind%'
          LOOP
            EXECUTE format('ALTER TABLE ledger DROP CONSTRAINT %I', r.conname);
          END LOOP;
        END $$""",
        "ALTER TABLE ledger ADD CONSTRAINT ledger_kind_check"
        " CHECK (kind IN ('topup','usage','adjustment','refund','bonus','referral'))",
    ],
    # v3: admin_audit table (admin action log) — a new table, created in schema.sql.
    3: [],
    # v4: Telegram message of a top-up request (topup_telegram.py) — updated even when resolved in the panel.
    4: [
        "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS tg_chat_id TEXT",
        "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS tg_message_id BIGINT",
    ],
}

# Advisory lock key for the write queue ("SFDB"). Other processes on the same
# database (cli, dbadmin) queue on the same key.
WRITE_LOCK_KEY = 0x53464442

# Initial model created in an empty database.
# NOTE: its name and description must not reveal the provider/architecture. This row
# is restored with `ON CONFLICT DO NOTHING` on every service start, so a real model
# name left here would come back even after deletion and reveal the architecture
# in the user-visible list.
DEFAULT_MODEL = {
    "id": "model-1.0",
    "upstream_name": os.getenv("SF_DEFAULT_MODEL_UPSTREAM", "qwen3:14b"),
    "display_name": "Model 1.0",
    "description": "Tezkor va arzon: kundalik savollar, tarjima, qisqa matnlar.",
    # 10 000 UZS / 1M input tokens, 30 000 UZS / 1M output tokens
    "price_in_per_1m": 10_000 * 1_000_000,
    "price_out_per_1m": 30_000 * 1_000_000,
    "context_window": 16384,
}


def now() -> int:
    return int(time.time())


# -- SQL translation -----------------------------------------------------------
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@lru_cache(maxsize=4096)
def translate(sql: str) -> str:
    """`?` -> `%s`, `:name` -> `%(name)s`, `%` -> `%%`. Quoted text and `--` comments are left unchanged
    (only `%` is doubled — psycopg parses those too in a parameterised query)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:   # '' — escaped quote
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1].replace("%", "%%"))
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif ch == "?":
            out.append("%s")
            i += 1
        elif ch == ":" and i + 1 < n and sql[i + 1] != ":" and (i == 0 or sql[i - 1] != ":"):
            m = _NAME.match(sql, i + 1)
            if m:
                out.append(f"%({m.group()})s")
                i = m.end()
            else:
                out.append(ch)
                i += 1
        elif ch == "%":
            out.append("%%")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


# -- rows ----------------------------------------------------------------------
class Row(tuple):
    """Drop-in for `sqlite3.Row`: works with an index, a column name and `dict(row)`."""
    __slots__ = ()
    _idx: dict[str, int] = {}
    _names: tuple[str, ...] = ()

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                k = self._idx[k]
            except KeyError:
                raise IndexError(f"No item with that key: {k!r}") from None
        return tuple.__getitem__(self, k)

    def keys(self) -> list[str]:
        return list(self._names)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self))!r})"


@lru_cache(maxsize=1024)
def _row_class(names: tuple[str, ...]) -> type[Row]:
    idx: dict[str, int] = {}
    for i, name in enumerate(names):
        idx.setdefault(name, i)          # like sqlite3.Row: the first one wins on duplicate names
    return type("Row", (Row,), {"__slots__": (), "_idx": idx, "_names": names})


def _row_factory(cursor):
    cls = _row_class(tuple(c.name for c in cursor.description or ()))
    return lambda values: tuple.__new__(cls, values)


class _NumericLoader(Loader):
    """SUM(bigint) returns NUMERIC; load int (when integral) or float instead of Decimal."""

    def load(self, data):
        s = bytes(data).decode()
        try:
            return int(s)
        except ValueError:
            return float(s)


class _BoolLoader(Loader):
    def load(self, data):
        return 1 if bytes(data) == b"t" else 0


class _BoolAsIntDumper(Dumper):
    """Python True/False -> 0/1: flag columns are BIGINT (as in the SQLite days)."""
    format = Format.TEXT

    def __init__(self, cls, context=None):
        super().__init__(cls, context)
        self.oid = psycopg.adapters.types["int8"].oid

    def dump(self, obj):
        return b"1" if obj else b"0"


def configure_connection(conn: psycopg.Connection) -> None:
    conn.adapters.register_loader("numeric", _NumericLoader)
    conn.adapters.register_loader("bool", _BoolLoader)
    conn.adapters.register_dumper(bool, _BoolAsIntDumper)


class Cursor:
    """Results are fetched immediately, so it keeps working after the connection returns to the pool."""

    def __init__(self, cur: psycopg.Cursor, auto_id: bool = False):
        self.rowcount = cur.rowcount
        self._rows = cur.fetchall() if cur.description else []
        self.lastrowid = None
        if auto_id:
            self.lastrowid = self._rows[0][0] if self._rows else None
            self._rows = []
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self) -> list:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Conn:
    """`sqlite3.Connection.execute`-style wrapper (inside a transaction)."""

    def __init__(self, raw: psycopg.Connection, db: "Database"):
        self.raw = raw
        self._db = db

    def execute(self, sql: str, args: tuple | list | dict = ()) -> Cursor:
        auto_id = False
        m = _INSERT.match(sql)
        if m and m.group(1).lower() in self._db.serial_tables and not re.search(r"\bRETURNING\b", sql, re.I):
            # for sqlite3-style `cur.lastrowid`: return the new row's id.
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            auto_id = True
        cur = self.raw.execute(translate(sql), args) if args else self.raw.execute(sql)
        return Cursor(cur, auto_id)

    def executemany(self, sql: str, seq) -> None:
        with self.raw.cursor() as cur:
            cur.executemany(translate(sql), seq)


class _AutoConn:
    """Outside a transaction: each statement runs with autocommit on a pooled connection."""

    def __init__(self, db: "Database"):
        self._db = db

    def execute(self, sql: str, args: tuple | list | dict = ()) -> Cursor:
        with self._db.pool.connection() as raw:
            return Conn(raw, self._db).execute(sql, args)


# -- schema ----------------------------------------------------------------------
def schema_version(conn: psycopg.Connection) -> int:
    if conn.execute("SELECT to_regclass('schema_meta')").fetchone()[0] is None:
        return 0
    row = conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
    return row[0] if row else 0


def apply_schema(conn: psycopg.Connection, app_role: str | None = None) -> int:
    """Creates/updates the schema and grants the app role minimal privileges. Idempotent.

    Called with the migration role (the table owner); `conn` must be in autocommit mode.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
        conn.execute(SCHEMA)
        current = schema_version(conn)
        for version in sorted(MIGRATIONS):
            if version > current:
                for stmt in MIGRATIONS[version]:
                    conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta(id, version) VALUES (1, %s) "
                     "ON CONFLICT (id) DO UPDATE SET version=GREATEST(schema_meta.version, excluded.version)",
                     (SCHEMA_VERSION,))
        if app_role:
            grant_app_role(conn, app_role)
    return schema_version(conn)


# Privileges granted to the app role. Default: select/insert/update/delete.
# Financial history and accounts cannot be deleted, and the ledger cannot be updated —
# the database refuses this even under SQL injection or buggy code.
NO_DELETE = ("users", "wallets", "api_keys", "usage_logs", "ledger", "topup_requests",
             "referrals", "referral_rewards", "admin_audit")
# referrals: a link is set once; referral_rewards: the history of paid rewards never changes.
NO_UPDATE = ("ledger", "referrals", "referral_rewards", "admin_audit")
READ_ONLY = ("schema_meta",)


def grant_app_role(conn: psycopg.Connection, role: str) -> None:
    r = pgsql.Identifier(role)
    schema = pgsql.Identifier(conn.execute("SELECT current_schema()").fetchone()[0])
    q = lambda s, **kw: conn.execute(pgsql.SQL(s).format(r=r, s=schema, **kw))  # noqa: E731
    q("GRANT USAGE ON SCHEMA {s} TO {r}")
    q("REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {r}")
    q("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {r}")
    q("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {s} TO {r}")
    for t in NO_DELETE:
        q("REVOKE DELETE, TRUNCATE ON {t} FROM {r}", t=pgsql.Identifier(t))
    for t in NO_UPDATE:
        q("REVOKE UPDATE ON {t} FROM {r}", t=pgsql.Identifier(t))
    for t in READ_ONLY:
        q("REVOKE INSERT, UPDATE, DELETE ON {t} FROM {r}", t=pgsql.Identifier(t))


# -- database --------------------------------------------------------------------
class Database:
    def __init__(self, dsn: str, *, password: str | None = None, pool_min: int = 1, pool_max: int = 10,
                 auto_migrate: bool = False, app_role: str | None = None, options: str | None = None):
        kwargs: dict[str, Any] = {"autocommit": True, "row_factory": _row_factory,
                                  "application_name": "ai-gateway"}
        if password:
            kwargs["password"] = password
        if options:
            kwargs["options"] = options
        self.pool = ConnectionPool(dsn, kwargs=kwargs, min_size=pool_min, max_size=pool_max, timeout=30,
                                   configure=configure_connection, check=ConnectionPool.check_connection,
                                   name="ai-gateway", open=False)
        self.pool.open(wait=True, timeout=30)
        self._tx: contextvars.ContextVar[Conn | None] = contextvars.ContextVar(f"sf_tx_{id(self)}", default=None)
        # Writers must not hold pooled connections while queueing: take the in-process lock first.
        self._write_lock = threading.Lock()
        self.serial_tables: frozenset[str] = frozenset()
        try:
            self._init(auto_migrate, app_role)
        except BaseException:
            self.pool.close()
            raise

    def close(self) -> None:
        self.pool.close()

    def _init(self, auto_migrate: bool, app_role: str | None) -> None:
        with self.pool.connection() as raw:
            version = schema_version(raw)
            if version < SCHEMA_VERSION:
                if not auto_migrate:
                    raise RuntimeError(
                        f"Database schema is outdated (v{version}, need v{SCHEMA_VERSION}). "
                        "Run `python -m app.dbadmin migrate` with the migration role.")
                apply_schema(raw, app_role)
            self.serial_tables = frozenset(r[0] for r in raw.execute(
                "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema()"
                " AND column_name = 'id' AND (is_identity = 'YES' OR column_default LIKE 'nextval%')"))
        with self.tx() as c:
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (k, v))
            m = DEFAULT_MODEL
            # In Docker installs the installer creates the models (SF_SEED_DEFAULT_MODEL=0)
            if os.getenv("SF_SEED_DEFAULT_MODEL", "1") == "1":
                c.execute(
                    """INSERT INTO models(id, upstream_name, display_name, description,
                           price_in_per_1m, price_out_per_1m, context_window, enabled, created_at)
                       VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT (id) DO NOTHING""",
                    (m["id"], m["upstream_name"], m["display_name"], m["description"],
                     m["price_in_per_1m"], m["price_out_per_1m"], m["context_window"], now()),
                )
            # No request survives a restart: release all reserved amounts.
            c.execute("UPDATE wallets SET held = 0 WHERE held <> 0")

    # -- connection ----------------------------------------------------------
    def conn(self) -> Conn | _AutoConn:
        """Inside a transaction, its connection; outside, each statement separately (autocommit)."""
        return self._tx.get() or _AutoConn(self)

    # -- transactions ----------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[Conn]:
        """Write transaction: BEGIN + the write-queue lock. ROLLBACK on error. A nested call joins the outer one."""
        outer = self._tx.get()
        if outer is not None:
            yield outer
            return
        with self._write_lock, self.pool.connection() as raw, raw.transaction():
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
            c = Conn(raw, self)
            token = self._tx.set(c)
            try:
                yield c
            finally:
                self._tx.reset(token)

    # -- convenience helpers ---------------------------------------------------
    def one(self, sql: str, args: tuple | list | dict = ()) -> Row | None:
        return self.conn().execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple | list | dict = ()) -> list[Row]:
        return self.conn().execute(sql, args).fetchall()

    def scalar(self, sql: str, args: tuple | list | dict = (), default: Any = 0) -> Any:
        row = self.conn().execute(sql, args).fetchone()
        return default if row is None or row[0] is None else row[0]

    # -- settings --------------------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.all("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def setting_int(self, key: str) -> int:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return int(row["value"]) if row else int(DEFAULT_SETTINGS[key])


def from_config(config) -> Database:
    return Database(config.database_url, password=config.database_password, pool_max=config.db_pool_max,
                    auto_migrate=config.db_auto_migrate, app_role=config.db_app_role or None)
