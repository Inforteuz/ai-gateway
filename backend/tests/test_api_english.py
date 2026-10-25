"""Everything /v1 says to a client is English.

/v1 is read by other people's programs: an SDK surfaces the message, an agent CLI
prints it ("Retry due · attempt 4 · Model bilan aloqa uzildi." in opencode,
2026-10-25), a log line carries it. Two routes had escaped the English boundary
in main's ApiError handler:

- errors sent INSIDE a stream (`data: {"error": ...}`) never pass through
  ApiError, so their text was written in Uzbek right where they are raised;
- codes with no API_ERRORS_EN entry and no `en=` kept their Uzbek message.

The panel is untouched: it receives the Uzbek wording and translates it itself.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.common import API_ERRORS_EN

APP = Path(__file__).resolve().parent.parent / "app"
# Modules that raise on the /v1 path
API_MODULES = ["gateway.py", "abuse.py", "billing.py", "admission.py", "local_guard.py", "loops.py",
               "refine.py", "routing.py", "security.py", "fastpath.py"]
# Raised only by panel routes (registration, sessions), never on /v1
PANEL_ONLY = {"too_many_accounts", "email_taken", "csrf"}
# Raised in English on purpose: agent CLIs read the context window out of it
ENGLISH_AT_SOURCE = {"context_length_exceeded"}

UZBEK = re.compile(r"[oOgG][ʻ‘'’]|\b(?:so'rov|soniya|qayta|urinib|bilan|uzildi|emas|kerak|qoldi|to'xtatildi)\b")


def _text(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return ""


def test_errors_inside_a_stream_are_english():
    found = []
    for name in API_MODULES:
        path = APP / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            # {"error": {"message": "..."}} literals and err_event("...", code) calls
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "message" and UZBEK.search(_text(v)):
                        found.append(f"{name}:{node.lineno}: {_text(v)!r}")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "err_event" and node.args:
                if UZBEK.search(_text(node.args[0])):
                    found.append(f"{name}:{node.lineno}: {_text(node.args[0])!r}")
    assert found == []


def test_every_api_error_code_has_english_wording():
    missing = []
    for name in API_MODULES:
        path = APP / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApiError" and len(node.args) >= 2):
                continue
            code = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
            if not isinstance(code, str) or code in PANEL_ONLY or code in ENGLISH_AT_SOURCE:
                continue
            if code not in API_ERRORS_EN and not any(k.arg == "en" for k in node.keywords):
                missing.append(f"{name}:{node.lineno}: {code}")
    assert missing == []


def test_unknown_v1_path_answers_in_english(env):
    r = env.client.get("/v1/no-such-endpoint")
    assert r.status_code == 404 and not UZBEK.search(r.json()["error"]["message"])
