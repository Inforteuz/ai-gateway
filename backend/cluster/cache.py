"""Response cache: an identical request is not sent to the browser again.

Why this is the most important optimization: every request runs in a real
browser tab and takes 8-40 seconds. When the same question repeats (doc
examples, test requests, short questions with the same system prompt),
answering from the cache saves both the server and the provider's daily quota.

The cache KEY is a full hash of the prompt, including the system prompt,
history and attachments. So "same key" means "same request": another user's
answer cannot come back by accident.

Key properties:

  * GENERATION PARAMETERS are part of the key (temperature, max_tokens,
    response_format, tools/tool_choice, reasoning_effort). Otherwise a
    `json_object` request could pick up the cached answer of a plain-text one.

  * The CONFIG VERSION is part of the key: a hash of the texts that shape
    the prompt (brand system prompt, reasoning template, tool protocol,
    format instruction). When they change, old entries simply stop being
    used; no manual purge needed.

  * The answer is validated BEFORE it is stored (`_cacheable` in `server.py`).
    Otherwise an invalid answer (e.g. an empty YAML plan template) would be
    served again on the next request with `cached: true`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import logs
from . import settings as cfg

log = logs.get("cache")


@dataclass
class Entry:
    answer: str
    images: List[Tuple[str, str]] = field(default_factory=list)
    model: str = ""
    created: float = field(default_factory=time.time)
    hits: int = 0


# Manually bumped part: when answer post-processing changes (adapter DOM
# reading, normalization), old answers go stale even if the prompt text did
# not change. It is part of the key together with `config_version`.
# 4: adapters re-read code blocks; older answers still contain
#    "Copy/Download", "Python/Run" headers and broken code.
CACHE_SCHEMA = "4"

_config_version: Optional[str] = None


def config_version() -> str:
    """Hash of the prompt-shaping texts plus the manually set schema number.

    Imports are local to avoid a circular import (compose also imports cfg).
    """
    global _config_version
    if _config_version is None:
        from . import compose, output_format, reasoning, tools
        h = hashlib.sha256()
        for part in (CACHE_SCHEMA, compose._BRAND_SYSTEM_PROMPT, compose._UNTRUSTED_RULE,
                     reasoning.INSTRUCTION, output_format.instruction(output_format.parse({"type": "json_object"})),
                     tools.PROTOCOL_VERSION):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        _config_version = h.hexdigest()[:12]
    return _config_version


def make_key(model: str, prompt: str, attachment_digests: Optional[List[str]] = None,
             params: Optional[dict] = None) -> str:
    """Cache key for a request.

    `model` is part of the key: each model answers the same question its own
    way, so they must not be mixed. For "auto" we do not know in advance which
    model will answer, so such a request is stored under `auto` and later
    serves only `auto` requests.

    `params` are the generation parameters that affect the answer. They are
    serialized in a stable order (sort_keys).
    """
    h = hashlib.sha256()
    h.update(config_version().encode("utf-8"))
    h.update(b"\x00")
    h.update((model or "auto").encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    for d in (attachment_digests or []):
        h.update(b"\x00")
        h.update(d.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()


def file_digest(path: str) -> str:
    """Content hash of an attached file: the same image yields the same key."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()[:32]


class ResponseCache:
    """TTL + LRU cache. Thread-safe (the watchdog may access it too)."""

    def __init__(self, max_entries: int = 500, ttl: float = 3600.0) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._data: "OrderedDict[str, Entry]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.evictions = 0

    def get(self, key: str) -> Optional[Entry]:
        with self._lock:
            e = self._data.get(key)
            if e is None:
                self.misses += 1
                return None
            if time.time() - e.created > self.ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)   # LRU: most recently used goes last
            e.hits += 1
            self.hits += 1
            return e

    def put(self, key: str, answer: str, images: Optional[List[Tuple[str, str]]] = None,
            model: str = "") -> None:
        # Do not cache empty answers, so an error state does not get stuck
        if not (answer or "").strip() and not images:
            return
        # A very large answer (with images) could flood the cache
        size = len(answer or "") + sum(len(b) for _, b in (images or []))
        if size > cfg.CACHE_MAX_ENTRY_BYTES:
            return
        with self._lock:
            self._data[key] = Entry(answer=answer, images=list(images or []), model=model)
            self._data.move_to_end(key)
            self.stores += 1
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)   # evict the least recently used
                self.evictions += 1

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data.clear()
            return n

    def purge(self, predicate) -> int:
        """Remove entries for which `predicate(entry)` returns True.

        Used to clean previously stored invalid answers (e.g. template
        leftovers) without dropping the whole cache.
        """
        with self._lock:
            bad = [k for k, e in self._data.items() if predicate(e)]
            for k in bad:
                del self._data[k]
            return len(bad)

    def snapshot(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data),
                "max_entries": self.max_entries,
                "ttl_seconds": int(self.ttl),
                "hits": self.hits,
                "misses": self.misses,
                "stores": self.stores,
                "evictions": self.evictions,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "config_version": config_version(),
            }


_cache: Optional[ResponseCache] = None


def get() -> ResponseCache:
    global _cache
    if _cache is None:
        _cache = ResponseCache(max_entries=cfg.CACHE_MAX_ENTRIES, ttl=cfg.CACHE_TTL)
    return _cache
