"""Helmsman — Telegram + Beszel-webhook bridge to Coolify/Beszel.

Inference backend is configurable: Anthropic (Claude Haiku 4.5) or local Ollama,
with optional automatic fallback. One process, one event loop. Two MCP servers
as long-lived stdio subprocesses. Read-only enforcement is done by filtering
the tool list before it ever reaches the model — see FORBIDDEN_TOKENS.
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
import uuid as uuid_lib
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic as _anthropic_pkg
import httpx
import ollama
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

# Backend-independent core. ANTHROPIC_API_KEY is required even when Ollama is
# the primary, because Anthropic is the default fallback. Set BACKEND_FALLBACK=
# (empty) to disable fallback if you really want pure-local with no safety net.
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

# Backend selection. When primary is anthropic the default fallback is empty
# (no useful safety net since the fallback would be the same backend); when
# primary is ollama, default fallback is anthropic.
BACKEND_NAME = os.environ.get("BACKEND", "anthropic").lower().strip()
_DEFAULT_FALLBACK = "anthropic" if BACKEND_NAME == "ollama" else ""
BACKEND_FALLBACK_NAME = os.environ.get("BACKEND_FALLBACK", _DEFAULT_FALLBACK).lower().strip()
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").strip()
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "").strip()
OLLAMA_TIMEOUT_SEC = float(os.environ.get("OLLAMA_TIMEOUT_SEC", "60"))

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _load_config() -> dict[str, str]:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.stderr.write(f"FATAL: missing env vars: {', '.join(missing)}\n")
        sys.exit(2)

    valid = {"anthropic", "ollama"}
    if BACKEND_NAME not in valid:
        sys.stderr.write(
            f"FATAL: BACKEND must be one of {sorted(valid)}, got {BACKEND_NAME!r}\n"
        )
        sys.exit(2)
    if BACKEND_FALLBACK_NAME and BACKEND_FALLBACK_NAME not in valid:
        sys.stderr.write(
            f"FATAL: BACKEND_FALLBACK must be empty or one of {sorted(valid)}, "
            f"got {BACKEND_FALLBACK_NAME!r}\n"
        )
        sys.exit(2)
    if BACKEND_NAME == BACKEND_FALLBACK_NAME and BACKEND_FALLBACK_NAME:
        sys.stderr.write(
            "FATAL: BACKEND_FALLBACK must be different from BACKEND (or empty)\n"
        )
        sys.exit(2)

    needs_ollama = "ollama" in (BACKEND_NAME, BACKEND_FALLBACK_NAME)
    if needs_ollama:
        ollama_missing: list[str] = []
        if not OLLAMA_BASE_URL:
            ollama_missing.append("OLLAMA_BASE_URL")
        if not OLLAMA_MODEL:
            ollama_missing.append("OLLAMA_MODEL")
        if ollama_missing:
            sys.stderr.write(
                f"FATAL: BACKEND or BACKEND_FALLBACK is 'ollama', but missing: "
                f"{', '.join(ollama_missing)}\n"
            )
            sys.exit(2)

    return {k: os.environ[k] for k in REQUIRED_ENV}


CONFIG = _load_config()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
ALLOWED_USER_ID = int(CONFIG["TELEGRAM_ALLOWED_USER_ID"])
MAX_TOOL_ITERS = 10
TOOL_RESULT_CHAR_CAP = 8000
HISTORY_TURN_CAP = 10  # 10 user + 10 assistant text turns

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

# Tools we've found to be broken upstream — separate from the read-only filter.
# Add a tool here when its underlying call reliably 4xx/5xx's against the current
# service schema, so the model doesn't keep retrying it and burning iterations.
TOOL_DENYLIST: frozenset[str] = frozenset({
    # beszel-mcp: filter/sort against the alerts_history collection 400s
    # (system_id field/operator mismatch with current Beszel schema).
    "list_alert_history",
})

# ----------------------------------------------------------------------------
# Background log-scan config (Coolify only for v1)
# ----------------------------------------------------------------------------

LOG_SCAN_ENABLED = os.environ.get("LOG_SCAN_ENABLED", "true").lower() in ("1", "true", "yes")
LOG_SCAN_INTERVAL_SEC = int(os.environ.get("LOG_SCAN_INTERVAL_SEC", "600"))
LOG_SCAN_LINES = int(os.environ.get("LOG_SCAN_LINES", "200"))
LOG_SCAN_COOLDOWN_SEC = int(os.environ.get("LOG_SCAN_COOLDOWN_SEC", "1800"))

# Comma-separated substrings; an app is skipped if any needle appears in its
# name (case-insensitive). Empty by default — every app is scanned, including
# Helmsman itself. Add app names here to silence noisy ones you don't want
# triaged.
LOG_SCAN_EXCLUDE: tuple[str, ...] = tuple(
    s.strip().lower()
    for s in os.environ.get("LOG_SCAN_EXCLUDE", "").split(",")
    if s.strip()
)

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

    Stores a unified tool list ({name, description, input_schema}) — each backend
    translates to its own native shape via Backend.format_tools().
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[ClientSession, str]] = {}
        self.tools: list[dict[str, Any]] = []
        self.dropped: list[str] = []

    async def register(self, label: str, session: ClientSession) -> None:
        listed = await session.list_tools()
        for tool in listed.tools:
            prefixed = f"{label}__{tool.name}"
            reason: str | None = None
            if is_write_tool(tool.name):
                reason = "write"
            elif tool.name.lower() in TOOL_DENYLIST:
                reason = "broken"
            if reason:
                self.dropped.append(f"{prefixed} ({reason})")
                continue
            self.routes[prefixed] = (session, tool.name)
            self.tools.append(
                {
                    "name": prefixed,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                }
            )

    def summary(self) -> str:
        return f"{len(self.tools)} tools registered, {len(self.dropped)} dropped"


# ----------------------------------------------------------------------------
# Backends (Anthropic / Ollama)
# ----------------------------------------------------------------------------


@dataclass
class ToolCall:
    """Backend-agnostic tool call. id is used to correlate with ToolResult."""
    id: str
    name: str  # prefixed: "beszel__list_systems"
    input: dict[str, Any]


@dataclass
class ToolResult:
    """Result of executing a single tool call."""
    id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class InferenceResult:
    """Outcome of a single backend.infer() call."""
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | other
    backend_used: str
    raw_assistant_message: Any  # opaque; passed to format_followup() next iteration


class Backend:
    """Abstract base. Subclasses translate the unified tool list and turn-scoped
    messages list to/from their native API shape."""

    name: str = "abstract"
    timeout_sec: float | None = None  # per-inference timeout, None = no wrap

    def format_tools(self, unified: list[dict[str, Any]]) -> list[Any]:
        raise NotImplementedError

    def build_initial_messages(
        self, history: list[dict[str, str]], new_user_text: str | None
    ) -> list[Any]:
        """Translate canonical text-only history (+ new user text) into backend
        message-list shape. Both Anthropic and Ollama happen to accept simple
        {role, content} dicts for plain text turns, so the default works."""
        msgs: list[Any] = [{"role": h["role"], "content": h["content"]} for h in history]
        if new_user_text is not None:
            msgs.append({"role": "user", "content": new_user_text})
        return msgs

    async def infer(
        self, *, system: str, messages: list[Any], tools: list[Any]
    ) -> InferenceResult:
        raise NotImplementedError

    def format_followup(
        self, prior: InferenceResult, results: list[ToolResult]
    ) -> list[Any]:
        """Build the messages to append for the next iteration: assistant
        tool_use turn + tool result(s)."""
        raise NotImplementedError

    def label(self) -> str:
        return self.name


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, api_key: str, model: str = ANTHROPIC_MODEL):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    def format_tools(self, unified: list[dict[str, Any]]) -> list[Any]:
        return list(unified)  # already Anthropic-shaped

    async def infer(
        self, *, system: str, messages: list[Any], tools: list[Any]
    ) -> InferenceResult:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            tools=tools,
            messages=messages,
        )
        text = "\n".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input or {})
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        ]
        return InferenceResult(
            text=text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "end_turn",
            backend_used=self.name,
            raw_assistant_message={"role": "assistant", "content": resp.content},
        )

    def format_followup(
        self, prior: InferenceResult, results: list[ToolResult]
    ) -> list[Any]:
        return [
            prior.raw_assistant_message,
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                    for tr in results
                ],
            },
        ]

    def label(self) -> str:
        return f"anthropic ({self.model})"


class OllamaBackend(Backend):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_sec: float):
        # Use the SDK's AsyncClient. We pass a long-ish HTTP timeout so single
        # inferences don't fail prematurely; the asyncio.wait_for in run_agent
        # bounds the call from outside.
        self.client = ollama.AsyncClient(host=base_url, timeout=timeout_sec + 10)
        self.model = model
        self.base_url = base_url
        self.timeout_sec = timeout_sec

    def format_tools(self, unified: list[dict[str, Any]]) -> list[Any]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in unified
        ]

    async def infer(
        self, *, system: str, messages: list[Any], tools: list[Any]
    ) -> InferenceResult:
        # Ollama wants the system prompt as the first message with role "system".
        # The persistent messages list does NOT contain a system entry; we
        # prepend on every call so re-running across iterations is consistent.
        ollama_messages = [{"role": "system", "content": system}] + messages
        resp = await self.client.chat(
            model=self.model,
            messages=ollama_messages,
            tools=tools,
        )
        msg = resp.message
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            # Ollama doesn't always provide a stable id; synthesize one.
            tc_id = f"call_{uuid_lib.uuid4().hex[:10]}"
            args = tc.function.arguments or {}
            # `arguments` is already a dict in ollama-python, but fall through if
            # a future version returns a JSON string.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(
                ToolCall(id=tc_id, name=tc.function.name, input=dict(args))
            )
        text = (msg.content or "").strip()
        stop_reason = "tool_use" if tool_calls else "end_turn"
        # Build a serializable assistant message for the next iteration.
        try:
            raw = msg.model_dump()
        except AttributeError:
            raw = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in (msg.tool_calls or [])
                ],
            }
        # Stuff the synthetic ids into the raw message so format_followup can
        # pair tool results back. Ollama doesn't use the id but having it makes
        # debug traces clearer.
        if isinstance(raw, dict):
            raw_calls = raw.get("tool_calls") or []
            for i, tc_dict in enumerate(raw_calls):
                if i < len(tool_calls) and isinstance(tc_dict, dict):
                    tc_dict.setdefault("id", tool_calls[i].id)
        return InferenceResult(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            backend_used=self.name,
            raw_assistant_message=raw,
        )

    def format_followup(
        self, prior: InferenceResult, results: list[ToolResult]
    ) -> list[Any]:
        out: list[Any] = [prior.raw_assistant_message]
        for tr in results:
            out.append(
                {
                    "role": "tool",
                    "content": tr.content,
                    "tool_name": tr.name,
                }
            )
        return out

    def label(self) -> str:
        return f"ollama ({self.model})"


# Exceptions that trip the fallback path. Narrow on purpose: only transport /
# timeout / outage symptoms, never quality or schema problems.
FALLBACK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    ollama.ResponseError,
    _anthropic_pkg.APIConnectionError,
    _anthropic_pkg.APITimeoutError,
)


@dataclass
class BackendStats:
    last_used: str = ""
    fallback_events: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    def fallback_count_24h(self) -> int:
        cutoff = time.monotonic() - 86400
        return sum(1 for ts in self.fallback_events if ts >= cutoff)


def _build_backend(name: str) -> Backend:
    if name == "anthropic":
        return AnthropicBackend(api_key=CONFIG["ANTHROPIC_API_KEY"])
    if name == "ollama":
        return OllamaBackend(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            timeout_sec=OLLAMA_TIMEOUT_SEC,
        )
    raise ValueError(f"unknown backend: {name}")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _truncate(s: str, cap: int = TOOL_RESULT_CHAR_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n[truncated, original was {len(s)} chars]"


def _result_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            # Non-text content block (image / resource ref) — stringify so the
            # model sees it.
            parts.append(str(block))
    if not parts and getattr(result, "structuredContent", None) is not None:
        parts.append(str(result.structuredContent))
    return "\n".join(parts) if parts else "(empty result)"


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


async def _dispatch_tool(tc: ToolCall) -> ToolResult:
    """Route a tool call to the right MCP session. Errors come back as
    ToolResult(is_error=True) so the model can recover gracefully."""
    route = REGISTRY.routes.get(tc.name)
    if route is None:
        msg = f"Unknown tool: {tc.name}"
        log.warning(msg)
        return ToolResult(id=tc.id, name=tc.name, content=msg, is_error=True)
    session, original = route
    try:
        log.info("call %s args=%s", tc.name, tc.input)
        result = await session.call_tool(original, arguments=tc.input or {})
        text = _truncate(_result_to_text(result))
        is_err = bool(getattr(result, "isError", False))
        return ToolResult(id=tc.id, name=tc.name, content=text, is_error=is_err)
    except Exception as exc:  # noqa: BLE001 — feed everything back to the model
        log.exception("tool %s failed", tc.name)
        return ToolResult(
            id=tc.id, name=tc.name, content=f"Tool error: {exc!s}", is_error=True
        )


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
        if not uuid:
            continue
        n = str(name).lower()
        if LOG_SCAN_EXCLUDE and any(needle in n for needle in LOG_SCAN_EXCLUDE):
            continue
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
        reply = await run_agent(history=[], new_user_text=synthetic)
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
        "log-scan: enabled (interval=%ds, lines=%d, cooldown=%ds, exclude=%s, patterns=%s)",
        LOG_SCAN_INTERVAL_SEC,
        LOG_SCAN_LINES,
        LOG_SCAN_COOLDOWN_SEC,
        ",".join(LOG_SCAN_EXCLUDE) or "(none)",
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


# ----------------------------------------------------------------------------
# Agent loop (backend-agnostic, with fallback)
# ----------------------------------------------------------------------------


async def run_agent(*, history: list[dict[str, str]], new_user_text: str) -> str:
    """Drive the tool-call loop for one user turn.

    `history` is the canonical text-only conversation log
    (`[{role, content}]` dicts, no tool blocks). Fresh empty list for one-shot
    contexts (webhook, log scan).

    Returns the final assistant text. Caller decides whether to persist
    `(user_text, reply)` to history.

    Backend handling: tries `PRIMARY_BACKEND`. On a transport-level failure
    (FALLBACK_EXCEPTIONS), if `FALLBACK_BACKEND` is configured, switches to it
    *for the rest of this run_agent call*. After the call returns, the next
    user turn tries the primary again from fresh.
    """
    backend = PRIMARY_BACKEND
    messages = backend.build_initial_messages(history, new_user_text)
    tools = backend.format_tools(REGISTRY.tools)
    fallback_used = False

    for iteration in range(MAX_TOOL_ITERS):
        log.debug("agent iter %d backend=%s", iteration, backend.name)
        try:
            if backend.timeout_sec:
                result = await asyncio.wait_for(
                    backend.infer(system=SYSTEM_PROMPT, messages=messages, tools=tools),
                    timeout=backend.timeout_sec,
                )
            else:
                result = await backend.infer(
                    system=SYSTEM_PROMPT, messages=messages, tools=tools
                )
        except FALLBACK_EXCEPTIONS as exc:
            if FALLBACK_BACKEND is None or fallback_used:
                # No fallback or already on it — let the caller handle it.
                raise
            log.warning(
                "backend %s failed (%s: %s); switching to fallback %s mid-turn",
                backend.name,
                type(exc).__name__,
                exc,
                FALLBACK_BACKEND.name,
            )
            BACKEND_STATS.fallback_events.append(time.monotonic())
            backend = FALLBACK_BACKEND
            messages = backend.build_initial_messages(history, new_user_text)
            tools = backend.format_tools(REGISTRY.tools)
            fallback_used = True
            continue

        BACKEND_STATS.last_used = backend.name

        if result.stop_reason == "end_turn":
            return result.text or "(no reply)"

        if result.stop_reason == "tool_use":
            tool_results = [await _dispatch_tool(tc) for tc in result.tool_calls]
            messages.extend(backend.format_followup(result, tool_results))
            continue

        # max_tokens / refusal / other — return whatever text we got.
        return result.text or f"(stopped: {result.stop_reason})"

    return "stopping after 10 tool calls — try a more specific question."


# ----------------------------------------------------------------------------
# Telegram glue
# ----------------------------------------------------------------------------

# Canonical per-chat history: {"role": "user"|"assistant", "content": "<text>"}.
# Lost on restart — the v1 bargain.
chat_histories: dict[int, list[dict[str, str]]] = {}


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep at most HISTORY_TURN_CAP user/assistant pairs."""
    cap = HISTORY_TURN_CAP * 2
    if len(history) <= cap:
        return history
    return history[-cap:]


async def on_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    n_tools = len(REGISTRY.tools)
    n_dropped = len(REGISTRY.dropped)
    primary_label = PRIMARY_BACKEND.label()
    fb_label = f", fallback: {FALLBACK_BACKEND.label()}" if FALLBACK_BACKEND else ""
    await update.message.reply_text(
        f"Helmsman's up. {n_tools} read-only tools wired across Coolify and "
        f"Beszel ({n_dropped} dropped). Brain: {primary_label}{fb_label}. "
        f"Commands: /health  /reset"
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
    n_tools = len(REGISTRY.tools)
    n_dropped = len(REGISTRY.dropped)

    def cap(n: str) -> str:
        return n[:1].upper() + n[1:]

    good = [cap(k) for k, v in statuses.items() if v == "ok"]
    bad = {cap(k): v.removeprefix("error: ") for k, v in statuses.items() if v != "ok"}

    if not bad:
        who = " and ".join(good) if good else "no MCPs"
        head = (
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
        head = (
            f"Helmsman's limpin'. Up {uptime_str}.\n"
            f"{n_tools} tools wired ({n_dropped} dropped). {tail}"
        )

    last_used = BACKEND_STATS.last_used or "(none yet)"
    fb_count = BACKEND_STATS.fallback_count_24h()
    fb_label = (
        FALLBACK_BACKEND.label() if FALLBACK_BACKEND else "(none)"
    )
    brain_line = (
        f"Brain: primary {PRIMARY_BACKEND.label()}, fallback {fb_label}. "
        f"Last used: {last_used}. Fallbacks in last 24h: {fb_count}."
    )

    await update.message.reply_text(f"{head}\n{brain_line}")


async def on_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    history = chat_histories.setdefault(chat_id, [])

    try:
        reply = await run_agent(history=history, new_user_text=text)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent crashed")
        await update.message.reply_text(f"Bridge error: {exc!s}")
        return

    # Persist canonical text turns only — no tool_use/tool_result clutter.
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    chat_histories[chat_id] = _trim_history(history)
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
        "tools": len(REGISTRY.tools),
        "dropped": len(REGISTRY.dropped),
        "mcps": mcps,
        "uptime_seconds": int(time.monotonic() - START_TIME) if START_TIME else 0,
        "backend": {
            "primary": PRIMARY_BACKEND.label(),
            "fallback": FALLBACK_BACKEND.label() if FALLBACK_BACKEND else None,
            "last_used": BACKEND_STATS.last_used or None,
            "fallback_count_24h": BACKEND_STATS.fallback_count_24h(),
        },
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
        reply = await run_agent(history=[], new_user_text=synthetic_user)
    except Exception as exc:  # noqa: BLE001
        log.exception("webhook agent crashed")
        reply = (
            "A Beszel alert came in but I couldn't investigate it. "
            f"Error: {exc!s}"
        )

    # Push to Telegram. Single private chat with the bot means chat_id == user_id.
    for chunk in _split_for_telegram(reply):
        try:
            await TG_APP.bot.send_message(chat_id=ALLOWED_USER_ID, text=chunk)
        except Exception:
            log.exception("failed to push webhook reply to Telegram")
    return {"ok": True}


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------

# Populated in main() before either polling or webhook starts.
PRIMARY_BACKEND: Backend = None  # type: ignore[assignment]
FALLBACK_BACKEND: Backend | None = None
BACKEND_STATS: BackendStats = BackendStats()
REGISTRY: ToolRegistry = ToolRegistry()
TG_APP: Application = None  # type: ignore[assignment]
SYSTEM_PROMPT: str = ""
MCP_SESSIONS: dict[str, ClientSession] = {}
START_TIME: float = 0.0


async def main() -> None:
    global PRIMARY_BACKEND, FALLBACK_BACKEND, TG_APP, SYSTEM_PROMPT, START_TIME  # noqa: PLW0603

    START_TIME = time.monotonic()
    SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

    PRIMARY_BACKEND = _build_backend(BACKEND_NAME)
    FALLBACK_BACKEND = (
        _build_backend(BACKEND_FALLBACK_NAME) if BACKEND_FALLBACK_NAME else None
    )
    log.info(
        "backend: primary=%s, fallback=%s",
        PRIMARY_BACKEND.label(),
        FALLBACK_BACKEND.label() if FALLBACK_BACKEND else "(none)",
    )

    async with AsyncExitStack() as mcp_stack:
        beszel = await open_mcp(mcp_stack, beszel_params(), "beszel")
        coolify = await open_mcp(mcp_stack, coolify_params(), "coolify")

        MCP_SESSIONS["beszel"] = beszel
        MCP_SESSIONS["coolify"] = coolify

        await REGISTRY.register("beszel", beszel)
        await REGISTRY.register("coolify", coolify)

        log.info("helmsman: %s", REGISTRY.summary())
        if REGISTRY.dropped:
            log.info("dropped tools: %s", ", ".join(sorted(REGISTRY.dropped)))
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
                # Windows / non-main-thread — fall back to KeyboardInterrupt.
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
