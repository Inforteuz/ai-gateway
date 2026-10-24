"""Live streaming: send the answer while the provider is still writing it.

The adapter reports the answer-so-far on every DOM poll (`on_progress`). This module
decides what may already go to the client, so that what is streamed is always a prefix
of the final answer:

  * nothing is sent until the text is LIVE_STREAM_HOLD_CHARS long and does not look like a
    provider service message (quota, login, rate limit) or a degenerate answer;
  * the text goes through the same finishing as the final answer (brand mask, fences);
  * the newest LIVE_STREAM_TAIL_CHARS are held back (the page may still re-render them)
    and a piece always ends at whitespace;
  * once anything was sent the request is "committed": the balancer no longer moves it to
    another worker, whose answer could not continue this one.

At the end the remainder of the final answer is sent. If the page changed text that was
already sent (rare), the stream continues from the first differing character and the
divergence is logged.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from . import logs
from . import settings as cfg

log = logs.get("live")


class LiveStream:
    def __init__(self, finish: Callable[[str], str], classify: Callable[[str], bool],
                 hold: Optional[int] = None, tail: Optional[int] = None):
        self._finish = finish            # same finishing as the final answer (mask, fences)
        self._is_answer = classify       # False for service messages / degenerate text
        self.hold = cfg.LIVE_STREAM_HOLD_CHARS if hold is None else hold
        self.tail = cfg.LIVE_STREAM_TAIL_CHARS if tail is None else tail
        self.latest = ""
        self.emitted = ""
        self.final_answer: Optional[str] = None
        self.changed = asyncio.Event()
        self.meta: Optional[dict] = None     # the request's runmeta, gets "stream_committed"
        self.reasoning = ""
        self.reasoning_emitted = ""

    def update_reasoning(self, text: str) -> None:
        self.reasoning = text or ""
        self.changed.set()

    def next_reasoning(self) -> str:
        piece = self.reasoning[len(self.reasoning_emitted):]
        if piece:
            self.reasoning_emitted = self.reasoning
            if self.meta is not None:
                self.meta["stream_committed"] = True
        return piece

    @property
    def committed(self) -> bool:
        return bool(self.emitted or self.reasoning_emitted)

    def update(self, text: str) -> None:
        """on_progress callback (runs in the event loop)."""
        self.latest = text or ""
        self.changed.set()

    def has_piece(self) -> bool:
        """Would next_piece() return text now? (does not consume it)"""
        return bool(self._candidate()[1] or len(self.reasoning) > len(self.reasoning_emitted))

    def next_piece(self) -> str:
        """Text that may be sent now (possibly empty)."""
        safe, piece = self._candidate()
        if not piece:
            return ""
        self.emitted = safe
        if self.meta is not None:
            self.meta["stream_committed"] = True
        return piece

    def _candidate(self) -> tuple[str, str]:
        text = self.latest
        if not self.emitted:
            if len(text) < self.hold or not self._is_answer(text):
                return "", ""
        safe = self._finish(text)
        safe = safe[: max(0, len(safe) - self.tail)]
        cut = max(safe.rfind(" "), safe.rfind("\n"))
        safe = safe[: cut + 1] if cut >= 0 else ""
        if len(safe) <= len(self.emitted) or not safe.startswith(self.emitted):
            return "", ""
        return safe, safe[len(self.emitted):]

    def remainder(self, final: str) -> str:
        """What is left to send once the final answer is known."""
        if final.startswith(self.emitted):
            return final[len(self.emitted):]
        common = 0
        for a, b in zip(final, self.emitted):
            if a != b:
                break
            common += 1
        log.warning("live stream diverged at char %d of %d sent (final %d chars)",
                    common, len(self.emitted), len(final))
        return final[common:]
