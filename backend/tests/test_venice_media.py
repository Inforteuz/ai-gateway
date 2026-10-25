"""Keep idle decorations cheap without pausing conversation media or breaking sends."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cluster.adapters import venice_adapter as va


@pytest.mark.parametrize("healthy", [True, False])
def test_session_probe_installs_media_guard_only_after_healthy_session(monkeypatch, healthy):
    calls = []

    async def session(self, ws, timeout, recover):
        assert timeout == 7 and recover is False
        return healthy, "ok" if healthy else "login_required"

    async def evaluate(ws, script, timeout):
        calls.append(script)

    monkeypatch.setattr(va.BaseAIAdapter, "check_session", session)
    monkeypatch.setattr(va.cdp, "evaluate", evaluate)
    result = asyncio.run(va.VeniceAdapter().check_session(None, timeout=7, recover=False))
    assert result[0] is healthy
    assert calls == ([va.QUIET_BACKGROUND_JS] if healthy else [])


@pytest.mark.parametrize("turns", [0, 1])
def test_prewarm_reinstalls_guard_without_changing_freshness(monkeypatch, turns):
    calls = []

    async def evaluate(ws, script, timeout):
        calls.append(script)
        return turns if script != va.QUIET_BACKGROUND_JS else None

    monkeypatch.setattr(va.cdp, "evaluate", evaluate)
    assert asyncio.run(va.VeniceAdapter().is_fresh_chat(None)) is (turns == 0)
    assert calls[0] == va.QUIET_BACKGROUND_JS


@pytest.mark.parametrize("guard_fails", [False, True])
def test_optional_media_guard_never_blocks_prompt_submission(monkeypatch, guard_fails):
    calls = []
    keys = []

    async def evaluate(ws, script, timeout):
        calls.append(script)
        if script == va.QUIET_BACKGROUND_JS:
            if guard_fails:
                raise asyncio.TimeoutError()
            return None
        return 12 if "setter.call" in script else 0

    async def key(ws, name):
        keys.append(name)

    async def sleep(seconds):
        pass

    monkeypatch.setattr(va, "cdp", SimpleNamespace(evaluate=evaluate, key=key))
    monkeypatch.setattr(va, "asyncio", SimpleNamespace(sleep=sleep))
    assert asyncio.run(va.VeniceAdapter().send_prompt(None, "test request"))
    assert calls[0] == va.QUIET_BACKGROUND_JS and keys == ["Enter"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runs the browser guard regression")
def test_media_guard_selection_and_spa_restarts():
    # Run the actual JS against a small DOM fixture: no network or real videos.
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Video {
    constructor(props = {}) {
        Object.assign(this, {loop: true, muted: true, controls: false, paused: false,
            hidden: true, inChat: false, pauses: 0,
            src: 'https://media.venice.ai/assets/light-waves-loop-v2.mp4'}, props);
    }
    closest(selector) { return selector === '[aria-hidden="true"]' ? this.hidden : this.inChat; }
    pause() { this.paused = true; this.pauses++; }
}
function page(videos) {
    const listeners = {};
    return {window: {}, HTMLVideoElement: Video, URL,
        location: {href: 'https://venice.ai/'}, listeners,
        document: {
            querySelectorAll: () => videos,
            addEventListener: (event, callback, capture) => {
                assert.equal(capture, true);
                (listeners[event] ||= []).push(callback);
            }
        }
    };
}
const decoration = new Video();
const protectedVideos = [
    new Video({inChat: true}), new Video({hidden: false}),
    new Video({controls: true}), new Video({muted: false}), new Video({loop: false}),
    new Video({src: 'https://media.venice.ai/generated/result.mp4'}),
    new Video({src: 'https://example.com/assets/light-waves-loop-v2.mp4'}),
    new Video({src: 'not a valid media URL'}),
];
const p = page([decoration, ...protectedVideos]);
vm.runInNewContext(source, p);
vm.runInNewContext(source, p);
assert.equal(decoration.pauses, 1);
assert(protectedVideos.every(v => v.pauses === 0));
assert.equal(p.listeners.play.length, 1);
assert.equal(p.listeners.loadeddata.length, 1);
const remounted = new Video();
p.listeners.play[0]({target: remounted});
assert.equal(remounted.pauses, 1);
remounted.paused = false;
p.listeners.loadeddata[0]({target: remounted});
assert.equal(remounted.pauses, 2);
for (const video of protectedVideos) p.listeners.play[0]({target: video});
p.listeners.play[0]({target: {}});
assert(protectedVideos.every(v => v.pauses === 0));
const afterNavigation = new Video();
const newPage = page([afterNavigation]);
vm.runInNewContext(source, newPage);
assert.equal(afterNavigation.pauses, 1);
assert.equal(newPage.listeners.play.length, 1);
"""
    subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(va.QUIET_BACKGROUND_JS),
                   text=True, capture_output=True, check=True, timeout=10)
