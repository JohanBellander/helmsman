"""Helmsman — Telegram + Beszel-webhook bridge to Coolify/Beszel via Claude Haiku 4.5.

One process, one event loop. Two MCP servers as long-lived stdio subprocesses.
Read-only enforcement is done by filtering the tool list before it ever reaches
Claude — see FORBIDDEN_TOKENS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import uvicorn
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

load_dotenv()

REQUIRED_ENV = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "ANTHROPIC_API_KEY",
    "COOLIFY_BASE_URL",
    "COOLIFY_ACCESS_TOKEN",
    "BESZEL_URL",
    "BESZEL_EMAIL",
    "BESZEL_PASSWORD",
    "BESZEL_WEBHOOK_SECRET",
]


def _load_config() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.stderr.write(f"FATAL: missing env vars: {', '.join(missing)}\n")
        sys.exit(2)
    return {k: os.environ[k] for k in REQUIRED_ENV}


CONFIG = _load_config()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
ALLOWED_USER_ID = int(CONFIG["TELEGRAM_ALLOWED_USER_ID"])
MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ITERS = 10
TOOL_RESULT_CHAR_CAP = 8000
HISTORY_TURN_CAP = 10  # 10 user + 10 assistant messages

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("helmsman")

# ----------------------------------------------------------------------------
# Read-only tool filter
# ----------------------------------------------------------------------------

FORBIDDEN_TOKENS = (
    "create",
    "update",
    "delete",
    "deploy",
    "start",
    "stop",
    "restart",
    "kill",
    "remove",
    "redeploy",
    "set_env",
    "write",
)

# ----------------------------------------------------------------------------
# Background log-scan config (Coolify only for v1)
# ----------------------------------------------------------------------------

LOG_SCAN_ENABLED = os.environ.get("LOG_SCAN_ENABLED", "true").lower() in ("1", "true", "yes")
LOG_SCAN_INTERVAL_SEC = int(os.environ.get("LOG_SCAN_INTERVAL_SEC", "600"))
LOG_SCAN_LINES = int(os.environ.get("LOG_SCAN_LINES", "200"))
LOG_SCAN_COOLDOWN_SEC = int(os.environ.get("LOG_SCAN_COOLDOWN_SEC", "1800"))

# Conservative — patterns that almost always indicate a real problem.
# Bias toward false negatives (tunable via env if you want it noisier).
LOG_PATTERNS: dict[str, "re.Pattern[str]"] = {
    "oom":        re.compile(r"OOMKilled|out of memory|OutOfMemoryError|MemoryError", re.IGNORECASE),
    "panic":      re.compile(r"\bpanic:|goroutine \d+ \[running\]:", re.IGNORECASE),
    "traceback":  re.compile(r"Traceback \(most recent call last\)"),
    "fatal":      re.compile(r"\b(?:FATAL|CRITICAL)\b"),
    "segfault":   re.compile(r"segmentation fault|\bSIGSEGV\b|\bSIGKILL\b", re.IGNORECASE),
    "unhandled":  re.compile(r"unhandled (?:promise )?(?:rejection|exception)", re.IGNORECASE),
}


def is_write_tool(name: str) -> bool:
    n = name.lower()
    return any(tok in n for tok in FORBIDDEN_TOKENS)


# ----------------------------------------------------------------------------
# MCP servers
# ----------------------------------------------------------------------------

# Inherit PATH so `npx` and `beszel-mcp` resolve. Pass through anything Coolify
# might inject (HTTP_PROXY etc.) — minimal baseline:
def _base_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
    return {k: os.environ[k] for k in keep if k in os.environ}


def beszel_params() -> StdioServerParameters:
    env = _base_env() | {
        "BESZEL_URL": CONFIG["BESZEL_URL"],
        "BESZEL_EMAIL": CONFIG["BESZEL_EMAIL"],
        "BESZEL_PASSWORD": CONFIG["BESZEL_PASSWORD"],
    }
    return StdioServerParameters(command="beszel-mcp", args=[], env=env)


def coolify_params() -> StdioServerParameters:
    env = _base_env() | {
        "COOLIFY_BASE_URL": CONFIG["COOLIFY_BASE_URL"],
        "COOLIFY_ACCESS_TOKEN": CONFIG["COOLIFY_ACCESS_TOKEN"],
    }
    return StdioServerParameters(
        command="npx",
        args=["@masonator/coolify-mcp@latest"],
        env=env,
    )


async def open_mcp(stack: AsyncExitStack, params: StdioServerParameters, label: str) -> ClientSession:
    log.info("spawning MCP %s: %s %s", label, params.command, " ".join(params.args or []))
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


# ----------------------------------------------------------------------------
# Tool registry
# ----------------------------------------------------------------------------


class ToolRegistry:
    """Maps prefixed tool name -> (mcp session, original tool name).

    Also produces the Anthropic-shaped tool schema list passed to messages.create().
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[ClientSession, str]] = {}
        self.anthropic_tools: list[dict[str, Any]] = []
        self.dropped: list[str] = []

    async def register(self, label: str, session: ClientSession) -> None:
        listed = await session.list_tools()
        for tool in listed.tools:
            prefixed = f"{label}__{tool.name}"
            if is_write_tool(tool.name):
                self.dropped.append(prefixed)
                continue
            self.routes[prefixed] = (session, tool.name)
            self.anthropic_tools.append(
                {
                    "name": prefixed,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                }
            )

    def summary(self) -> str:
        return f"{len(self.anthropic_tools)} tools registered, {len(self.dropped)} dropped"


# ----------------------------------------------------------------------------
# Agent loop
# ----------------------------------------------------------------------------


def _truncate(s: str, cap: int = TOOL_RESULT_CHAR_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n[truncated, original was {len(s)} chars]"


async def _mcp_probe(timeout: float = 3.0) -> dict[str, str]:
    """Liveness check on every live MCP session via list_tools()."""
    out: dict[str, str] = {}
    for label, session in MCP_SESSIONS.items():
        try:
            await asyncio.wait_for(session.list_tools(), timeout=timeout)
            out[label] = "ok"
        except Exception as exc:  # noqa: BLE001
            out[label] = f"error: {type(exc).__name__}"
    return out


# ----------------------------------------------------------------------------
# Background log scan
# ----------------------------------------------------------------------------

# uuid -> {"seen": set[str of recent lines], "alerts": {pattern_name: monotonic_ts}}
_log_scan_state: dict[str, dict[str, Any]] = {}


def _diff_new_lines(uuid: str, lines: list[str], cap: int = 1000) -> list[str]:
    state = _log_scan_state.setdefault(uuid, {"seen": set(), "alerts": {}})
    seen: set[str] = state["seen"]
    fresh = [l for l in lines if l and l not in seen]
    seen.update(fresh)
    if len(seen) > cap:
        # Keep only the most recently observed half — bounded memory.
        state["seen"] = set(lines[-cap // 2 :])
    return fresh


async def _list_apps_for_scan() -> list[tuple[str, str]]:
    """Returns list of (uuid, name) for Coolify apps, or [] if unavailable."""
    session = MCP_SESSIONS.get("coolify")
    if session is None:
        return []
    try:
        result = await asyncio.wait_for(
            session.call_tool("list_applications", arguments={}), timeout=10
        )
    except Exception:
        log.exception("log-scan: list_applications failed")
        return []
    text = _result_to_text(result)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("log-scan: list_applications returned non-JSON")
        return []
    apps_iter: list[Any] = []
    if isinstance(data, list):
        apps_iter = data
    elif isinstance(data, dict):
        for key in ("data", "applications", "apps", "items"):
            if isinstance(data.get(key), list):
                apps_iter = data[key]
                break
    out: list[tuple[str, str]] = []
    for app in apps_iter:
        if not isinstance(app, dict):
            continue
        uuid = app.get("uuid") or app.get("id") or app.get("_id")
        name = app.get("name") or app.get("fqdn") or str(uuid)
        if uuid:
            out.append((str(uuid), str(name)))
    return out


async def _fetch_app_logs(uuid: str) -> list[str]:
    session = MCP_SESSIONS.get("coolify")
    if session is None:
        return []
    try:
        result = await asyncio.wait_for(
            session.call_tool(
                "application_logs",
                arguments={"uuid": uuid, "lines": LOG_SCAN_LINES},
            ),
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("log-scan: application_logs(%s) failed: %s", uuid, exc)
        return []
    text = _result_to_text(result)
    return [l for l in text.splitlines() if l.strip()]


async def _scan_one_app(uuid: str, name: str) -> None:
    lines = await _fetch_app_logs(uuid)
    if not lines:
        return
    new_lines = _diff_new_lines(uuid, lines)
    if not new_lines:
        return
    state = _log_scan_state[uuid]
    alerts: dict[str, float] = state["alerts"]

    triggered: list[tuple[str, list[str]]] = []
    now = time.monotonic()
    for pname, pattern in LOG_PATTERNS.items():
        matches = [l for l in new_lines if pattern.search(l)]
        if not matches:
            continue
        last = alerts.get(pname, 0.0)
        if now - last < LOG_SCAN_COOLDOWN_SEC:
            continue
        triggered.append((pname, matches[:5]))
        alerts[pname] = now

    if not triggered:
        return

    pat_summary = ", ".join(f"`{p}` ({len(m)})" for p, m in triggered)
    sample_lines: list[str] = []
    for _, m in triggered:
        sample_lines.extend(m)
    sample_blob = "\n".join(_truncate(l, 400) for l in sample_lines[:8])

    synthetic = (
        f"Background log scan: `{name}` triggered {pat_summary}.\n\n"
        f"Sample lines:\n{sample_blob}\n\n"
        "What's goin' on?"
    )

    log.info("log-scan: alert on %s patterns=%s", name, [p for p, _ in triggered])

    try:
        reply, _ = await run_agent(
            client=ANTHROPIC,
            registry=REGISTRY,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": synthetic}],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("log-scan agent crashed")
        reply = (
            f"Caught somethin' suspicious in `{name}` logs ({pat_summary}) "
            f"but I choked investigatin' it: {exc!s}"
        )

    for chunk in _split_for_telegram(reply):
        try:
            await TG_APP.bot.send_message(chat_id=ALLOWED_USER_ID, text=chunk)
        except Exception:
            log.exception("log-scan: failed to push to Telegram")


async def _log_scan_loop() -> None:
    if not LOG_SCAN_ENABLED:
        log.info("log-scan: disabled")
        return
    log.info(
        "log-scan: enabled (interval=%ds, lines=%d, cooldown=%ds, patterns=%s)",
        LOG_SCAN_INTERVAL_SEC,
        LOG_SCAN_LINES,
        LOG_SCAN_COOLDOWN_SEC,
        ",".join(LOG_PATTERNS.keys()),
    )
    # First pass: prime the seen-line set so we don't alert on already-aged logs.
    try:
        apps = await _list_apps_for_scan()
        for uuid, _name in apps:
            lines = await _fetch_app_logs(uuid)
            _diff_new_lines(uuid, lines)
        log.info("log-scan: primed %d apps; entering loop", len(apps))
    except Exception:
        log.exception("log-scan: priming failed; will retry next cycle")

    while True:
        await asyncio.sleep(LOG_SCAN_INTERVAL_SEC)
        try:
            apps = await _list_apps_for_scan()
            for uuid, name in apps:
                try:
                    await _scan_one_app(uuid, name)
                except Exception:
                    log.exception("log-scan: error scanning %s", name)
        except Exception:
            log.exception("log-scan: outer loop error")


def _result_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            # Non-text content block (image / resource ref) — stringify so Claude sees it.
            parts.append(str(block))
    if not parts and getattr(result, "structuredContent", None) is not None:
        parts.append(str(result.structuredContent))
    return "\n".join(parts) if parts else "(empty result)"


async def run_agent(
    *,
    client: AsyncAnthropic,
    registry: ToolRegistry,
    system: str,
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Drive the tool-call loop. Returns (final_text, updated_messages)."""
    for iteration in range(MAX_TOOL_ITERS):
        log.debug("agent iter %d, %d messages", iteration, len(messages))
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system,
            tools=registry.anthropic_tools,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            messages.append({"role": "assistant", "content": resp.content})
            return text.strip() or "(no reply)", messages

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                prefixed = block.name
                route = registry.routes.get(prefixed)
                if route is None:
                    payload = f"Unknown tool: {prefixed}"
                    log.warning(payload)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": payload,
                            "is_error": True,
                        }
                    )
                    continue
                session, original = route
                try:
                    log.info("call %s args=%s", prefixed, block.input)
                    result = await session.call_tool(original, arguments=block.input or {})
                    text = _truncate(_result_to_text(result))
                    is_err = bool(getattr(result, "isError", False))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": text,
                            "is_error": is_err,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — we want everything fed back to Claude
                    log.exception("tool %s failed", prefixed)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Tool error: {exc!s}",
                            "is_error": True,
                        }
                    )
            # tool_result blocks must come FIRST in the user content array.
            messages.append({"role": "user", "content": tool_results})
            continue

        # Any other stop_reason (max_tokens, refusal, etc.) — bail with whatever text we got.
        text = "\n".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        messages.append({"role": "assistant", "content": resp.content})
        return text or f"(stopped: {resp.stop_reason})", messages

    return "stopping after 10 tool calls — try a more specific question.", messages


# ----------------------------------------------------------------------------
# Telegram glue
# ----------------------------------------------------------------------------

# In-memory per-chat conversation history. Lost on restart — that's the v1 bargain.
chat_histories: dict[int, list[dict[str, Any]]] = {}


def _trim_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Keep at most the last (HISTORY_TURN_CAP * 2) messages, but never break a
    # tool_use / tool_result pair: assistant message containing tool_use must be
    # immediately followed by the user message containing tool_result. Simplest
    # safe rule: if the head we'd cut into is a user-with-tool_result, drop one
    # more so we start cleanly on a user text message or assistant message.
    cap = HISTORY_TURN_CAP * 2
    if len(history) <= cap:
        return history
    trimmed = history[-cap:]
    # Make sure we don't start mid-pair:
    if trimmed and trimmed[0]["role"] == "user":
        first_content = trimmed[0]["content"]
        if isinstance(first_content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in first_content
        ):
            trimmed = trimmed[1:]
    return trimmed


async def on_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    n_tools = len(REGISTRY.anthropic_tools)
    n_dropped = len(REGISTRY.dropped)
    await update.message.reply_text(
        f"Helmsman's up. {n_tools} read-only tools wired across Coolify and "
        f"Beszel ({n_dropped} dropped). Commands: /health  /reset"
    )


async def on_reset(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    chat_histories.pop(update.effective_chat.id, None)
    await update.message.reply_text("History cleared.")


async def on_health(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    uptime_s = int(time.monotonic() - START_TIME) if START_TIME else 0
    h, rem = divmod(uptime_s, 3600)
    m, s = divmod(rem, 60)
    uptime_str = f"{h}h {m}m" if h else f"{m}m {s}s"

    statuses = await _mcp_probe()
    n_tools = len(REGISTRY.anthropic_tools)
    n_dropped = len(REGISTRY.dropped)

    def cap(n: str) -> str:
        return n[:1].upper() + n[1:]

    good = [cap(k) for k, v in statuses.items() if v == "ok"]
    bad = {cap(k): v.removeprefix("error: ") for k, v in statuses.items() if v != "ok"}

    if not bad:
        who = " and ".join(good) if good else "no MCPs"
        msg = (
            f"Yo, Helmsman's still standin' — up {uptime_str}.\n"
            f"{n_tools} tools wired ({n_dropped} dropped on the read-only filter), "
            f"{who} answerin' clean."
        )
    else:
        bad_phrase = ", ".join(f"{n} choked ({e})" for n, e in bad.items())
        if good:
            tail = f"{' and '.join(good)} clean, but {bad_phrase}."
        else:
            tail = f"{bad_phrase}."
        msg = (
            f"Helmsman's limpin'. Up {uptime_str}.\n"
            f"{n_tools} tools wired ({n_dropped} dropped). {tail}"
        )

    await update.message.reply_text(msg)


async def on_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    history = chat_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})

    try:
        reply, history = await run_agent(
            client=ANTHROPIC,
            registry=REGISTRY,
            system=SYSTEM_PROMPT,
            messages=history,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("agent crashed")
        await update.message.reply_text(f"Bridge error: {exc!s}")
        # Roll back the unanswered user message so /reset isn't required.
        history.pop()
        return

    chat_histories[chat_id] = _trim_history(history)
    # Telegram caps a single message at 4096 chars. Send chunks if needed.
    for chunk in _split_for_telegram(reply):
        await update.message.reply_text(chunk)


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if user.id != ALLOWED_USER_ID:
        log.info("ignoring update from non-allowed user id=%s", user.id)
        return False
    return True


def _split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out


# ----------------------------------------------------------------------------
# FastAPI webhook (Beszel -> us via Shoutrrr)
# ----------------------------------------------------------------------------

api = FastAPI(title="Helmsman")


@api.get("/health")
async def health() -> JSONResponse:
    mcps = await _mcp_probe()
    ok = bool(mcps) and all(v == "ok" for v in mcps.values())
    body = {
        "ok": ok,
        "tools": len(REGISTRY.anthropic_tools),
        "dropped": len(REGISTRY.dropped),
        "mcps": mcps,
        "uptime_seconds": int(time.monotonic() - START_TIME) if START_TIME else 0,
    }
    return JSONResponse(content=body, status_code=200 if ok else 503)


@api.post("/webhook/beszel")
async def beszel_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    expected = f"Bearer {CONFIG['BESZEL_WEBHOOK_SECRET']}"
    if authorization != expected:
        log.warning("rejected webhook: bad/missing Authorization header")
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        # Shoutrrr generic:// can also send form-encoded; accept body as text.
        body = (await request.body()).decode("utf-8", errors="replace")
        payload = {"raw": body}

    title = payload.get("title") or payload.get("subject")
    message = payload.get("message") or payload.get("body") or payload.get("raw") or ""
    log.info("beszel webhook: title=%r len(message)=%d", title, len(str(message)))

    synthetic_user = "Beszel webhook alert.\n"
    if title:
        synthetic_user += f"Title: {title}\n"
    synthetic_user += f"Message: {message}\n\nWhat's goin' on?"

    try:
        reply, _ = await run_agent(
            client=ANTHROPIC,
            registry=REGISTRY,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": synthetic_user}],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("webhook agent crashed")
        reply = (
            "A Beszel alert came in but I couldn't investigate it. "
            f"Error: {exc!s}"
        )

    # Push to Telegram. We send to the allowed user's chat — for a single private
    # chat with the bot, chat_id == user_id.
    for chunk in _split_for_telegram(reply):
        try:
            await TG_APP.bot.send_message(chat_id=ALLOWED_USER_ID, text=chunk)
        except Exception:
            log.exception("failed to push webhook reply to Telegram")
    return {"ok": True}


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------

# These are populated in main() before either the polling or webhook starts.
ANTHROPIC: AsyncAnthropic = None  # type: ignore[assignment]
REGISTRY: ToolRegistry = ToolRegistry()
TG_APP: Application = None  # type: ignore[assignment]
SYSTEM_PROMPT: str = ""
MCP_SESSIONS: dict[str, ClientSession] = {}
START_TIME: float = 0.0


async def main() -> None:
    global ANTHROPIC, TG_APP, SYSTEM_PROMPT, START_TIME  # noqa: PLW0603

    START_TIME = time.monotonic()
    SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")
    ANTHROPIC = AsyncAnthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])

    async with AsyncExitStack() as mcp_stack:
        beszel = await open_mcp(mcp_stack, beszel_params(), "beszel")
        coolify = await open_mcp(mcp_stack, coolify_params(), "coolify")

        MCP_SESSIONS["beszel"] = beszel
        MCP_SESSIONS["coolify"] = coolify

        await REGISTRY.register("beszel", beszel)
        await REGISTRY.register("coolify", coolify)

        log.info("helmsman: %s", REGISTRY.summary())
        if REGISTRY.dropped:
            log.info("dropped (write) tools: %s", ", ".join(sorted(REGISTRY.dropped)))
        else:
            log.info("no tools dropped — surprising; double-check FORBIDDEN_TOKENS")

        # Build PTB application. Handlers MUST be registered before initialize().
        TG_APP = Application.builder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
        TG_APP.add_handler(CommandHandler("start", on_start))
        TG_APP.add_handler(CommandHandler("reset", on_reset))
        TG_APP.add_handler(CommandHandler("health", on_health))
        TG_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

        await TG_APP.initialize()
        await TG_APP.start()
        await TG_APP.updater.start_polling(drop_pending_updates=True)
        log.info("telegram polling up")

        cfg = uvicorn.Config(
            api,
            host="0.0.0.0",
            port=8000,
            log_level=LOG_LEVEL.lower(),
            loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        # We manage signals ourselves so PTB shutdown can also run.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main-thread — fall back to KeyboardInterrupt propagation.
                pass

        async def _watcher() -> None:
            await stop_event.wait()
            log.info("shutdown signal received")
            server.should_exit = True

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(server.serve(), name="uvicorn")
                tg.create_task(_watcher(), name="signal-watcher")
                tg.create_task(_log_scan_loop(), name="log-scan")
        except* KeyboardInterrupt:
            log.info("KeyboardInterrupt — shutting down")
        finally:
            try:
                await TG_APP.updater.stop()
            except Exception:
                log.exception("error stopping telegram updater")
            try:
                await TG_APP.stop()
            except Exception:
                log.exception("error stopping telegram application")
            try:
                await TG_APP.shutdown()
            except Exception:
                log.exception("error shutting down telegram application")
            log.info("bye")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
