"""Cluster model monitoring, alerting and public status.

`monitor.py` only watches the ollama gateway. Cluster providers (browser-based
models) can fail on their own: if a browser hangs or a provider hits its limit,
the `model-*` model backed by it stops working, yet no alert fires because
ollama is healthy.

Every `interval` seconds this module reads the cluster status and:
  1. keeps an up/down state per upstream model in the `model_health`
     table (N consecutive failures count as an outage);
  2. sends a Telegram message when the state changes;
  3. also checks the proxy watchdog: a proxy swap or all proxies being
     down is reported as well;
  4. prepares the public status for `/portal/status` — the frontend uses it
     to show users a "temporary outage on this model" banner.

Privacy: the public status shows only `model-*` names. Upstream provider
names, ports and IPs never leave the server.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi.concurrency import run_in_threadpool

from . import audit, monitor
from .db import Database, now

log = logging.getLogger("cluster_watch")

# An outage is declared after this many consecutive failed checks.
# A single check can fail by chance (e.g. busy during a scan).
DOWN_THRESHOLD = 3

# Minimum pause (seconds) before the same message is sent again.
MESSAGE_COOLDOWN = 1800

# No repeat messages while the state is unchanged.
_state: dict[str, dict[str, Any]] = {}


# The `model_health` table lives in `schema.sql` (the app role cannot run DDL).


def _public_names(db: Database, upstream: str) -> list[str]:
    """Public model IDs backed by the given upstream provider name."""
    rows = db.all(
        "SELECT id FROM models WHERE provider='cluster' AND upstream_name=? AND enabled=1 AND listed=1",
        (upstream,),
    )
    return [r["id"] for r in rows]


# Memory guard (cluster `memory_guard`) events. Automatic recoveries are written
# to the admin log under this name; these kinds require a Telegram alert.
AUTO_ADMIN = {"id": None, "email": "avto-nazorat"}
MEMORY_ALERT_KINDS = ("session_lost", "reopen_failed")
_STARTED = time.time()


def _record_memory_event(db: Database, ev: dict) -> bool:
    """Writes an automatic event to the log once. `True` means it was newly written.

    The cluster keeps events in memory and sends them again after an API restart —
    the `[#id]` marker prevents duplicate entries.
    """
    target, marker = f"instance:{int(ev['instance'])}", f"[#{int(ev['id'])}]"
    if db.one("SELECT 1 AS x FROM admin_audit WHERE target=? AND detail LIKE ?", (target, f"%{marker}")):
        return False
    audit.record(db, admin=AUTO_ADMIN, action=f"memory_{ev.get('kind') or 'event'}", target=target,
                 ok=bool(ev.get("ok")), detail=f"{str(ev.get('summary') or '')[:400]} {marker}")
    return True


async def _memory_events(app, db: Database, base: str, headers: dict) -> list[str]:
    h = _state.setdefault("_memory", {"after": 0})
    r = await app.state.cluster_http.get(f"{base}/cluster/memory", params={"after": h["after"]},
                                         headers=headers, timeout=20.0)
    if r.status_code != 200:
        return []
    messages: list[str] = []
    for ev in r.json().get("events") or []:
        h["after"] = max(h["after"], int(ev["id"]))
        if ev.get("auto"):
            is_new = await run_in_threadpool(_record_memory_event, db, ev)
        else:
            # A manual recovery was already logged by the admin route; only alert here
            is_new = ev.get("ts", 0) >= _STARTED
        if is_new and ev.get("kind") in MEMORY_ALERT_KINDS:
            messages.append(
                f"DIQQAT: instansiya #{ev.get('instance')} ({ev.get('model') or '?'}) — xotira tiklashi:\n"
                f"{ev.get('summary') or ''}\n\n"
                "Shu instansiyada avto-tiklash to'xtatildi. VNC orqali tekshiring; sessiya joyida bo'lsa, "
                "admin paneldagi \"Tabni tiklash\" tugmasi blokni olib tashlaydi."
            )
    return messages


# Distinguishes "no change" from "never seen before". Both used to be None,
# so the caller could not tell them apart and re-sent the message on every
# check (one Telegram message per minute).
UNCHANGED = object()


def _store_state(db: Database, upstream: str, state: str, detail: str | None):
    """Saves the state.

    Returns:
      `UNCHANGED`     — same state as before (no message is sent),
      `None`          — this upstream is seen for the first time,
      previous state  — a real change happened.
    """
    ts = int(time.time())
    row = db.one("SELECT state, since FROM model_health WHERE upstream=?", (upstream,))
    previous = row["state"] if row else None
    with db.tx() as c:
        if previous == state:
            c.execute("UPDATE model_health SET last_check=?, detail=? WHERE upstream=?",
                      (ts, detail, upstream))
        else:
            c.execute(
                "INSERT INTO model_health(upstream, state, since, last_check, detail) "
                "VALUES (?,?,?,?,?) ON CONFLICT(upstream) DO UPDATE SET "
                "state=excluded.state, since=excluded.since, last_check=excluded.last_check, "
                "detail=excluded.detail",
                (upstream, state, ts, ts, detail),
            )
    if previous == state:
        return UNCHANGED
    return previous


async def _check(app) -> None:
    db: Database = app.state.db
    s = await run_in_threadpool(db.get_settings)
    base = (s.get("cluster_base_url") or "").rstrip("/")
    if not base:
        return
    # The setting stores the OpenAI path (".../v1") because the gateway appends
    # `/chat/completions` to it. The management endpoints live at the root,
    # so the `/v1` suffix is stripped.
    if base.endswith("/v1"):
        base = base[:-3]
    headers = {"User-Agent": "ai-gateway/1.0"}
    if s.get("cluster_api_key"):
        headers["Authorization"] = f"Bearer {s['cluster_api_key']}"

    try:
        r = await app.state.cluster_http.get(f"{base}/cluster/status", headers=headers, timeout=20.0)
        r.raise_for_status()
        health = r.json().get("health", {})
    except Exception as e:
        log.warning("could not read cluster status: %s", type(e).__name__)
        return

    by_model: dict[str, dict] = health.get("by_model", {})
    # Upstreams that need operator action (login/quota). They are counted in
    # `total` but cannot serve requests — qwen once sat in this state for 20+
    # minutes while the table said "up" and no alert went out.
    attention = {x.get("model") for x in (health.get("needs_attention") or [])}
    messages: list[str] = []

    for upstream, v in sorted(by_model.items()):
        # `available` also reports a BUSY worker as 0: a worker serving a request
        # or briefly idle is NOT "down". A real outage is the worker being gone
        # entirely (`total == 0`). Otherwise heavy load would trigger a flood
        # of false "model is down" alerts.
        total = int(v.get("total", 0))
        working = total > 0 and not (upstream in attention and int(v.get("available", 0)) == 0)
        h = _state.setdefault(upstream, {"streak": 0})
        if working:
            h["streak"] = 0
            previous = await run_in_threadpool(_store_state, db, upstream, "up", None)
            if previous == "down":
                names = await run_in_threadpool(_public_names, db, upstream)
                if names:
                    messages.append(f"Model qayta ishladi: {', '.join(names)}")
        else:
            h["streak"] += 1
            if h["streak"] < DOWN_THRESHOLD:
                continue
            previous = await run_in_threadpool(
                _store_state, db, upstream, "down", "worker yo'q" if total == 0 else "operator aralashuvi kerak")
            if previous is UNCHANGED:
                continue  # already reported — do not repeat
            names = await run_in_threadpool(_public_names, db, upstream)
            if names:
                messages.append(
                    f"Model ishlamayapti: {', '.join(names)}\n"
                    f"Sabab: provayder vaqtincha javob bermayapti"
                )

    # Self-heal — page a human only when recovery FAILED.
    # Successful automatic recovery is not reported: it is normal operation
    # and can happen several times a day.
    try:
        hr = await app.state.cluster_http.get(f"{base}/cluster/self-heal", headers=headers, timeout=20.0)
        if hr.status_code == 200:
            hd = hr.json()
            stuck = sorted(hd.get("qolga_muhtoj") or [])
            heal = _state.setdefault("_heal", {"stuck": []})
            if stuck and stuck != heal["stuck"]:
                reasons = {
                    x["instansiya"]: (x.get("oxirgi_xato") or "noma'lum")
                    for x in hd.get("instansiyalar", []) if x.get("qolga_muhtoj")
                }
                lines = "\n".join(f"  - instansiya {i}: {reasons.get(i, '')}" for i in stuck)
                messages.append(
                    "DIQQAT: instansiya o'zi tiklana olmadi — qo'lda aralashuv kerak\n"
                    f"{lines}\n\n"
                    "VNC orqali kirib, brauzerni tekshiring (sessiya tugagan bo'lishi mumkin)."
                )
            elif not stuck and heal["stuck"]:
                messages.append("Barcha instansiyalar yana normal ishlayapti.")
            heal["stuck"] = stuck
    except Exception:
        pass

    # Memory guard: automatic recoveries go to the log, lost sessions to Telegram
    try:
        messages.extend(await _memory_events(app, db, base, headers))
    except Exception as e:
        log.warning("could not read memory events: %s", type(e).__name__)

    # Proxy watchdog — report a proxy swap or a complete outage
    try:
        pr = await app.state.cluster_http.get(f"{base}/cluster/proxy-health", headers=headers, timeout=20.0)
        if pr.status_code == 200:
            pd = pr.json()
            swapped = pd.get("almashtirildi") or []
            total, working = pd.get("jami", 0), pd.get("ishlayapti", 0)
            if swapped:
                messages.append(f"Proxy almashtirildi: {len(swapped)} ta instansiya")
            if total and working == 0:
                messages.append("DIQQAT: hamma proxy ishlamayapti — to'g'ridan-to'g'ri rejimga o'tildi")
    except Exception:
        pass

    # Anti-flap: a message with the same content is not repeated within `MESSAGE_COOLDOWN`.
    # When the state flaps (e.g. under heavy load) the operator must not be
    # buried in Telegram messages.
    t_now = time.time()
    last_sent = _state.setdefault("_messages", {})
    to_send = []
    for text in messages:
        if t_now - last_sent.get(text, 0) < MESSAGE_COOLDOWN:
            continue
        last_sent[text] = t_now
        to_send.append(text)

    for text in to_send:
        ok, detail = await monitor.send_telegram(app, text)
        if not ok and detail != "Telegram sozlanmagan":
            log.warning("cluster notification not sent: %s", detail)
        log.info("cluster event: %s", text.replace("\n", " | "))


# Failure based on recent requests: if at least FAULTY_MIN of the last FAULTY_COUNT requests
# within FAULTY_WINDOW seconds exist and ALL of them are server errors (5xx), the model is "down".
FAULTY_WINDOW = 15 * 60
FAULTY_COUNT = 5
FAULTY_MIN = 3


def _recently_faulty(db: Database) -> dict[str, int]:
    """{model: time of first error} — models whose latest requests were all 5xx."""
    since = now() - FAULTY_WINDOW
    rows = db.all("SELECT model, status_code, created_at FROM usage_logs WHERE created_at>=? "
                  "ORDER BY id DESC LIMIT 500", (since,))
    latest: dict[str, list] = {}
    for r in rows:
        lst = latest.setdefault(r["model"], [])
        if len(lst) < FAULTY_COUNT:
            lst.append(r)
    return {m: min(x["created_at"] for x in lst) for m, lst in latest.items()
            if len(lst) >= FAULTY_MIN and all(int(x["status_code"] or 0) >= 500 for x in lst)}


def public_status(db: Database, local_state: str | None = None) -> dict:
    """For `/portal/status/models`: the state of EVERY public model.

    Previously only the failed ones were returned, while the status page showed
    something else entirely — the local model gateway. So when a cluster model
    went down the page still said "everything works". Now every listed model
    is returned with its own state:
      - cluster models from the `model_health` table (written by cluster_watch);
      - local models from the ollama gateway state (`local_state`).
    """
    states = {r["upstream"]: r for r in db.all(
        "SELECT upstream, state, since FROM model_health")}
    models = db.all(
        "SELECT id, provider, upstream_name FROM models "
        "WHERE enabled=1 AND listed=1 ORDER BY id")

    items: list[dict] = []
    faulty = _recently_faulty(db)
    for m in models:
        if m["provider"] == "cluster":
            r = states.get(m["upstream_name"])
            state = r["state"] if r else "unknown"
            since = r["since"] if r else None
            if state == "up" and m["id"] in faulty:
                # Workers exist but the latest requests all ended in server errors —
                # showing "up" would mislead clients (once every 3.1/3.5 request returned
                # 502/503 while the catalog said health=up).
                state, since = "down", faulty[m["id"]]
        else:
            # Local models run through ollama: if the gateway goes down, they all do
            state = "up" if local_state == "up" else ("down" if local_state else "unknown")
            since = None
        items.append({"model": m["id"], "state": state, "since": since,
                      "local": m["provider"] != "cluster"})

    down = [{"model": x["model"], "since": x["since"]}
            for x in items if x["state"] == "down"]
    return {"items": items, "degraded": down, "ok": not down}


async def loop(app, interval: float = 60.0) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await _check(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("error in the cluster watch loop")
        await asyncio.sleep(interval)
