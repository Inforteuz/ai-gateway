"""Regressions for a container restart leaving one instance's X display down."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest

from cluster import display, self_heal


@pytest.fixture
def xfiles():
    # macOS Unix socket paths must fit in 104 bytes.
    with tempfile.TemporaryDirectory(prefix="agx-", dir="/tmp") as name:
        root = Path(name)
        (root / ".X11-unix").mkdir()
        yield root


def test_stale_lock_with_reused_live_pid_and_dead_socket_is_removed(xfiles):
    lock = xfiles / ".X100-lock"
    lock.write_text(str(os.getpid()))
    endpoint = xfiles / ".X11-unix/X100"
    with socket.socket(socket.AF_UNIX) as old:
        old.bind(str(endpoint))
    other = xfiles / ".X99-lock"
    other.write_text("other instance")

    assert display.clear_stale_display(":100", xfiles)
    assert not lock.exists() and not endpoint.exists()
    assert other.read_text() == "other instance"


def test_live_display_keeps_its_lock_and_socket(xfiles):
    lock = xfiles / ".X100-lock"
    lock.write_text(str(os.getpid()))
    endpoint = xfiles / ".X11-unix/X100"
    with socket.socket(socket.AF_UNIX) as live:
        live.bind(str(endpoint))
        live.listen()
        assert display.clear_stale_display(":100", xfiles) is False
        assert lock.exists() and endpoint.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux abstract sockets")
def test_live_abstract_display_is_not_treated_as_stale(xfiles):
    lock = xfiles / ".X100-lock"
    lock.write_text(str(os.getpid()))
    with socket.socket(socket.AF_UNIX) as live:
        live.bind("\0" + str(xfiles / ".X11-unix/X100"))
        live.listen()
        assert display.clear_stale_display(":100", xfiles) is False
        assert lock.exists()


def test_inconclusive_socket_probe_keeps_lock(monkeypatch, xfiles):
    lock = xfiles / ".X100-lock"
    lock.write_text("123")

    class DeniedSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, address): raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(display.socket, "socket", lambda *args: DeniedSocket())
    with pytest.raises(PermissionError):
        display.clear_stale_display(":100", xfiles)
    assert lock.read_text() == "123"


@pytest.mark.parametrize("value", ["100", ":../100", ":100.0", ":-1"])
def test_invalid_display_cannot_remove_files(value, xfiles):
    with pytest.raises(ValueError):
        display.clear_stale_display(value, xfiles)


def run_helper(tmp_path, states, requests, *, maximum=13, fail_start=""):
    state_path = tmp_path / "state.json"
    calls_path = tmp_path / "calls.jsonl"
    state_path.write_text(json.dumps(states))
    binary = tmp_path / "supervisorctl"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
path = Path(os.environ["TEST_STATE"])
state = json.loads(path.read_text())
command, *names = sys.argv[3:]
with open(os.environ["TEST_CALLS"], "a") as calls:
    calls.write(json.dumps([command, *names]) + "\\n")
if command == "status":
    name = names[0]
    status = state.get(name, "FATAL")
    print(name, status)
    sys.exit(0 if status == "RUNNING" else 3)
if command == "start" and names[0] == os.environ.get("TEST_FAIL_START"):
    sys.exit(1)
for name in names:
    state[name] = "STOPPED" if command == "stop" else "RUNNING"
path.write_text(json.dumps(state))
''')
    binary.chmod(0o755)
    directory = tmp_path / "requests"
    directory.mkdir()
    for request in requests:
        (directory / f"{request}.req").touch()
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "SF_CLUSTER_HEAL_REQ_DIR": str(directory), "SF_MAX_INSTANCES": str(maximum),
           "TEST_STATE": str(state_path), "TEST_CALLS": str(calls_path),
           "TEST_FAIL_START": fail_start}
    script = Path(__file__).resolve().parents[2] / "docker/cluster/heal-watch.sh"
    result = subprocess.run(["bash", str(script), "--once"], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
    assert not list(directory.glob("*.req"))
    return json.loads(state_path.read_text()), [c for c in calls if c[0] != "status"]


def test_recovery_repairs_display_before_starting_browser(tmp_path):
    states = {"xvfb-1": "FATAL", "openbox-1": "FATAL", "x11vnc-1": "FATAL",
              "chrome-1": "FATAL", "chrome-0": "RUNNING"}
    state, calls = run_helper(tmp_path, states, ["1"])
    assert calls == [["stop", "chrome-1", "openbox-1", "x11vnc-1"],
                     ["start", "xvfb-1"], ["start", "openbox-1"],
                     ["start", "x11vnc-1"], ["restart", "chrome-1"]]
    assert all(status == "RUNNING" for status in state.values())


def test_browser_recovery_preserves_running_display(tmp_path):
    states = {f"{name}-1": "RUNNING" for name in ("xvfb", "openbox", "x11vnc", "chrome")}
    _, calls = run_helper(tmp_path, states, ["1"])
    assert calls == [["restart", "chrome-1"]]


def test_browser_is_not_restarted_when_display_cannot_start(tmp_path):
    _, calls = run_helper(tmp_path, {"xvfb-1": "FATAL"}, ["1"], fail_start="xvfb-1")
    assert calls == [["stop", "chrome-1", "openbox-1", "x11vnc-1"], ["start", "xvfb-1"]]


def test_configured_double_digit_instances_can_recover(tmp_path):
    states = {f"{name}-{number}": "RUNNING"
              for name in ("xvfb", "openbox", "x11vnc") for number in (10, 12)}
    _, calls = run_helper(tmp_path, states, ["10", "12", "13", "-1", "01", "1;echo unsafe"])
    assert calls == [["restart", "chrome-10"], ["restart", "chrome-12"]]


def test_default_instance_cap_rejects_unconfigured_number(tmp_path):
    _, calls = run_helper(tmp_path, {}, ["10"], maximum=10)
    assert calls == []


def test_python_restart_accepts_configured_instance_twelve(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "MAX_INSTANCES", 13)
    monkeypatch.setattr(self_heal, "REQUEST_DIR", tmp_path)
    monkeypatch.setattr(self_heal.time, "sleep", lambda _: (tmp_path / "12.req").unlink())
    assert self_heal.request_restart(12) == (True, None)
    assert self_heal.request_restart(13)[0] is False
    assert self_heal.request_restart(-1)[0] is False
