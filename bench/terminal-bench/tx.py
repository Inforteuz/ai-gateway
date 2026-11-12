"""Compact transcript of a harbor trial (codex / claude-code / opencode)."""
import json, sys, glob, os
d = sys.argv[1]
f = (glob.glob(d + "/agent/codex.txt") + glob.glob(d + "/agent/claude-code.txt") + glob.glob(d + "/agent/opencode.txt") + [None])[0]
if not f:
    print("no agent log", os.listdir(d + "/agent")); sys.exit()
for l in open(f):
    try: o = json.loads(l)
    except ValueError:
        if l.strip(): print("RAW", l.strip()[:200])
        continue
    t = o.get("type")
    if t == "item.completed":
        it = o["item"]
        if it["type"] == "command_execution": print("CMD", it["command"][:160], "->", it.get("exit_code"), it.get("aggregated_output", "")[:160].replace("\n", " | "))
        elif it["type"] == "agent_message": print("MSG", it["text"][:300].replace("\n", " "))
        elif it["type"] == "file_change": print("EDIT", json.dumps(it.get("changes"))[:200])
        else: print(it["type"].upper(), json.dumps(it)[:250])
    elif t == "assistant":
        for b in o["message"]["content"]:
            if b["type"] == "text": print("MSG", b["text"][:300].replace("\n", " "))
            elif b["type"] == "tool_use": print("TOOL", b["name"], json.dumps(b["input"])[:220])
    elif t == "user" and isinstance(o["message"]["content"], list):
        for b in o["message"]["content"]:
            if b.get("type") == "tool_result": print("  RES", "ERR" if b.get("is_error") else "", str(b.get("content"))[:200].replace("\n", " | "))
    elif t == "result": print("RESULT", o.get("subtype"), o.get("num_turns"), str(o.get("result"))[:200])
    elif t == "tool_use": 
        p = o["part"]; st = p.get("state", {})
        print("TOOL", p.get("tool"), json.dumps(st.get("input"))[:200], "->", str(st.get("output"))[:160].replace("\n", " | "))
    elif t == "text": print("MSG", o["part"]["text"][:300].replace("\n", " "))
    elif t in ("error", "turn.failed"): print("ERROR", json.dumps(o)[:300])
