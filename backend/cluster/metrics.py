"""Observability: latency, success rate and per-model statistics.

No Prometheus required; the `/cluster/metrics` endpoint serves plain JSON so
you can see exactly what happened over the last N seconds.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Dict


@dataclass
class _ModelStats:
    total: int = 0
    success: int = 0
    failure: int = 0
    latency_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=200))
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    tool_requests: int = 0
    tool_initial_calls: int = 0
    tool_retries: int = 0
    tool_recovered: int = 0
    tool_missing: int = 0
    tool_retry_errors: int = 0
    tool_final_answers: int = 0
    tool_retry_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=200))
    cache_hits: int = 0


class Metrics:
    """Other modules call `record_*`; the API reads `snapshot()`."""

    def __init__(self):
        self._lock = Lock()
        self._by_model: Dict[str, _ModelStats] = {}
        self._global_latency: Deque[float] = deque(maxlen=500)
        self._started_at = time.time()
        self._total_requests = 0

    def _bucket(self, model: str) -> _ModelStats:
        s = self._by_model.get(model)
        if s is None:
            s = _ModelStats()
            self._by_model[model] = s
        return s

    def record_success(self, model: str, latency_ms: float) -> None:
        with self._lock:
            self._total_requests += 1
            b = self._bucket(model)
            b.total += 1
            b.success += 1
            b.latency_ms.append(latency_ms)
            b.last_success_at = time.time()
            self._global_latency.append(latency_ms)

    def record_failure(self, model: str) -> None:
        with self._lock:
            self._total_requests += 1
            b = self._bucket(model)
            b.total += 1
            b.failure += 1
            b.last_failure_at = time.time()

    def record_cache_hit(self, model: str) -> None:
        with self._lock:
            self._bucket(model).cache_hits += 1

    def record_tools(self, model: str, *, initial: bool, retried: bool, recovered: bool,
                     missing: bool, retry_ms: float = 0, retry_error: bool = False, final: bool = False) -> None:
        with self._lock:
            b = self._bucket(model)
            b.tool_requests += 1
            b.tool_initial_calls += int(initial)
            b.tool_retries += int(retried)
            b.tool_recovered += int(recovered)
            b.tool_missing += int(missing)
            b.tool_retry_errors += int(retry_error)
            b.tool_final_answers += int(final)
            if retried:
                b.tool_retry_ms.append(retry_ms)

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        k = (len(s) - 1) * pct
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return s[int(k)]
        return s[f] * (c - k) + s[c] * (k - f)

    def snapshot(self) -> dict:
        with self._lock:
            uptime = time.time() - self._started_at
            all_lat = list(self._global_latency)
            per_model = []
            for model, b in sorted(self._by_model.items()):
                lat = list(b.latency_ms)
                per_model.append({
                    "model": model,
                    "total": b.total,
                    "success": b.success,
                    "failure": b.failure,
                    "success_rate": round(b.success / b.total, 4) if b.total else 0.0,
                    "latency_ms": {
                        "p50": round(self._percentile(lat, 0.5), 1),
                        "p90": round(self._percentile(lat, 0.9), 1),
                        "p99": round(self._percentile(lat, 0.99), 1),
                        "samples": len(lat),
                    },
                    "last_success_at": int(b.last_success_at) or None,
                    "last_failure_at": int(b.last_failure_at) or None,
                    # Success latency excludes cache hits (recorded separately).
                    "cache_hits": b.cache_hits,
                    "tool_protocol": {
                        "requests": b.tool_requests, "initial_calls": b.tool_initial_calls,
                        "retries": b.tool_retries, "recovered": b.tool_recovered,
                        "missing_calls": b.tool_missing, "retry_errors": b.tool_retry_errors,
                        "final_answers": b.tool_final_answers,
                        "retry_success_rate": round(b.tool_recovered / b.tool_retries, 4) if b.tool_retries else None,
                        "retry_ms_p50": round(self._percentile(list(b.tool_retry_ms), .5), 1),
                        "retry_ms_p95": round(self._percentile(list(b.tool_retry_ms), .95), 1),
                    },
                })
            return {
                "uptime_s": int(uptime),
                "total_requests": self._total_requests,
                "latency_ms_global": {
                    "p50": round(self._percentile(all_lat, 0.5), 1),
                    "p90": round(self._percentile(all_lat, 0.9), 1),
                    "p99": round(self._percentile(all_lat, 0.99), 1),
                    "samples": len(all_lat),
                },
                "per_model": per_model,
            }


# Module-level singleton
_instance: Metrics | None = None


def get() -> Metrics:
    global _instance
    if _instance is None:
        _instance = Metrics()
    return _instance
