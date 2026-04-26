\# Helmsman — Build Plan



Helmsman is a self-hosted Telegram bot that lets me chat with my

Coolify+Beszel cluster through Claude. It watches the bridge while I sleep,

investigates alerts, and answers questions about the fleet. Read-only for v1.

Deployed as a Docker container in Coolify.



\## What we're building



A single Python service that:



1\. Runs a Telegram bot (long-polling, no public URL needed for the bot side)

2\. Exposes ONE HTTP endpoint, `POST /webhook/beszel`, on the internal Docker

&#x20;  network so Beszel can fire alerts at it

3\. Spawns two MCP servers as local subprocesses and talks to them over stdio:

&#x20;  - \*\*Beszel MCP\*\* — `Red5d/beszel-mcp` (Python, FastMCP-based)

&#x20;  - \*\*Coolify MCP\*\* — `StuMason/coolify-mcp` (TypeScript/Node)

4\. Calls Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) via the Anthropic

&#x20;  Messages API, exposing all discovered MCP tools as native `tools`

5\. Loops tool calls until Claude produces a final answer, then posts to Telegram



Read-only for v1: filter the tool list before passing to Claude so write tools

(start/stop/restart/deploy/delete/update/create) are simply not exposed.

Easier and safer than relying on the prompt.



\## Repo layout



```

helmsman/

├── Dockerfile

├── docker-compose.yml          # for Coolify "Docker Compose" deploy

├── requirements.txt            # python deps for the bridge + beszel-mcp

├── package.json                # for coolify-mcp (npm install at build time)

├── bridge.py                   # the whole bridge, \~300 lines

├── system\_prompt.md            # ops-focused system prompt

├── .env.example

├── .gitignore                  # .env, \_\_pycache\_\_, node\_modules

└── README.md

```



Single repo, single container, single process group. No microservices, no orchestration.



\## Dockerfile



Base: `python:3.12-slim`. Install Node.js 20 (we need it for the Coolify MCP),

copy in `requirements.txt` and `package.json`, run `pip install` and

`npm install`, copy the rest of the source. Default CMD: `python bridge.py`.



Don't use Alpine — the MCP Python SDK and FastMCP have C extensions that are

painful on musl. Slim Debian is the right call.



Image will be \~400-500MB. Acceptable.



\## requirements.txt



```

anthropic>=0.40.0

mcp>=1.0.0

python-telegram-bot>=21.0

fastapi>=0.115.0

uvicorn>=0.30.0

httpx>=0.27.0

python-dotenv>=1.0.0

beszel-mcp @ git+https://github.com/Red5d/beszel-mcp.git

```



Pulling beszel-mcp from git directly is fine — it's a Python package with a

proper `pyproject.toml`. Pin to a commit SHA once we know it works.



\## package.json



```json

{

&#x20; "name": "helmsman-deps",

&#x20; "version": "1.0.0",

&#x20; "private": true,

&#x20; "dependencies": {

&#x20;   "@stumason/coolify-mcp": "latest"

&#x20; }

}

```



Actually verify the npm package name when building — it might be published

under a different scope or just `coolify-mcp`. Falls back to running from

a git clone in the Dockerfile if there's no published package.



\## bridge.py — the structure



One file, async throughout. Rough shape:



```python

\# 1. Config from env (TELEGRAM\_BOT\_TOKEN, TELEGRAM\_ALLOWED\_USER\_ID,

\#    ANTHROPIC\_API\_KEY, COOLIFY\_BASE\_URL, COOLIFY\_API\_TOKEN,

\#    BESZEL\_URL, BESZEL\_EMAIL, BESZEL\_PASSWORD, BESZEL\_WEBHOOK\_SECRET)



\# 2. Spawn the two MCP servers as subprocesses using mcp.client.stdio

\#    Keep ClientSession objects alive for the lifetime of the process



\# 3. On startup: list\_tools() on each session, build a unified tool list

\#    with prefixed names (beszel\_\_list\_systems, coolify\_\_list\_applications)

\#    Filter out any tool whose name suggests writes:

\#      drop if name contains: create, update, delete, deploy, start, stop,

\#      restart, kill, remove, redeploy, set\_env, write

\#    Keep the unfiltered list too — useful for logging/debugging



\# 4. Build the Anthropic tools schema from the filtered MCP tools



\# 5. Two entry points feed the same agent loop:

\#    a) Telegram message handler (filtered to allowed user ID)

\#    b) FastAPI POST /webhook/beszel (auth via shared secret header)



\# 6. Agent loop:

\#    - Maintain per-chat conversation history (in-memory dict, keyed by

\#      Telegram chat\_id; webhook gets a fresh history each alert)

\#    - Call Anthropic with messages + tools

\#    - If response contains tool\_use blocks: route each to the right

\#      MCP session by prefix, get results, append as tool\_result, loop

\#    - Stop when stop\_reason is end\_turn or max\_turns hit (cap at 10)

\#    - Send final text to Telegram



\# 7. Beszel webhook handler:

\#    - Verify shared secret header

\#    - Parse Beszel's JSON payload (server name, alert type, value, threshold)

\#    - Construct a synthetic user message: "Beszel alert: {server} {type}

\#      = {value} (threshold {threshold}). Investigate using available tools

\#      and summarize what's happening."

\#    - Run the agent loop with a fresh conversation

\#    - Post the result to Telegram (to the allowed user, not via reply)

```



Important details:



\- \*\*MCP subprocess lifecycle\*\*: Use `mcp.client.stdio.stdio\_client` as an

&#x20; async context manager held open via `AsyncExitStack` for the bot's

&#x20; lifetime. Don't spawn-per-request — too slow.

\- \*\*Tool routing\*\*: split on `\_\_` to get server name and original tool name.

\- \*\*Truncation\*\*: cap each tool result at \~8000 chars before sending back

&#x20; to Claude. Long log dumps will eat tokens otherwise. Add a note like

&#x20; `\[truncated, original was N chars]`.

\- \*\*Conversation memory\*\*: keep last 10 turns per chat. Wipe with `/reset`.

\- \*\*Cost guard\*\*: hard cap at 10 tool-call iterations per request. If we hit

&#x20; it, send "stopping after 10 tool calls — try a more specific question."

\- \*\*Error handling\*\*: every MCP tool call wrapped in try/except. On error,

&#x20; feed the error string back to Claude as the tool\_result so it can recover

&#x20; or apologize gracefully.



\## system\_prompt.md



Short, operational, role-defining. Something like:



> You are Helmsman, the operations assistant on Johan's homelab cluster.

> The cluster runs Coolify and Beszel on Ubuntu+Docker. You have

> read-only access to both via tools.

>

> Style: concise, direct, technical. No corporate hedging. Use Telegram

> markdown sparingly — backticks for identifiers, no emoji unless asked.

> A little nautical voice is fine but don't overdo it.

>

> When investigating an alert: start broad (what's running on this server,

> recent deployments, current resource state), narrow to the specific

> service, then summarize: what's happening, why it might be happening,

> what Johan should consider doing. Don't suggest fixes you can't verify

> from the data.

>

> When asked questions: answer directly first, add context after. Don't

> narrate your tool use ("I'll check..."). Just check and report.

>

> If a tool fails, say so plainly and suggest what might be wrong.

> Don't pretend.

>

> You cannot make changes — only read state. If Johan asks you to restart

> or deploy something, tell him you're read-only in this version.



Read this file at startup and pass as the `system` parameter to Anthropic.



\## docker-compose.yml



For Coolify deploy:



```yaml

services:

&#x20; helmsman:

&#x20;   build: .

&#x20;   restart: unless-stopped

&#x20;   environment:

&#x20;     - TELEGRAM\_BOT\_TOKEN=${TELEGRAM\_BOT\_TOKEN}

&#x20;     - TELEGRAM\_ALLOWED\_USER\_ID=${TELEGRAM\_ALLOWED\_USER\_ID}

&#x20;     - ANTHROPIC\_API\_KEY=${ANTHROPIC\_API\_KEY}

&#x20;     - COOLIFY\_BASE\_URL=${COOLIFY\_BASE\_URL}

&#x20;     - COOLIFY\_API\_TOKEN=${COOLIFY\_API\_TOKEN}

&#x20;     - BESZEL\_URL=${BESZEL\_URL}

&#x20;     - BESZEL\_EMAIL=${BESZEL\_EMAIL}

&#x20;     - BESZEL\_PASSWORD=${BESZEL\_PASSWORD}

&#x20;     - BESZEL\_WEBHOOK\_SECRET=${BESZEL\_WEBHOOK\_SECRET}

&#x20;     - LOG\_LEVEL=INFO

&#x20;   networks:

&#x20;     - coolify

&#x20;   expose:

&#x20;     - "8000"

networks:

&#x20; coolify:

&#x20;   external: true

```



`expose` not `ports` — we don't want it on the host, just reachable on the

internal Docker network so Beszel can POST to `http://helmsman:8000/webhook/beszel`.



Verify the actual external Coolify network name when deploying — it's usually

`coolify` but check with `docker network ls`.



\## .env.example



```

\# Telegram — get from @BotFather, get user ID from @userinfobot

TELEGRAM\_BOT\_TOKEN=

TELEGRAM\_ALLOWED\_USER\_ID=



\# Anthropic

ANTHROPIC\_API\_KEY=



\# Coolify — internal hostname works since we're on the coolify network

COOLIFY\_BASE\_URL=http://coolify:8000

COOLIFY\_API\_TOKEN=



\# Beszel — same, internal hostname

BESZEL\_URL=http://beszel:8090

BESZEL\_EMAIL=

BESZEL\_PASSWORD=



\# Shared secret Beszel sends in a header when firing webhooks

BESZEL\_WEBHOOK\_SECRET=

```



\## Beszel webhook configuration (manual step, document in README)



In Beszel UI → Settings → Notifications → add a Shoutrrr URL like:



```

generic://helmsman:8000/webhook/beszel?@authorization=Bearer+<BESZEL\_WEBHOOK\_SECRET>

```



The `@authorization=Bearer+xxx` syntax is Shoutrrr's way of adding custom

headers. Confirm the exact escaping when testing — Shoutrrr docs are the

source of truth here, not memory.



The bridge verifies that header against `BESZEL\_WEBHOOK\_SECRET` before doing

anything. Reject with 401 if missing/wrong.



\## Testing checklist



Before declaring it done:



1\. `/start` in Telegram from allowed user ID → friendly hello, mentions tools

2\. `/start` from any other user ID → silent ignore (or "not authorized")

3\. "what servers do I have?" → calls beszel tool, returns list

4\. "what apps are deployed?" → calls coolify tool, returns list

5\. "what's running on prod-1?" → multi-tool: beszel for server status,

&#x20;  coolify for apps on it

6\. POST to /webhook/beszel with valid secret + a fake alert payload →

&#x20;  Telegram message arrives with investigation summary

7\. POST to /webhook/beszel with wrong secret → 401

8\. Try to ask "restart medianalyzer" → bot says it's read-only (because the

&#x20;  write tool isn't in its tool list, the model just won't have an option)

9\. `/reset` clears conversation history

10\. Container restart → reconnects MCPs, bot keeps working



\## Deploy to Coolify (manual steps for me, document in README)



1\. Push this repo to GitHub (private)

2\. In Coolify: New Resource → Application → GitHub (use existing GitHub App)

3\. Pick the repo, branch `main`, build pack: `Docker Compose`

4\. Set all env vars from `.env.example`

5\. Disable auto-deploy (manual deploy preference)

6\. Deploy

7\. In Beszel: add the Shoutrrr notification URL pointing at the internal

&#x20;  hostname `helmsman:8000`

8\. Send a test message in Telegram



\## Out of scope for v1 (note these in README as "future")



\- Write actions with confirmation codes (planned for v2)

\- Homebutler / direct Docker control (skip until proven needed)

\- Persistent conversation memory across restarts (in-memory dict is fine for v1)

\- Multiple users / team mode (single-user allowlist is enough)

\- Slack/Discord (Telegram only)

\- Cron-scheduled proactive checks (Beszel webhook covers the proactive case)

\- Dashboard / web UI (chat is the interface)



\## Notes for Claude Code



\- Use `python-telegram-bot` v21+ async API, not the old sync style

\- The MCP Python SDK changed import paths around v1.0 — use what's current

&#x20; at build time, don't rely on examples older than mid-2025

\- FastAPI and python-telegram-bot need to share an event loop. Run uvicorn

&#x20; programmatically via `uvicorn.Server` so we can `await` it alongside

&#x20; `application.run\_polling()` — see python-telegram-bot's "webhook + custom

&#x20; webserver" examples for the pattern, but adapt for polling + webhook on

&#x20; the same loop

\- Don't add tests for v1. This is a personal tool. Logs + manual testing is fine.

\- Verify all package names and versions actually exist before pinning. Don't

&#x20; trust the names in this plan blindly — they're best-effort.

