# Helmsman

Self-hosted Telegram ops bot for Johan's Coolify + Beszel homelab. It receives Beszel alerts on a webhook, scans
Coolify app logs every 10 minutes, and answers questions about the fleet through Claude Haiku 4.5 (or a local
llama.cpp server). Single user, read-only by design. [`README.md`](README.md) is the full reference (env vars,
Beszel webhook URL, troubleshooting).

## Stack

- Python 3.12 in the image (`python:3.12-slim`); one asyncio process: python-telegram-bot, FastAPI/uvicorn on `:8000`,
  `anthropic` and `openai` SDKs (`requirements.txt`, lower-bound pins).
- Two MCP servers as long-lived stdio subprocesses: `beszel-mcp` (pip, from GitHub) and `@masonator/coolify-mcp`
  (npm, `package.json`, version `latest`, run with `node`, not `npx`).
- No tests, no linter or formatter config.

## Layout

- `bridge.py` — the whole service: config, read-only tool filter (`FORBIDDEN_TOKENS`, `TOOL_DENYLIST`), backends
  with fallback, agent loop, log scanner (`LOG_PATTERNS`), Telegram handlers, `/health` and `POST /webhook/beszel`.
- `system_prompt.md` — Helmsman's voice and operating rules (loaded at runtime, shipped in the image).
- `Dockerfile` (multi-stage, npm and curl removed from runtime), `docker-compose.yml` (the Coolify deploy unit).
- `.env.example` — every env var with comments. `PLAN.md` — original build spec; the code decides where they differ.

## Commands

```bash
py -I -m py_compile bridge.py          # syntax check (verified)
docker compose config --quiet          # compose file parses; warns about unset env vars (verified)
uv venv && uv pip install -r requirements.txt && npm install   # local deps (not verified here)
.venv/Scripts/python.exe bridge.py     # needs a filled .env and reachable Coolify/Beszel; exits on missing vars
```

There is no test suite. Verify behaviour changes by reading the startup log line
`helmsman: N tools registered, M dropped` and `/health` on a running instance.

## Conventions

- Read-only is the product. Every MCP tool whose name contains a `FORBIDDEN_TOKENS` word is dropped before the model
  sees it. Never loosen that list or add a write path without Johan asking; README "What's not here yet" plans v2.
- Broken upstream tools go in `TOOL_DENYLIST` with a comment saying why, not into `FORBIDDEN_TOKENS`.
- The Telegram allowlist is silent: unknown users get no reply. Keep it that way.
- Log scanner patterns stay conservative (false negatives over noise); per-app silencing goes through
  `LOG_SCAN_EXCLUDE` / `LOG_SCAN_EXCLUDE_PATTERNS`, not by weakening `LOG_PATTERNS`.
- A new env var touches four places: `bridge.py`, `docker-compose.yml` `environment:`, `.env.example`, `README.md`.

## Deploy

- Coolify, Docker Compose build pack, branch `main`, on the external `coolify` network with network alias `helmsman`
  (Beszel posts to `helmsman:8000/webhook/beszel`). Env vars live in Coolify's UI.
- Coolify never deploys on its own. After Johan merges, the agent lands the change with the global `prod-landing`
  skill. Helmsman is not in `local-knowledge` `references/apps.md` yet; find its Coolify resource there or ask before deploying.
- Health: compose healthcheck calls `GET /health` with Python's stdlib (curl is not in the runtime image).

## Ways to hurt yourself

- Coolify's compose build pack only passes variables listed under `environment:` in `docker-compose.yml`. A var set in
  Coolify's UI but missing there never reaches `bridge.py`, silently.
- Coolify v4 listens on `8080` inside the `coolify` network (README); `.env.example` still shows `:8000`.
- Don't reintroduce `curl` or `npx` at runtime: both are removed from the image on purpose (CVE surface).
- `compose pull: true` and `apt-get upgrade` in the Dockerfile are deliberate: they force fresh base layers on the
  build host. Don't "clean them up".
- `@masonator/coolify-mcp` is `latest` and the npm lockfile is generated at build time, so a rebuild can change tool
  names and therefore what the read-only filter drops.
- Importing `bridge.py` runs config loading and exits when required env vars are missing; use `py_compile` for checks.

## Review
Process: global skill `pr-crosscheck`. Tier triggers beyond the defaults:
- T2: `FORBIDDEN_TOKENS`, `is_write_tool`, the tool-registration path, `_is_allowed`, the webhook auth check,
  `Dockerfile`, `docker-compose.yml`, `package.json` overrides.
Invariants a reviewer must check:
- No write-capable MCP tool can reach the model; no unauthenticated path into the agent loop.
- New env vars appear in all four places listed under Conventions.
- Secrets never appear in logs or Telegram replies.
Checks: `py -I -m py_compile bridge.py`, `docker compose config --quiet`.
Failure looks like: the bot can restart or redeploy an app, a stranger can talk to it, or alerts silently stop.
Skip: `PLAN.md`, README prose, voice tweaks in `system_prompt.md`. At most 3 minor findings per review.

## Agent setup
This repo is worked on by both Claude Code and Codex; keep them in sync.
- Rules: this file. `CLAUDE.md` only imports it — edit here.
- Skills: `.agents/skills/<name>/SKILL.md` is the source; `.claude/skills/` is a generated copy. After editing a skill run `node .agents/sync-skills.mjs` (`--check` to verify).
- Handoff between agents: before stopping mid-task, write `.agent/handoff.md` (gitignored): goal, done, next, how to verify.
