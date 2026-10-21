# AI Gateway

**English** · [Русский](README.ru.md)

A self-hosted, OpenAI-compatible AI API platform with your own brand: API keys,
usage accounting and billing, a web console for users and admins, and three ways
to source the answers - web-chat automation, imported browser sessions, or any
upstream OpenAI-compatible API - plus optional local models through Ollama.

```
clients (OpenAI SDK, curl, opencode, aider, ...)
        │  https://your-domain/v1/chat/completions
        ▼
   ┌──────────┐     ┌────────────┐
   │  Caddy   │────▶│  api       │── PostgreSQL (users, keys, usage, ledger)
   │ (HTTPS)  │     │  FastAPI + │── Ollama        (local models)
   └──────────┘     │  web UI    │── external API  (mode 3)
                    └─────┬──────┘
                          ▼
                    ┌────────────┐
                    │  cluster   │  Chrome instances on a virtual display,
                    │            │  driven over CDP (modes 1 and 2), VNC
                    └────────────┘
```

## Features

- **OpenAI-compatible API**: `/v1/chat/completions` (streaming, tools, vision,
  reasoning effort), `/v1/models`, `/v1/images/generations`, `/v1/images/edits`.
- **Your names**: platform name, assistant name and every public model id are
  chosen at install time; upstream names are never shown to clients.
- **Accounts and keys**: sign-up (optional), Google / Telegram login, per-key rate
  and monthly limits, usage history, referral program, top-up requests.
- **Billing** per real token (or a free mode with no balances at all).
- **Admin console**: models and prices, users, monitoring and failures, settings,
  Chrome instances with a built-in VNC viewer.
- **Reliability helpers** for web-chat channels: session checks, quota and captcha
  detection with cool-downs, fallback between channels, memory guard, self-healing.
- **Interface languages**: Uzbek, Russian (partial), English defaults.

## The three modes

| Mode | Where answers come from | You need |
|---|---|---|
| **1. Web-chat automation** | Chrome instances on your server open provider web chats (Gemini, Qwen, DeepSeek, ChatGPT, ...). You log in once through the built-in VNC viewer. | A server with ~1 GB RAM per channel, your own provider accounts |
| **2. Session import** | The same Chrome cluster, but you import your existing session: cookies exported from your browser (JSON or `cookies.txt`) or a JWT/session token kept in `localStorage`. | Exported cookies / token from your own browser |
| **3. External API** | Any OpenAI-compatible upstream (OpenAI, OpenRouter, a local vLLM, ...). You resell it under your own model names and prices. | Upstream base URL and API key |

Any mode can be combined with **local models via Ollama** (in Docker on the same
server, with or without an NVIDIA GPU, or an Ollama you already run elsewhere).

> **Terms of Service.** Automating consumer chat websites (modes 1 and 2) may
> violate the providers' terms and can get accounts limited. Use only accounts you
> own, keep the load low, and check each provider's rules. This project does not
> bypass captchas or bot protection: if a provider shows one, an operator solves it
> manually in VNC. For production workloads prefer mode 3 with official APIs.

## Requirements

- Linux server (x86_64 recommended; on arm64 the cluster uses Chromium)
- Docker 24+ with the Compose v2 plugin (`docker compose version`)
- RAM: ~1.5 GB for the base stack, plus ~0.6-1 GB per Chrome channel,
  plus whatever your Ollama models need
- For automatic HTTPS: a domain whose DNS points to the server, ports 80/443 free

## Install

```bash
git clone https://github.com/Inforteuz/ai-gateway.git
cd ai-gateway
./install.sh
```

The installer asks for:

1. platform and assistant names, default interface language;
2. how the site is reached: public domain with automatic HTTPS (Caddy), behind
   your own reverse proxy (a local port), or local test only;
3. the mode (1, 2 or 3) and, per channel / upstream model, the public model id,
   display name and prices;
4. local models via Ollama (optional);
5. the admin account, whether public sign-up is open, and free mode.

It then generates all secrets, writes `.env` (mode 600), builds the images,
starts the stack, applies database migrations, creates the admin and the models.
Re-run `./install.sh` at any time to reconfigure; existing secrets are kept.

### After installing

- **Mode 1** - open the admin panel → **VNC** and sign in to each Chrome window.
  Logins are stored in the `cluster-data` Docker volume and survive restarts.
- **Mode 2** - import your session per instance:

  ```bash
  ./manage.sh import-session 0 gemini cookies-google.json
  ./manage.sh import-session 1 deepseek --token userToken='{"value":"<token>"}'
  ```

  Export cookies from your own logged-in browser with any cookie-export extension
  (JSON) or as Netscape `cookies.txt`. Only cookies of the channel's domains are
  imported; values are never printed.
- **Mode 3** - nothing else to do; add more models with `./manage.sh add-model`.

## Using the API

```bash
curl https://your-domain/v1/chat/completions \
  -H "Authorization: Bearer sk-sf-..." \
  -H "Content-Type: application/json" \
  -d '{"model": "my-model-1", "messages": [{"role": "user", "content": "Hello!"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="https://your-domain/v1", api_key="sk-sf-...")
print(client.chat.completions.create(model="my-model-1",
      messages=[{"role": "user", "content": "Hello!"}]).choices[0].message.content)
```

Users create keys in the console (**Keys**); the **Docs** page shows examples
with your own domain and model names.

## Operations

```bash
./manage.sh status            # containers + health
./manage.sh logs api          # follow logs (api, cluster, db, caddy, ollama)
./manage.sh add-model --id my-fast --provider local --upstream qwen3:8b --price-in 0 --price-out 0
./manage.sh list-models
./manage.sh backup-full       # encrypted: DB + .env + Chrome sessions + TLS state
./manage.sh update            # git pull + rebuild + restart, data is kept
./manage.sh help
```

Per-instance proxies for the Chrome cluster (optional): see
`backend/cluster/proxies.txt.example`, then
`docker compose cp proxies.txt cluster:/data/proxies.txt && ./manage.sh restart cluster`.

Everything else (prices, users, limits, Google/Telegram login, reCAPTCHA,
fallbacks, fast path for trivial messages) is configured in the admin console.

### Reconfiguring

Re-running `./install.sh` on an installed server is a reconfigure: current values are the
defaults, and it never silently changes the billing mode, the sign-up policy, the admin
password, stored provider keys or existing model prices. Switching a paid service to free
mode asks you to type `FREE`; a new admin password needs `./install.sh --reset-admin-password`.

### Backups and restore

```bash
./manage.sh backup-full                    # backups/gateway-full-<date>.tar.enc (+ .sha256)
./manage.sh verify-backup FILE             # decrypt + checksums + DB dump check, changes nothing
./manage.sh restore-full FILE              # asks for confirmation, replaces DB and volumes
```

A full backup holds the database (`pg_dump`), `.env`, `Caddyfile`, the Chrome profiles
(logged-in provider sessions) and Caddy's certificates; add `--with-ollama` for local models.
It is encrypted with AES-256 (PBKDF2); the passphrase is asked for or taken from
`BACKUP_PASSPHRASE`. The cluster is paused for a moment so Chrome's files are consistent.

- **Keep copies off the server** (e.g. `rclone copy backups/ remote:gateway`), and keep the
  passphrase somewhere else - without it the backup cannot be restored.
- **Schedule it**, e.g. nightly with cron:
  `15 3 * * * cd /opt/ai-gateway && BACKUP_PASSPHRASE=... ./manage.sh backup-full >> backups/cron.log 2>&1`
  (read the passphrase from a root-only file rather than writing it into crontab).
- **New server:** clone the repo, `./manage.sh extract-env FILE`, review `.env`, then
  `docker compose up -d db && ./manage.sh restore-full FILE`.
- `./manage.sh backup` / `restore` still exist for quick database-only dumps.

### Main `.env` settings

| Variable | Meaning |
|---|---|
| `SF_BRAND_NAME`, `SF_ASSISTANT_NAME` | platform name / how the assistant introduces itself |
| `SF_SITE_URL`, `SF_API_URL` | public URLs (used in links, docs and captcha checks) |
| `SF_DEFAULT_LANG` | `uz`, `ru` or `en` |
| `COMPOSE_PROFILES` | `cluster`, `ollama`, `caddy` - which optional services run |
| `SF_CHROME_CHANNELS` | e.g. `gemini,qwen` - one Chrome instance per channel, in order (instance 0, 1, ...) |
| `SF_UPSTREAM_URL` | Ollama URL (`http://ollama:11434` for the bundled container) |
| `API_BIND`, `API_PORT` | where the API is published on the host (default `127.0.0.1:8310`) |
| `DB_*_PASSWORD`, `CLUSTER_API_KEY`, `SF_VNC_PASSWORD` | generated secrets |
| `FORWARDED_ALLOW_IPS` | proxies whose `X-Forwarded-For` is trusted (default: loopback + private ranges) |
| `SF_CHROME_SANDBOX` | `auto` (default), `1` or `0` - Chrome's sandbox, see Security notes |
| `SF_CHROME_SITE_ISOLATION` | `1` = Chrome site isolation on (more RAM per instance) |

Upstream API keys, OAuth secrets and similar values are stored in the database
settings (write-only in the admin console), not in `.env`.

### Behind your own reverse proxy

Choose "behind my own reverse proxy" in the installer and forward to
`http://127.0.0.1:<API_PORT>`. Example for nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8310;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;   # replace, never append client-sent values
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;      # VNC viewer uses WebSocket
    proxy_set_header Connection "upgrade";
    proxy_buffering off;                          # streaming responses
    proxy_read_timeout 600s;
    client_max_body_size 30m;                     # image uploads
}
```

## Security notes

- **Network:** only the API port (bound to `127.0.0.1` unless Caddy serves it) and, with
  Caddy, 80/443 are published. PostgreSQL, the cluster API and VNC stay on the internal
  Docker network (raw VNC is additionally published on `127.0.0.1` for SSH tunnels). Chrome's
  DevTools ports listen on `127.0.0.1` inside the cluster container only.
- **Client IPs:** `X-Forwarded-For` is trusted only from `FORWARDED_ALLOW_IPS`. Your reverse
  proxy must *replace* the header (`$remote_addr` in nginx; Caddy does this by default).
  Anyone who can connect to the API port from the host itself counts as a proxy.
- **Billing limits** (key monthly limit, free daily quota, per-key concurrency) count requests
  that are still running, atomically in the database, so parallel requests and several API
  processes cannot overshoot them. Holds of a crashed process are released within ~75 s.
- **Request bodies** are capped also for chunked uploads (`SF_MAX_BODY`, `SF_MAX_MULTIMODAL_BODY`).
- **Attachment URLs** sent by API clients are fetched only from public addresses: the name is
  resolved once, internal/metadata/CGNAT ranges are refused and redirects are not followed.
- **Accounts:** API keys and sessions are stored as hashes. When someone signs in with Google
  for an email that already has a self-registered password account, that password and all its
  sessions are dropped (protects against pre-registering someone else's address); the owner
  can set a new password via reset. Admin accounts keep their password.
- **Containers** run without Linux capabilities they do not need (`cap_drop: ALL`,
  `no-new-privileges`); the API container has a read-only filesystem.
- **Chrome sandbox:** `SF_CHROME_SANDBOX=auto` uses Chrome's sandbox when the host allows it
  (it needs unprivileged user namespaces; Ubuntu 24.04 blocks them by default through AppArmor)
  and otherwise falls back to `--no-sandbox` with a warning in the logs. In that case the
  container is the isolation boundary. Chrome profiles contain logged-in sessions: treat the
  `cluster-data` volume and full backups like a password vault.
- **Shared provider accounts:** every client's prompts go through the same provider accounts.
  Chats are opened in temporary mode where the provider supports it and cleaned up otherwise,
  but the provider still sees all prompts. Tell your users.
- The database app role has no DELETE on financial tables; the ledger is append-only.
- Prompt and answer texts are not stored by the gateway, only token counts and metadata.

## Development

```bash
# backend (needs a local PostgreSQL for tests)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
# frontend
cd frontend && npm ci && npm run dev
```

## License

See [LICENSE](LICENSE).
