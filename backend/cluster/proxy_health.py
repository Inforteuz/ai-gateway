"""Proxy health monitoring and replacement of dead proxies.

PROBLEM: a proxy is chosen when Chrome starts and written into the tinyproxy
config. If the proxy expires or stops working, ALL requests of that instance
fail, and replacing it would normally require restarting Chrome — which
risks losing the provider session.

SOLUTION: the tinyproxy config is rewritten and the process gets a SIGHUP.
Tinyproxy re-reads its config while Chrome keeps running — the session is
preserved, only the egress IP changes.

If no proxy works, the upstream is removed and the instance keeps running
directly (with the server's IP): an unchanged IP is better than the service
stopping altogether.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import logs
from . import settings as cfg

log = logs.get("proxy_health")

STATE_DIR = Path(os.getenv("SF_CLUSTER_STATE_DIR", "/var/lib/ai-gateway/cluster"))
CHECK_URL = os.getenv("SF_CLUSTER_PROXY_CHECK_URL", "https://api.ipify.org")
CHECK_TIMEOUT = float(os.getenv("SF_CLUSTER_PROXY_CHECK_TIMEOUT", "12"))


@dataclass
class ProxyCheck:
    instance: int
    port: int
    working: bool
    ip: Optional[str] = None
    error: Optional[str] = None
    replaced: bool = False


def _local_port(instance: int) -> int:
    """The instance's tinyproxy port (same convention as launch-chrome.sh)."""
    return 16222 + instance


def _conf_path(instance: int) -> Path:
    return STATE_DIR / f"tinyproxy_{instance}.conf"


def _pid_path(instance: int) -> Path:
    return STATE_DIR / f"tinyproxy_{instance}.pid"


def check(port: int) -> tuple[bool, Optional[str], Optional[str]]:
    """Whether the outside world is reachable through the given local proxy.

    Returns: (working, external_ip, error).
    """
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(CHECK_URL, timeout=CHECK_TIMEOUT) as r:
            ip = r.read(64).decode("utf-8", "replace").strip()
        return True, ip, None
    except Exception as e:
        return False, None, f"{type(e).__name__}"


def _proxy_list() -> list[str]:
    """Proxy lines from the file (comments and blank lines dropped)."""
    path = cfg.PROXIES_FILE
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [q.strip() for q in lines if q.strip() and not q.startswith("#")]


def _upstream_line(proxy: str) -> Optional[str]:
    """Converts `host:port:user:pass` or `host:port` to the tinyproxy format."""
    q = proxy.split(":")
    if len(q) == 4:
        return f"http {q[2]}:{q[3]}@{q[0]}:{q[1]}"
    if len(q) == 2:
        return f"http {q[0]}:{q[1]}"
    return None


def _current_upstream(instance: int) -> Optional[str]:
    try:
        for line in _conf_path(instance).read_text(encoding="utf-8").splitlines():
            if line.startswith("upstream "):
                return line[len("upstream "):].strip()
    except OSError:
        pass
    return None


def replace(instance: int, new_proxy: Optional[str]) -> bool:
    """Rewrites the tinyproxy config and sends SIGHUP.

    If `new_proxy` is None the upstream is removed — the instance connects
    directly.
    """
    conf = _conf_path(instance)
    try:
        text = conf.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("instance %d: could not read config (%s)", instance, e)
        return False

    lines = [q for q in text.splitlines() if not q.startswith("upstream ")]
    if new_proxy:
        line = _upstream_line(new_proxy)
        if not line:
            return False
        lines.append(f"upstream {line}")
    try:
        conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("instance %d: could not write config (%s)", instance, e)
        return False

    # SIGHUP: tinyproxy re-reads its config, Chrome keeps running
    try:
        pid = int(_pid_path(instance).read_text().strip())
        os.kill(pid, signal.SIGHUP)
    except (OSError, ValueError) as e:
        log.warning("instance %d: could not send SIGHUP to tinyproxy (%s)", instance, e)
        return False
    return True


def check_and_repair(instances: list[int]) -> list[ProxyCheck]:
    """Checks each instance's proxy and replaces the ones that do not work."""
    pool = _proxy_list()
    results: list[ProxyCheck] = []

    for inst in instances:
        port = _local_port(inst)
        # An instance without a config does not use a proxy — not an error,
        # so it is not added to the results at all.
        if not _conf_path(inst).exists():
            continue
        # Is tinyproxy running at all (is the port listening)
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                results.append(ProxyCheck(inst, port, False, error="tinyproxy ishlamayapti"))
                continue

        ok, ip, err = check(port)
        if ok:
            results.append(ProxyCheck(inst, port, True, ip=ip))
            continue

        log.warning("instance %d: proxy not working (%s), replacing it", inst, err)
        current = _current_upstream(inst)
        replaced = False
        # Find the first working proxy in the list
        for candidate in pool:
            line = _upstream_line(candidate)
            if not line or line == current:
                continue
            if not replace(inst, candidate):
                continue
            ok2, ip2, _ = check(port)
            if ok2:
                log.info("instance %d: new proxy works, IP %s", inst, ip2)
                results.append(ProxyCheck(inst, port, True, ip=ip2, replaced=True))
                replaced = True
                break
        if not replaced:
            # None worked — switch to a direct connection.
            # An unchanged IP is better than the service stopping.
            replace(inst, None)
            log.warning("instance %d: no working proxy found, switched to DIRECT mode", inst)
            results.append(ProxyCheck(inst, port, False, error="proxy topilmadi, DIRECT",
                                      replaced=True))
    return results
