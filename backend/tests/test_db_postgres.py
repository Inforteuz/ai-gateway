"""PostgreSQL layer: SQL translation, row type, transactions, schema and app-role privileges."""
import threading

import psycopg
import pytest

from app import billing
from app.db import SCHEMA_VERSION, Database, Row, apply_schema, translate

from .conftest import TEST_APP_ROLE, add_user


# --- SQL translation ------------------------------------------------------------
@pytest.mark.parametrize("src, out", [
    ("SELECT * FROM t WHERE a=? AND b=?", "SELECT * FROM t WHERE a=%s AND b=%s"),
    ("WHERE email ILIKE :q OR name ILIKE :q", "WHERE email ILIKE %(q)s OR name ILIKE %(q)s"),
    ("SELECT '?', 'a''?:x', \"q?\" FROM t WHERE x=?", "SELECT '?', 'a''?:x', \"q?\" FROM t WHERE x=%s"),
    ("SELECT x::text, '%' FROM t WHERE y LIKE ?", "SELECT x::text, '%%' FROM t WHERE y LIKE %s"),
    ("SELECT 1 -- izoh? :yo'q\nFROM t WHERE a=?", "SELECT 1 -- izoh? :yo'q\nFROM t WHERE a=%s"),
    ("SELECT to_char(x, 'HH24:MI') WHERE a=?", "SELECT to_char(x, 'HH24:MI') WHERE a=%s"),
])
def test_translate_placeholders(src, out):
    assert translate(src) == out


def test_parameters_are_never_interpolated(env):
    """Values are never spliced into the SQL text: an injection attempt stays a plain string."""
    evil = "x' OR '1'='1"
    assert env.db.one("SELECT ? AS v", (evil,))["v"] == evil
    assert env.db.one("SELECT COUNT(*) n FROM users WHERE email=?", (evil,))["n"] == 0
    assert env.db.all("SELECT id FROM users WHERE email ILIKE :q", {"q": "%' OR 1=1 --"}) == []


def test_row_behaves_like_sqlite_row(env):
    r = env.db.one("SELECT 1 AS a, 'x' AS b, 2 AS a")
    assert isinstance(r, Row) and r["a"] == 1 and r[2] == 2 and r["b"] == "x"
    assert dict(r) == {"a": 1, "b": "x"} and list(r.keys()) == ["a", "b", "a"]
    a, b, _ = r
    assert (a, b) == (1, "x")
    with pytest.raises(IndexError):
        r["yoq"]


def test_numeric_and_bool_results_match_sqlite(env):
    r = env.db.one("SELECT SUM(x) s, AVG(x) a, 1=1 t, 1=2 f FROM (VALUES (1::bigint), (2::bigint)) v(x)")
    assert (r["s"], type(r["s"]), r["a"], r["t"], r["f"]) == (3, int, 1.5, 1, 0)
    assert env.db.scalar("SELECT ?::bigint", (True,)) == 1          # Python bool -> 0/1 (flag columns are BIGINT)


# --- transactions ---------------------------------------------------------------
def test_lastrowid_rollback_and_nested_tx(env):
    uid = add_user(env)
    with env.db.tx() as c:
        kid = c.execute("INSERT INTO api_keys(user_id, name, prefix, last4, key_hash, created_at) VALUES (?,?,?,?,?,1)",
                        (uid, "a", "p", "1234", "h1")).lastrowid
        assert isinstance(kid, int) and env.db.one("SELECT id FROM api_keys WHERE id=?", (kid,))  # a read inside the tx sees it
        with env.db.tx() as inner:                                                                # nested — the same transaction
            assert inner is c
    with pytest.raises(RuntimeError):
        with env.db.tx() as c:
            c.execute("UPDATE api_keys SET name='b' WHERE id=?", (kid,))
            raise RuntimeError("bekor")
    assert env.db.one("SELECT name FROM api_keys WHERE id=?", (kid,))["name"] == "a"


def test_concurrent_credits_are_serialized(env):
    """credit_tx is read-modify-write (SELECT balance -> UPDATE): without the lock updates would be lost."""
    uid = add_user(env)
    errors = []

    def worker():
        try:
            for _ in range(5):
                billing.credit(env.db, user_id=uid, amount=1, kind="adjustment")
        except Exception as e:  # pragma: no cover - an error fails the test
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert env.db.scalar("SELECT balance FROM wallets WHERE user_id=?", (uid,)) == 40
    after = [r["balance_after"] for r in env.db.all("SELECT balance_after FROM ledger WHERE user_id=? ORDER BY id", (uid,))]
    assert after == list(range(1, 41))


# --- schema and privileges --------------------------------------------------------
def _app_conn(env):
    return psycopg.connect(env.cfg.database_url, autocommit=True)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE hack(x int)",
    "ALTER TABLE users ADD COLUMN hack int",
    "DROP TABLE settings",
    "UPDATE ledger SET amount = 0",
    "DELETE FROM ledger",
    "DELETE FROM users",
    "DELETE FROM usage_logs",
    "TRUNCATE sessions",
    "UPDATE schema_meta SET version = 0",
])
def test_app_role_is_least_privilege(env, sql):
    with _app_conn(env) as c:
        assert c.execute("SELECT current_user").fetchone()[0] == TEST_APP_ROLE
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(sql)


def test_app_role_can_do_its_job(env):
    with _app_conn(env) as c:
        c.execute("DELETE FROM sessions")
        c.execute("DELETE FROM health_checks")
        c.execute("UPDATE wallets SET held = 0")
        c.execute("SELECT * FROM schema_meta")


def test_app_refuses_unmigrated_database(env):
    """The app refuses to start on a database without the schema (it never runs DDL itself)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    opts = conninfo_to_dict(env.cfg.database_url)["options"].replace("search_path=", "search_path=missing_schema_")
    with pytest.raises(RuntimeError, match="app.dbadmin migrate"):
        Database(make_conninfo(env.cfg.database_url, options=opts))


def test_apply_schema_is_idempotent(env, _pg_session_schema):
    _, owner, _, _ = _pg_session_schema
    assert apply_schema(owner, TEST_APP_ROLE) == SCHEMA_VERSION
    assert apply_schema(owner, TEST_APP_ROLE) == SCHEMA_VERSION
    assert env.db.scalar("SELECT version FROM schema_meta") == SCHEMA_VERSION
