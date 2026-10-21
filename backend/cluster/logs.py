"""Structured logging. Every record carries time, level, module and an optional request_id."""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

from . import settings as cfg

_request_id: ContextVar[str] = ContextVar("cluster_request_id", default="-")


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def get_request_id() -> str:
    return _request_id.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "lvl": record.levelname,
            "mod": record.name,
            "msg": record.getMessage(),
            "rid": _request_id.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        rid = _request_id.get()
        prefix = f"{ts} {record.levelname:5s} [{record.name}]"
        if rid != "-":
            prefix += f" [{rid}]"
        line = f"{prefix} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_configured = False


def configure() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if cfg.LOG_JSON else _TextFormatter())
    root = logging.getLogger("cluster")
    root.setLevel(cfg.LOG_LEVEL)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False


def get(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(f"cluster.{name}")
