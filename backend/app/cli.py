"""Management commands (run on the server): python -m app.cli <command>

  create-admin     --email E [--name N] [--password P]      creates an administrator (random password if omitted)
  set-password     --email E [--password P]                 changes a password
  import-key       --conf NGINX.conf --email E [--name N]   moves the single key from a legacy nginx config into the DB (free key)
"""
from __future__ import annotations

import argparse
import re
import sys

from . import security
from .common import ApiError, create_user
from .config import Config
from .db import from_config, now


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    a.add_argument("--password")

    s = sub.add_parser("set-password")
    s.add_argument("--email", required=True)
    s.add_argument("--password")

    k = sub.add_parser("import-key")
    k.add_argument("--conf", required=True)
    k.add_argument("--email", required=True)
    k.add_argument("--name", default="Eski kalit (nginx)")

    args = p.parse_args(argv)
    db = from_config(Config())

    if args.cmd == "create-admin":
        password = args.password or security.new_temp_password()
        try:
            create_user(db, email=args.email, name=args.name, password=password, role="admin")
        except ApiError as e:
            print(f"Error: {e.message}", file=sys.stderr)
            return 1
        print(f"Administrator created: {args.email.lower()}")
        if not args.password:
            print(f"Password: {password}")
        return 0

    if args.cmd == "set-password":
        password = args.password or security.new_temp_password()
        row = db.one("SELECT id FROM users WHERE email=?", (args.email.lower(),))
        if row is None:
            print("Error: user not found", file=sys.stderr)
            return 1
        with db.tx() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(password), row["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        print("Password changed.")
        if not args.password:
            print(f"Password: {password}")
        return 0

    if args.cmd == "import-key":
        text = open(args.conf, encoding="utf-8").read()
        m = re.search(r'Bearer\s+([^"\s]+)"', text)
        if not m:
            print("Error: 'Bearer <key>' not found in the config", file=sys.stderr)
            return 1
        raw = m.group(1)
        user = db.one("SELECT id FROM users WHERE email=?", (args.email.lower(),))
        if user is None:
            print("Error: user not found", file=sys.stderr)
            return 1
        digest = security.sha256_hex(raw)
        if db.one("SELECT 1 FROM api_keys WHERE key_hash=?", (digest,)):
            print("This key is already in the database.")
            return 0
        with db.tx() as c:
            c.execute(
                """INSERT INTO api_keys(user_id, name, prefix, last4, key_hash, free, rpm_limit, created_at)
                   VALUES (?,?,?,?,?,1,120,?)""",
                (user["id"], args.name, raw[:6] + "…", raw[-4:], digest, now()),
            )
        print("Legacy key imported (free, 120 requests per minute). The key itself was not printed.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
