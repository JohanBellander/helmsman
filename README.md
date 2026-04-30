<div align="center">

# ⚓ Helmsman

**A self-hosted Telegram ops bot for a Coolify + Beszel homelab.**
Watches the bridge while you sleep, investigates alerts, and answers questions about the fleet — through Claude Haiku 4.5.

![Python](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-Haiku%204.5-d97757)
![MCP](https://img.shields.io/badge/MCP-Beszel%20%2B%20Coolify-2563eb)
![Deploy](https://img.shields.io/badge/deploy-Coolify-7c3aed)
![Access](https://img.shields.io/badge/access-read--only-22c55e)

</div>

---

> **You:** prod-1 cpu is at 95%
>
> **Helmsman:** Yo, prod-1's sweatin'. MediAnalyzer's wildin' — 80% of the cycles, deadass. Tick's been goin' off every 200ms since 14:30. Either somebody messed with her or upstream's dumpin' on her. Want the logs, or just wanna know who?

---

## How it works

```
  ── user ────────────────────────────────────────────────
   📱 Telegram (long-poll, single-user allowlist)
  ── process ─────────────────────────────────────────────
   bridge.py · single asyncio event loop
   ├ telegram.ext.Application (handlers)
   ├ FastAPI on :8000  ◄── POST /webhook/beszel
   ├ Backend (Anthropic Haiku 4.5  or  local Ollama  + optional fallback)
   ├ AsyncExitStack { beszel-mcp, coolify-mcp }
   └ log scanner (every 10 min, regex-first, escalate to backend on hit)
  ── cluster ─────────────────────────────────────────────
   Beszel (HTTP)        ·        Coolify API (HTTP)
  ────────────────────────────────────────────────────────
```

One Python process, one async event loop. Two MCP servers run as long-lived stdio subprocesses. Telegram polling and the FastAPI webhook server share the loop via `asyncio.TaskGroup`.

**Read-only by design.** Every MCP tool whose name contains `create / update / delete / deploy / start / stop / restart / kill / remove / redeploy / set_env / write` is dropped at startup *before* tools are exposed to Claude. The dropped names are logged so you can see what got filtered. The model has no write tool to call.

---

## Quick deploy

1. Push (or fork) this repo to GitHub.
2. In Coolify: **+ New** → **Resource** → **Application** → **Public Repository** → paste the repo URL → branch `main` → build pack `Docker Compose`.
3. Paste in the env vars (see [Setup](#setup)).
4. Deploy. Watch logs for `helmsman: N tools registered, M dropped`.
5. In Beszel UI, add a Shoutrrr URL pointing at `helmsman:8000/webhook/beszel`.

Telegram `/start` should reply. Ask *"what apps are deployed?"* and watch it call out to Coolify.

---

## Setup

### Credentials

| Var | What it is | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot auth token | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_ALLOWED_USER_ID` | Your numeric Telegram ID | [@userinfobot](https://t.me/userinfobot) — copy the `Id` field |
| `ANTHROPIC_API_KEY` | Claude API key | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| `COOLIFY_ACCESS_TOKEN` | Coolify API token (read scope is enough) | Coolify → user menu → **Keys & Tokens** → **API Tokens** |
| `BESZEL_EMAIL` / `BESZEL_PASSWORD` | Beszel **superuser** account | Beszel → **Users** |
| `BESZEL_WEBHOOK_SECRET` | Shared secret for the webhook | `openssl rand -hex 32` |

### Network hostnames

Defaults assume conventional Docker service names. Verify against your actual cluster:

```bash
docker network inspect coolify --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'
```

- `COOLIFY_BASE_URL` is typically `http://coolify:8080` — Coolify v4 listens on **port 8080** internally (not 8000, despite the `EXPOSE` in the upstream image).
- `BESZEL_URL` for a Coolify-managed Beszel is the hashed container name, e.g. `http://beszel-vuxoeyr2t9d5pcm84xu0o9pk:8090`.

### Beszel Shoutrrr webhook

In Beszel UI → **Settings** → **Notifications** → add:

```
generic://helmsman:8000/webhook/beszel?@authorization=Bearer+<BESZEL_WEBHOOK_SECRET>
```

The `+` after `Bearer` is Shoutrrr's encoded space. The `helmsman` hostname resolves because the bridge registers it as a network alias on the `coolify` network. Wrong/missing header → 401.

---

## Background log scan

Every 10 minutes the bridge fetches recent logs for each Coolify app and runs them through a small set of conservative regex patterns:

```
oom        OOMKilled, out of memory, OutOfMemoryError, MemoryError
panic      panic:, goroutine N [running]:
traceback  Traceback (most recent call last)
fatal      FATAL, CRITICAL
segfault   segmentation fault, SIGSEGV, SIGKILL
unhandled  unhandled (promise) rejection / exception
```

Most cycles match nothing → zero Claude calls. When a pattern matches *new* lines (the scanner dedupes against what it's already seen), it sends a focused snippet to Claude for an investigation reply, then pushes that to Telegram. Per-(app, pattern) cooldown of 30 minutes keeps repeat events from spamming.

Cost: ~cents/day on a healthy cluster. Tune via env vars (`LOG_SCAN_ENABLED=false` to disable, `LOG_SCAN_INTERVAL_SEC` for polling cadence, etc. — see [`.env.example`](./.env.example)).

---

## Switching to a local model

Helmsman can run inference against a local Ollama server instead of (or with fallback from) Anthropic. Tool-capable models only — pick one of `llama3.1`, `qwen2.5`, `mistral-nemo`, or any other model Ollama lists as supporting tool calls.

| Var | Default | What |
|---|---|---|
| `BACKEND` | `anthropic` | `anthropic` or `ollama` — the primary brain |
| `BACKEND_FALLBACK` | `anthropic` | The other one, used when the primary fails on transport (connection error / timeout). Set to empty to disable the safety net. |
| `OLLAMA_BASE_URL` | — | Required when either backend is Ollama. e.g. `http://10.0.0.42:11434`. |
| `OLLAMA_MODEL` | — | Required when either backend is Ollama. e.g. `qwen2.5:7b`. |
| `OLLAMA_TIMEOUT_SEC` | `60` | Per-inference timeout. On exceed, fall back to Anthropic for the rest of that turn. |

`ANTHROPIC_API_KEY` is required regardless — it serves as the default fallback. Set `BACKEND_FALLBACK=` (empty) if you really want pure-local with no safety net.

The fallback is intentionally narrow: connection errors and timeouts only, not quality / parsing problems. A flaky local model that returns garbage will get fed back as-is — that's a model choice, not a transport failure.

`/health` reports which backend handled the most recent inference and a 24-hour fallback counter, so you can spot patterns.

---

## Telegram commands

| Command | What it does |
|---|---|
| `/start` | Sanity check, replies with tool count |
| `/health` | Uptime + liveness probe on each MCP subprocess |
| `/reset` | Clear conversation history for this chat |

Anything else you type goes to the agent loop. Helmsman picks tools as it needs them and replies in voice.

---

## When stuff breaks

<details>
<summary><b>Bot doesn't reply at all</b></summary>

The allowlist is intentionally silent for any user ID that isn't yours — there's no "unauthorized" reply. Confirm `TELEGRAM_ALLOWED_USER_ID` matches the numeric ID from [@userinfobot](https://t.me/userinfobot) for *your* account.

</details>

<details>
<summary><b>Container crashes at startup with <code>FATAL: missing env vars</code></b></summary>

A required environment variable isn't set in Coolify. See [`.env.example`](./.env.example) for the full list.

</details>

<details>
<summary><b>MCP fails to spawn</b></summary>

`docker logs helmsman` and look for stderr from `npx` or `beszel-mcp`. Common causes:

- `COOLIFY_BASE_URL` wrong — Coolify v4 listens on **port 8080** internally, not 8000
- `COOLIFY_ACCESS_TOKEN` invalid or revoked
- `BESZEL_URL` not reachable on the `coolify` network — use the actual container name from `docker network inspect coolify`
- Beszel email/password aren't a **superuser**

</details>

<details>
<summary><b>Telegram says <code>InvalidToken</code> / <code>Unauthorized</code></b></summary>

Token rejected by Telegram. Either it's wrong (extra quotes/whitespace from Coolify env paste), revoked, or the bot was deleted. In [@BotFather](https://t.me/BotFather), `/mybots` → pick the bot → **API Token** → **Revoke current token** to get a fresh one.

</details>

<details>
<summary><b>Webhook returns 401</b></summary>

The `Authorization` header didn't match. The Bearer token after `Bearer ` must equal `BESZEL_WEBHOOK_SECRET` exactly — no quotes, no trailing whitespace, no encoding tricks.

</details>

<details>
<summary><b>Coolify shows "Running (unknown)" health</b></summary>

The healthcheck is defined in `docker-compose.yml`. After a redeploy from `main`, expect `Starting` for ~30s, then `Healthy`. If it sticks on `Unhealthy`, run the healthcheck manually:

```bash
docker exec $(docker ps --filter name=helmsman --format '{{.Names}}' | head -1) curl -v http://localhost:8000/health
```

A 503 with an MCP listed as `error: <Class>` tells you which subprocess is in trouble.

</details>

---

## What's not here yet

- Write actions, gated behind confirmation codes (v2)
- Persistent conversation memory across restarts (currently in-memory)
- Multiple users / team mode (single-user allowlist for now)
- Slack / Discord output (Telegram only)
- Dashboard / web UI (chat is the interface)
- Smarter read-only filter — the current denylist drops some legitimate read tools like `list_deployments` because they contain "deploy". v2 will switch to an explicit *allowlist* of read verbs.

---

## Files

```
.
├── bridge.py            # the whole service, async, ~470 lines
├── system_prompt.md     # Helmsman's voice and operational rules
├── Dockerfile           # python:3.12-slim + Node 20 (NodeSource)
├── docker-compose.yml   # Coolify deploy unit, with healthcheck
├── requirements.txt     # pinned Python deps
├── package.json         # @masonator/coolify-mcp
├── .env.example         # every env var the bridge needs
└── PLAN.md              # original build spec
```

---

<div align="center">

> *"You can peep, you can't touch."*
> — Helmsman, on his read-only constraint

</div>
