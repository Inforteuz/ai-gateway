"""Repetition-loop detection: stop generation early when a model starts repeating the same text forever.

Only catches EXACT repetition (the tail is at least MIN_REPEATS consecutive copies of one chunk): a common
failure of small models that runs until max_tokens. Ordinary text, tables and lists do not trigger it.
"""
from __future__ import annotations

MAX_PERIOD = 400     # longest repeating chunk (characters)
MIN_REPEATS = 4      # this many consecutive repeats count as a loop
# Was 600: legitimate repetition in code (a zero matrix, "[0, 0, 0, 0, 0, 0, 0, 0],\n" x 25) exceeded
# that threshold and cut correct answers mid-way. A real broken loop runs until max_tokens,
# so waiting for more evidence is cheap.
MIN_WINDOW = 1500    # the inspected tail is at least this many characters
TINY_PERIOD = 8      # chunks shorter than this ("0, 0, 0, ...", "-----") also occur in correct answers,
TINY_WINDOW = 3000   # so they need more evidence
TAIL = max(MAX_PERIOD * MIN_REPEATS, TINY_WINDOW)


def _tiny_unit(unit: str) -> bool:
    """Is the chunk really a repeat of a very short pattern (e.g. "0, 0, 0, " = "0, " x3)?"""
    n = len(unit)
    return any(n % q == 0 and unit == unit[:q] * (n // q) for q in range(1, TINY_PERIOD))


def looping_period(text: str) -> int:
    """Length of the chunk if the text ends in exact repeats of a short chunk; otherwise 0."""
    tail = text[-TAIL:]
    n = len(tail)
    for p in range(1, MAX_PERIOD + 1):
        w = max(MIN_REPEATS * p, MIN_WINDOW)
        if n < w:
            continue
        seg = tail[-w:]
        if seg[:-p] != seg[p:]:
            continue
        if _tiny_unit(seg[-p:]):                 # very short pattern (or a multiple of one): needs more evidence
            if n < TINY_WINDOW:
                continue
            seg = tail[-TINY_WINDOW:]
            if seg[:-p] != seg[p:]:
                continue
        return p
    return 0


def is_looping(text: str) -> bool:
    return looping_period(text) > 0
