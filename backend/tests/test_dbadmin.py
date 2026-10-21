"""SQLite -> PostgreSQL migration script (app.dbadmin): dry-run, idempotency, orphans, sequences, export back."""
import secrets
import sqlite3
from pathlib import Path

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo

from app import dbadmin
from app.common import create_user
from app.db import Database

from .conftest import TEST_APP_ROLE

LEGACY = (Path(__file__).parent / "data" / "sqlite_schema_legacy.sql").read_text(encoding="utf-8")


@pytest.fixture
def target(pg_dsn, monkeypatch):
    """Empty PG schema (no tables); dbadmin connects to it as the owner (migration role)."""
    name = "m_" + secrets.token_hex(6)
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(pgsql.SQL("CREATE SCHEMA {}").format(pgsql.Identifier(name)))
    dsn = make_conninfo(pg_dsn, options=f"-c search_path={name},public")
    monkeypatch.setenv("SF_MIGRATE_DATABASE_URL", dsn)
    monkeypatch.delenv("SF_MIGRATE_DATABASE_PASSWORD", raising=False)
    monkeypatch.setenv("SF_DB_APP_ROLE", TEST_APP_ROLE)
    yield type("T", (), {"dsn": dsn, "app_dsn": make_conninfo(pg_dsn, options=f"-c search_path={name},public -c role={TEST_APP_ROLE}")})
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(pgsql.SQL("DROP SCHEMA {} CASCADE").format(pgsql.Identifier(name)))


@pytest.fixture
def legacy_db(tmp_path):
    """A production-like SQLite file: AUTOINCREMENT gaps, logs pointing at a deleted key, special characters."""
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.executescript(LEGACY)
    c.executescript("""
        INSERT INTO users(id, email, name, password_hash, role, created_at, signup_ip) VALUES
          (1, 'admin@test.uz', 'Admin', 'h1', 'admin', 100, '1.1.1.1'), (2, 'ali@test.uz', 'Ali "Vali" O''g''li', 'h2', 'user', 200, NULL),
          (3, 'del@test.uz', 'O''chirilgan', 'h3', 'user', 300, NULL);
        DELETE FROM users WHERE id=3;
        INSERT INTO wallets VALUES (1, 5000000, 0, 1), (2, 0, 1200, 2);
        INSERT INTO api_keys(id, user_id, name, prefix, last4, key_hash, free, created_at) VALUES
          (1, 1, 'k1', 'sk-sf-a', 'aaaa', 'hash1', 1, 10), (2, 2, 'k2', 'sk-sf-b', 'bbbb', 'hash2', 0, 20), (3, 2, 'k3', 'sk-sf-c', 'cccc', 'hash3', 0, 30);
        INSERT INTO usage_logs(id, request_id, user_id, key_id, model, endpoint, cost, status_code, latency_ms, error, created_at) VALUES
          (1, 'r1', 2, 2, 'model-1.0', '/v1/chat/completions', 700, 200, 12, NULL, 1000),
          (2, 'r2', 2, 3, 'model-1.0', '/v1/chat/completions', 0, 500, NULL, 'boom 100% ?', 1001),
          (5, 'r5', 2, 3, 'model-1.0', '/v1/chat/completions', 0, 429, NULL, NULL, 1002);
        INSERT INTO ledger(id, user_id, kind, amount, balance_after, usage_id, note, admin_id, created_at) VALUES
          (1, 1, 'topup', 5000000, 5000000, NULL, 'naqd', 1, 5), (2, 2, 'usage', -700, -700, 1, '', NULL, 1000);
        INSERT INTO settings VALUES ('payment_info', 'Karta: 8600 **** 1234 — 100% "tez"'), ('free_mode', '0');
        INSERT INTO health_checks VALUES (1, 'up', 5, NULL), (1, 'up', 5, NULL), (2, 'tunnel_down', NULL, 'x');
        INSERT INTO samples(position, category, prompt, fast_model, fast_answer, pro_model, pro_answer, created_at)
          VALUES (1, 'kod', 'satr1
satr2', 'a', 'javob', 'b', 'javob2', 9);
        INSERT INTO model_health VALUES ('deepseek', 'up', 1, 2, NULL);
        INSERT INTO benchmark_results(model, kategoriya, jami, togri, o_rtacha_ms, yangilandi) VALUES ('m', 'k', 10, 7, 300, 1);
    """)
    c.execute("PRAGMA foreign_keys=OFF")   # as happened in production: the key was deleted over a connection without FK checks
    c.execute("DELETE FROM api_keys WHERE id=3")
    c.commit()
    c.close()
    return str(path)


def run(*argv):
    return dbadmin.main(list(argv))


def test_dry_run_changes_nothing_and_orphans_need_decision(target, legacy_db, capsys):
    assert run("import-sqlite", legacy_db, "--dry-run") == 2
    assert "yetim" in capsys.readouterr().err
    assert run("import-sqlite", legacy_db, "--dry-run", "--orphans", "null") == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "FARQ" not in out and "usage_logs" in out
    with psycopg.connect(target.dsn) as c:                      # not even the schema was created (DDL is rolled back too)
        assert c.execute("SELECT to_regclass('users')").fetchone()[0] is None


def test_import_verify_idempotent_and_sequences(target, legacy_db, capsys):
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 2      # a real import stops when the schema is missing
    assert run("migrate") == 0
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 0
    assert run("verify-sqlite", legacy_db, "--orphans", "null") == 0
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 0      # again — nothing to do
    assert "allaqachon" in capsys.readouterr().out

    db = Database(target.app_dsn)                                         # as the app role
    try:
        assert db.scalar("SELECT COUNT(*) FROM usage_logs") == 3
        assert [r["key_id"] for r in db.all("SELECT key_id FROM usage_logs ORDER BY id")] == [2, None, None]
        assert db.one("SELECT name FROM users WHERE id=2")["name"] == 'Ali "Vali" O\'g\'li'
        assert db.scalar("SELECT COUNT(*) FROM health_checks") == 3        # duplicate rows without a PK too
        assert create_user(db, email="new@test.uz", name="N", password="parol-12345") == 4   # sqlite_sequence=3 was honoured
        with db.tx() as c:
            assert c.execute("INSERT INTO usage_logs(request_id, user_id, model, endpoint, status_code, created_at)"
                             " VALUES ('r6', 4, 'm', '/v1', 200, 1)").lastrowid == 6
    finally:
        db.close()

    assert run("verify-sqlite", legacy_db, "--orphans", "null") == 1      # PG now has new rows
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 2      # there is a diff — nothing is written without --replace
    assert "--replace" in capsys.readouterr().err


def test_export_roundtrip_for_rollback(target, legacy_db, tmp_path, capsys):
    assert run("migrate") == 0
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 0
    db = Database(target.app_dsn)
    try:
        uid = create_user(db, email="later@test.uz", name="L", password="parol-12345")
    finally:
        db.close()
    out = str(tmp_path / "rollback.db")
    assert run("export-sqlite", legacy_db, out) == 0
    assert run("export-sqlite", legacy_db, out) == 2                      # an existing file is not overwritten
    c = sqlite3.connect(out)
    assert c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone() == ("later@test.uz",)
    assert c.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()[0] >= uid
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    assert run("verify-sqlite", out, "--orphans", "null") == 0           # the export is identical to PG


def test_export_refuses_when_referral_links_exist(target, legacy_db, tmp_path, capsys):
    """The referral code (users.ref_code) does not fit the old schema and is dropped, but referral links must not be lost."""
    assert run("migrate") == 0
    assert run("import-sqlite", legacy_db, "--orphans", "null") == 0
    db = Database(target.app_dsn)
    try:
        a = create_user(db, email="ref-a@test.uz", name="A", password="parol-12345")
        code = db.scalar("SELECT ref_code FROM users WHERE id=?", (a,))
        create_user(db, email="ref-b@test.uz", name="B", password="parol-12345", referral_code=code)
    finally:
        db.close()
    assert run("export-sqlite", legacy_db, str(tmp_path / "rollback.db")) == 2
    assert "referrals" in capsys.readouterr().err
