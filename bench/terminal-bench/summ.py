"""Summaries: harbor job results + proxy log, per job."""
import collections
import glob
import json
import os
import sys

B = os.path.dirname(os.path.abspath(__file__))


def proxy_rows(since=0.0, until=1e12):
    rows = []
    for l in open(f"{B}/proxylog/requests.jsonl"):
        r = json.loads(l)
        if since <= r["t"] <= until and r["path"] != "/v1/models":
            rows.append(r)
    return rows


def trials(job):
    out = []
    for f in glob.glob(f"{B}/jobs/{job}/*/result.json"):
        d = json.load(open(f))
        vr = d.get("verifier_result") or {}
        rew = (vr.get("rewards") or {}).get("reward") if isinstance(vr, dict) else None
        exc = d.get("exception_info") or {}
        out.append({"trial": os.path.basename(os.path.dirname(f)), "task": d.get("task_name"), "reward": rew,
                    "exc": (exc.get("exception_type") or "") + ": " + (exc.get("exception_message") or "")[:160] if exc else "",
                    "start": d.get("started_at"), "end": d.get("finished_at")})
    return out


if __name__ == "__main__":
    for job in sys.argv[1:]:
        print("==", job)
        for t in sorted(trials(job), key=lambda x: x["task"] or ""):
            print(f"  {t['task']:<32} reward={t['reward']} {t['exc']}")
