"""Running the benchmark and storing the results.

Results are stored in the database so the benchmark is not re-run every time
the page opens (that would be expensive and slow). To re-run the benchmark,
this module is invoked as an admin command.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .benchmark import TASKS
from .db import Database

# The `benchmark_results` table lives in `schema.sql` (the app role cannot run DDL).


# Pause between requests. The key is limited to 30 requests per minute, so
# requests cannot go faster than one per 2 seconds. Ignoring this once made
# the last two models get nothing but 429 and score 0%: a measurement fault,
# not a quality result.
REQUEST_PAUSE = 2.5
MAX_ATTEMPTS = 5

# HTTP codes WORTH retrying. They have nothing to do with model quality:
#   429 - rate limit
#   502/503/504 - the gateway is restarting or a worker is temporarily busy
# A single service restart during a run must not corrupt the result,
# otherwise we would falsely conclude that "the model is bad".
RETRYABLE_CODES = {429, 500, 502, 503, 504}


def _ask(base: str, key: str, model: str, prompt: str, timeout: int = 150):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 700}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            answer = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            return answer, (time.time() - t0) * 1000, None
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(min(60, 8 * (attempt + 1)))
                continue
            return "", (time.time() - t0) * 1000, f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # Connection dropped (the service may be restarting)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(min(60, 8 * (attempt + 1)))
                continue
            return "", (time.time() - t0) * 1000, type(e).__name__
        except Exception as e:
            return "", (time.time() - t0) * 1000, type(e).__name__
    return "", (time.time() - t0) * 1000, "retries exhausted"


def run(db: Database, base: str, key: str, models: list[str],
        log=print) -> dict:
    """Runs the benchmark on all models and writes the results to the database.

    `db` is e.g. `app.db.from_config(Config())`.
    """
    ts = int(time.time())
    summary: dict[str, dict] = {}

    for model in models:
        by_category: dict[str, list] = {}
        for t in TASKS:
            answer, ms, error = _ask(base, key, model, t.prompt)
            time.sleep(REQUEST_PAUSE)
            try:
                correct = bool(answer) and bool(t.check(answer))
            except Exception:
                correct = False
            by_category.setdefault(t.category, []).append((correct, ms, error))
            mark = "+" if correct else ("x" if not error else "!")
            log(f"    {mark} {t.category:24s} {int(ms):>6}ms"
                + (f"  {error}" if error else ""))

        # If most requests failed for technical reasons (429, timeout), that
        # says nothing about the model's quality: do not store the result,
        # otherwise the page would show a false 0%.
        all_results = [x for results in by_category.values() for x in results]
        technical_errors = sum(1 for x in all_results if x[2])
        if technical_errors > len(all_results) * 0.3:
            log(f"  {model}: unreliable measurement ({technical_errors}/{len(all_results)} "
                f"technical errors), result not saved")
            summary[model] = {}
            continue

        summary[model] = {}
        with db.tx() as conn:
            for k, results in by_category.items():
                correct = sum(1 for x in results if x[0])
                avg_ms = int(sum(x[1] for x in results) / len(results))
                conn.execute(
                    "INSERT INTO benchmark_results(model, kategoriya, jami, togri,"
                    " o_rtacha_ms, yangilandi) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(model, kategoriya) DO UPDATE SET"
                    " jami=excluded.jami, togri=excluded.togri,"
                    " o_rtacha_ms=excluded.o_rtacha_ms, yangilandi=excluded.yangilandi",
                    (model, k, len(results), correct, avg_ms, ts))
                summary[model][k] = (correct, len(results))
        total_correct = sum(v[0] for v in summary[model].values())
        total = sum(v[1] for v in summary[model].values())
        log(f"  {model}: {total_correct}/{total}\n")

    return summary
