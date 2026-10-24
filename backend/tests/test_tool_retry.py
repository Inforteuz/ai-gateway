"""The model announces the action but never calls the tool.

Measured 2026-10-25 with opencode on a web-chat model: one turn called a tool
and created a file, the next answered only "let me update it and add ..." with
no `actions:` block. The agent had nothing to run and the turn died silently.

The gateway now asks once more. These tests cover the decision and the text of
that second ask; the request path itself is exercised live (the cluster server
has no offline harness).
"""
from __future__ import annotations

from cluster import tools as tool_proto


# --- when to ask again ------------------------------------------------------
def test_a_call_is_never_retried():
    calls = [{"id": "1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]
    assert tool_proto.should_retry(calls, "auto") is False
    assert tool_proto.should_retry(calls, "required") is False


def test_text_only_answer_is_retried_under_auto():
    # Legal as a final answer, and indistinguishable from an announcement —
    # so it is asked again. The owner chose latency over a stalled agent.
    assert tool_proto.should_retry([], "auto") is True
    assert tool_proto.should_retry([], "required") is True
    assert tool_proto.should_retry([], {"function": {"name": "read"}}) is True


def test_client_forbidding_tools_is_respected():
    # `tool_choice: "none"` means the client wants prose; asking again would
    # push the model toward exactly what was forbidden.
    assert tool_proto.should_retry([], "none") is False


# --- what the second ask says ----------------------------------------------
def test_reminder_names_the_available_tools_and_the_block():
    text = tool_proto.retry_instruction(["read", "write", "edit"])
    assert "actions:" in text
    for name in ("read", "write", "edit"):
        assert f"`{name}`" in text


def test_reminder_allows_a_final_answer():
    # Without this the model is cornered into inventing a call when the work
    # is in fact finished.
    text = tool_proto.retry_instruction(["read"])
    assert "actions" in text and "blokisiz" in text


def test_reminder_repeats_a_forced_tool():
    text = tool_proto.retry_instruction(["read", "write"], {"function": {"name": "write"}})
    assert "MAJBURIY" in text and "`write`" in text

    text = tool_proto.retry_instruction(["read"], "required")
    assert "MAJBURIY" in text


def test_reminder_survives_a_long_tool_list():
    names = [f"tool_{i}" for i in range(40)]
    text = tool_proto.retry_instruction(names)
    assert "`tool_0`" in text
    assert len(text) < 2000      # it goes at the end of an already long prompt
