"""Circuit breaker and adaptive cooldown.

Problems this module solves:
  1. When a provider goes down completely, the balancer keeps sending requests
     to unreliable workers and users get slow responses.
  2. A static cooldown (30s for every error) does not match real patterns: a
     worker that failed 10 times in a row should rest much longer than 30s.

Solution:
  - CircuitBreaker: per-model state (closed / open / half-open). While open,
    new requests are rejected; after recovery_timeout it moves to half-open.
  - AdaptiveCooldown: per-worker exponential cooldown based on the consecutive
    failure streak, reset to zero on success.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict

from . import logs

log = logs.get("circuit")


class CircuitState:
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # fully open: send no requests at all
    HALF_OPEN = "half_open"  # one probe request; close again on success


@dataclass
class ModelCircuit:
    """Circuit breaker for a single model (channel)."""
    model: str
    failure_threshold: int = 5          # consecutive failures before opening
    recovery_timeout: float = 60.0      # seconds after opening before half-open
    half_open_max_probes: int = 1       # concurrent probes allowed while half-open

    state: str = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probes_inflight: int = 0
    total_opens: int = 0

    def allow_request(self) -> bool:
        """May a request be sent to this model right now?"""
        now = time.monotonic()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if (now - self.opened_at) >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.probes_inflight = 0
                log.info("circuit half_open model=%s", self.model)
            else:
                return False
        # HALF_OPEN: allow a limited number of probe requests
        if self.probes_inflight >= self.half_open_max_probes:
            return False
        self.probes_inflight += 1
        return True

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.probes_inflight = max(0, self.probes_inflight - 1)
            log.info("circuit closed model=%s (recovered)", self.model)
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.opened_at = 0.0
        elif self.state == CircuitState.CLOSED:
            self.consecutive_failures = 0

    def record_failure(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.probes_inflight = max(0, self.probes_inflight - 1)
            # The probe failed too: open again
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            self.total_opens += 1
            log.warning("circuit reopened model=%s", self.model)
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold and self.state == CircuitState.CLOSED:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            self.total_opens += 1
            log.warning(
                "circuit opened model=%s failures=%d recovery=%.0fs",
                self.model, self.consecutive_failures, self.recovery_timeout,
            )

    def snapshot(self) -> dict:
        now = time.monotonic()
        remaining = 0.0
        if self.state == CircuitState.OPEN:
            remaining = max(0.0, self.recovery_timeout - (now - self.opened_at))
        return {
            "model": self.model,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_opens": self.total_opens,
            "recovery_in": round(remaining, 1),
        }


class CircuitRegistry:
    """Collection of per-model circuit breakers."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._by_model: Dict[str, ModelCircuit] = {}

    def get(self, model: str) -> ModelCircuit:
        c = self._by_model.get(model)
        if c is None:
            c = ModelCircuit(model=model, failure_threshold=self.failure_threshold,
                             recovery_timeout=self.recovery_timeout)
            self._by_model[model] = c
        return c

    def allow(self, model: str) -> bool:
        return self.get(model).allow_request()

    def record_success(self, model: str) -> None:
        self.get(model).record_success()

    def record_failure(self, model: str) -> None:
        self.get(model).record_failure()

    def snapshot(self) -> list[dict]:
        return [c.snapshot() for c in self._by_model.values()]


def exponential_cooldown(failure_streak: int, base: float = 15.0, cap: float = 600.0) -> float:
    """Cooldown grows exponentially with the streak: 15s, 30s, 60s, 120s, 240s, 480s, 600s (cap).

    Never exceeds cap. Returns 0 for a zero streak.
    """
    if failure_streak <= 0:
        return 0.0
    # Base-2 exponential: 15 * 2^(n-1)
    val = base * math.pow(2, failure_streak - 1)
    return float(min(val, cap))
