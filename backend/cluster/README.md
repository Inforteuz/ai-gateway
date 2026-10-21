# AI Cluster Gateway

A gateway that keeps several web-chat providers (ChatGPT, DeepSeek, Qwen, Grok, etc.) open in
Chromium browsers and exposes them as an **OpenAI-compatible API**.

The gateway backend connects to it over HTTP (the `cluster_base_url` setting) and uses it for
models whose provider is `"cluster"`.

## How it works

1. The operator starts several Chromium instances with CDP enabled, using `start_cluster.sh` or
   `browser_manager.py` (default ports: `9222–9225`).
2. Each browser holds one or more tabs; each tab has a web-chat site open, logged in by hand.
3. The gateway discovers the tabs over CDP and registers each one as a "worker".
4. When a client calls `/v1/chat/completions`, the balancer picks a free worker for the requested
   model, opens a fresh chat, types the prompt through CDP and reads the answer back from the DOM.

Along the way it adds a brand system prompt, masks provider names in self-introductions, emulates
OpenAI tool calling via YAML blocks, enforces `response_format`, uploads attachments, caches
identical requests, and applies circuit breakers, cooldowns and per-account pacing.

## Running

```bash
cd backend
export SF_CLUSTER_API_KEY="secret-token"     # the gateway backend uses the same key
export SF_CLUSTER_PORT=8800
python -m cluster.server
```

Or from the repository root:

```bash
python -m backend.cluster.server
```

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `SF_CLUSTER_HOST` | `127.0.0.1` | Listen address |
| `SF_CLUSTER_PORT` | `8800` | Port |
| `SF_CLUSTER_API_KEY` | `` (empty) | Bearer token. Empty disables auth (dev only) |
| `SF_CLUSTER_CDP_PORTS` | `9222,9223,9224,9225` | Chromium CDP ports |
| `SF_CLUSTER_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING |
| `SF_CLUSTER_LOG_JSON` | `0` | `1` writes JSON logs |
| `SF_CLUSTER_REQUEST_TIMEOUT` | `60` | Maximum wait per worker (s) |
| `SF_CLUSTER_REQUEST_DEADLINE` | `200` | Overall deadline per request, including queue and retries (s) |
| `SF_CLUSTER_SCAN_INTERVAL` | `30` | How often tabs are rescanned (s) |
| `SF_CLUSTER_CACHE` | `1` | Response cache on/off |
| `SF_CLUSTER_PROXIES_FILE` | `/var/lib/ai-gateway/cluster/proxies.txt` | Proxy list file |
| `SF_CLUSTER_MEM_GUARD_INTERVAL` | `120` | Renderer memory check interval (s); `0` disables auto-recovery |
| `SF_CLUSTER_MEM_GUARD_MB` | `900` | When a site renderer's RSS+swap exceeds this, the tab is reopened in a new renderer |
| `SF_CLUSTER_MEM_GUARD_MIN_GAP` | `1800` | Minimum time between auto-recoveries of one instance (s) |
| `SF_CLUSTER_MEM_GUARD_MAX_PER_DAY` | `6` | Daily auto-recovery cap per instance |
| `SF_CLUSTER_MEM_GUARD_RESTART` | `9` | Instances whose recovery restarts the Chrome unit (only those hosting Gemini; others hold personal logins) |
| `SF_CLUSTER_MEM_GUARD_STATE` | `/var/lib/ai-gateway/cluster/memory_guard.json` | Interval, counter and block state (survives restarts) |

See `settings.py` for the full list (queue limits, attachment limits, pacing, image timeouts,
fallback models, prewarming).

## HTTP API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | none | Liveness probe (worker count) |
| GET | `/cluster/status` | Bearer | Full statistics |
| POST | `/cluster/refresh` | Bearer | Rescan tabs |
| POST | `/cluster/probe` | Bearer | Check all sessions without sending a prompt |
| GET | `/cluster/metrics` | Bearer | Latency and success-rate metrics |
| GET | `/cluster/circuits` | Bearer | Circuit breaker states |
| GET | `/cluster/cache` | Bearer | Cache statistics |
| POST | `/cluster/cache/clear` | Bearer | Clear the cache |
| GET | `/cluster/instances` | Bearer | Instances with memory usage |
| POST | `/cluster/instances/{n}/probe` | Bearer | Check one instance's session |
| POST | `/cluster/instances/{n}/reload` | Bearer | Navigate the tab to the provider home page |
| POST | `/cluster/instances/{n}/cleanup` | Bearer | Delete old chats (dry run by default) |
| POST | `/cluster/instances/{n}/recover-memory` | Bearer | Reopen the tab in a new renderer (Chrome is not restarted, except on Gemini #9) |
| POST | `/cluster/instances/{n}/focus` | Bearer | Bring the instance window to the front (for VNC) |
| GET | `/cluster/memory` | Bearer | Renderer memory, server swap and memory-guard events (`?after=<id>`) |
| GET | `/cluster/self-heal` | none | Self-heal watchdog status |
| GET | `/cluster/proxy-health` | none | Proxy watchdog status |
| GET | `/cluster/proxies` | Bearer | Proxy pool summary |
| POST | `/cluster/proxies/refresh` | Bearer | Reload the proxy list from a URL |
| GET | `/v1/models` | Bearer | OpenAI-compatible model list |
| POST | `/v1/chat/completions` | Bearer | OpenAI-compatible chat (streaming and non-streaming) |
| POST | `/v1/images/generations` | Bearer | OpenAI-compatible image generation |
| POST | `/v1/images/edits` | Bearer | OpenAI-compatible image editing |

## Supported models (channels)

`chatgpt`, `deepseek`, `qwen`, `gemini`, `genspark`, `deepai`, `zai`, `grok`, `chatbotapp`,
`chatbotchatapp`, plus `auto` (picks the best available worker automatically).

## Integration with the gateway backend

The backend's `settings` table holds these keys:

- `cluster_base_url`, e.g. `http://127.0.0.1:8800`
- `cluster_api_key`, the same value as `SF_CLUSTER_API_KEY`

In the admin console, create a model with `provider: cluster` on the Models page and set its
`upstream_name` to the desired channel (`chatgpt`, `qwen`, etc.). When a client then requests
that model through the gateway with a regular OpenAI SDK, the request is served by the cluster.
