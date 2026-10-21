"""Setup commands used by install.sh / manage.sh: python -m app.setup_cli <command>

  set-setting KEY [VALUE]        stores an admin setting; with no VALUE it is read from stdin
                                 (use that for secrets so they never appear in argv or shell history).
                                 An empty value from stdin keeps an already stored secret.
  get-setting KEY                prints a non-secret setting (secrets are refused)
  model-ids                      prints the existing model ids, one per line
  add-model --id ID --upstream NAME --provider local|cluster|external [options]
                                 creates or updates a public model
  list-models                    prints the model table (no secrets)
"""
from __future__ import annotations

import argparse
import sys

from . import billing
from .config import DEFAULT_SETTINGS, Config
from .db import from_config, now

PROVIDERS = ("local", "cluster", "external")
CLUSTER_CHANNELS = ("auto", "chatgpt", "deepseek", "qwen", "gemini", "genspark", "deepai", "zai", "grok",
                    "chatbotapp", "chatbotchatapp")


def _price(value: float) -> int:
    """Price per 1M tokens in currency units -> stored micro-units."""
    return int(round(value * billing.MICRO))


def is_secret(key: str) -> bool:
    return any(w in key for w in ("key", "secret", "token", "password"))


def cmd_set_setting(db, args) -> int:
    if args.key not in DEFAULT_SETTINGS:
        print(f"unknown setting: {args.key}", file=sys.stderr)
        return 2
    from_stdin = args.value is None
    value = sys.stdin.read().strip() if from_stdin else args.value
    if from_stdin and not value and is_secret(args.key) and db.get_settings().get(args.key):
        print(f"setting {args.key} kept (empty input)")
        return 0
    db.set_setting(args.key, value)
    print(f"setting {args.key} saved")
    return 0


def cmd_get_setting(db, args) -> int:
    if args.key not in DEFAULT_SETTINGS or is_secret(args.key):
        print(f"not readable: {args.key}", file=sys.stderr)
        return 2
    print(db.get_settings().get(args.key, DEFAULT_SETTINGS[args.key]))
    return 0


def cmd_model_ids(db, args) -> int:
    for r in db.all("SELECT id FROM models ORDER BY id"):
        print(r["id"])
    return 0


def cmd_add_model(db, args) -> int:
    if args.provider == "cluster" and args.upstream.lower() not in CLUSTER_CHANNELS:
        print(f"unknown cluster channel: {args.upstream} (known: {', '.join(CLUSTER_CHANNELS)})", file=sys.stderr)
        return 2
    row = (args.upstream, args.display or args.id, args.description, _price(args.price_in), _price(args.price_out),
           args.context, int(not args.disabled), args.provider, int(not args.unlisted), int(not args.no_thinking))
    with db.tx() as c:
        exists = c.execute("SELECT 1 FROM models WHERE id=?", (args.id,)).fetchone()
        if exists:
            c.execute("""UPDATE models SET upstream_name=?, display_name=?, description=?, price_in_per_1m=?,
                         price_out_per_1m=?, context_window=?, enabled=?, provider=?, listed=?, allow_thinking=?
                         WHERE id=?""", (*row, args.id))
        else:
            c.execute("""INSERT INTO models(upstream_name, display_name, description, price_in_per_1m, price_out_per_1m,
                         context_window, enabled, provider, listed, allow_thinking, id, created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (*row, args.id, now()))
    print(f"model {args.id} {'updated' if exists else 'created'} ({args.provider}:{args.upstream})")
    return 0


def cmd_list_models(db, args) -> int:
    rows = db.all("SELECT id, provider, upstream_name, enabled, listed, price_in_per_1m, price_out_per_1m "
                  "FROM models ORDER BY id")
    for r in rows:
        print(f"{r['id']:<24} {r['provider']:<9} {r['upstream_name']:<28} enabled={r['enabled']} listed={r['listed']} "
              f"in={r['price_in_per_1m'] / billing.MICRO:g} out={r['price_out_per_1m'] / billing.MICRO:g}")
    if not rows:
        print("(no models)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.setup_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set-setting")
    s.add_argument("key")
    s.add_argument("value", nargs="?")

    m = sub.add_parser("add-model")
    m.add_argument("--id", required=True, help="public model name clients use")
    m.add_argument("--upstream", required=True, help="Ollama model, cluster channel or external model name")
    m.add_argument("--provider", required=True, choices=PROVIDERS)
    m.add_argument("--display", help="display name (default: the id)")
    m.add_argument("--description", default="")
    m.add_argument("--price-in", type=float, default=0.0, help="price per 1M input tokens")
    m.add_argument("--price-out", type=float, default=0.0, help="price per 1M output tokens")
    m.add_argument("--context", type=int, default=32768, help="context window in tokens")
    m.add_argument("--unlisted", action="store_true")
    m.add_argument("--disabled", action="store_true")
    m.add_argument("--no-thinking", action="store_true", help="never enable reasoning for this model")

    sub.add_parser("list-models")
    g = sub.add_parser("get-setting")
    g.add_argument("key")
    sub.add_parser("model-ids")

    args = p.parse_args(argv)
    db = from_config(Config())
    try:
        return {"set-setting": cmd_set_setting, "get-setting": cmd_get_setting, "add-model": cmd_add_model,
                "list-models": cmd_list_models, "model-ids": cmd_model_ids}[args.cmd](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
