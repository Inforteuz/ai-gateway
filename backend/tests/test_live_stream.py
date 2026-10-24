"""Live streaming: text reaches the client while the provider writes it, and what was sent is
always a prefix of the final answer."""
import asyncio
import json
import time

import pytest

from cluster import balancer as B
from cluster import runmeta
from cluster import server as S
from cluster import settings as cfg
from cluster.adapters import base_adapter as BA
from cluster.live import LiveStream
from cluster.pool import CLASS_OK, classify_response

ANSWER = ("Bu birinchi gap, u yetarlicha uzun bo'lishi kerak. Ikkinchi gap ham bor, "
          "va uchinchi gap esa javobni tugatadi. To'rtinchi gap qo'shimcha matn beradi, "
          "beshinchi gap bilan javob nihoyasiga yetadi.")


def make_live(prompt="salom"):
    return LiveStream(finish=lambda t: t,
                      classify=lambda t: classify_response(t, prompt)[0] == CLASS_OK, hold=80, tail=20)


def test_nothing_is_sent_before_enough_text():
    live = make_live()
    live.update(ANSWER[:40])
    assert live.next_piece() == "" and not live.committed


def test_service_message_is_never_streamed():
    live = make_live()
    live.update("You've reached your limit for today. Please try again later. " * 2)
    assert live.next_piece() == ""


def test_pieces_are_prefixes_end_at_whitespace_and_hold_back_the_tail():
    live = make_live()
    meta = {}
    live.meta = meta
    sent = ""
    for n in range(90, len(ANSWER) + 1, 17):
        live.update(ANSWER[:n])
        piece = live.next_piece()
        sent += piece
        assert ANSWER.startswith(sent)
        assert not piece or piece[-1].isspace()
        assert len(sent) <= n - 20
    assert live.committed and meta["stream_committed"] is True
    assert sent + live.remainder(ANSWER) == ANSWER


def test_page_rewriting_sent_text_is_logged_and_stream_continues():
    live = make_live()
    live.update(ANSWER)
    sent = live.next_piece()
    final = "X" + ANSWER[1:]            # the page changed the first character afterwards
    assert sent and live.remainder(final) == final


def collect(gen):
    async def run():
        out = []
        async for item in gen:
            out.append((time.monotonic(), item))
        return out
    return asyncio.run(run())


def contents(events):
    text = ""
    for _, e in events:
        if e.startswith("data: {") and '"content"' in e:
            text += json.loads(e[6:])["choices"][0]["delta"]["content"]
    return text


def test_generator_streams_before_the_answer_is_ready_and_ends_exactly_with_the_final_text():
    live = make_live()
    t0 = time.monotonic()

    async def job():
        for n in range(100, len(ANSWER) + 1, 30):
            live.update(ANSWER[:n])
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)                 # provider "finishes" late
        live.final_answer = ANSWER
        base = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m"}
        return S._sse_chunks(base, [], ANSWER, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    events = collect(S._stream_with_keepalive(job(), "x", 1, "m", live))
    first_content = next(ts for ts, e in events if '"content"' in e)
    last = events[-1][0]
    assert first_content - t0 < 0.5 and last - first_content > 0.4      # first words long before the end
    assert contents(events) == ANSWER                                   # nothing lost or duplicated
    assert events[-1][1] == "data: [DONE]\n\n" and '"finish_reason": "stop"' in events[-3][1]


def test_generator_without_progress_behaves_as_before():
    async def job():
        await asyncio.sleep(0.05)
        base = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m"}
        return S._sse_chunks(base, [], "qisqa javob", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    events = collect(S._stream_with_keepalive(job(), "x", 1, "m", make_live()))
    assert contents(events) == "qisqa javob"


def test_reasoning_streams_before_content_without_replaying_the_trace():
    live = make_live()
    live.meta = {}
    trace = "Avval hisoblayman. Natija tayyor."

    async def job():
        live.update_reasoning(trace[:18])
        await asyncio.sleep(0.05)
        live.update_reasoning(trace)
        await asyncio.sleep(0.05)
        live.update(ANSWER)
        await asyncio.sleep(0.05)
        live.final_answer = ANSWER
        return S._sse_chunks({}, [], ANSWER, {}, reasoning_content=trace)

    events = collect(S._stream_with_keepalive(job(), "x", 1, "deepseek-reasoner", live))
    deltas = [json.loads(e[6:])["choices"][0]["delta"] for _, e in events
              if e.startswith("data: {") and json.loads(e[6:]).get("choices")]
    assert "".join(d.get("reasoning_content", "") for d in deltas) == trace
    assert contents(events) == ANSWER
    assert next(i for i, d in enumerate(deltas) if d.get("reasoning_content")) < next(i for i, d in enumerate(deltas) if d.get("content"))
    assert live.meta["stream_committed"] is True


def test_reasoning_does_not_bypass_answer_classification():
    live = make_live()
    live.update_reasoning("Thinking")
    assert live.next_reasoning() == "Thinking"
    live.update("You've reached your limit for today. Please try again later. " * 2)
    assert live.next_piece() == ""


class _Adapter:
    class caps:
        tool_calling = vision = files = image_gen = temporary_chat = False

    def clean_text(self, t):
        return t


class _Worker:
    def __init__(self, port, commit_then_fail=False):
        self.model_name, self.browser_port, self.short_tab = "gemini", port, "t"
        self.lock = asyncio.Lock()
        self.cooldown_until, self.health_score, self.total_requests, self.last_used = 0.0, 1.0, 0, 0
        self.adapter, self.commit_then_fail, self.calls = _Adapter(), commit_then_fail, 0

    def is_available(self):
        return not self.lock.locked() and time.time() >= self.cooldown_until

    def set_cooldown(self, s, reason=""):
        self.cooldown_until = time.time() + s

    async def execute_prompt(self, prompt, timeout, **kw):
        self.calls += 1
        if self.commit_then_fail:
            runmeta.get()["stream_committed"] = True        # part of the answer already went out
            raise RuntimeError("tab crashed mid-answer")
        return "javob", []


class _Pool:
    def __init__(self, workers):
        self.workers = workers

    async def scan(self, force=False):
        pass

    def workers_for_model(self, m):
        return [w for w in self.workers if w.model_name == m]


def test_committed_request_is_not_moved_to_another_worker(monkeypatch):
    monkeypatch.setattr(B.self_heal, "is_recovering", lambda p: False)
    first, second = _Worker(9222, commit_then_fail=True), _Worker(9223)
    second.total_requests = 5                                   # first is picked first
    bal = B.ClusterBalancer(_Pool([first, second]))

    async def run():
        runmeta.start()
        return await bal.execute("salom", requested_model="gemini", timeout_per_worker=5)

    with pytest.raises(B.UpstreamFailed):
        asyncio.run(run())
    assert first.calls == 1 and second.calls == 0


def test_poll_answer_reports_progress_and_waits_the_initial_delay(monkeypatch):
    texts = iter(["Thinking", "Salom", "Salom dunyo", "Salom dunyo", "Salom dunyo", "Salom dunyo",
                  "Salom dunyo", "Salom dunyo", "Salom dunyo", "Salom dunyo"])

    async def fake_eval(ws, script, timeout=20):
        return {"answer": next(texts, "Salom dunyo"), "generating": False}

    monkeypatch.setattr(BA.cdp, "evaluate", fake_eval)
    monkeypatch.setattr(cfg, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(cfg, "POLL_FIRST_DELAY", 0.05)
    seen = []
    class _A(BA.BaseAIAdapter):
        async def send_prompt(self, ws, text):
            return True

        async def read_response(self, ws, timeout=45):
            return ""

    adapter = object.__new__(_A)

    async def run():
        runmeta.start(on_progress=seen.append)
        t0 = time.monotonic()
        out = await adapter.poll_answer(None, "script", timeout=10, initial_delay=0.6)
        return out, time.monotonic() - t0

    out, took = asyncio.run(run())
    assert out == "Salom dunyo" and seen[:3] == ["Thinking", "Salom", "Salom dunyo"]
    assert took >= 0.6                     # "done" is never decided before the initial delay
    assert seen.index("Thinking") == 0     # but the first look happened early


def test_stream_preflight_ends_at_the_first_live_piece():
    live = make_live()

    async def job():
        await asyncio.sleep(0.1)
        live.update(ANSWER)              # first words ready after 0.1 s
        await asyncio.sleep(5)           # the provider keeps writing for a long time
        return []

    async def run():
        task = asyncio.ensure_future(job())
        t0 = time.monotonic()
        done = await S._preflight_live(task, live, 12)
        took = time.monotonic() - t0
        task.cancel()
        return done, took

    done, took = asyncio.run(run())
    assert done == set() and took < 1.0   # headers go out at once, not after 12 s or at the end
