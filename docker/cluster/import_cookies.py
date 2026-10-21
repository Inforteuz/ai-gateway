"""Import an existing browser session into a cluster Chrome instance.

Use this instead of logging in through VNC: export the session from your own
browser and load it into instance N. Supported inputs:

  * cookies as JSON - the array exported by "Cookie-Editor" / "EditThisCookie"
    style extensions, or a CDP-style {"cookies": [...]} object;
  * cookies in Netscape `cookies.txt` format;
  * --local-storage FILE.json - {"key": "value", ...} written into the
    provider's localStorage (for sites that keep a JWT/session token there);
  * --token KEY=VALUE - a single localStorage entry, may be repeated.

Run inside the cluster container (manage.sh does this for you):

  python /opt/gateway/bin/import_cookies.py --instance 0 --channel qwen cookies.json
  python /opt/gateway/bin/import_cookies.py --instance 1 --channel deepseek --token userToken='{"value":"..."}'

Values are never printed. Only cookies whose domain belongs to the channel's
site are imported unless --all-domains is given.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from urllib.parse import urlparse

import websockets

HOME_URLS = {
    "chatgpt": "https://chatgpt.com/",
    "deepseek": "https://chat.deepseek.com/",
    "qwen": "https://chat.qwen.ai/",
    "gemini": "https://gemini.google.com/app",
    "zai": "https://chat.z.ai/",
    "grok": "https://grok.com/",
    "deepai": "https://deepai.org/chat",
    "genspark": "https://www.genspark.ai/",
    "chatbotapp": "https://chatbotappai.ai/",
    "chatbotchatapp": "https://chatbotchatapp.com/",
}
# Cookie domains that belong to a channel (login often lives on a parent or SSO domain)
CHANNEL_DOMAINS = {
    "chatgpt": ("chatgpt.com", "openai.com"),
    "deepseek": ("deepseek.com",),
    "qwen": ("qwen.ai", "aliyun.com", "alibaba.com"),
    "gemini": ("google.com", "gemini.google.com"),
    "zai": ("z.ai",),
    "grok": ("grok.com", "x.ai", "x.com"),
    "deepai": ("deepai.org",),
    "genspark": ("genspark.ai",),
    "chatbotapp": ("chatbotappai.ai",),
    "chatbotchatapp": ("chatbotchatapp.com",),
}
SAME_SITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None", "unspecified": None}


def parse_json_cookies(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of cookies")
    out = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c or not c.get("domain"):
            continue
        item = {"name": str(c["name"]), "value": str(c["value"]), "domain": str(c["domain"]),
                "path": str(c.get("path") or "/"), "secure": bool(c.get("secure", False)),
                "httpOnly": bool(c.get("httpOnly", False))}
        exp = c.get("expirationDate", c.get("expires"))
        if isinstance(exp, (int, float)) and exp > 0 and not c.get("session"):
            item["expires"] = float(exp)
        ss = SAME_SITE.get(str(c.get("sameSite") or "unspecified").lower())
        if ss:
            item["sameSite"] = ss
            if ss == "None":
                item["secure"] = True
        out.append(item)
    return out


def parse_netscape(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        item = {"name": name, "value": value.rstrip("\n"), "domain": domain, "path": path or "/",
                "secure": secure.upper() == "TRUE", "httpOnly": http_only}
        if expires.isdigit() and int(expires) > 0:
            item["expires"] = float(expires)
        out.append(item)
    return out


def load_cookies(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return parse_json_cookies(json.loads(raw))
    return parse_netscape(raw)


def belongs(domain: str, channel: str) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == base or d.endswith("." + base) for base in CHANNEL_DOMAINS.get(channel, ()))


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self.n += 1
        my_id = self.n
        await self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(method)


def cdp_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


async def run(args) -> int:
    port = 9222 + args.instance
    home = HOME_URLS[args.channel]
    cookies = load_cookies(args.cookies) if args.cookies else []
    if cookies and not args.all_domains:
        skipped = [c for c in cookies if not belongs(c["domain"], args.channel)]
        cookies = [c for c in cookies if belongs(c["domain"], args.channel)]
        if skipped:
            print(f"skipped {len(skipped)} cookie(s) from other domains (use --all-domains to keep them)")
    storage: dict[str, str] = {}
    if args.local_storage:
        storage.update({str(k): v if isinstance(v, str) else json.dumps(v)
                        for k, v in json.load(open(args.local_storage, encoding="utf-8")).items()})
    for pair in args.token or []:
        if "=" not in pair:
            raise SystemExit(f"--token expects KEY=VALUE, got: {pair.split('=')[0]}")
        k, v = pair.split("=", 1)
        storage[k] = v
    if not cookies and not storage:
        raise SystemExit("nothing to import: give a cookies file, --local-storage or --token")

    browser_ws = cdp_json(port, "/json/version")["webSocketDebuggerUrl"]
    async with websockets.connect(browser_ws, max_size=None) as ws:
        cdp = Cdp(ws)
        if cookies:
            await cdp.call("Storage.setCookies", {"cookies": cookies})
            print(f"imported {len(cookies)} cookie(s) into instance {args.instance}")

    host = urlparse(home).hostname or ""
    tabs = [t for t in cdp_json(port, "/json") if t.get("type") == "page"]
    tab = next((t for t in tabs if (urlparse(t.get("url", "")).hostname or "").endswith(host)), None)
    if tab is None:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{home}", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.load(r)
        await asyncio.sleep(5)

    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = Cdp(ws)
        if storage:
            # localStorage is per origin: make sure the tab is on the provider's site first
            cur = (await cdp.call("Runtime.evaluate", {"expression": "location.hostname", "returnByValue": True}))
            if not str(cur.get("result", {}).get("value", "")).endswith(host):
                await cdp.call("Page.navigate", {"url": home})
                await asyncio.sleep(5)
            expr = "(() => { const d = %s; for (const k in d) localStorage.setItem(k, d[k]); return Object.keys(d).length; })()" % json.dumps(storage)
            res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            print(f"wrote {res.get('result', {}).get('value')} localStorage entr(y/ies) on {host}")
        await cdp.call("Page.navigate", {"url": home})
    print("reloaded the provider tab; check the session in the admin panel (Instances -> Probe)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Import a browser session into a cluster Chrome instance")
    p.add_argument("--instance", type=int, required=True, help="instance number (0-9), CDP port 9222+N")
    p.add_argument("--channel", required=True, choices=sorted(HOME_URLS), help="web-chat channel of that instance")
    p.add_argument("cookies", nargs="?", help="cookies file (JSON or Netscape cookies.txt), '-' for stdin")
    p.add_argument("--local-storage", help="JSON object with localStorage entries")
    p.add_argument("--token", action="append", help="localStorage entry KEY=VALUE (repeatable)")
    p.add_argument("--all-domains", action="store_true", help="also import cookies of unrelated domains")
    args = p.parse_args()
    if not 0 <= args.instance <= 9:
        p.error("--instance must be 0-9")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
