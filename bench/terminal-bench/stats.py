"""Proxy log stats per (client, model): calls, statuses, stops, tool-call JSON problems, errors."""
import collections
import json
import os
import sys
import time

B = os.path.dirname(os.path.abspath(__file__))
since = time.time() - float(sys.argv[1]) * 60 if len(sys.argv) > 1 else 0
rows = [json.loads(l) for l in open(f"{B}/proxylog/requests.jsonl")]
rows = [r for r in rows if r["t"] >= since and r["path"] != "/v1/models" and r.get("method", "POST") == "POST"]
g = collections.defaultdict(list)
for r in rows:
    ua = r.get("hdr", {}).get("user-agent", "?").split("/")[0][:14]
    g[(ua, r["model"])].append(r)
print(f"{'client':<15}{'model':<11}{'calls':>6}{'200':>5}{'4xx':>5}{'5xx':>5}{'tc':>5}{'badjs':>6}{'errev':>6}{'len':>5}  statuses/errors")
for (ua, m), rs in sorted(g.items()):
    st = collections.Counter(r.get("status") for r in rs)
    stops = collections.Counter(str(r.get("stop")) for r in rs)
    errs = collections.Counter(str(r.get("error") or r.get("upstream_exc") or "")[:70] for r in rs if r.get("error") or r.get("upstream_exc"))
    ok = st.get(200, 0)
    c4 = sum(v for k, v in st.items() if k and 400 <= k < 500)
    c5 = sum(v for k, v in st.items() if k and k >= 500)
    print(f"{ua:<15}{m:<11}{len(rs):>6}{ok:>5}{c4:>5}{c5:>5}{sum(r.get('tool_calls') or 0 for r in rs):>5}"
          f"{sum(r.get('bad_tool_json') or 0 for r in rs):>6}{sum(r.get('error_events') or 0 for r in rs):>6}"
          f"{stops.get('length', 0) + stops.get('max_tokens', 0) + stops.get('incomplete', 0):>5}  {dict(st)} {dict(errs)}")
