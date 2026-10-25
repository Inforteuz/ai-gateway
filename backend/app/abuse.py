"""Abuse protection: multiple accounts, bonus farming, key sharing.

PRINCIPLE: never hurt honest users. So this module mostly removes the
INCENTIVE and applies soft limits instead of hard blocks:

  - When many accounts are created from one IP, the later ones get NO signup
    bonus (cloning accounts to collect the bonus again and again becomes pointless),
    but creating the account itself is not forbidden — people behind one home,
    office or mobile carrier must not be punished.
  - A key used from many places is not blocked — it is slowed down
    and flagged in the admin panel. Teams, CI and mobile networks also
    produce many IPs, so a hard block would be wrong.
  - There is a separate per-IP rate limit: spreading one key across dozens
    of machines does not multiply total throughput.

All thresholds are read from the `settings` table, so an admin can change them.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .common import ApiError

# Default values (used when the settings table has none)
DEFAULTS = {
    # Max accounts created from one IP (0 = unlimited)
    "max_accounts_per_ip": 4,
    # The signup bonus is given only once per IP (1 = enabled)
    "bonus_once_per_ip": 1,
    # Requests per minute from one IP (regardless of key)
    "ip_rpm": 90,
    # A key used from this many distinct IPs within an hour counts as shared
    "key_max_ips_per_hour": 6,
    # Per-minute limit for a key flagged as shared
    "shared_key_rpm": 12,
}


def setting(db, name: str) -> int:
    """Integer value from `settings`; the default when missing."""
    try:
        raw = db.get_settings().get(name)
    except Exception:
        raw = None
    if raw is None or str(raw).strip() == "":
        return int(DEFAULTS[name])
    try:
        return int(str(raw).strip())
    except ValueError:
        return int(DEFAULTS[name])


# --- Per-key IP diversity tracking --------------------------------------------

@dataclass
class KeyIPs:
    """Which IPs a single key was used from (within the time window)."""
    seen: dict[str, float] = field(default_factory=dict)

    def add(self, ip: str, now: float, window: float) -> int:
        self.seen[ip] = now
        cutoff = now - window
        for k in [k for k, t in self.seen.items() if t <= cutoff]:
            del self.seen[k]
        return len(self.seen)


class SharingTracker:
    """Tracks per-key IP diversity in memory.

    Kept in memory because it changes on every request — writing it to the
    database would be expensive. It starts from zero after a restart; that is
    good enough for an abuse signal, since a shared key shows itself again
    within minutes.
    """

    def __init__(self, window: float = 3600.0) -> None:
        self.window = window
        self._keys: dict[object, KeyIPs] = {}
        self._lock = threading.Lock()

    def record(self, key_id: object, ip: str) -> int:
        """Records the IP and returns the number of distinct IPs in the window."""
        if not ip or ip == "-":
            return 0
        now = time.time()
        with self._lock:
            entry = self._keys.setdefault(key_id, KeyIPs())
            return entry.add(ip, now, self.window)

    def count(self, key_id: object) -> int:
        with self._lock:
            entry = self._keys.get(key_id)
            if not entry:
                return 0
            cutoff = time.time() - self.window
            return sum(1 for t in entry.seen.values() if t > cutoff)

    def ips(self, key_id: object) -> list[str]:
        with self._lock:
            entry = self._keys.get(key_id)
            if not entry:
                return []
            cutoff = time.time() - self.window
            return sorted(ip for ip, t in entry.seen.items() if t > cutoff)

    def flagged(self, limit: int) -> list[tuple[object, int]]:
        """Keys over the limit — for the admin panel."""
        with self._lock:
            cutoff = time.time() - self.window
            out = []
            for kid, entry in self._keys.items():
                n = sum(1 for t in entry.seen.values() if t > cutoff)
                if n > limit:
                    out.append((kid, n))
            return sorted(out, key=lambda x: -x[1])

    def prune(self) -> None:
        with self._lock:
            cutoff = time.time() - self.window
            for kid in list(self._keys):
                entry = self._keys[kid]
                for ip in [ip for ip, t in entry.seen.items() if t <= cutoff]:
                    del entry.seen[ip]
                if not entry.seen:
                    del self._keys[kid]


# --- Signup checks -------------------------------------------------------------

def accounts_from_ip(db, ip: str) -> int:
    """Number of accounts created from this IP."""
    if not ip or ip == "-":
        return 0
    row = db.one("SELECT COUNT(*) AS n FROM users WHERE signup_ip=?", (ip,))
    return int(row["n"]) if row else 0


def check_signup(db, ip: str) -> None:
    """Limits mass account creation. Raises ApiError when over the limit."""
    limit = setting(db, "max_accounts_per_ip")
    if limit <= 0:
        return
    if accounts_from_ip(db, ip) >= limit:
        raise ApiError(
            429, "too_many_accounts",
            "Bu internet manzilidan ochilgan akkauntlar soni chegaraga yetdi. "
            "Agar bu xato bo'lsa, administrator bilan bog'laning.",
            "rate_limit_error")


def signup_bonus(db, ip: str, configured: int) -> tuple[int, bool]:
    """Computes the signup bonus for a new account.

    Returns (bonus_micro, restricted). A second account from the same IP gets
    no bonus — collecting money by cloning accounts becomes pointless.
    Creating the account itself is not forbidden.
    """
    if configured <= 0:
        return 0, False
    if not setting(db, "bonus_once_per_ip"):
        return configured, False
    if accounts_from_ip(db, ip) > 0:
        return 0, True
    return configured, False


# --- Per-request checks ------------------------------------------------------

def check_request(state, key: dict, ip: str) -> Optional[str]:
    """Checks rate limits (per IP and for shared keys).

    Raises ApiError when over a limit. Returns a warning string or None
    (the string is set when the key is flagged as shared — for logging).
    """
    db = state.db

    # 1) Per-IP limit — prevents multiplying throughput by spreading
    #    one key across many machines.
    ip_limit = setting(db, "ip_rpm")
    if ip_limit > 0 and ip and ip != "-":
        ok, retry = state.ip_rpm.allow(ip, ip_limit)
        if not ok:
            raise ApiError(
                429, "ip_rate_limit_exceeded",
                f"Bu internet manzilidan daqiqasiga {ip_limit} ta so'rov chegarasi oshdi. "
                f"{retry} soniyadan keyin urinib ko'ring.",
                "rate_limit_error", {"Retry-After": str(retry)},
                en=f"Rate limit of {ip_limit} requests per minute from this IP address exceeded. "
                   f"Try again in {retry} s.")

    # 2) Is the key used from many places? Do not block, slow it down
    max_ips = setting(db, "key_max_ips_per_hour")
    distinct = state.sharing.record(key["id"], ip)
    if max_ips <= 0 or distinct <= max_ips:
        return None

    shared_rpm = setting(db, "shared_key_rpm")
    if shared_rpm > 0:
        ok, retry = state.rpm.allow(("shared", key["id"]), shared_rpm)
        if not ok:
            raise ApiError(
                429, "key_shared_rate_limit",
                f"Bu kalit oxirgi bir soatda {distinct} xil manzildan ishlatildi, "
                f"shuning uchun tezligi daqiqasiga {shared_rpm} ta so'rovga tushirildi. "
                f"Har bir foydalanuvchi uchun alohida kalit yarating. "
                f"{retry} soniyadan keyin urinib ko'ring.",
                "rate_limit_error", {"Retry-After": str(retry)},
                en=(f"This key was used from {distinct} addresses in the last hour, so it is slowed to "
                    f"{shared_rpm} requests per minute. Create a separate key for each user. "
                    f"Try again in {retry} s."))
    return f"shared_key:{distinct}_ips"
