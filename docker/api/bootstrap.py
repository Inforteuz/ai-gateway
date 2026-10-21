"""One-shot database bootstrap, run by the `migrate` service before the API starts.

Connects as the migrator (owner) role and:
  1. waits until PostgreSQL accepts connections;
  2. makes sure the `citext` extension exists;
  3. creates/updates the restricted application login role and its password;
  4. applies the schema and grants (same code as `python -m app.dbadmin migrate`).

Idempotent: safe to run on every `docker compose up`.
"""
from __future__ import annotations

import os
import sys
import time

import psycopg
from psycopg import sql

from app.db import apply_schema, configure_connection, schema_version


def env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        sys.exit(f"bootstrap: {name} is not set")
    return value


def connect(dsn: str, password: str, attempts: int = 60) -> psycopg.Connection:
    for i in range(attempts):
        try:
            return psycopg.connect(dsn, password=password, autocommit=True, application_name="gateway-bootstrap")
        except psycopg.OperationalError as e:
            if i == attempts - 1:
                raise
            print(f"bootstrap: waiting for database ({type(e).__name__})", flush=True)
            time.sleep(2)
    raise RuntimeError("unreachable")


def main() -> int:
    conn = connect(env("SF_MIGRATE_DATABASE_URL"), env("SF_MIGRATE_DATABASE_PASSWORD"))
    role = env("SF_DB_APP_ROLE")
    app_password = env("SF_DATABASE_PASSWORD")

    conn.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(role)))
        print(f"bootstrap: created role {role}", flush=True)
    # Keep the password in sync with .env (rotating it in .env + restart is enough)
    conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(app_password)))

    configure_connection(conn)
    conn.execute("SET statement_timeout = 0")
    before = schema_version(conn)
    after = apply_schema(conn, role)
    print(f"bootstrap: schema v{before} -> v{after}, grants for {role} updated", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
