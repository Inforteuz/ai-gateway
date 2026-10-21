"""A prompt longer than the provider's input field goes partly into an attached context.txt."""
import asyncio
import os

from cluster import pool as pool_mod
from cluster import settings as cfg
from cluster.adapters.base_adapter import Capabilities
from cluster.compose import OVERFLOW_FILE_NAME, OVERFLOW_NOTE, fit_prompt, split_for_file

SYSTEM = "[System]:\n" + "Ko'rsatma qatori. " * 600            # ~11 000 chars
HISTORY = "\n\n[History]:\n" + "\n\n".join(f"User: savol {i} " + "x" * 900 for i in range(30))
TOOLS = "\n\n[Vositalar — MAJBURIY protokol]\nread_file: path"
TASK = "\n\n[Current task]:\nhello.py faylini yarat"
PROMPT = SYSTEM + HISTORY + TOOLS + TASK
LIMIT = 31000


def test_split_moves_system_and_history_to_the_file_without_losing_anything():
    text, content = split_for_file(PROMPT, LIMIT)
    assert len(PROMPT) > LIMIT and len(text) <= LIMIT
    assert text.startswith(OVERFLOW_NOTE) and text.endswith(TASK) and TOOLS in text
    assert content.strip() == (SYSTEM + HISTORY).strip()          # every history entry is kept
    assert "savol 0 " in content and "savol 29 " in content
    # The old way would have dropped the oldest history
    assert "savol 0 " not in fit_prompt(PROMPT, LIMIT)


def test_no_split_when_it_fits_or_the_tail_alone_is_too_long():
    assert split_for_file("[System]:\nqisqa" + TASK, LIMIT) is None
    huge_task = "\n\n[Current task]:\n" + "y" * 40000
    assert split_for_file(SYSTEM + HISTORY + huge_task, LIMIT) is None
    assert split_for_file("x" * 50000, LIMIT) is None          # no sections to split on


class _Adapter:
    MAX_INPUT_CHARS = LIMIT
    caps = Capabilities(files=True)


def _tab(adapter):
    tab = object.__new__(pool_mod.WorkerTab)
    tab.adapter, tab.model_name = adapter, "gemini"
    return tab


def test_worker_attaches_context_file_and_removes_it_afterwards(monkeypatch):
    tab = _tab(_Adapter())
    seen = {}

    async def fake_execute(prompt_text, send_text, timeout, paths, images_only, want_images):
        path = paths[-1]
        seen.update(full=prompt_text, sent=send_text, paths=list(paths),
                    content=open(path, encoding="utf-8").read(), mode=oct(os.stat(path).st_mode & 0o777))
        return "ok", []

    monkeypatch.setattr(tab, "_execute_prompt", fake_execute)
    assert asyncio.run(tab.execute_prompt(PROMPT, 30, attachment_paths=["/tmp/user.pdf"])) == ("ok", [])
    assert seen["full"] == PROMPT and len(seen["sent"]) <= LIMIT and seen["sent"].endswith(TASK)
    assert seen["paths"][0] == "/tmp/user.pdf" and seen["paths"][1].endswith(OVERFLOW_FILE_NAME)
    assert "savol 0 " in seen["content"] and seen["mode"] == "0o600"
    assert not os.path.exists(seen["paths"][1])                 # cleaned up


def test_file_is_removed_even_when_the_request_fails(monkeypatch):
    tab = _tab(_Adapter())
    seen = {}

    async def failing(prompt_text, send_text, timeout, paths, images_only, want_images):
        seen["path"] = paths[-1]
        raise RuntimeError("provider down")

    monkeypatch.setattr(tab, "_execute_prompt", failing)
    try:
        asyncio.run(tab.execute_prompt(PROMPT, 30))
    except RuntimeError:
        pass
    assert not os.path.exists(seen["path"])


def test_providers_without_files_images_and_switch_off_keep_the_old_behaviour(monkeypatch):
    class NoFiles(_Adapter):
        caps = Capabilities(files=False)

    for tab, kw in ((_tab(NoFiles()), {}), (_tab(_Adapter()), {"want_images": True}),
                    (_tab(_Adapter()), {"images_only": True})):
        text, tmp = tab._overflow_to_file(PROMPT, None, kw.get("images_only", False), kw.get("want_images", False))
        assert text == PROMPT and tmp is None
    monkeypatch.setattr(cfg, "OVERFLOW_TO_FILE", False)
    assert _tab(_Adapter())._overflow_to_file(PROMPT, None, False, False) == (PROMPT, None)


def test_attachment_slots_full_falls_back_to_trimming(monkeypatch):
    paths = [f"/tmp/f{i}" for i in range(cfg.MAX_ATTACHMENTS)]
    assert _tab(_Adapter())._overflow_to_file(PROMPT, paths, False, False) == (PROMPT, None)
