"""Automatic recovery of Chrome instances.

PROBLEM: a Chrome process sometimes hangs — the port is listening and TCP
connections are accepted, but CDP requests get no reply. The model on that
port is then completely down and, worse, every scan blocks until its timeout,
slowing down the whole cluster.

SOLUTION: if a port fails to respond several times in a row, a privileged
helper restarts the instance. The Docker helper also starts failed display
dependencies before Chrome. Login data lives on disk inside
`--user-data-dir` and Chrome flushes it cleanly on SIGTERM, so the session
survives the restart (verified in practice). Even so, the cookie file is
backed up before every recovery, so a session lost unexpectedly can still
be restored.

LIMIT: an instance is recovered at most `MAX_PER_HOUR` times per hour. Beyond
that the problem is not Chrome (e.g. the provider blocked the account) — the
instance is marked as "needs manual intervention" and the API side reports it
via Telegram.
"""
from __future__ import annotations

import os
import threading
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import logs


log = logs.get("self_heal")

PROFILE_DIR = Path(os.getenv("SF_CLUSTER_PROFILE_DIR", "/var/lib/ai-gateway/cluster/profiles"))
BACKUP_DIR = Path(os.getenv("SF_CLUSTER_BACKUP_DIR", "/var/lib/ai-gateway/cluster/backups"))
# Directory watched by the root-side recovery helper
REQUEST_DIR = Path(os.getenv("SF_CLUSTER_HEAL_REQ_DIR", "/run/ai-gateway-heal"))
# How long to wait for the request to be accepted (seconds)
ACCEPT_WAIT = float(os.getenv("SF_CLUSTER_HEAL_ACCEPT_WAIT", "20"))

# Recover after this many consecutive failed CDP probes.
THRESHOLD = int(os.getenv("SF_CLUSTER_HEAL_THRESHOLD", "3"))
# CDP probe timeout (seconds). A hung Chrome accepts TCP but never replies,
# hence the short timeout.
PROBE_TIMEOUT = float(os.getenv("SF_CLUSTER_HEAL_PROBE_TIMEOUT", "8"))
# How long to wait for CDP after a restart (seconds).
RECOVERY_WAIT = float(os.getenv("SF_CLUSTER_HEAL_WAIT", "90"))
MAX_PER_HOUR = int(os.getenv("SF_CLUSTER_HEAL_MAX_PER_HOUR", "3"))
MAX_INSTANCES = int(os.getenv("SF_MAX_INSTANCES", "10"))


@dataclass
class PortState:
    port: int
    instance: int
    failures: int = 0                      # consecutive failed probes
    recoveries: list[float] = field(default_factory=list)  # recent recovery times
    needs_manual: bool = False
    last_error: str | None = None
    last_recovery: int = 0


_states: dict[int, PortState] = {}


def instance_number(port: int) -> int:
    """launch-chrome.sh convention: ai-gateway-chrome@N -> CDP port 9222+N."""
    return port - 9222


def _cdp_responds(port: int) -> tuple[bool, str | None]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=PROBE_TIMEOUT
        ) as r:
            return (r.status == 200), None
    except urllib.error.URLError as e:
        return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    except Exception as e:  # TimeoutError and others
        return False, type(e).__name__


def backup_cookies(inst: int) -> None:
    """Copies the cookie file before recovery (so the session can be restored)."""
    src = PROFILE_DIR / f"profile_{inst}" / "Default" / "Cookies"
    if not src.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, BACKUP_DIR / f"cookies_{inst}.bak")
    except Exception as e:
        log.warning("instance %d: cookie backup failed: %s", inst, e)


def request_restart(inst: int) -> tuple[bool, str | None]:
    """Hands the restart off to the root-side helper.

    The cluster service runs with `NoNewPrivileges=yes`, which blocks sudo
    entirely (and rightly so: the service must not be able to escalate).
    So the restart request is sent through the file `/run/ai-gateway-heal/<N>.req`:
    a systemd `path` unit (or the Docker heal watcher) sees it and a root-side
    script restarts ONLY that configured instance and its failed display
    dependencies. The helper validates the number itself, so no arbitrary
    command can be run through this channel.
    """
    if not (0 <= inst < MAX_INSTANCES):
        return False, f"noto'g'ri instansiya raqami: {inst}"
    req = REQUEST_DIR / f"{inst}.req"
    try:
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        req.write_text("")
    except Exception as e:
        return False, f"so'rov yozilmadi: {type(e).__name__}: {e}"

    # The helper deleting the file means the request was accepted.
    deadline = time.monotonic() + ACCEPT_WAIT
    while time.monotonic() < deadline:
        if not req.exists():
            return True, None
        time.sleep(0.5)
    try:
        req.unlink(missing_ok=True)
    except Exception:
        pass
    return False, "tiklash yordamchisi javob bermadi (ai-gateway-heal.path ishlayaptimi?)"


def _wait_for_cdp(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, _ = _cdp_responds(port)
        if ok:
            return True
        time.sleep(4)
    return False


def _within_hourly_limit(h: PortState) -> bool:
    cutoff = time.time() - 3600
    h.recoveries = [t for t in h.recoveries if t > cutoff]
    return len(h.recoveries) < MAX_PER_HOUR


def _run_cycle_unlocked(ports: list[int]) -> list[dict]:
    """Probes every port and recovers it if needed. Returns the list of events."""
    events: list[dict] = []

    for port in ports:
        inst = instance_number(port)
        if inst < 0:
            continue
        h = _states.setdefault(port, PortState(port=port, instance=inst))

        ok, err = _cdp_responds(port)
        if ok:
            if h.failures:
                log.info("instance %d: CDP is responding again", inst)
            h.failures = 0
            h.last_error = None
            # Once it works again the "manual intervention" flag is cleared
            if h.needs_manual:
                h.needs_manual = False
                events.append({"tur": "tiklandi", "instansiya": inst, "port": port})
            continue

        h.failures += 1
        h.last_error = err
        if h.failures < THRESHOLD:
            log.warning("instance %d: CDP did not respond (%d/%d) — %s",
                        inst, h.failures, THRESHOLD, err)
            continue

        if h.needs_manual:
            # A failed helper or exhausted restart budget is not a permanent
            # disable. Retry after five minutes, still respecting the hourly cap.
            if time.time() - h.last_recovery < 300 or not _within_hourly_limit(h):
                continue
            h.needs_manual = False

        if not _within_hourly_limit(h):
            h.needs_manual = True
            log.error("instance %d: recovered more than %d times in an hour — manual intervention needed",
                      inst, MAX_PER_HOUR)
            events.append({"tur": "qolga_muhtoj", "instansiya": inst, "port": port,
                           "sabab": "tez-tez tiklanmoqda"})
            continue

        log.warning("instance %d: recovering (CDP failed to respond %d times)", inst, h.failures)
        backup_cookies(inst)
        h.recoveries.append(time.time())
        h.last_recovery = int(time.time())

        success, err2 = request_restart(inst)
        if not success:
            h.needs_manual = True
            log.error("instance %d: restart command failed: %s", inst, err2)
            events.append({"tur": "qolga_muhtoj", "instansiya": inst, "port": port,
                           "sabab": f"restart xatosi: {err2}"})
            continue

        if _wait_for_cdp(port, RECOVERY_WAIT):
            h.failures = 0
            log.info("instance %d: recovered, CDP is responding", inst)
            events.append({"tur": "avtomatik_tiklandi", "instansiya": inst, "port": port})
        else:
            h.needs_manual = True
            log.error("instance %d: CDP still not responding after recovery", inst)
            events.append({"tur": "qolga_muhtoj", "instansiya": inst, "port": port,
                           "sabab": "tiklashdan keyin ham javob yo'q"})

    return events


def summary() -> dict:
    return {
        "chegara": THRESHOLD,
        "soatlik_chegara": MAX_PER_HOUR,
        "instansiyalar": [
            {
                "instansiya": h.instance,
                "port": h.port,
                "uzilish": h.failures,
                "tiklashlar_soatda": len(h.recoveries),
                "qolga_muhtoj": h.needs_manual,
                "oxirgi_xato": h.last_error,
                "oxirgi_tiklash": h.last_recovery,
            }
            for h in sorted(_states.values(), key=lambda x: x.instance)
        ],
        "qolga_muhtoj": [h.instance for h in _states.values() if h.needs_manual],
    }


def is_recovering(port: int) -> bool:
    """Whether to hold a request: is this port currently being recovered."""
    h = _states.get(port)
    return bool(h and h.failures >= THRESHOLD and not h.needs_manual)


def _fast_recover_unlocked(port: int) -> bool:
    """Probes the port IMMEDIATELY and recovers it right here if it does not respond.

    The watchdog runs once every 45 seconds and waits 3 cycles — far too slow
    for an instance that died mid-request. The balancer calls this after a
    failed request: if the fault is confirmed, recovery starts at once and the
    request is held for a retry.

    `True` — the instance was recovered and CDP is responding.
    """
    inst = instance_number(port)
    if inst < 0:
        return False
    ok, err = _cdp_responds(port)
    if ok:
        return False  # port is healthy — the problem is elsewhere

    h = _states.setdefault(port, PortState(port=port, instance=inst))
    h.last_error = err
    if h.needs_manual:
        return False
    if not _within_hourly_limit(h):
        h.needs_manual = True
        log.error("instance %d: hourly recovery limit reached — manual intervention needed", inst)
        return False

    # Mark it so the watchdog does not pick up this port as well
    h.failures = max(h.failures, THRESHOLD)
    log.warning("instance %d: died during a request — fast recovery", inst)
    backup_cookies(inst)
    h.recoveries.append(time.time())
    h.last_recovery = int(time.time())

    success, err2 = request_restart(inst)
    if not success:
        h.needs_manual = True
        log.error("instance %d: fast recovery failed: %s", inst, err2)
        return False
    if _wait_for_cdp(port, RECOVERY_WAIT):
        h.failures = 0
        log.info("instance %d: fast recovery succeeded", inst)
        return True
    h.needs_manual = True
    log.error("instance %d: still no response after fast recovery", inst)
    return False


# Watchdog and request-time recovery run on executor threads. Only one may
# restart a given browser; the per-port gate prevents duplicate restarts.
_recovery_gates: dict[int, threading.Lock] = {}
_gates_lock = threading.Lock()


def _recovery_gate(port: int):
    with _gates_lock:
        return _recovery_gates.setdefault(port, threading.Lock())


def run_cycle(ports: list[int]) -> list[dict]:
    events = []
    for port in ports:
        gate = _recovery_gate(port)
        if not gate.acquire(blocking=False):
            continue
        try:
            events.extend(_run_cycle_unlocked([port]))
        finally:
            gate.release()
    return events


def fast_recover(port: int) -> bool:
    gate = _recovery_gate(port)
    if not gate.acquire(blocking=False):
        return False
    try:
        return _fast_recover_unlocked(port)
    finally:
        gate.release()
