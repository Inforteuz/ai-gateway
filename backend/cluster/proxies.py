"""Proxy list management.

- Downloads proxies from a given URL (e.g. a webshare.io list download address).
- Writes them to `proxies.txt` (read by browser_manager.py).
- Can refresh periodically (asyncio task).
- `direct_slots` setting — how many Chromium instances run without a proxy (with the host's own IP).

Format — one `host:port` or `host:port:username:password` per line (webshare 'direct' format).
"""
from __future__ import annotations

import asyncio
import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import logs
from . import settings as cfg

log = logs.get("proxies")


class ProxyPool:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or cfg.PROXIES_FILE
        self.proxies: list[str] = []
        self.updated_at: float = 0.0
        self.last_source: str = ""
        self.last_error: Optional[str] = None

    # --- loading --------------------------------------------------------------
    def load_from_file(self) -> int:
        if not self.path.exists():
            self.proxies = []
            return 0
        try:
            lines = [ln.strip() for ln in self.path.read_text(encoding="utf-8").splitlines()]
        except OSError as e:
            self.last_error = f"read_failed: {e}"
            return 0
        self.proxies = [ln for ln in lines if ln and not ln.startswith("#")]
        self.last_source = f"file:{self.path.name}"
        self.updated_at = time.time()
        self.last_error = None
        log.info("loaded %d proxies from %s", len(self.proxies), self.path)
        return len(self.proxies)

    def fetch(self, url: str, timeout: int = 20) -> int:
        """Downloads the proxy list from a URL and writes it to the file."""
        if not url:
            self.last_error = "no_url"
            return 0
        if not url.lower().startswith("https://"):
            self.last_error = "https_required"
            return 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-gateway-cluster/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            # Not str(e): it can contain the list URL, and such URLs usually carry an access token
            status = getattr(e, "code", None)
            self.last_error = f"fetch_failed: {type(e).__name__}" + (f" HTTP {status}" if status else "")
            log.error("proxy fetch failed: %s", self.last_error)
            return 0

        lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
        self.proxies = lines
        self.updated_at = time.time()
        self.last_source = url.split("?")[0]
        self.last_error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            self.last_error = f"write_failed: {e}"
            log.warning("proxy file write failed: %s", e)
        log.info("fetched %d proxies from %s", len(lines), self.last_source)
        return len(lines)

    # --- formatting -----------------------------------------------------------
    @staticmethod
    def format_for_chromium(line: str) -> Optional[str]:
        """webshare `host:port:user:pass` -> `http://user:pass@host:port`.
        Plain `host:port` -> `http://host:port`.
        """
        parts = line.split(":")
        if len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        if len(parts) == 4:
            host, port, user, pwd = parts
            return f"http://{user}:{pwd}@{host}:{port}"
        return None

    def assignments(self, slots: int, direct_slots: int = 0) -> list[Optional[str]]:
        """Distributes proxies across `slots` Chromium instances.

        The first `direct_slots` slots get None (no proxy, the host's own IP).
        The rest get proxies round-robin.
        """
        result: list[Optional[str]] = []
        pool_size = len(self.proxies)
        proxied_idx = 0
        for i in range(slots):
            if i < direct_slots or pool_size == 0:
                result.append(None)
                continue
            raw = self.proxies[proxied_idx % pool_size]
            proxied_idx += 1
            result.append(self.format_for_chromium(raw))
        return result

    def summary(self) -> dict:
        return {
            "total": len(self.proxies),
            "source": self.last_source,
            "updated_at": int(self.updated_at),
            "last_error": self.last_error,
        }


# Global instance (one per module)
_pool: Optional[ProxyPool] = None


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        _pool = ProxyPool()
        _pool.load_from_file()
    return _pool


async def auto_refresh(url: str, interval: int = 3600) -> None:
    """Periodically refreshes the proxy list (run as an asyncio task)."""
    while True:
        try:
            get_pool().fetch(url)
        except Exception as e:
            log.error("auto_refresh error: %s", e)
        await asyncio.sleep(max(60, interval))
