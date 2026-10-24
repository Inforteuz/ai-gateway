"""Load distribution and failover across workers.

v2 improvements:
  - Circuit breaker: no requests are sent to a model that is completely down
  - Bounded wait queue: if a busy worker may free up soon, wait briefly
  - Health score: more reliable workers are picked first
  - Metrics: the outcome and latency of every request are recorded
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import List, Optional, Sequence, Tuple

from . import logs, metrics, runmeta
from . import self_heal
from . import settings as cfg
from .circuit import CircuitRegistry
from .pool import ClusterPool, WorkerTab

log = logs.get("balancer")


# How long to wait for a busy worker's lock to be released.
# If another FREE candidate exists, a busy worker is waited on this long (then we move to the free one).
# If the only candidate is busy, we queue for up to `cfg.QUEUE_WAIT_SECONDS`.
WAIT_FOR_LOCK_SECONDS = 2.0



class DispatchError(RuntimeError):
    """Balancer failure. `status` and `code` set the HTTP status returned to the client.

    Distinct subclasses keep "model does not support this feature", "queue
    full" and "provider did not answer" distinguishable for the client instead
    of collapsing them all into one generic 503/502.
    """
    status = 503
    code = "model_unavailable"
    retry_after: Optional[int] = 15


class Unsupported(DispatchError):
    """The model does not support the requested feature at all (image, file, tools...)."""
    status, code, retry_after = 400, "unsupported_feature", None


class NoWorker(DispatchError):
    """The model has no running instance (all inactive)."""
    status, code, retry_after = 503, "model_unavailable", 30


class QueueFull(DispatchError):
    """Too many requests waiting in the queue; the request is rejected immediately."""
    status, code, retry_after = 503, "queue_full", 10


class Busy(DispatchError):
    """Queue wait timed out; the worker did not free up."""
    status, code, retry_after = 503, "model_busy", 15


class UpstreamFailed(DispatchError):
    """The worker took the request but could not answer (timeout, disconnect, empty answer)."""
    status, code, retry_after = 502, "upstream_failed", 10


def _circuit_key(worker: WorkerTab) -> str:
    # A broken account must never open the circuit of healthy sibling accounts.
    return f"{worker.model_name}:{worker.browser_port}"


def _dispatch_lock(worker: WorkerTab):
    return getattr(worker, "dispatch_lock", worker.lock)


def _is_busy(worker: WorkerTab) -> bool:
    """The lock is held OR has waiters.

    `locked()` returns False right after release, before the next waiter has
    woken up. An `acquire()` at that moment joins the end of the FIFO queue
    and waits indefinitely, so waiters are counted too.
    """
    lock = _dispatch_lock(worker)
    return (lock.locked() or bool(getattr(lock, "_waiters", None))
            or worker.lock.locked() or bool(getattr(worker.lock, "_waiters", None)))


def _stream_committed() -> bool:
    """True once live streaming has sent part of this request's answer to the client."""
    return bool(runmeta.get().get("stream_committed"))


class ClusterBalancer:
    def __init__(self, pool: ClusterPool):
        self.pool = pool
        # Circuit breaker: open after 5 consecutive failures, half-open after 60s
        self.circuits = CircuitRegistry(failure_threshold=5, recovery_timeout=60.0)
        self.metrics = metrics.get()
        # Requests waiting in each model's queue (for the QueueFull limit)
        self._waiting: dict[str, int] = {}

    def pick_candidates(self, requested_model: Optional[str] = "auto",
                        require: Sequence[str] = ()) -> List[WorkerTab]:
        """Matching workers, best first (free, healthy, least used).

        `require` lists the required capabilities ("vision", "files", "image_gen").
        Workers lacking one are excluded entirely: a request with an image cannot
        go to a text-only worker, otherwise the model would answer without seeing
        the image and the user would not even know.
        """
        model_req = (requested_model or "auto").lower()

        if model_req in cfg.DEFAULT_PRIORITY_ORDER:
            # A specific model was requested: send only to that model's workers.
            # No failover to ANOTHER model (the user knows exactly what they asked for).
            target = self.pool.workers_for_model(model_req)
            others = []
        elif model_req in cfg.DEEPSEEK_MODEL_ALIASES:
            target = self.pool.workers_for_model("deepseek")
            others = []
        else:
            # "auto" or unknown model: all workers allowed (failover inside)
            target = list(self.pool.workers)
            others = []

        # Circuit breaker check: skip models whose circuit is open
        target = [w for w in target if self.circuits.get(_circuit_key(w)).is_available()]

        # Capability filter (vision / files / image_gen)
        if require:
            target = [w for w in target
                      if all(getattr(w.adapter.caps, c, False) for c in require)]

        # Sort by health score + request count
        def _key(w: WorkerTab) -> tuple:
            # A tab holding a ready clean chat first: it saves the whole new-chat
            # wait. Then healthiest, least used, oldest.
            warm = 0 if getattr(w, 'warm_since', 0.0) else 1
            return (warm, -w.health_score, w.total_requests, w.last_used)

        available = [w for w in target if w.is_available() and not _is_busy(w)]
        available.sort(key=_key)
        busy_but_fresh = [w for w in target if w not in available and time.time() >= w.cooldown_until]
        busy_but_fresh.sort(key=_key)
        backup = [w for w in others if w.is_available()]
        backup.sort(key=_key)

        if model_req == "auto":
            priority = {m: i for i, m in enumerate(cfg.DEFAULT_PRIORITY_ORDER)}
            available.sort(key=lambda w: (priority.get(w.model_name, 999), *_key(w)))

        return available + busy_but_fresh + backup

    async def _wait_for_lock(self, worker: WorkerTab, max_wait: float) -> bool:
        """Wait up to `max_wait` seconds for a busy worker's lock. True if it was released."""
        if not worker.lock.locked():
            return True
        start = time.monotonic()
        while worker.lock.locked() and (time.monotonic() - start) < max_wait:
            await asyncio.sleep(0.1)
        return not worker.lock.locked()

    def _soonest_cooldown(self, candidates: List[WorkerTab]) -> Optional[float]:
        """Seconds until the earliest cooling-down worker becomes available."""
        now = time.time()
        waits = [w.cooldown_until - now for w in candidates if w.cooldown_until > now]
        return min(waits) if waits else None

    async def execute(
        self,
        prompt_text: str,
        requested_model: str = "auto",
        timeout_per_worker: int = 40,
        require: Sequence[str] = (),
        attachment_paths: Optional[List[str]] = None,
        images_only: bool = False,
        want_images: bool = False,
    ) -> Tuple[str, str, WorkerTab, List[Tuple[str, str]]]:
        """Send the request to a matching worker.

        Returns (answer, model used, worker, images).
        """
        await self.pool.scan(force=False)
        # The capability check is SEPARATE from availability, so a temporarily
        # inactive model is not reported as "unsupported" (or vice versa). Only
        # the adapter's declared capabilities are considered here.
        missing = self.missing_caps(requested_model, require)
        if missing:
            nice = {"vision": "rasmni ko'rish", "files": "fayl o'qish",
                    "image_gen": "rasm yaratish", "tool_calling": "vosita chaqirish (tools)"}
            need = ", ".join(nice.get(c, c) for c in missing)
            # NOTE: provider names must not appear in the error text.
            raise Unsupported(
                f"Tanlangan model bu imkoniyatni qo'llab-quvvatlamaydi: {need}. "
                f"Modellar ro'yxatidan imkoniyati bor modelni tanlang.")
        if not self.pick_candidates(requested_model, require):
            # Matching workers exist but are all in a SHORT cooldown (e.g. 15 s after
            # one empty answer). `pick_candidates` skips cooling-down workers, so
            # without this wait a single empty answer would fail the next several
            # requests with an immediate 503.
            wait = self._soonest_cooldown(self._matching(requested_model, require))
            if wait is not None and wait <= cfg.MAX_COOLDOWN_WAIT:
                log.info("matching worker cooling down, waiting %.1fs (model=%s)", wait, requested_model)
                await asyncio.sleep(wait + 0.2)
            if not self.pick_candidates(requested_model, require):
                raise NoWorker("The model is unavailable right now. Try again shortly.")
        return await self._dispatch(prompt_text, requested_model, timeout_per_worker,
                                    time.monotonic(), retried=False, require=require,
                                    attachment_paths=attachment_paths,
                                    images_only=images_only, want_images=want_images)

    def _matching(self, requested_model: Optional[str], require: Sequence[str]) -> List[WorkerTab]:
        """Workers matching model and capabilities, REGARDLESS of busy/cooldown state."""
        model_req = (requested_model or "auto").lower()
        if model_req in cfg.DEEPSEEK_MODEL_ALIASES:
            ws = self.pool.workers_for_model("deepseek")
        elif model_req in cfg.DEFAULT_PRIORITY_ORDER:
            ws = self.pool.workers_for_model(model_req)
        else:
            ws = list(self.pool.workers)
        return [w for w in ws if self.circuits.get(_circuit_key(w)).is_available()
                and all(getattr(w.adapter.caps, c, False) for c in require)]

    def missing_caps(self, requested_model: Optional[str], require: Sequence[str]) -> list[str]:
        """Capabilities that NO instance of the model has (regardless of availability)."""
        if not require:
            return []
        model_req = (requested_model or "auto").lower()
        if model_req in cfg.DEEPSEEK_MODEL_ALIASES:
            workers = self.pool.workers_for_model("deepseek")
        elif model_req in cfg.DEFAULT_PRIORITY_ORDER:
            workers = self.pool.workers_for_model(model_req)
        else:
            workers = list(self.pool.workers)
        if not workers:
            return []   # no instance: that is "unavailable", not "unsupported"
        return [c for c in require
                if not any(getattr(w.adapter.caps, c, False) for w in workers)]

    @asynccontextmanager
    async def _queue_slot(self, model: str):
        """A slot in the model's queue. Raises QueueFull immediately when over the limit."""
        n = self._waiting.get(model, 0)
        if n >= cfg.QUEUE_MAX_WAITING:
            raise QueueFull(
                "Navbat to'la — bu modelga hozir juda ko'p so'rov kelmoqda. "
                "Birozdan keyin qayta urinib ko'ring.")
        self._waiting[model] = n + 1
        try:
            yield
        finally:
            self._waiting[model] = max(0, self._waiting.get(model, 1) - 1)

    def _remaining(self, t0: float) -> float:
        """Seconds left of the overall request deadline."""
        return cfg.REQUEST_DEADLINE - (time.monotonic() - t0)


    async def _wait_for_recovery(self, requested_model: str,
                                 require: Sequence[str],
                                 failed_ports: Optional[set] = None,
                                 deadline: Optional[float] = None) -> bool:
        """Recover a crashed instance and prepare the request for a retry.

        If the browser drops mid-request, waiting for the watchdog's next cycle
        would be far too slow (45s x 3). So the failed port is checked right
        away and, if needed, recovered right here.
        """
        loop = asyncio.get_event_loop()

        # 1) Try to recover the port that failed during the request right away
        for port in sorted(failed_ports or ()):
            try:
                recovered = await asyncio.wait_for(loop.run_in_executor(None, self_heal.fast_recover, port),
                                               timeout=max(0.001, min(cfg.HEAL_WAIT_SECONDS,
                                                   deadline - time.monotonic() if deadline else cfg.HEAL_WAIT_SECONDS)))
            except asyncio.TimeoutError:
                return False
            if recovered:
                try:
                    await self.pool.scan(force=True)
                except Exception:
                    pass
                if self.pick_candidates(requested_model, require):
                    return True

        # 2) If the watchdog is recovering another instance, wait for it
        if not any(self_heal.is_recovering(p) for p in cfg.CDP_PORTS):
            return False

        until = time.monotonic() + cfg.HEAL_WAIT_SECONDS
        if deadline is not None:
            # Do not exceed the overall request deadline
            until = min(until, deadline)
        log.info("instance recovering, holding request for up to %ds",
                 cfg.HEAL_WAIT_SECONDS)
        while time.monotonic() < until:
            await asyncio.sleep(5)
            if not any(self_heal.is_recovering(p) for p in cfg.CDP_PORTS):
                # Recovery finished: rescan so the worker list is refreshed
                try:
                    await self.pool.scan(force=True)
                except Exception:
                    pass
                return bool(self.pick_candidates(requested_model, require))
        return False

    async def _dispatch(
        self,
        prompt_text: str,
        requested_model: str,
        timeout_per_worker: int,
        t0: float,
        retried: bool,
        healed: bool = False,
        require: Sequence[str] = (),
        attachment_paths: Optional[List[str]] = None,
        images_only: bool = False,
        want_images: bool = False,
    ) -> Tuple[str, str, WorkerTab, List[Tuple[str, str]]]:
        candidates = self.pick_candidates(requested_model, require)

        # If every matching worker is in a short cooldown, do not return 503 right away;
        # wait for one to free up. Otherwise a single transient fault also fails the next
        # few requests and the user thinks the model is gone entirely.
        if candidates and not any(w.is_available() for w in candidates):
            wait = self._soonest_cooldown(candidates)
            if wait is not None and wait <= cfg.MAX_COOLDOWN_WAIT:
                log.info("all workers cooling down, waiting %.1fs (model=%s)", wait, requested_model)
                await asyncio.sleep(wait + 0.2)
                candidates = self.pick_candidates(requested_model, require)

        last_error = ""
        tried = 0
        failed_ports: set[int] = set()

        waited_busy = False
        # One bounded queue slot per request, and reselect across ALL accounts on
        # every wakeup. Waiting on one tab's FIFO can strand idle sibling accounts.
        queue_deadline = time.monotonic() + min(cfg.QUEUE_WAIT_SECONDS, max(0, self._remaining(t0) - 1))
        attempted = set()
        while self._remaining(t0) > 0:
            if _stream_committed():
                break
            candidates = [w for w in self.pick_candidates(requested_model, require)
                          if id(w) not in attempted and time.time() >= w.cooldown_until]
            if not candidates:
                break
            worker = next((w for w in candidates if not _is_busy(w)), None)
            if worker is None:
                waited_busy = True
                async with self._queue_slot(requested_model):
                    while time.monotonic() < queue_deadline:
                        await asyncio.sleep(min(0.05, max(0, queue_deadline - time.monotonic())))
                        ready = [w for w in self.pick_candidates(requested_model, require)
                                 if id(w) not in attempted and time.time() >= w.cooldown_until
                                 and not _is_busy(w)]
                        if ready:
                            worker = ready[0]
                            break
                if worker is None:
                    break
            dispatch_lock = _dispatch_lock(worker)
            await dispatch_lock.acquire()
            tab_held = False
            try:
                if dispatch_lock is not worker.lock:
                    await asyncio.wait_for(worker.lock.acquire(), timeout=max(0.001, self._remaining(t0)))
                    tab_held = True
            except BaseException:
                dispatch_lock.release()
                raise
            if not self.circuits.allow(_circuit_key(worker)):
                if tab_held:
                    worker.lock.release()
                dispatch_lock.release()
                attempted.add(id(worker))
                continue
            attempted.add(id(worker))
            tried += 1
            try:
                meta = runmeta.get()
                if requested_model.lower() in cfg.DEEPSEEK_MODEL_ALIASES:
                    meta["upstream_model"] = requested_model.lower()
                try:
                    log.info(
                        "dispatch model=%s port=%d tab=%s requested=%s health=%.2f",
                        worker.model_name, worker.browser_port, worker.short_tab,
                        requested_model, worker.health_score,
                    )
                    ans, images = await asyncio.wait_for(
                        worker.execute_prompt(
                            prompt_text, timeout=timeout_per_worker,
                            attachment_paths=attachment_paths,
                            images_only=images_only, want_images=want_images),
                        timeout=max(0.001, min(float(timeout_per_worker + 15), self._remaining(t0))),
                    )
                    if (ans and ans.strip()) or images:
                        latency = (time.monotonic() - t0) * 1000
                        self.circuits.record_success(_circuit_key(worker))
                        self.metrics.record_success(worker.model_name, latency)
                        resp_model = requested_model if requested_model.lower() in cfg.DEEPSEEK_MODEL_ALIASES else worker.model_name
                        return (worker.adapter.clean_text(ans or ""), resp_model,
                                worker, images)
                    last_error = f"{worker.model_name}: empty answer"
                    self.circuits.record_failure(_circuit_key(worker))
                    self.metrics.record_failure(worker.model_name)
                except asyncio.CancelledError:
                    self.circuits.get(_circuit_key(worker)).release_probe()
                    raise
                except asyncio.TimeoutError:
                    failed_ports.add(worker.browser_port)
                    worker.set_cooldown(30, reason="timeout")
                    last_error = f"{worker.model_name} (port {worker.browser_port}): timeout"
                    log.warning(last_error)
                    self.circuits.record_failure(_circuit_key(worker))
                    self.metrics.record_failure(worker.model_name)
                except Exception as e:
                    failed_ports.add(worker.browser_port)
                    last_error = f"{worker.model_name}: {e}"
                    log.warning("worker_failed %s", last_error)
                    self.circuits.record_failure(_circuit_key(worker))
                    self.metrics.record_failure(worker.model_name)
            finally:
                if tab_held:
                    worker.lock.release()
                dispatch_lock.release()
                # SPEED: prepare a clean chat in the background for the next request (skipped if queued)
                if cfg.PREWARM_ENABLED and hasattr(worker, "prewarm"):
                    asyncio.get_event_loop().create_task(worker.prewarm())

        if _stream_committed():
            log.warning("dispatch stopped: answer already partly streamed, last=%s", last_error or "-")
            raise UpstreamFailed("Model javobi yarmida uzildi. Qayta urinib ko'ring.")

        # Nobody answered. If the fault is transient (the worker is now in a short
        # cooldown), wait and retry once; only then return an error.
        if tried and not retried:
            wait = self._soonest_cooldown(self.pick_candidates(requested_model, require))
            if (wait is not None and wait <= cfg.MAX_COOLDOWN_WAIT
                    and wait + timeout_per_worker < self._remaining(t0)):
                log.info("waiting %.1fs before retry (model=%s)", wait, requested_model)
                await asyncio.sleep(wait + 0.2)
                return await self._dispatch(
                    prompt_text, requested_model, timeout_per_worker, t0, retried=True,
                    require=require, attachment_paths=attachment_paths,
                    images_only=images_only, want_images=want_images)

        # If the instance froze, self-heal is restarting it. Returning 503 then
        # would be wrong: HOLD the request and wait for recovery. For streaming
        # responses the client keeps getting keep-alives ("thinking") meanwhile,
        # so the connection does not drop.
        if not healed and self._remaining(t0) > timeout_per_worker + 10:
            recovered = await self._wait_for_recovery(requested_model, require, failed_ports,
                                                      deadline=t0 + cfg.REQUEST_DEADLINE - timeout_per_worker)
            if recovered:
                log.info("instance recovered, resending request (model=%s)",
                         requested_model)
                return await self._dispatch(
                    prompt_text, requested_model, timeout_per_worker, t0, retried=True,
                    healed=True, require=require, attachment_paths=attachment_paths,
                    images_only=images_only, want_images=want_images)

        # Internal details (provider name, port) go to the log only, never to the client.
        log.warning("dispatch finished: attempts=%d busy=%s last=%s",
                    tried, waited_busy, last_error or "-")
        if tried:
            raise UpstreamFailed("Model javob bera olmadi. Birozdan keyin qayta urinib ko'ring.")
        if waited_busy:
            raise Busy("Model band — navbatda kutish muddati tugadi. Birozdan keyin qayta urinib ko'ring.")
        raise NoWorker("The model is unavailable right now. Try again shortly.")
