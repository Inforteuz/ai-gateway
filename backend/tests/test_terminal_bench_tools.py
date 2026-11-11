"""Web-chat tool protocol failures seen in the Terminal-Bench pilot (2026-11-12, Codex + sable-3.5).

1. The model wrote its `actions:` block without a code fence. The bare-block pattern stopped at the
   first blank line, so a heredoc command lost everything after its first paragraph and still ran:
   bash warned "here-document delimited by end-of-file" and the file was written half-empty.
2. Codex refused one call ("approval policy is Never; reject command ..."). The loop guard did not
   read that as a failure, blocked the model's corrected retry as a "repeat" and ended the agent
   turn with "I did not repeat this action".
"""
from __future__ import annotations

import json

from cluster import tools

HEREDOC_BLOCK = """actions:
  - name: exec_command
    arguments:
      cmd: |
        cat > _includes/about.md << 'EOF'
        I am a Postdoctoral Researcher.

        Previously, I was a Student Researcher.
        EOF
        git add _includes/about.md
"""


def _cmd(text):
    calls, leftover = tools.parse_tool_calls(text, ["exec_command"])
    assert len(calls) == 1, text
    return json.loads(calls[0]["function"]["arguments"])["cmd"], leftover


def test_unfenced_block_keeps_blank_lines_of_a_multiline_value():
    cmd, leftover = _cmd(HEREDOC_BLOCK)
    assert cmd.endswith("Previously, I was a Student Researcher.\nEOF\ngit add _includes/about.md\n")
    assert "Researcher.\n\nPreviously" in cmd and leftover == ""


def test_unfenced_block_between_commentary_still_ends_at_unindented_text():
    cmd, leftover = _cmd("I'll resolve the conflict.\n\n" + HEREDOC_BLOCK + "\nThen I will commit.")
    assert cmd.count("EOF") == 2 and "Then I will commit" not in cmd
    assert "resolve the conflict" in leftover and "Then I will commit." in leftover


def test_fenced_block_is_unchanged():
    cmd, _ = _cmd("```yaml\n" + HEREDOC_BLOCK + "```")
    assert "Researcher.\n\nPreviously" in cmd


def _guarded(result_text):
    call = {"id": "a", "type": "function", "function": {
        "name": "exec_command", "arguments": json.dumps({"cmd": "make", "sandbox_permissions": "require_escalated"})}}
    again = {**call, "id": "b"}
    messages = [{"role": "user", "content": "build it"},
                {"role": "assistant", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "a", "content": result_text}]
    exact, target = tools.repeated_successful_actions([again], messages)
    return bool(exact or target)


def test_refused_or_failed_commands_stay_retryable():
    for text in ["approval policy is Never; reject command — you cannot ask for escalated permissions",
                 "Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 2\nOutput:\nmake: *** No rule",
                 "Process exited with code 0\nOutput:\n/bin/bash: line 3: warning: here-document at line 2 "
                 "delimited by end-of-file (wanted `EOF')",
                 "`justification` requires an explicit `sandbox_permissions`; use ... or omit `justification`."]:
        assert not _guarded(text), text


def test_a_real_successful_repeat_is_still_stopped():
    assert _guarded("Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\nbuilt ok\n")
