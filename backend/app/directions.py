"""Product directions (Free / Core / Code / Pro) — DISABLED FOR NOW.

Directions will be enabled once the evaluation is finished.
With these empty tables the gateway behaves as before: no new names,
no `X-SF-Replacement` header, legacy names unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Direction:
    id: str
    title: str
    primary: str
    replaces: tuple[str, ...]
    summary: str


DIRECTIONS: dict[str, Direction] = {}
LEGACY: dict[str, str] = {}


def resolve(model_id: str) -> tuple[str, str]:
    d = DIRECTIONS.get(model_id)
    return (d.primary, d.id) if d else (model_id, model_id)


def listed(enabled_ids: set[str]) -> list[str]:
    return [d.id for d in DIRECTIONS.values() if d.primary in enabled_ids]


def replacement(model_id: str) -> str | None:
    return LEGACY.get(model_id)
