#!/usr/bin/env python3
"""UX46 adapter over the real native Hermes TUI gateway (Pane's runtime).

This is a thin overlay on the Hermes install that is already on this machine,
already configured and already logged in. It starts one native TUI gateway
process of its own, drives it over the documented JSON-RPC protocol, and
exposes the same loopback HTTP shape the UX46 console already speaks to
``tools/atlas_rivet.py``.

    python3 tools/atlas_hermes.py --port 8880

What it deliberately is not
---------------------------
* Not a second agent, and not a Codex agent wearing Pane's name. Every turn is
  run by the installed Hermes runtime, by the persona in its own ``SOUL.md``.
  This adapter adds no model, no prompt and no agent layer of its own.
* Not a manager of anybody else's Hermes. The messaging gateway that is already
  running (``hermes gateway run``) is never contacted, never signalled and
  never reconfigured. No lock is deleted, no process is stopped.
* Not an owner of sessions it did not open. Hermes's ``_claim_active_session_slot``
  is a *capacity lease*, not an exclusive writer lock — and on this machine the
  cap is unset, so it records nothing at all. Ownership is therefore decided
  here, conservatively, from evidence: our own live session list, the session's
  own ``ended_at``, and the messaging gateway's own routing table. Anything this
  adapter cannot prove is idle is read-only, and says why.
* Not an RPC tunnel. A short allowlist bounds every method that can be reached;
  a browser names a room and an action, never a method or a session id.
* Not a filing clerk. A conversation is filed under a real Session Vault project
  only when a record there carries a verified ``hermes`` origin naming its exact
  persisted session id. Everything else is Unfiled — an internal namespace of
  this adapter, not a project, with nothing created on disk for it.
* Not a credential reader. It never opens ``auth.json``, ``.env`` or any secret,
  and never copies a provider credential. The native gateway child resolves its
  own environment exactly as Hermes always does.

The wire contract is ``tui_gateway/server.py`` in the installed build and
``website/docs/developer-guide/programmatic-integration.md``; what was verified
and when is recorded in ``architecture/hermes-pane-adapter-1.md``.
"""

from __future__ import annotations

import argparse
import atlas_creation as creation
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import subprocess
import threading
import time
import traceback
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atlas_files as files  # noqa: E402
import atlas_rivet as agent3  # noqa: E402

# Reused verbatim from the Agent3 adapter because they are runtime-neutral: a
# durable client-id journal, a long-poll event ring, and a compare-and-set
# draft store. Nothing OpenClaw-specific comes across with them.
Events = agent3.Events
DraftConflict = agent3.DraftConflict
DuplicateMismatch = agent3.DuplicateMismatch
body_hash = agent3.body_hash

PENDING = agent3.PENDING
DISPATCHING = agent3.DISPATCHING
ACCEPTED = agent3.ACCEPTED
FAILED = agent3.FAILED
UNCERTAIN = agent3.UNCERTAIN
CANCELLED = agent3.CANCELLED

ADAPTER_NAME = "ux46-hermes"
ADAPTER_VERSION = "1"
RUNTIME = "hermes"

DEFAULT_PORT = 8880
DEFAULT_HERMES_HOME = "~/.hermes"
DEFAULT_HERMES_REPO = "~/.hermes/hermes-agent"
DEFAULT_REGISTRY = "~/.codex/projects/registry.json"
DEFAULT_UNFILED_PROJECT = "unfiled"
DEFAULT_AGENT_NAME = "Pane"
DEFAULT_SOURCE = "ux46"

MAX_BODY = 64 * 1024
CATALOG_TTL = 10.0
HISTORY_PAGE_CAP = 200
SEARCH_SCAN_CAP = 2000
LIVE_TAIL_CHARS = 60_000
# One refresh-triggering event per session per this many seconds while text is
# streaming. The growing text itself is served from the live tail, so the
# console still animates without one transcript read per token.
DELTA_EVENT_INTERVAL = 0.8

# Read out of the installed build (`image.attach_bytes` / `_ATTACH_BYTES_MAX_BYTES`),
# not guessed. `file.attach` has no explicit byte cap of its own; the same
# ceiling is applied here so one number is true for every kind of file.
NATIVE_IMAGE_MAX_BYTES = 25 * 1024 * 1024
NATIVE_FILE_MAX_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_UPLOAD_OVERHEAD = 256 * 1024

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SLUG_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Every native method this adapter may ask for, and nothing else. A browser
# names a room and an action; it never names a method or a native session id.
ALLOWED_METHODS = frozenset({
    "session.list",
    "session.active_list",
    "session.create",
    "session.resume",
    "session.close",
    "session.interrupt",
    "session.status",
    "session.compress",
    "slash.exec",
    "prompt.submit",
    "commands.catalog",
    "image.attach_bytes",
    "file.attach",
    "approval.respond",
    "clarify.respond",
})

# `session.list` denies only the `tool` source, so this adapter does its own
# filtering: sub-agent runs are machine work, not conversations, and there are
# hundreds of them.
DERIVED_SOURCES = frozenset({"tool", "subagent"})
# Conversations the already-running `hermes gateway` owns end-to-end. A person
# is talking to Pane there right now; this adapter never resumes one.
ROUTED_SOURCES = frozenset({
    "discord", "telegram", "slack", "whatsapp", "signal",
    "imessage", "email", "sms", "webhook", "api",
})
AUTOMATION_SOURCES = frozenset({"cron"})

# How this adapter decided who holds a session. Every room carries one, said
# out loud, alongside the evidence it came from.
CLAIM_ADAPTER = "adapter_session"
CLAIM_LIVE = "live_here"
CLAIM_CLOSED = "closed"
CLAIM_ROUTED = "gateway_routed"
CLAIM_OPEN = "open_elsewhere"
CLAIM_UNVERIFIED = "unverified"
CLAIM_DERIVED = "derived"

CLAIM_BASIS = {
    CLAIM_ADAPTER: "this adapter created this conversation (source ux46)",
    CLAIM_LIVE: "this adapter's own native gateway reports it live in session.active_list",
    CLAIM_CLOSED: "Hermes last recorded this conversation as ended",
    CLAIM_ROUTED: "the running Hermes messaging gateway routes this conversation",
    CLAIM_OPEN: "state.db has no ended_at, so some surface may still be writing to it",
    CLAIM_UNVERIFIED: "the session metadata could not be read, so ownership is unknown",
    CLAIM_DERIVED: "a sub-agent or tool run, not a conversation anyone types into",
}

ATTACHABLE_CLAIMS = frozenset({CLAIM_ADAPTER, CLAIM_LIVE, CLAIM_CLOSED})

STATUS_MEANING = {
    PENDING: "journaled here, not yet handed to the native runtime",
    DISPATCHING: "handed to the native runtime; no answer yet",
    ACCEPTED: "the native runtime started a turn — not a completed answer",
    FAILED: "the native runtime refused it; nothing was delivered",
    UNCERTAIN: "delivery is unknown; this adapter will not resend on its own",
    CANCELLED: "cancelled here before it was ever handed to the runtime",
}

# Every console action this adapter does not implement, and the honest reason.
# Each is answered with an explicit refusal and is never forwarded as chat text.
UNSUPPORTED_ACTIONS = {
    "goal": "Hermes keeps goals inside a live session's own /goal state; this adapter "
            "does not read or set one",
    "effort": "reasoning effort is a Hermes session/config setting this adapter does "
              "not change",
    "reasoning": "reasoning effort is a Hermes session/config setting this adapter does "
                 "not change",
    "speak": "local speech belongs to the Codex console, not to this adapter",
    "interrupt": "use Stop, which maps to the native session.interrupt for a session "
                 "this adapter opened",
}

# These names are the UX46 command contract. Commands marked native below are
# advertised only when this running Hermes gateway's own catalog confirms the
# underlying operation. Nothing is ever passed to prompt.submit as a fallback.
COMMANDS_SUPPORTED = (
    "/help", "/new", "/status", "/refresh", "/model", "/effort",
    "/reasoning", "/reasononing", "/steer", "/compact", "/goal",
)
ADAPTER_COMMANDS = frozenset({"/help", "/new", "/status", "/refresh"})
NATIVE_COMMANDS = {
    "/model": "/model",
    "/effort": "/reasoning",
    "/reasoning": "/reasoning",
    "/reasononing": "/reasoning",
    "/steer": "/steer",
    "/compact": "/compress",
    "/goal": "/goal",
}
COMMAND_HELP = (
    {"name": "/help", "usage": "/help",
     "description": "show commands this Pane adapter can currently execute"},
    {"name": "/new", "usage": "/new [title]",
     "description": "start a fresh native Hermes conversation through session.create and "
                    "open it here; the conversation you are in is left exactly as it is"},
    {"name": "/status", "usage": "/status",
     "description": "read the native session.status block for a conversation this "
                    "adapter has open"},
    {"name": "/refresh", "usage": "/refresh",
     "description": "re-check this adapter's own native gateway connection"},
    {"name": "/model", "usage": "/model [name]",
     "description": "run Hermes's native model command in this open conversation"},
    {"name": "/effort", "usage": "/effort [level]",
     "description": "alias for Hermes's native /reasoning command"},
    {"name": "/reasoning", "usage": "/reasoning [level]",
     "description": "run Hermes's native reasoning command in this open conversation"},
    {"name": "/reasononing", "usage": "/reasononing [level]",
     "description": "alias for /reasoning"},
    {"name": "/steer", "usage": "/steer <message>",
     "description": "send a native steering instruction to a running Hermes turn"},
    {"name": "/compact", "usage": "/compact [focus]",
     "description": "run Hermes's native context compression for this open conversation"},
    {"name": "/goal", "usage": "/goal [text | status | pause | resume | clear]",
     "description": "run Hermes's native goal command in this open conversation"},
)
COMMAND_UNSUPPORTED = {
    "/resume": "pick the conversation in the room list instead; resuming is what "
               "Continue here does",
}

CAPABILITY = "hermes-native"
CAPABILITY_SHORT = "Pane · native Hermes"
CAPABILITY_LABEL = (
    "the native Hermes runtime on this machine — readable, and sendable through a "
    "native session this adapter opened"
)
CAPABILITY_READONLY_SHORT = "Pane · read-only"
CAPABILITY_READONLY_LABEL = (
    "a native Hermes conversation this adapter cannot prove is free; readable here, "
    "not writable"
)


def capabilities(*, stream: bool, attachments: bool) -> dict:
    return {
        "read_history": True,
        "search_history": True,
        "send": True,
        "stop": True,
        "stream": stream,
        "queue": True,
        "attach_scope": "native_session",
        "attachments": attachments,
        "attachment_images": attachments,
        "attachment_files": attachments,
        "attachment_previews": True,
        "attachment_limits": {
            "image_max_bytes": NATIVE_IMAGE_MAX_BYTES,
            "file_max_bytes": NATIVE_FILE_MAX_BYTES,
            "message_total_bytes": NATIVE_FILE_MAX_BYTES,
            "per_message": MAX_ATTACHMENTS_PER_MESSAGE,
            "basis": "the installed build's image.attach_bytes cap (25 MB), applied to "
                     "file.attach too so one number is true for every kind of file",
        },
        "drafts": True,
        "commands": True,
        "commands_supported": list(COMMANDS_SUPPORTED),
        "command_help": [dict(entry) for entry in COMMAND_HELP],
        "approvals": True,
        "goal": False,
        "effort": False,
        # Voice belongs to the Codex console. Nothing here speaks, and saying
        # so is better than an endpoint that exists and does nothing.
        "voice": False,
        "notes": [
            "Continue here opens a real native session through session.resume (or "
            "session.create for /new). Release closes exactly that session and nothing "
            "else — no other Hermes surface, process or lock is touched.",
            "A conversation this adapter cannot prove is free is read-only. Hermes's "
            "active-session lease is a capacity counter, not an exclusive writer lock, "
            "so it is never treated as proof that a session is free.",
            "Send is idempotent on the client id: a repeat of a known id reports the "
            "journaled outcome and calls the runtime zero more times.",
            "A send whose outcome the runtime never confirmed is reported uncertain and "
            "is never resent automatically.",
            "Transcripts are read from Hermes's own state.db, opened read-only. This "
            "adapter never writes to it.",
            "A native session is reported Running only when this adapter's own gateway "
            "says the turn is working. An unfinished goal and a live process are not "
            "turns, and neither is reported as one.",
        ],
    }


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class AdapterError(Exception):
    """A refusal this adapter is choosing to make, with the reason."""

    def __init__(self, status: HTTPStatus, code: str, message: str, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


class NativeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NativeUnavailable(NativeError):
    """The native gateway is not there to ask."""


class NativeUncertain(NativeError):
    """The native gateway was asked and never answered. Nothing is repeated."""


# ---------------------------------------------------------------------------
# the native gateway child
# ---------------------------------------------------------------------------

class NativeGateway:
    """One adapter-owned ``python -m tui_gateway.entry`` process.

    JSON-RPC 2.0, one object per line, over the child's stdin/stdout — exactly
    the protocol the Ink TUI speaks. Notifications arrive as
    ``{"method": "event", "params": {"type", "session_id", "payload"}}`` and are
    handed to ``on_event`` on the reader thread.

    The child resolves its own Hermes environment. This class passes
    ``HERMES_HOME`` and nothing else that touches credentials: it never reads
    ``auth.json``, ``.env`` or any secret, and no credential is ever seen by,
    stored in or emitted from this process.
    """

    name = "native"

    def __init__(self, command: list[str], *, cwd: str, env: dict,
                 on_event=None, ready_timeout: float = 90.0):
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env)
        self._on_event = on_event
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._waiters: dict[str, dict] = {}
        self._seq = 0
        self._ready: dict = {}
        self._error = ""
        self._reader: threading.Thread | None = None
        self.generation = 0
        self.started_at = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError as exc:
            raise NativeUnavailable("gateway_unreachable",
                                    f"the native gateway could not be started: {exc}") from exc
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        deadline = time.time() + self._ready_timeout
        with self._cond:
            while not self._ready and not self._error and time.time() < deadline:
                self._cond.wait(timeout=max(0.05, deadline - time.time()))
            if self._error:
                raise NativeUnavailable("gateway_unreachable", self._error)
            if not self._ready:
                raise NativeUnavailable(
                    "gateway_unreachable",
                    "the native gateway did not report gateway.ready in time")

    def stop(self) -> None:
        """Close our own child. Never signals any other Hermes process."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

    @property
    def alive(self) -> bool:
        proc = self._proc
        return bool(proc and proc.poll() is None)

    # -- io ----------------------------------------------------------------
    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("method") == "event":
                params = frame.get("params") or {}
                event = str(params.get("type") or "")
                if event == "gateway.ready":
                    with self._cond:
                        self._ready = dict(params.get("payload") or {}) or {"ready": True}
                        self._cond.notify_all()
                if self._on_event:
                    try:
                        self._on_event(event, str(params.get("session_id") or ""),
                                       params.get("payload") or {})
                    except Exception:
                        pass
                continue
            key = str(frame.get("id") or "")
            if not key:
                continue
            with self._cond:
                waiter = self._waiters.get(key)
                if waiter is not None:
                    waiter["frame"] = frame
                    self._cond.notify_all()
        with self._cond:
            if not self._ready and not self._error:
                self._error = "the native gateway exited before it reported ready"
            # Every outstanding call is now unanswerable rather than pending.
            for waiter in self._waiters.values():
                waiter.setdefault("frame", None)
            self._cond.notify_all()

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if method not in ALLOWED_METHODS:
            raise NativeError("method_not_allowed", f"this adapter never calls {method}")
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise NativeUnavailable("gateway_unreachable",
                                    "this adapter's native gateway is not running")
        with self._lock:
            self._seq += 1
            key = str(self._seq)
            payload = json.dumps({"jsonrpc": "2.0", "id": key,
                                  "method": method, "params": params or {}})
            with self._cond:
                self._waiters[key] = {"frame": None}
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except OSError as exc:
                with self._cond:
                    self._waiters.pop(key, None)
                raise NativeUnavailable("gateway_unreachable", str(exc)) from exc
        deadline = time.time() + timeout
        with self._cond:
            while self._waiters[key]["frame"] is None and time.time() < deadline:
                self._cond.wait(timeout=max(0.05, deadline - time.time()))
            frame = self._waiters.pop(key)["frame"]
        if frame is None:
            raise NativeUncertain(
                "uncertain",
                "the native gateway gave no answer in time; this adapter will not "
                "repeat the call")
        if "error" in frame and frame["error"]:
            error = frame["error"] or {}
            message = str(error.get("message") or "the native gateway refused the call")
            code = str(error.get("code") or "native_error")
            raise NativeError(code, message)
        result = frame.get("result")
        return result if isinstance(result, dict) else {}

    def status(self) -> dict:
        proc = self._proc
        return {
            "transport": self.name,
            "connected": self.alive,
            "pid": proc.pid if proc else 0,
            "detail": self._error,
            "generation": self.generation,
            "started_at": self.started_at,
            "gateway": dict(self._ready),
        }


# ---------------------------------------------------------------------------
# Hermes state, read-only
# ---------------------------------------------------------------------------

class StateReader:
    """Metadata and transcripts out of Hermes's own ``state.db``, read-only.

    Opened with ``mode=ro`` on a file: URI, so this process cannot create the
    database, cannot write a row and cannot take a write lock that another
    Hermes surface is waiting on. Nothing outside ``sessions``, ``messages`` and
    ``gateway_routing`` is read, and no ``request_dump`` file is ever opened.
    """

    SESSION_COLUMNS = (
        "id", "source", "title", "started_at", "ended_at", "end_reason",
        "message_count", "cwd", "session_key", "parent_session_id",
        "profile_name", "archived",
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.error = ""
        try:
            self._connect().execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
        except Exception as exc:
            self.error = f"Hermes state.db could not be read ({exc})"

    @property
    def available(self) -> bool:
        return not self.error

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        if self.error:
            return []
        try:
            return self._connect().execute(sql, args).fetchall()
        except Exception as exc:
            self.error = f"Hermes state.db could not be read ({exc})"
            return []

    # -- sessions ----------------------------------------------------------
    def catalog_rows(self, limit: int, include_derived: bool) -> list[dict]:
        # Native session.list computes previews across message history. On a
        # large installation that can block the gateway for tens of seconds.
        # Navigation needs metadata only; read no message or prompt blobs here.
        hidden = sorted(DERIVED_SOURCES | AUTOMATION_SOURCES)
        where = "" if include_derived else (
            " WHERE lower(COALESCE(source,'')) NOT IN (" +
            ",".join("?" for _ in hidden) + ")")
        args = (() if include_derived else tuple(hidden)) + (max(1, limit),)
        return [dict(row) for row in self._query(
            "SELECT " + ", ".join(self.SESSION_COLUMNS) + " FROM sessions" +
            where + " ORDER BY COALESCE(ended_at,started_at) DESC, id DESC LIMIT ?",
            args)]

    def session_meta(self, ids: list[str]) -> dict[str, dict]:
        """Metadata for the exact ids asked about. Never message content."""
        out: dict[str, dict] = {}
        columns = ", ".join(self.SESSION_COLUMNS)
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            for row in self._query(
                    f"SELECT {columns} FROM sessions WHERE id IN ({marks})", tuple(chunk)):
                out[str(row["id"])] = {key: row[key] for key in self.SESSION_COLUMNS}
        return out

    def routed_ids(self) -> set[str]:
        """Every session the running messaging gateway has a routing entry for.

        Both the routing key and the ``session_id`` inside the stored entry are
        collected, because a routed conversation is identified by either one.
        """
        found: set[str] = set()
        for row in self._query("SELECT session_key, entry_json FROM gateway_routing"):
            key = str(row["session_key"] or "")
            if key:
                found.add(key)
            try:
                entry = json.loads(row["entry_json"] or "{}")
            except ValueError:
                continue
            sid = str((entry or {}).get("session_id") or "")
            if sid:
                found.add(sid)
        return found

    # -- transcripts -------------------------------------------------------
    def message_count(self, session_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND active = 1",
            (session_id,))
        return int(rows[0]["n"]) if rows else 0

    def messages(self, session_id: str, *, limit: int, before_id: int = 0,
                 ascending: bool = True) -> list[dict]:
        """One bounded page of a stored transcript, newest-anchored.

        ``before_id`` is an exact ``messages.id`` this adapter previously
        issued, so a cursor can never be turned into a row the database did not
        report.
        """
        args: list = [session_id]
        clause = "session_id = ? AND active = 1"
        if before_id:
            clause += " AND id < ?"
            args.append(int(before_id))
        args.append(int(limit))
        rows = self._query(
            "SELECT id, role, content, tool_name, tool_calls, tool_call_id, timestamp,"
            " reasoning, reasoning_content, finish_reason"
            f" FROM messages WHERE {clause} ORDER BY id DESC LIMIT ?", tuple(args))
        out = [dict(row) for row in rows]
        return list(reversed(out)) if ascending else out

    def search(self, session_id: str, needle: str, limit: int) -> tuple[list[dict], bool]:
        """Scan a bounded window of one conversation. Never the whole database."""
        rows = self._query(
            "SELECT id, role, content, tool_name, timestamp FROM messages"
            " WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT ?",
            (session_id, SEARCH_SCAN_CAP))
        hits = []
        folded = needle.casefold()
        for row in rows:
            text = _content_text(row["content"])
            if folded and folded not in text.casefold():
                continue
            hits.append(dict(row))
            if len(hits) >= limit:
                break
        return hits, len(rows) >= SEARCH_SCAN_CAP


def _content_text(raw) -> str:
    """Hermes stores content as text or as a JSON block list. Read both."""
    if raw is None:
        return ""
    if isinstance(raw, (int, float)):
        return str(raw)
    text = str(raw)
    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return text
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            parts = []
            for block in parsed:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            if parts:
                return "\n".join(parts)
    return text


# ---------------------------------------------------------------------------
# Session Vault projects
# ---------------------------------------------------------------------------

def read_projects(registry_path: Path,
                  runtime: str = RUNTIME) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Every registry project, and every *verified* origin for one runtime.

    A conversation is linked to a project only when that project's own
    ``sessions/*.origins.json`` carries an origin whose ``runtime`` is exactly
    the runtime asked for and which names the exact persisted session id. A
    ``codex`` or ``openclaw`` origin is never read as a Hermes one, and nothing
    is matched on a title. Nothing is written and no project is created.
    """

    projects: dict[str, dict] = {}
    links: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"the registry could not be read ({exc}); every conversation "
                        "will show as Unfiled")
        return projects, links, warnings
    for entry in registry.get("projects") or []:
        project_id = str(entry.get("id") or "")
        if not PROJECT_ID_RE.match(project_id):
            continue
        projects[project_id] = {
            "id": project_id,
            "name": str(entry.get("name") or project_id),
            "root": str(entry.get("root") or ""),
        }
    for project_id, project in projects.items():
        root = Path(project["root"]).expanduser() if project["root"] else None
        if root is None or not (root / "sessions").is_dir():
            continue
        for sidecar in sorted((root / "sessions").glob("*.origins.json")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                warnings.append(f"{sidecar.name} in {project_id} could not be read")
                continue
            session = str(data.get("session") or sidecar.name.split(".")[0])
            for origin in data.get("origins") or []:
                if str(origin.get("runtime") or "") != runtime:
                    continue
                key = str(origin.get("session_id") or origin.get("session_key") or "")
                if not key:
                    continue
                if key in links and links[key]["project_id"] != project_id:
                    warnings.append(
                        f"{key} is claimed by both {links[key]['project_id']} and "
                        f"{project_id}; showing it under {links[key]['project_id']}")
                    continue
                links[key] = {
                    "project_id": project_id,
                    "session": session,
                    "record_path": str(sidecar),
                    "node": str(origin.get("node") or ""),
                    "cwd": str(origin.get("cwd") or ""),
                }
    return projects, links, warnings


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------

def session_slug(session_id: str, title: str = "") -> str:
    """A stable, collision-free room name for one exact persisted session id.

    The readable part comes from the title when there is one; the eight hex
    characters are a digest of the exact id, so the slug is stable across
    restarts and two conversations can never share one. The slug is
    presentation only — every lookup goes back through the catalog map.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    core = SLUG_STRIP_RE.sub("-", (title or "").strip()).strip("-._").lower()[:60]
    if core and not core[0].isalnum():
        core = core[1:]
    return f"{core}-{digest}" if core else f"s-{digest}"


class Room:
    """One persisted native Hermes conversation, as UX46 addresses it."""

    def __init__(self, *, project_id: str, project_name: str, project_root: str,
                 session: str, session_id: str, row: dict, meta: dict,
                 claim: str, node: str, record: dict | None, unfiled: bool):
        self.project_id = project_id
        self.project_name = project_name
        self.project_root = project_root
        self.session = session
        # The exact persisted Hermes session id. This is the room's identity
        # everywhere; the per-connection native session id is separate and
        # never used to address a room.
        self.session_id = session_id
        self.row = row
        self.meta = meta
        self.claim = claim
        self.node = node
        self.record = record
        self.unfiled = unfiled

    @property
    def id(self) -> str:
        return f"{self.project_id}/{self.session}"

    @property
    def source(self) -> str:
        return str(self.row.get("source") or self.meta.get("source") or "")

    @property
    def attachable(self) -> bool:
        return self.claim in ATTACHABLE_CLAIMS

    @property
    def updated_ts(self) -> float:
        for value in (self.meta.get("ended_at"), self.row.get("started_at"),
                      self.meta.get("started_at")):
            try:
                if value:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @property
    def last_active(self) -> str:
        if not self.updated_ts:
            return ""
        try:
            return time.strftime("%Y-%m-%d", time.localtime(self.updated_ts))
        except (OverflowError, OSError, ValueError):
            return ""

    @property
    def title(self) -> str:
        if self.record and self.record.get("title"):
            return str(self.record["title"])
        for value in (self.row.get("title"), self.meta.get("title"), self.row.get("preview")):
            text = str(value or "").strip()
            if text:
                return " ".join(text.split())[:80]
        return self.session_id

    @property
    def native_role(self) -> str:
        if self.source in AUTOMATION_SOURCES:
            return "automation"
        if self.source in DERIVED_SOURCES:
            return "derived"
        return "human"

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "session": self.session,
            "title": self.title,
            # Not a lifecycle claim: this adapter has no Vault record for a
            # conversation unless one carries a verified hermes origin.
            "status": "unrecorded",
            "status_source": "none",
            "updated": self.last_active,
            "capability": CAPABILITY,
            "capability_label": (CAPABILITY_LABEL if self.attachable
                                 else CAPABILITY_READONLY_LABEL),
            "capability_short": (CAPABILITY_SHORT if self.attachable
                                 else CAPABILITY_READONLY_SHORT),
            "controllable": self.attachable,
            "runtime": RUNTIME,
            "node": self.node,
            "origin_count": 1 if self.record else 0,
            "origins_status": "linked" if self.record else "unlinked",
            "worker": False,
            "worker_provenance": "",
            "identity_key": f"hermes@{self.node}:{self.session_id}",
            "conversation_key": f"{self.project_id}/hermes@{self.node}:{self.session_id}",
            "last_active": self.last_active,
            "recency_source": "state.db" if self.updated_ts else "none",
            "native_role": self.native_role,
            "native_provenance": self.source,
            "native_archived": bool(self.meta.get("archived")),
            "record_aliases": [],
            "attention": {"state": "none", "source": "adapter",
                          "basis": "this adapter reads no checkpoint"},
            "checkpoint": None,
            # The exact persisted id, said plainly, with where it came from.
            "session_key": self.session_id,
            "session_id": self.session_id,
            "session_key_source": "hermes.state.db" if self.meta else "hermes.session.active_list",
            "thread_id": self.session_id,
            "ownership_claim": self.claim,
            "ownership_basis": CLAIM_BASIS.get(self.claim, ""),
            "record_linked": bool(self.record),
            "record_path": str((self.record or {}).get("record_path") or ""),
            "unfiled": self.unfiled,
            "vault_project": not self.unfiled,
            "project_source": "vault_origin" if self.record else "unfiled",
        }


class Catalog:
    """The rooms, rebuilt from read-only metadata plus our own live sessions.

    Metadata browsing excludes automation, sub-agent and tool runs by default. Where a conversation *lives* is decided separately and
    conservatively: a real Session Vault project only when a record there
    carries a verified ``hermes`` origin for its exact id, and the internal
    Unfiled namespace otherwise. No project is ever created.
    """

    def __init__(self, gateway: NativeGateway, state: StateReader, *,
                 registry: Path, unfiled_project: str, node: str,
                 limit: int, include_derived: bool, adapter_source: str):
        self.gateway = gateway
        self.state = state
        self.registry = Path(registry).expanduser()
        self.unfiled_project = unfiled_project
        self.node = node
        self.limit = limit
        self.include_derived = include_derived
        self.adapter_source = adapter_source
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {}
        self._by_session: dict[str, Room] = {}
        self._read_at = 0.0
        self.error = ""
        self.vault_warnings: list[str] = []
        # ephemeral native session id -> persisted session id, for rooms this
        # adapter has open right now.
        self.live: dict[str, str] = {}

    # -- vault -------------------------------------------------------------
    def _placement(self, session_id: str, title: str,
                   projects: dict, links: dict) -> tuple[dict, dict | None, str]:
        record = links.get(session_id)
        if record and record["project_id"] in projects:
            return projects[record["project_id"]], record, record["session"]
        return ({"id": self.unfiled_project, "name": "Unfiled", "root": ""},
                None, session_slug(session_id, title))

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._read_at < CATALOG_TTL and self._rooms:
                return
        projects, links, warnings = read_projects(self.registry)
        if self.unfiled_project in projects:
            # Refusing here rather than mixing unfiled conversations into
            # somebody's real project.
            raise SystemExit(
                f"{ADAPTER_NAME}: --unfiled-project {self.unfiled_project!r} is also a "
                "real Session Vault project; choose another name")

        error = ""
        rows = self.state.catalog_rows(self.limit, self.include_derived)
        if not self.state.available:
            error = self.state.error
            # Keep previously visible rooms addressable, but _claim below
            # removes write access when their metadata cannot be verified.
            with self._lock:
                rows = [dict(room.row) for room in self._rooms.values()]

        live_rows: list[dict] = []
        live_map: dict[str, str] = {}
        try:
            active = self.gateway.call("session.active_list", {}, timeout=20.0)
            for row in active.get("sessions") or []:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("session_key") or "")
                sid = str(row.get("id") or "")
                if key and sid:
                    live_map[sid] = key
                    live_rows.append(row)
        except NativeError as exc:
            if not error:
                error = f"{exc.code}: {exc.message}"

        # A conversation this adapter opened seconds ago has no state.db row
        # yet (Hermes writes it lazily on the first prompt), so it would be
        # invisible in session.list. Merge our own live sessions in by their
        # persisted key so a fresh /new is immediately addressable.
        known = {str(row.get("id") or "") for row in rows}
        for row in live_rows:
            key = str(row.get("session_key") or "")
            if key and key not in known:
                rows.append({"id": key, "title": row.get("title") or "",
                             "preview": row.get("preview") or "",
                             "started_at": row.get("started_at") or 0,
                             "message_count": row.get("message_count") or 0,
                             "source": self.adapter_source})
                known.add(key)

        if not self.include_derived:
            rows = [row for row in rows
                    if str(row.get("source") or "").strip().lower()
                    not in DERIVED_SOURCES | AUTOMATION_SOURCES]

        ids = [str(row["id"]) for row in rows]
        meta = self.state.session_meta(ids)
        routed = self.state.routed_ids()
        live_keys = set(live_map.values())

        rooms: dict[str, Room] = {}
        by_session: dict[str, Room] = {}
        for row in rows:
            session_id = str(row["id"])
            info = meta.get(session_id, {})
            claim = self._claim(session_id, row, info, routed, live_keys)
            if claim == CLAIM_DERIVED and not self.include_derived:
                continue
            project, record, name = self._placement(
                session_id, str(row.get("title") or row.get("preview") or ""),
                projects, links)
            room = Room(
                project_id=project["id"], project_name=project["name"],
                project_root=project.get("root", ""), session=name,
                session_id=session_id, row=row, meta=info, claim=claim,
                node=self.node, record=record,
                unfiled=project["id"] == self.unfiled_project)
            if room.id in rooms:
                # Two records naming one project/session name. Keep the first
                # reading and give the second its digest name rather than
                # silently replacing somebody's conversation.
                room.session = session_slug(session_id, room.title)
            rooms[room.id] = room
            by_session[session_id] = room

        with self._lock:
            self._rooms = rooms
            self._by_session = by_session
            self._read_at = time.time()
            self.error = error
            self.live = live_map
            self.vault_warnings = warnings

    def _claim(self, session_id: str, row: dict, meta: dict,
               routed: set[str], live_keys: set[str]) -> str:
        source = str(row.get("source") or meta.get("source") or "").strip().lower()
        if source in DERIVED_SOURCES or meta.get("parent_session_id") and source in DERIVED_SOURCES:
            return CLAIM_DERIVED
        if session_id in live_keys:
            return CLAIM_LIVE
        if session_id in routed or str(meta.get("session_key") or "") in routed:
            return CLAIM_ROUTED
        if source in ROUTED_SOURCES:
            return CLAIM_ROUTED
        if source == self.adapter_source:
            return CLAIM_ADAPTER
        if not self.state.available or not meta:
            # No metadata means no evidence. Unknown is the safe answer.
            return CLAIM_UNVERIFIED
        if meta.get("ended_at") in (None, ""):
            return CLAIM_OPEN
        return CLAIM_CLOSED

    # -- reads -------------------------------------------------------------
    def rooms(self) -> list[Room]:
        with self._lock:
            values = list(self._rooms.values())
        return sorted(values, key=lambda room: room.updated_ts, reverse=True)

    def room(self, room_id: str) -> Room | None:
        with self._lock:
            return self._rooms.get(room_id)

    def room_for_session(self, session_id: str) -> Room | None:
        with self._lock:
            return self._by_session.get(session_id)

    def live_session_for(self, session_id: str) -> str:
        with self._lock:
            for ephemeral, key in self.live.items():
                if key == session_id:
                    return ephemeral
        return ""

    def by_project(self) -> list[tuple[dict, list[Room]]]:
        groups: dict[str, list[Room]] = {}
        names: dict[str, dict] = {}
        for room in self.rooms():
            groups.setdefault(room.project_id, []).append(room)
            names.setdefault(room.project_id, {
                "id": room.project_id, "name": room.project_name,
                "root": room.project_root})
        ordered = sorted(
            groups.items(),
            key=lambda item: (item[0] == self.unfiled_project,
                              -max((room.updated_ts for room in item[1]), default=0.0)))
        return [(names[pid], members) for pid, members in ordered]


# ---------------------------------------------------------------------------
# transcript projection
# ---------------------------------------------------------------------------

def _tool_arguments(raw) -> dict:
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return {}
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict):
            fn = first.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            return {"name": str(fn.get("name") or ""), "arguments": args}
    return {}


def project_messages(rows: list[dict], session_id: str) -> list[dict]:
    """One console item per stored message row.

    Item ids are the exact ``messages.id`` the database reported, prefixed so
    they cannot collide with a live-tail id. Nothing is renumbered, and a row
    that carried reasoning gets its own reasoning item rather than being folded
    into the answer.
    """
    items: list[dict] = []
    pending_calls: dict[str, str] = {}
    for row in rows:
        row_id = int(row.get("id") or 0)
        role = str(row.get("role") or "")
        text = _content_text(row.get("content"))
        stamp = float(row.get("timestamp") or 0)
        base = {
            "seq": row_id,
            "message_id": f"hermes-{session_id}-{row_id}",
            "at": stamp,
            "created_at": stamp,
            "role": role,
            "source": "state.db",
        }
        if role == "assistant":
            reasoning = _content_text(row.get("reasoning")) or _content_text(
                row.get("reasoning_content"))
            if reasoning.strip():
                items.append({**base, "id": f"h{row_id}r", "type": "reasoning",
                              "text": reasoning})
            call = _tool_arguments(row.get("tool_calls"))
            if call.get("name"):
                pending_calls[str(row.get("tool_call_id") or "")] = call["name"]
                items.append({**base, "id": f"h{row_id}c", "type": "mcpToolCall",
                              "name": call["name"], "arguments": call.get("arguments") or {},
                              "text": call["name"]})
            if text.strip():
                items.append({**base, "id": f"h{row_id}", "type": "agentMessage",
                              "text": text, "phase": "final_answer"})
            continue
        if role == "user":
            if not text.strip():
                continue
            items.append({**base, "id": f"h{row_id}", "type": "userMessage", "text": text})
            continue
        if role == "tool":
            name = str(row.get("tool_name") or "") or pending_calls.get(
                str(row.get("tool_call_id") or ""), "tool")
            items.append({**base, "id": f"h{row_id}", "type": "functionCallOutput",
                          "name": name, "text": text})
            continue
        if role == "system" and text.strip():
            items.append({**base, "id": f"h{row_id}", "type": "unknown", "text": text})
    _mark_interim(items)
    return items


def _mark_interim(items: list[dict]) -> None:
    """Only the last assistant message of a turn is that turn's answer.

    Hermes does not label phases in a stored transcript, so this is derived
    from the transcript's own shape — an assistant message with more assistant
    work after it, before the next person's message, was not the final answer.
    """
    seen_final = False
    for item in reversed(items):
        if item["type"] == "userMessage":
            seen_final = False
            continue
        if item["type"] != "agentMessage":
            continue
        if seen_final:
            item["phase"] = "interim"
        else:
            seen_final = True


class LiveTail:
    """What a turn is saying right now, before Hermes has stored it.

    The stored transcript is the truth; this is the gap between a token
    arriving and the row being written. Cleared the moment the runtime says the
    message is complete, so nothing here is ever shown twice.
    """

    def __init__(self, room_id: str):
        self.room_id = room_id
        self.user = ""
        self.user_at = 0.0
        self.assistant = ""
        self.reasoning = ""
        self.tools: list[dict] = []
        self.status = ""
        self.running = False
        self.turn_id = ""
        self.started_at = 0.0
        self.last_event = 0.0

    def clear_turn(self) -> None:
        self.assistant = ""
        self.reasoning = ""
        self.tools = []
        self.status = ""
        self.running = False
        self.turn_id = ""

    def items(self, stored: list[dict]) -> list[dict]:
        """Provisional items to append after the stored transcript.

        A user message is dropped from here the instant the same text shows up
        in the stored transcript, so a person's own send appears exactly once.
        """
        out: list[dict] = []
        if self.user:
            last_user = next((item for item in reversed(stored)
                              if item["type"] == "userMessage"), None)
            if not (last_user and last_user.get("text", "").strip() == self.user.strip()):
                out.append({"id": f"live-{self.room_id}-user", "type": "userMessage",
                            "text": self.user, "at": self.user_at,
                            "created_at": self.user_at, "role": "user",
                            "pending": True, "source": "live"})
        for index, tool in enumerate(self.tools):
            out.append({"id": f"live-{self.room_id}-tool-{index}",
                        "type": "mcpToolCall" if not tool.get("done") else "functionCallOutput",
                        "name": tool.get("name", "tool"),
                        "text": tool.get("text", tool.get("name", "")),
                        "at": tool.get("at", 0.0), "created_at": tool.get("at", 0.0),
                        "role": "assistant", "pending": not tool.get("done"),
                        "source": "live"})
        if self.reasoning.strip():
            out.append({"id": f"live-{self.room_id}-reasoning", "type": "reasoning",
                        "text": self.reasoning, "at": self.started_at,
                        "created_at": self.started_at, "role": "assistant",
                        "pending": True, "source": "live"})
        if self.assistant.strip():
            out.append({"id": f"live-{self.room_id}-assistant", "type": "agentMessage",
                        "text": self.assistant, "phase": "final_answer",
                        "at": self.started_at, "created_at": self.started_at,
                        "role": "assistant", "pending": True, "source": "live"})
        return out


def _item_text(item: dict) -> str:
    return str(item.get("text") or "")


def _search_kind(item: dict) -> str:
    if item["type"] == "userMessage":
        return "human"
    if item["type"] == "agentMessage":
        return "final" if item.get("phase") == "final_answer" else "interim"
    if item["type"] in ("mcpToolCall", "functionCallOutput"):
        return "tool"
    return item["type"]


def _snippet(text: str, needle: str, width: int = 160) -> str:
    if not needle:
        return " ".join(text.split())[:width]
    lowered = text.casefold()
    at = lowered.find(needle.casefold())
    if at < 0:
        return " ".join(text.split())[:width]
    start = max(0, at - width // 3)
    return " ".join(text[start:start + width].split())


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

class Journal(agent3.Journal):
    """The Agent3 journal, saying what is true for this runtime.

    Only the reported wording changes; the durability rules — reserved before
    the runtime is called, settled after, never replayed on its own — are the
    ones already proven there.
    """

    @staticmethod
    def _submission_json(row) -> dict:
        data = agent3.Journal._submission_json(row)
        data["mode"] = "native"
        data["runtime"] = RUNTIME
        data["meaning"] = STATUS_MEANING.get(row["status"], "")
        return data


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class HermesService:
    def __init__(self, config: argparse.Namespace, gateway: NativeGateway):
        self.config = config
        self.gateway = gateway
        self.csrf_token = secrets.token_urlsafe(32)
        self.started_at = time.time()
        self.state_dir = Path(config.state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.journal = Journal(self.state_dir / "journal.db")
        self.files = files.FileStore(self.state_dir / "attachments")
        self.events = Events()
        self.state = StateReader(Path(config.hermes_home).expanduser() / "state.db")
        self.catalog = Catalog(
            gateway, self.state,
            registry=Path(config.registry).expanduser(),
            unfiled_project=config.unfiled_project,
            node=config.node, limit=int(config.session_limit),
            include_derived=bool(config.include_derived),
            adapter_source=config.source)
        self._tails: dict[str, LiveTail] = {}
        self._approvals: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._queue_stop = threading.Event()
        self._queue_thread: threading.Thread | None = None
        self.identity = self._read_identity()
        self.native_commands: dict = {}
        # The names this port answers under. The socket is loopback-only; a
        # request that arrives under some other name is refused, not answered.
        self.allowed_hosts = {"127.0.0.1", "localhost", "::1"} | {
            str(name).strip().casefold()
            for name in (getattr(config, "allow_host", None) or [])}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        # Our native gateway is brand new, so it holds no session. Any room
        # this adapter remembered following before a restart is gone with the
        # old child; forgetting them is the honest thing to do.
        for room_id in list(self.journal.attached()):
            self.journal.detach(room_id)
        self.catalog.refresh(force=True)
        try:
            self.native_commands = self.gateway.call("commands.catalog", {}, timeout=25.0)
        except NativeError as exc:
            self.native_commands = {"warning": f"{exc.code}: {exc.message}"}
        self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True)
        self._queue_thread.start()

    def stop(self) -> None:
        self._queue_stop.set()
        if self._queue_thread:
            self._queue_thread.join(timeout=3)
        # Close only sessions this adapter opened, one by one, by their exact
        # native ids. Nothing else on this machine is signalled.
        for ephemeral in list(self.catalog.live):
            try:
                self.gateway.call("session.close", {"session_id": ephemeral}, timeout=10.0)
            except NativeError:
                pass

    def _read_identity(self) -> dict:
        """Name the persona from Hermes's own SOUL.md, and say so.

        SOUL.md is the persona file, not a credential. It is read bounded and
        only to confirm the configured name is the one this Hermes actually
        declares — never to discover a secret, and never dumped anywhere.
        """
        soul = Path(self.config.hermes_home).expanduser() / "SOUL.md"
        declared = ""
        confirmed = False
        try:
            head = soul.read_text(encoding="utf-8", errors="replace")[:4096]
            confirmed = bool(re.search(rf"\b{re.escape(self.config.agent_name)}\b", head))
            match = re.search(r"You are ([A-Z][A-Za-z0-9._-]{0,40})", head)
            declared = match.group(1) if match else ""
        except OSError as exc:
            return {"name": self.config.agent_name, "declared": "", "confirmed": False,
                    "source": str(soul), "error": str(exc)}
        return {"name": self.config.agent_name, "declared": declared,
                "confirmed": confirmed, "source": str(soul), "error": ""}

    # -- rooms -------------------------------------------------------------
    def require_room(self, room_id: str) -> Room:
        if not ROOM_ID_RE.match(room_id or ""):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_room",
                               "a room is named <project>/<session>")
        self.catalog.refresh()
        room = self.catalog.room(room_id)
        if room is None:
            self.catalog.refresh(force=True)
            room = self.catalog.room(room_id)
        if room is None:
            raise AdapterError(HTTPStatus.NOT_FOUND, "unknown_room",
                               "this adapter has no such conversation; it never guesses "
                               "a session id from a room name")
        return room

    def require_owned(self, room: Room) -> str:
        """The exact native session id this adapter holds for a room."""
        ephemeral = self.catalog.live_session_for(room.session_id)
        if not ephemeral:
            raise AdapterError(
                HTTPStatus.CONFLICT, "not_attached",
                "this adapter has no native session open for that conversation; press "
                "Continue here first")
        return ephemeral

    def _tail(self, room_id: str) -> LiveTail:
        with self._lock:
            tail = self._tails.get(room_id)
            if tail is None:
                tail = LiveTail(room_id)
                self._tails[room_id] = tail
            return tail

    def _live_row(self, ephemeral: str) -> dict:
        """What our own gateway says about one session it holds, right now."""
        try:
            active = self.gateway.call("session.active_list", {}, timeout=15.0)
        except NativeError:
            return {}
        for row in active.get("sessions") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == ephemeral:
                return row
        return {}

    def room_state(self, room: Room) -> dict:
        payload = room.as_json()
        ephemeral = self.catalog.live_session_for(room.session_id)
        attached = bool(ephemeral)
        live = self._live_row(ephemeral) if attached else {}
        native_status = str(live.get("status") or "")
        running = native_status == "working"
        tail = self._tail(room.id)

        if attached:
            state = "atlas_owned"
            detail = ("this adapter holds a native Hermes session for this conversation; "
                      "releasing it closes only that session")
        elif room.claim == CLAIM_ROUTED:
            state = "held_elsewhere"
            detail = ("the running Hermes messaging gateway routes this conversation; "
                      "this adapter will not resume it and can only read it")
        elif room.claim in (CLAIM_OPEN, CLAIM_UNVERIFIED):
            state = "unknown"
            detail = (CLAIM_BASIS[room.claim] + "; this adapter will not resume a "
                      "conversation it cannot prove is free, so this view is read-only")
        else:
            state = "idle"
            detail = ("nothing holds this conversation; Continue here opens a native "
                      "Hermes session for it")

        payload["ownership"] = {
            "state": state,
            "atlas_owned": attached,
            "detected": self.state.available,
            "scope": "native_session",
            # A native session opened here is genuinely ours to write to and to
            # close, but Hermes has no cross-process writer lock, so no claim of
            # exclusivity is made on the runtime's behalf.
            "exclusive": False,
            "adapter_state": "attached" if attached else "detached",
            "claim": room.claim,
            "basis": CLAIM_BASIS.get(room.claim, ""),
            "detail": detail,
            "also_open_as": [],
        }
        payload["controllable"] = room.attachable
        payload["submissions"] = self.journal.recent(room.id, limit=10)
        payload["pending"] = self.journal.queue_list(room.id)
        payload["draft"] = self.journal.draft(room.id)
        payload["approvals"] = self.approvals_for(room)
        payload["native"] = {
            # The console addresses a conversation by native.thread_id. For this
            # runtime that is the *persisted* Hermes session id, which is stable
            # across attach and release; the per-connection native session id is
            # reported separately and is never used to address a room.
            "thread_id": room.session_id,
            "session_key": room.session_id,
            "persisted_session_id": room.session_id,
            "native_session_id": ephemeral,
            "runtime": RUNTIME,
            "source": room.source,
            "cwd": str(room.meta.get("cwd") or ""),
            "profile": str(room.meta.get("profile_name") or ""),
            "model": str(live.get("model") or ""),
            "message_count": int(room.meta.get("message_count")
                                 or room.row.get("message_count") or 0),
            "status": native_status or ("unconnected" if not attached else "idle"),
            "status_source": ("hermes.session.active_list" if attached
                              else "not connected here"),
            "ended_at": room.meta.get("ended_at"),
            "end_reason": str(room.meta.get("end_reason") or ""),
            # A turn id only when our own gateway reports the turn working.
            # Never derived from an unfinished goal or a running process.
            "active_turn": tail.turn_id if running else "",
            "active_run": running,
            "active_run_ids": [tail.turn_id] if (running and tail.turn_id) else [],
            "waiting_on_person": native_status == "waiting",
            "started_at": room.row.get("started_at") or room.meta.get("started_at"),
        }
        payload["history_source"] = "hermes state.db, opened read-only"
        payload["capabilities"] = capabilities(
            stream=self.gateway.alive, attachments=attached)
        payload["unsupported"] = dict(UNSUPPORTED_ACTIONS)
        payload["commands"] = self.command_metadata()["help"]
        payload["capabilities"]["commands_supported"] = self.supported_commands()
        payload["capabilities"]["command_help"] = payload["commands"]
        if not room.attachable:
            payload["capability_short"] = CAPABILITY_READONLY_SHORT
        return payload

    def workspace(self) -> dict:
        attached = self.journal.attached()
        projects = []
        for project, members in self.catalog.by_project():
            unfiled = project["id"] == self.config.unfiled_project
            suggested = []
            for index, room in enumerate(members[: max(0, int(self.config.suggest))]):
                entry = room.as_json()
                open_now = room.id in attached
                entry.update({
                    "pinned": False, "hidden": False, "aliases": [], "alias_count": 0,
                    "group_role": room.native_role,
                    "reason": "open" if open_now else
                              ("recent" if index == 0 else "also_recent"),
                    "reason_label": "open right now" if open_now else
                                    ("most recent conversation here" if index == 0
                                     else "recently active here"),
                })
                suggested.append(entry)
            projects.append({
                "id": project["id"],
                "name": project["name"],
                "root": project.get("root", ""),
                "last_active": members[0].last_active if members else "",
                "recency_source": "state.db" if members else "none",
                "pinned": [],
                "suggested": suggested,
                "more_total": max(0, len(members) - len(suggested)),
                "agent_work_total": sum(1 for room in members
                                        if room.native_role != "human"),
                "hidden_total": 0,
                "record_total": len(members),
                "conversation_total": len(members),
                "unfiled": unfiled,
                "vault_project": not unfiled,
                "note": ("native Hermes conversations with no verified Vault origin; no "
                         "project was created for them" if unfiled else ""),
            })
        return {
            "projects": projects,
            "suggest_limit": int(self.config.suggest),
            "native_metadata": self.state.available,
            "prefs": {},
            "gateway_error": self.catalog.error,
            "vault_warnings": self.catalog.vault_warnings[:5],
            "state_warning": self.state.error,
        }

    def project_rows(self) -> list[dict]:
        rows = []
        for project, members in self.catalog.by_project():
            unfiled = project["id"] == self.config.unfiled_project
            rows.append({
                "id": project["id"], "name": project["name"],
                "root": project.get("root", ""),
                "sessions": len(members),
                "controllable": sum(1 for room in members if room.attachable),
                "needs_person": 0,
                "updated": members[0].last_active if members else "",
                "unfiled": unfiled, "vault_project": not unfiled,
            })
        return rows

    def search_rooms(self, query: str, limit: int, offset: int) -> dict:
        rooms = self.catalog.rooms()
        needle = (query or "").strip().casefold()
        if needle:
            rooms = [room for room in rooms
                     if needle in " ".join(
                         [room.id, room.title, room.session_id, room.source]).casefold()]
        size = max(1, min(int(limit), 200))
        window = rooms[offset:offset + size]
        return {
            "total": len(rooms),
            "offset": offset,
            "returned": len(window),
            "grouped": False,
            "rooms": [room.as_json() for room in window],
            "truncated": offset + len(window) < len(rooms),
        }

    # -- history -----------------------------------------------------------
    def history(self, room: Room, limit: int, cursor: str, direction: str) -> dict:
        size = max(1, min(int(limit), HISTORY_PAGE_CAP))
        before = 0
        if cursor:
            try:
                before = int(cursor)
            except ValueError:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_cursor",
                                   "a history cursor is a message id this adapter issued")
        if not self.state.available:
            return {
                "room": room.id, "items": [], "complete": False,
                "unavailable": True, "retryable": True,
                "error": "state_unreadable",
                "message": self.state.error or "Hermes state.db could not be read",
                "source": "hermes state.db (read-only)",
            }
        rows = self.state.messages(room.session_id, limit=size, before_id=before)
        items = project_messages(rows, room.session_id)
        tail_items: list[dict] = []
        if not before:
            tail_items = self._tail(room.id).items(items)
        merged = items + tail_items
        if direction == "desc":
            merged = list(reversed(merged))
        next_cursor = str(rows[0]["id"]) if rows and len(rows) >= size else ""
        return {
            "room": room.id,
            "items": merged,
            "count": len(merged),
            "complete": not next_cursor,
            "cursor": next_cursor,
            "next_cursor": next_cursor,
            "direction": direction,
            "unavailable": False,
            "retryable": False,
            "source": "hermes state.db (read-only)",
            "live_items": len(tail_items),
            "total": int(room.meta.get("message_count") or 0),
        }

    def search_history(self, room: Room, query: str, kinds: tuple[str, ...],
                       limit: int) -> dict:
        if not self.state.available:
            return {"room": room.id, "query": query, "hits": [], "complete": False,
                    "unavailable": True, "retryable": True,
                    "message": self.state.error}
        rows, capped = self.state.search(room.session_id, query,
                                         max(1, min(int(limit), 200)) * 4)
        items = project_messages(sorted(rows, key=lambda r: int(r["id"])), room.session_id)
        hits = []
        for item in reversed(items):
            if kinds and _search_kind(item) not in kinds:
                continue
            text = _item_text(item)
            if query and query.casefold() not in text.casefold():
                continue
            hits.append({
                "id": item["id"], "kind": _search_kind(item), "type": item["type"],
                "at": item.get("at", 0.0), "snippet": _snippet(text, query),
                "role": item.get("role", ""),
            })
            if len(hits) >= limit:
                break
        return {
            "room": room.id, "query": query, "hits": hits, "count": len(hits),
            "complete": not capped, "scan_cap": SEARCH_SCAN_CAP,
            "unavailable": False, "retryable": False,
            "source": "hermes state.db (read-only)",
        }

    # -- attach / release --------------------------------------------------
    def attach(self, room: Room) -> dict:
        """Open a real native Hermes session for this conversation.

        Refused outright for a conversation this adapter cannot prove is free.
        Hermes's active-session lease is a capacity counter — it is never read
        as proof that nobody else is writing.
        """
        existing = self.catalog.live_session_for(room.session_id)
        if existing:
            return self._attach_result(room, existing, reused=True)
        if not room.attachable:
            raise AdapterError(
                HTTPStatus.CONFLICT, "owner_unverified",
                self._refusal_text(room),
                {"claim": room.claim, "basis": CLAIM_BASIS.get(room.claim, ""),
                 "readable": True, "new_conversation": "/new"})
        try:
            result = self.gateway.call(
                "session.resume",
                {"session_id": room.session_id, "source": self.config.source},
                timeout=float(self.config.attach_timeout))
        except NativeUncertain as exc:
            raise AdapterError(HTTPStatus.ACCEPTED, "uncertain", str(exc)) from exc
        except NativeError as exc:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, exc.code, exc.message) from exc
        ephemeral = str(result.get("session_id") or "")
        resumed = str(result.get("session_key") or result.get("resumed") or "")
        if not ephemeral:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, "no_session",
                               "the native runtime did not name a session it opened")
        if resumed and resumed != room.session_id:
            # Hermes follows a compression-continuation chain, so a resume can
            # legitimately land on a descendant. Say so rather than pretending
            # the id asked for is the id opened.
            self.events.publish({"type": "native", "room": room.id,
                                 "method": "session.rotated", "lifecycle": True,
                                 "detail": resumed})
        self.journal.attach(room.id, room.session_id)
        self.catalog.refresh(force=True)
        self.events.publish({"type": "ownership", "room": room.id})
        return self._attach_result(room, ephemeral, reused=False, result=result)

    def _refusal_text(self, room: Room) -> str:
        if room.claim == CLAIM_ROUTED:
            return ("that conversation is routed by the Hermes messaging gateway that is "
                    "already running, so somebody may be talking to Pane in it right now. "
                    "This adapter will not resume it. You can read it here, or start a new "
                    "conversation with /new.")
        if room.claim == CLAIM_DERIVED:
            return ("that is a sub-agent or tool run, not a conversation anyone types "
                    "into. It is readable here and nothing more.")
        return ("this adapter cannot prove that conversation is free — "
                + CLAIM_BASIS.get(room.claim, "no evidence either way")
                + ". Hermes's active-session lease is a capacity counter, not an "
                  "exclusive writer lock, so it is not treated as proof. You can read it "
                  "here, or start a new conversation with /new.")

    def _attach_result(self, room: Room, ephemeral: str, *, reused: bool,
                       result: dict | None = None) -> dict:
        payload = self.room_state(room)
        payload["attached"] = True
        payload["reused"] = reused
        payload["native_session_id"] = ephemeral
        payload["message"] = ("Connected to a native Hermes session for this conversation."
                              if not reused else
                              "Already connected to a native Hermes session here.")
        payload["opened_messages"] = int((result or {}).get("message_count") or 0)
        payload["runtime_untouched"] = True
        payload["note"] = ("this opened one native session in this adapter's own gateway; "
                           "no other Hermes process, session or lock was touched")
        return payload

    def release(self, room: Room) -> dict:
        ephemeral = self.catalog.live_session_for(room.session_id)
        if not ephemeral:
            self.journal.detach(room.id)
            return {"room": room.id, "released": False, "release_state": "not_attached",
                    "runtime_untouched": True, "native_work_continues": True,
                    "message": "This adapter had no native session open here."}
        unsettled = self.journal.unsettled(room.session_id)
        if unsettled:
            raise AdapterError(
                HTTPStatus.CONFLICT, "send_unsettled",
                "a send to this conversation has no recorded outcome yet; releasing now "
                "would detach over an unknown state",
                {"submissions": unsettled})
        live = self._live_row(ephemeral)
        # "starting" is agent construction, not a turn. Any submitted prompt
        # still awaiting a reply was already refused by unsettled above.
        # Hermes closes this exact session safely during construction.
        if str(live.get("status") or "") not in ("idle", "starting"):
            raise AdapterError(
                HTTPStatus.CONFLICT, "session_busy",
                "this session is working, waiting for input, or its idle state could "
                "not be confirmed. It was left open. Finish or stop its turn before "
                "releasing it.",
                {"native_status": live.get("status"), "interrupted": False,
                 "work_stopped": False})
        try:
            result = self.gateway.call("session.close", {"session_id": ephemeral},
                                       timeout=25.0)
        except NativeError as exc:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, exc.code, exc.message) from exc
        self.journal.detach(room.id)
        with self._lock:
            self._tails.pop(room.id, None)
            self._approvals.pop(room.id, None)
        self.catalog.refresh(force=True)
        self.events.publish({"type": "ownership", "room": room.id})
        return {
            "room": room.id,
            "released": bool(result.get("closed", True)),
            "release_state": "detached",
            "native_session_id": ephemeral,
            "closed_exactly": ephemeral,
            "runtime_untouched": True,
            "native_work_continues": True,
            "message": "Closed exactly the native session this adapter opened. Hermes "
                       "itself, its messaging gateway and every other session are "
                       "untouched.",
        }

    def stop_turn(self, room: Room) -> dict:
        ephemeral = self.require_owned(room)
        try:
            result = self.gateway.call("session.interrupt", {"session_id": ephemeral},
                                       timeout=25.0)
        except NativeError as exc:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, exc.code, exc.message) from exc
        tail = self._tail(room.id)
        tail.running = False
        self.events.publish({"type": "native", "room": room.id,
                             "method": "session.interrupt", "lifecycle": True,
                             "active_run": False})
        return {"room": room.id, "stopped": True,
                "native_status": str(result.get("status") or ""),
                "scope": "this turn in the session this adapter opened",
                "message": "Asked the native runtime to interrupt this turn. Background "
                           "processes it started are left alone."}

    # -- refresh -----------------------------------------------------------
    def refresh_connection(self) -> dict:
        """Re-check this adapter's own native gateway. Never Hermes config.

        Nothing here reloads env, MCP servers or ``config.yaml``; that is Hermes
        configuration and this adapter never edits or reloads it.
        """
        if self.gateway.alive:
            self.catalog.refresh(force=True)
            return {"refreshed": True, "restarted": False,
                    "scope": "this adapter's own native gateway connection",
                    "config_reloaded": False,
                    "gateway": self.gateway.status(),
                    "message": "The native gateway this adapter owns is up; the "
                               "conversation list was re-read.",
                    "attachments_dropped": []}
        dropped = sorted(self.journal.attached())
        for room_id in dropped:
            self.journal.detach(room_id)
        with self._lock:
            self._tails.clear()
            self._approvals.clear()
        self.gateway.generation += 1
        try:
            self.gateway.start()
        except NativeError as exc:
            raise AdapterError(HTTPStatus.SERVICE_UNAVAILABLE, "gateway_unreachable",
                               exc.message) from exc
        self.catalog.refresh(force=True)
        self.events.publish({"type": "runtime/exited", "global": True})
        return {
            "refreshed": True, "restarted": True,
            "scope": "this adapter's own native gateway connection",
            "config_reloaded": False,
            "gateway": self.gateway.status(),
            "attachments_dropped": dropped,
            "message": "This adapter's native gateway had exited and was started again. "
                       "Every session it held is gone with it, so those conversations are "
                       "no longer connected here. Nothing was resent.",
        }

    # -- approvals ---------------------------------------------------------
    def approvals_for(self, room: Room) -> list[dict]:
        with self._lock:
            record = self._approvals.get(room.id)
        return [record] if record else []

    def all_approvals(self) -> list[dict]:
        with self._lock:
            return [dict(record) for record in self._approvals.values()]

    def answer_approval(self, body: dict) -> dict:
        """Answer exactly the request that was surfaced, and only that one."""
        key = str(body.get("key") or "")
        decision = str(body.get("decision") or "")
        if not key:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_approval",
                               "name the exact request being answered")
        with self._lock:
            match = next((record for record in self._approvals.values()
                          if record.get("key") == key), None)
        if match is None:
            raise AdapterError(HTTPStatus.CONFLICT, "approval_unknown",
                               "that request is not the one this runtime is waiting on; "
                               "nothing was answered")
        room = self.catalog.room(match["room"])
        if room is None:
            raise AdapterError(HTTPStatus.CONFLICT, "unknown_room",
                               "that request's conversation is no longer listed")
        ephemeral = self.require_owned(room)
        if match["kind"] == "user_input":
            answers = body.get("answers")
            if not isinstance(answers, dict) or not answers:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_approval",
                                   "that request needs an answer, not a choice")
            text = "\n".join(str(value) for value in answers.values())
            method, params = "clarify.respond", {"session_id": ephemeral,
                                                 "request_id": match.get("request_id", ""),
                                                 "answer": text}
        else:
            choice = {"accept": "once", "decline": "deny"}.get(decision)
            if choice is None:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_approval",
                                   "answer a command request with accept or decline")
            # `all: false` on purpose: accepting answers this one request and
            # never widens the session's standing permissions.
            method, params = "approval.respond", {"session_id": ephemeral,
                                                  "choice": choice, "all": False}
        try:
            result = self.gateway.call(method, params, timeout=25.0)
        except NativeError as exc:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, exc.code, exc.message) from exc
        with self._lock:
            self._approvals.pop(match["room"], None)
        self.events.publish({"type": "approval", "room": match["room"]})
        return {"answered": True, "key": key, "decision": decision,
                "method": method, "native": result,
                "scope": "this one request", "permissions_changed": False}

    # -- native events -----------------------------------------------------
    def on_native_event(self, event: str, session_id: str, payload: dict) -> None:
        """Map one native event onto the console's refresh contract.

        The native event name is kept verbatim in ``method`` so nothing is
        renamed into a turn lifecycle the runtime did not report. ``lifecycle``
        is set only for events that really change whether a turn is in flight.
        """
        if event == "gateway.ready":
            self.events.publish({"type": "native", "method": event, "global": True})
            return
        persisted = self.catalog.live.get(session_id, "")
        room = self.catalog.room_for_session(persisted) if persisted else None
        if room is None:
            return
        tail = self._tail(room.id)
        now = time.time()
        publish = True
        extra: dict = {}

        if event == "message.start":
            tail.clear_turn()
            tail.running = True
            tail.started_at = now
            # A turn id this adapter minted for a turn it saw the runtime start.
            tail.turn_id = f"hermes-{session_id}-{int(now * 1000)}"
            extra = {"lifecycle": True, "active_run": True}
        elif event == "message.delta":
            tail.assistant = (tail.assistant + str(payload.get("text") or ""))[-LIVE_TAIL_CHARS:]
            # One refresh-triggering event per interval; the growing text itself
            # is served from the tail, so the console still animates.
            publish = (now - tail.last_event) >= DELTA_EVENT_INTERVAL
        elif event == "reasoning.delta":
            tail.reasoning = (tail.reasoning + str(payload.get("text") or ""))[-LIVE_TAIL_CHARS:]
            publish = (now - tail.last_event) >= DELTA_EVENT_INTERVAL
        elif event == "message.interim":
            tail.assistant = str(payload.get("text") or "")[-LIVE_TAIL_CHARS:]
        elif event == "message.complete":
            tail.clear_turn()
            tail.user = ""
            extra = {"lifecycle": True, "active_run": False,
                     "status": str(payload.get("status") or "")}
            if str(payload.get("status") or "") == "error":
                extra["terminal"] = True
        elif event in ("tool.start", "tool.generating"):
            tail.tools.append({"name": str(payload.get("name") or "tool"),
                               "text": str(payload.get("preview") or payload.get("name") or ""),
                               "at": now, "done": False})
            del tail.tools[:-12]
        elif event == "tool.complete":
            for tool in reversed(tail.tools):
                if not tool.get("done"):
                    tool["done"] = True
                    tool["text"] = str(payload.get("preview") or tool.get("text", ""))
                    break
        elif event == "status.update":
            tail.status = str(payload.get("text") or "")
            extra = {"status_kind": str(payload.get("kind") or "")}
        elif event == "error":
            tail.running = False
            extra = {"lifecycle": True, "active_run": False, "terminal": True,
                     "detail": str(payload.get("message") or "")}
        elif event in ("approval.request", "clarify.request", "sudo.request",
                       "secret.request"):
            self._record_approval(room, event, session_id, payload)
            self.events.publish({"type": "approval", "room": room.id})
            extra = {"lifecycle": True}
        elif event in ("sudo.expire", "secret.expire"):
            with self._lock:
                self._approvals.pop(room.id, None)
            self.events.publish({"type": "approval", "room": room.id})
        elif event == "session.info":
            extra = {"lifecycle": True}

        if publish:
            tail.last_event = now
            self.events.publish({"type": "native", "room": room.id, "method": event,
                                 "runtime": RUNTIME, **extra})

    def _record_approval(self, room: Room, event: str, session_id: str,
                         payload: dict) -> None:
        kind = {"approval.request": str(payload.get("kind") or "command"),
                "clarify.request": "user_input",
                "sudo.request": "permissions",
                "secret.request": "permissions"}[event]
        request_id = str(payload.get("request_id") or "")
        key = hashlib.sha256(
            f"{session_id}\x00{event}\x00{request_id}\x00{payload.get('command', '')}"
            .encode("utf-8")).hexdigest()[:24]
        params: dict = {}
        if "command" in payload:
            params["command"] = str(payload.get("command") or "")
        if payload.get("cwd"):
            params["cwd"] = str(payload.get("cwd"))
        if payload.get("reason"):
            params["reason"] = str(payload.get("reason"))
        if payload.get("paths"):
            params["paths"] = [str(p) for p in payload.get("paths") or []]
        if event == "clarify.request":
            params["questions"] = [{
                "id": request_id or "answer",
                "title": str(payload.get("question") or payload.get("prompt")
                             or "The runtime is asking you something"),
                "options": [str(option) for option in payload.get("options") or []],
            }]
        record = {
            "key": key, "kind": kind, "room": room.id,
            "request_id": request_id,
            "native_event": event,
            "thread_id": room.session_id,
            "turn_id": self._tail(room.id).turn_id,
            "params": params,
            "choices": [str(choice) for choice in payload.get("choices") or []],
            "at": time.time(),
        }
        with self._lock:
            self._approvals[room.id] = record

    # -- sending -----------------------------------------------------------
    def checked_attachments(self, room: Room, raw) -> list[dict]:
        """Validate the exact file ids a browser named, without reading bytes."""
        if raw in (None, []):
            return []
        if not isinstance(raw, list):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_attachment",
                               "attachments must be a list of {file_id}")
        if len(raw) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise AdapterError(
                HTTPStatus.BAD_REQUEST, "too_many_attachments",
                f"at most {MAX_ATTACHMENTS_PER_MESSAGE} attachments fit in one message")
        clean: list[dict] = []
        total = 0
        for item in raw:
            if (not isinstance(item, dict) or set(item) != {"file_id"}
                    or not isinstance(item["file_id"], str)
                    or not FILE_ID_RE.match(item["file_id"])):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_attachment",
                                   "each attachment is exactly {file_id}")
            try:
                record = self.files.get(item["file_id"], room=room.id)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            self.check_sendable(record)
            total += int(record.get("size") or 0)
            # The exact id the browser named, unchanged through queue and edit.
            clean.append({"file_id": record["id"]})
        if total > NATIVE_FILE_MAX_BYTES:
            raise AdapterError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachments_too_large",
                f"those attachments total {total} bytes, over the "
                f"{NATIVE_FILE_MAX_BYTES}-byte ceiling the native attach hooks accept. "
                "Send them across more than one message; every file stays stored here.")
        return clean

    @staticmethod
    def check_sendable(record: dict) -> None:
        """Refuse before sending what the native runtime would refuse mid-turn."""
        size = int(record.get("size") or 0)
        mime = str(record.get("mime") or "")
        if size <= 0:
            raise AdapterError(
                HTTPStatus.BAD_REQUEST, "attachment_empty",
                f"{record.get('name')} is empty, and the native runtime refuses an "
                "empty attachment")
        cap = NATIVE_IMAGE_MAX_BYTES if mime.startswith("image/") else NATIVE_FILE_MAX_BYTES
        if size > cap:
            raise AdapterError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachment_too_large",
                f"{record.get('name')} is {size} bytes; the native runtime refuses an "
                f"attachment over {cap} bytes. It stays stored and downloadable here.")

    def deliver_attachments(self, room: Room, ephemeral: str,
                            wanted: list[dict]) -> list[dict]:
        """Stage each stored file into the native session by its own hook.

        The managed bytes go across verbatim: an image through
        ``image.attach_bytes``, everything else through ``file.attach`` as a
        data URL. Nothing is re-encoded, resized or truncated, and the exact
        file id is carried through so the send can be traced back to it.
        """
        staged = []
        for entry in wanted:
            file_id = str(entry.get("file_id") or "")
            try:
                record, data = self.files.open_download(file_id, room=room.id)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            self.check_sendable(record)
            mime = str(record.get("mime") or "application/octet-stream")
            encoded = base64.b64encode(data).decode("ascii")
            if mime.startswith("image/"):
                result = self.gateway.call("image.attach_bytes", {
                    "session_id": ephemeral,
                    "content_base64": encoded,
                    "filename": record.get("name", ""),
                }, timeout=90.0)
                staged.append({"file_id": file_id, "name": record.get("name", ""),
                               "mime": mime, "native": "image.attach_bytes",
                               "native_path": str(result.get("path") or "")})
            else:
                result = self.gateway.call("file.attach", {
                    "session_id": ephemeral,
                    "data_url": f"data:{mime};base64,{encoded}",
                    "name": record.get("name", ""),
                }, timeout=90.0)
                staged.append({"file_id": file_id, "name": record.get("name", ""),
                               "mime": mime, "native": "file.attach",
                               "native_path": str(result.get("path") or ""),
                               "ref_text": str(result.get("ref_text") or "")})
        return staged

    def dispatch(self, room: Room, client_id: str, text: str,
                 attachments: list[dict]) -> dict:
        """One send, journaled before the runtime is called and never replayed."""
        ephemeral = self.require_owned(room)
        try:
            record, fresh = self.journal.reserve(
                client_id, room.id, room.session_id, text,
                f"{ADAPTER_NAME}:{client_id}", attachments)
        except DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                               str(exc), exc.detail) from exc
        if not fresh:
            # A known client id reports what was journaled and calls the
            # runtime zero more times, whatever that outcome was.
            return {"room": room.id, "submission": record, "replayed": True,
                    "accepted": record["status"] == ACCEPTED,
                    "uncertain": record["status"] == UNCERTAIN,
                    "failed": record["status"] == FAILED}

        staged: list[dict] = []
        if attachments:
            try:
                staged = self.deliver_attachments(room, ephemeral, attachments)
            except NativeUncertain as exc:
                settled = self.journal.settle(client_id, UNCERTAIN, str(exc))
                return {"room": room.id, "submission": settled, "uncertain": True}
            except NativeError as exc:
                settled = self.journal.settle(client_id, FAILED,
                                              f"{exc.code}: {exc.message}")
                return {"room": room.id, "submission": settled, "failed": True}

        body = text
        for entry in staged:
            ref = str(entry.get("ref_text") or "")
            if ref and ref not in body:
                body = f"{body}\n{ref}".strip()

        try:
            result = self.gateway.call(
                "prompt.submit", {"session_id": ephemeral, "text": body},
                timeout=float(self.config.send_timeout))
        except NativeUncertain as exc:
            settled = self.journal.settle(
                client_id, UNCERTAIN,
                f"{exc.message} — this adapter will not resend it")
            self._echo(room, "")
            return {"room": room.id, "submission": settled, "uncertain": True,
                    "message": "The native runtime did not confirm this message. It was "
                               "not resent, and nothing here knows whether it started."}
        except NativeError as exc:
            settled = self.journal.settle(client_id, FAILED,
                                          f"{exc.code}: {exc.message}")
            return {"room": room.id, "submission": settled, "failed": True,
                    "message": f"The native runtime refused this message: {exc.message}"}

        status = str(result.get("status") or "")
        if status != "streaming":
            # `prompt.submit` acknowledges a started turn with exactly
            # "streaming". Anything else is not an acknowledgement.
            settled = self.journal.settle(
                client_id, UNCERTAIN,
                f"the runtime answered {status or 'nothing'} rather than a started turn")
            return {"room": room.id, "submission": settled, "uncertain": True,
                    "message": "The native runtime gave an answer this adapter does not "
                               "recognise as a started turn. Nothing was resent."}
        settled = self.journal.settle(client_id, ACCEPTED, "", run_id="",
                                      ack_status=status)
        self._echo(room, body)
        self.events.publish({"type": "submission", "room": room.id,
                             "client_id": client_id})
        return {"room": room.id, "submission": settled, "accepted": True,
                "attachments": staged,
                "message": "The native runtime started a turn. That is not a finished "
                           "answer."}

    def _echo(self, room: Room, text: str) -> None:
        """Show the person's own message immediately, exactly once.

        It is dropped from the live tail as soon as the stored transcript
        carries the same text, so it never appears twice.
        """
        tail = self._tail(room.id)
        tail.user = text
        tail.user_at = time.time()

    def submit(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id") or "")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a send needs a stable client id")
        text = body.get("body", body.get("text", ""))
        if not isinstance(text, str) or not text.strip():
            raise AdapterError(HTTPStatus.BAD_REQUEST, "empty", "there is nothing to send")
        if len(text) > MAX_BODY:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "too_long", "that message is too long")
        thread = str(body.get("thread_id") or "")
        if thread and thread != room.session_id:
            raise AdapterError(
                HTTPStatus.CONFLICT, "thread_mismatch",
                "that message names a different conversation than the room it was sent "
                "to; nothing was sent")
        attachments = self.checked_attachments(room, body.get("attachments"))
        return self.dispatch(room, client_id, text, attachments)

    def queue(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id") or "")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a queued message needs a stable client id")
        text = body.get("body", body.get("text", ""))
        if not isinstance(text, str) or not text.strip():
            raise AdapterError(HTTPStatus.BAD_REQUEST, "empty", "there is nothing to queue")
        attachments = self.checked_attachments(room, body.get("attachments"))
        try:
            record, _ = self.journal.enqueue(
                client_id, room.id, room.session_id, text,
                "waiting for a native session in this conversation", attachments)
        except DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                               str(exc), exc.detail) from exc
        self.events.publish({"type": "queued_message", "room": room.id})
        return {"room": room.id, "queued": record}

    def _queue_loop(self) -> None:
        """Send queued messages once, and only once the view is really attached."""
        while not self._queue_stop.wait(2.0):
            try:
                ready = self.journal.queue_ready()
            except Exception:
                continue
            for item in ready:
                room = self.catalog.room(item["room"])
                if room is None or not self.catalog.live_session_for(room.session_id):
                    continue
                try:
                    self.journal.queue_mark(item["client_id"], DISPATCHING)
                    result = self.dispatch(room, item["client_id"], item["body"],
                                           item.get("attachments") or [])
                    status = (ACCEPTED if result.get("accepted")
                              else UNCERTAIN if result.get("uncertain")
                              else FAILED if result.get("failed") else DISPATCHING)
                    self.journal.queue_mark(item["client_id"], status)
                except AdapterError as exc:
                    self.journal.queue_mark(item["client_id"], PENDING, exc.message)
                except Exception as exc:  # pragma: no cover - never lose the loop
                    self.journal.queue_mark(item["client_id"], PENDING, str(exc)[:200])
                self.events.publish({"type": "queued_message", "room": item["room"]})

    # -- commands ----------------------------------------------------------
    def _native_command(self, wanted: str) -> str:
        """Return the runtime's canonical spelling only when it advertised it."""
        canon = self.native_commands.get("canon")
        if isinstance(canon, dict):
            found = canon.get(wanted.casefold())
            if isinstance(found, str) and found.startswith("/"):
                return found
        for pair in self.native_commands.get("pairs") or []:
            if (isinstance(pair, (list, tuple)) and pair and
                    str(pair[0]).casefold() == wanted.casefold()):
                return str(pair[0])
        return ""

    def supported_commands(self) -> list[str]:
        return [name for name in COMMANDS_SUPPORTED
                if name in ADAPTER_COMMANDS or self._native_command(
                    NATIVE_COMMANDS.get(name, ""))]

    def command_metadata(self) -> dict:
        supported = self.supported_commands()
        unsupported = dict(COMMAND_UNSUPPORTED)
        for name, native in NATIVE_COMMANDS.items():
            if name not in supported:
                unsupported[name] = (
                    f"the running Hermes gateway did not advertise {native} in its "
                    "native command catalog, so UX46 will not send it as text")
        catalog = {
            key: self.native_commands.get(key)
            for key in ("pairs", "canon", "categories", "skill_count", "warning")
            if key in self.native_commands
        }
        return {
            "supported": supported,
            "help": [dict(entry) for entry in COMMAND_HELP if entry["name"] in supported],
            "unsupported": unsupported,
            "native_catalog": {
                "count": len(self.native_commands.get("pairs") or []),
                "categories": [category.get("name") for category
                               in self.native_commands.get("categories") or []],
                "skill_count": self.native_commands.get("skill_count", 0),
                "warning": self.native_commands.get("warning", ""),
                "reachable_here": supported,
                "catalog": catalog,
            },
        }

    def _native_command_result(self, room: Room, name: str, argument: str, client_id: str) -> dict:
        mutates = bool(argument) or name == "/compact"
        if not mutates or not self._native_command(NATIVE_COMMANDS[name]) or not self.catalog.live_session_for(room.session_id):
            return self._dispatch_native_command(room, name, argument)
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(400, "bad_client_id", "A command needs a stable client id.")
        text = (name + " " + argument).strip()
        with self._command_lock:
            try:
                record, created = self.journal.reserve_command(client_id, room.id, room.session_id,
                    text, name, body_hash(text), "")
            except agent3.DuplicateMismatch as exc:
                raise AdapterError(409, "duplicate_mismatch", "This command id was already used.") from exc
            if not created:
                try:
                    return json.loads(record["detail"])
                except (ValueError, TypeError):
                    return {"name": name, "state": UNCERTAIN, "reason": "The earlier command has no confirmed result and was not repeated."}
            result = self._dispatch_native_command(room, name, argument)
            self.journal.settle_command(client_id, result["state"], json.dumps(result))
            return result

    def _dispatch_native_command(self, room: Room, name: str, argument: str) -> dict:
        wanted = NATIVE_COMMANDS[name]
        native_name = self._native_command(wanted)
        if not native_name:
            return {"name": name, "state": "unsupported", "sent_as_text": False,
                    "reason": (f"the running Hermes gateway did not advertise {wanted}; "
                               "UX46 did not send it as chat text"),
                    "supported": self.supported_commands()}
        ephemeral = self.catalog.live_session_for(room.session_id)
        if not ephemeral:
            return {"name": name, "state": "unsupported", "sent_as_text": False,
                    "reason": "Continue here first; this native command needs the "
                              "session this adapter opened",
                    "supported": self.supported_commands()}
        try:
            if name == "/compact":
                result = self.gateway.call("session.compress", {
                    "session_id": ephemeral, "focus_topic": argument}, timeout=120.0)
                output = str(result.get("message") or result.get("status") or "completed")
                method = "session.compress"
            else:
                command = f"{native_name} {argument}".strip()
                result = self.gateway.call("slash.exec", {
                    "session_id": ephemeral, "command": command}, timeout=60.0)
                output = str(result.get("output") or result.get("message") or "(no output)")
                method = "slash.exec"
        except NativeError as exc:
            return {"name": name, "state": UNCERTAIN if isinstance(exc, NativeUncertain) else FAILED, "sent_as_text": False,
                    "reason": f"{exc.code}: {exc.message}",
                    "native": {"method": "session.compress" if name == "/compact"
                               else "slash.exec", "session_id": ephemeral}}
        return {"name": name, "state": "completed", "sent_as_text": False,
                "output": output, "native": {"method": method,
                                                   "session_id": ephemeral,
                                                   "command": native_name}}

    def command(self, room: Room, body: dict) -> dict:
        raw = str(body.get("command") or "").strip()
        if not raw:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_command", "name a command")
        name, _, argument = raw.partition(" ")
        name = name.lower()
        name = {"/streer": "/steer"}.get(name, name)
        argument = argument.strip()
        thread = str(body.get("thread_id") or "")
        if thread and thread != room.session_id:
            raise AdapterError(HTTPStatus.CONFLICT, "thread_mismatch",
                               "that command names a different conversation")
        if name == "/help":
            return {"room": room.id, "command": {"name": "/help", "state": "completed",
                    "sent_as_text": False, **self.command_metadata()}}
        if name == "/refresh":
            refreshed = self.refresh_connection()
            return {"room": room.id, "command": {"name": "/refresh", "state": "completed",
                    "sent_as_text": False, "output": str(refreshed.get("message") or ""),
                    "native": {"method": "adapter.refresh_connection"}, **refreshed}}
        if name == "/status":
            return {"room": room.id, "command": self._status_command(room)}
        if name == "/new":
            client_id = str(body.get("client_id") or "")
            if not CLIENT_ID_RE.match(client_id):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                                   "/new needs a stable client id")
            with self._command_lock:
                return {"room": room.id,
                        "command": self._new_conversation(room, client_id, argument,
                            project=({"id": room.project_id, "name": room.project_name, "root": room.project_root} if not room.unfiled else None))}
        if name in NATIVE_COMMANDS:
            return {"room": room.id,
                    "command": self._native_command_result(room, name, argument, str(body.get("client_id") or ""))}
        return {"room": room.id, "command": {
            "name": name, "state": "unsupported", "sent_as_text": False,
            "reason": COMMAND_UNSUPPORTED.get(
                name, "this command is not available through Pane; it was not sent as text"),
            "supported": self.supported_commands(),
        }}

    def _status_command(self, room: Room) -> dict:
        ephemeral = self.catalog.live_session_for(room.session_id)
        if not ephemeral:
            return {"name": "/status", "state": "unsupported", "sent_as_text": False,
                    "reason": "session.status reads a session this adapter has open; "
                              "press Continue here first"}
        try:
            result = self.gateway.call("session.status", {"session_id": ephemeral},
                                       timeout=20.0)
        except NativeError as exc:
            return {"name": "/status", "state": "failed", "sent_as_text": False,
                    "reason": f"{exc.code}: {exc.message}"}
        return {"name": "/status", "state": "completed", "sent_as_text": False,
                "output": str(result.get("output") or ""),
                "native": {"method": "session.status", "session_id": ephemeral}}

    def _new_conversation(self, room: Room, client_id: str, argument: str, project=None) -> dict:
        """Start a real native conversation through ``session.create``.

        ``session.create`` mints its own id and cannot be made idempotent by
        key, so the journal is the only thing standing between a retry and a
        second conversation: the row is written before the call, and the call is
        never made twice for one client id. An unanswered create is a dead end
        on purpose — it is recorded uncertain and never repeated.
        """
        title = argument[:80]
        record = self.journal.command_record(client_id)
        if record is not None:
            if record["room"] != room.id or record["name"] != "/new" or record["payload_hash"] != body_hash(title):
                raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                                   "that command id was already used for something else")
            if record["status"] in (agent3.COMPLETED, ACCEPTED):
                return self._new_result(record, recovered=True)
            if record["status"] in (DISPATCHING, UNCERTAIN):
                # A dead end on purpose. `session.create` mints its own id, so
                # a second call cannot be aimed at the same conversation — it
                # would simply make another one.
                return {"name": "/new", "state": UNCERTAIN, "sent_as_text": False,
                        "created_session_id": record.get("created_key", ""),
                        "reason": "a previous /new with this id was handed to the runtime "
                                  "and never answered. It was not repeated, because a "
                                  "second call would risk a second conversation. Check "
                                  "the conversation list before trying again."}
            if record["status"] == FAILED:
                return {"name": "/new", "state": FAILED, "sent_as_text": False,
                        "reason": record.get("detail", "the runtime refused it")}
        self.journal.reserve_command(client_id, room.id, room.session_id, "/new",
                                     "/new", body_hash(title), "")
        try:
            result = self.gateway.call("session.create", {
                "title": title,
                "cwd": str(Path(project['root']).expanduser()) if project else self.config.new_cwd,
                "source": self.config.source,
            }, timeout=float(self.config.command_timeout))
        except NativeUncertain as exc:
            self.journal.settle_command(client_id, UNCERTAIN, exc.message)
            return {"name": "/new", "state": UNCERTAIN, "sent_as_text": False,
                    "reason": "the native runtime never answered. Nothing was repeated, "
                              "because a second call would risk a second conversation. "
                              "Check the conversation list before trying again."}
        except NativeError as exc:
            self.journal.settle_command(client_id, FAILED, f"{exc.code}: {exc.message}")
            return {"name": "/new", "state": FAILED, "sent_as_text": False,
                    "reason": f"the native runtime refused it: {exc.message}"}

        persisted = str(result.get("stored_session_id") or "")
        ephemeral = str(result.get("session_id") or "")
        if not persisted or not ephemeral:
            self.journal.settle_command(
                client_id, UNCERTAIN,
                "the runtime did not name both the stored and the live session")
            return {"name": "/new", "state": UNCERTAIN, "sent_as_text": False,
                    "reason": "the native runtime answered without naming the "
                              "conversation it created. Nothing was repeated."}
        self.journal.settle_command(client_id, agent3.COMPLETED, "",
                                    created_key=persisted, session_id=ephemeral)
        if project:
            creation.link(project, title, client_id, RUNTIME, persisted,
                          self.config.node, str(Path(project['root']).expanduser()))
        self.catalog.refresh(force=True)
        new_room = self.catalog.room_for_session(persisted)
        if new_room is not None:
            self.journal.attach(new_room.id, persisted)
        self.events.publish({"type": "command", "room": room.id})
        record = self.journal.command_record(client_id) or {}
        return self._new_result(record, recovered=False)

    def _new_result(self, record: dict, *, recovered: bool) -> dict:
        persisted = str(record.get("created_key") or "")
        self.catalog.refresh(force=True)
        new_room = self.catalog.room_for_session(persisted)
        return {
            "name": "/new",
            "state": agent3.COMPLETED,
            "recovered": recovered,
            "sent_as_text": False,
            "created_session_id": persisted,
            "native_session_id": str(record.get("session_id") or ""),
            "room": new_room.id if new_room else "",
            "open_room": new_room.id if new_room else "",
            "new_room": new_room.id if new_room else "",
            "source_intact": True,
            "source_released": False,
            "run_started": False,
            "message": ("Opened the conversation this id already created."
                        if recovered else
                        "Started a new native Hermes conversation and opened it here. "
                        "The conversation you were in is untouched."),
            "note": "The new conversation retains its selected project when one is linked.",
        }

    # -- bootstrap ---------------------------------------------------------
    def session_options(self) -> dict:
        projects, _, _ = read_projects(self.catalog.registry)
        return creation.options(projects)

    def create_session(self, body: dict) -> dict:
        projects, _, _ = read_projects(self.catalog.registry)
        try:
            client, title, project = creation.request(body, projects)
        except ValueError as exc:
            raise AdapterError(400, "bad_session", str(exc)) from exc
        dest = project or {"id": self.config.unfiled_project, "name": "Unfiled", "root": ""}
        # The title digest also binds retries to the original requested name.
        token = hashlib.sha256((dest['id'] + '\0' + title).encode()).hexdigest()[:20]
        room = Room(project_id=dest['id'], project_name=dest['name'], project_root=dest['root'],
                    session='new-'+token, session_id='new-'+token, row={}, meta={},
                    claim=CLAIM_UNVERIFIED, node=self.config.node, record=None, unfiled=not project)
        result = self._new_conversation(room, client, title, project)
        found = self.catalog.room_for_session(result.get('created_session_id', ''))
        return {"state": "created" if result['state'] == agent3.COMPLETED else result['state'],
                "message": result.get('message') or result.get('reason', ''),
                "new_room": found.as_json() if found else None}

    def bootstrap(self, identity: str) -> dict:
        status = self.gateway.status()
        return {
            "csrf": self.csrf_token,
            "mode": "proxy",
            "identity": identity,
            "node": self.config.node,
            "public_origin": "",
            "runtime_started": bool(status.get("connected")),
            "started_at": self.started_at,
            "seq": self.events.seq,
            "state_dir": str(self.state_dir),
            "vault_errors": [],
            "remembered_owned": self.journal.attached(),
            "voice": {"enabled": False, "loaded": False, "voices": [],
                      "reason": "this adapter does not speak; local speech belongs to "
                                "the Codex console"},
            "voice_default": "",
            "agent": {
                "id": self.config.source,
                "name": self.identity.get("name", ""),
                "emoji": "",
                "runtime": RUNTIME,
                # Reported, never adopted: the persona is Hermes's own.
                "identity_source": self.identity.get("source", ""),
                "identity_confirmed": bool(self.identity.get("confirmed")),
                "identity_declared": self.identity.get("declared", ""),
                "identity_error": self.identity.get("error", ""),
                "hermes_home": str(Path(self.config.hermes_home).expanduser()),
            },
            "adapter": {
                "name": ADAPTER_NAME,
                "version": ADAPTER_VERSION,
                "unfiled_project": self.config.unfiled_project,
                "projects": [row["id"] for row in self.project_rows()],
                "session_source": self.config.source,
                "derived_filtered": not self.config.include_derived,
                "filtered_sources": sorted(DERIVED_SOURCES),
                "history_source": "hermes state.db (read-only)",
                "state_readable": self.state.available,
                "state_warning": self.state.error,
                **status,
                "stream": bool(status.get("connected")),
            },
            "capabilities": {**capabilities(stream=bool(status.get("connected")), attachments=True),
                             "commands_supported": self.supported_commands(),
                             "command_help": self.command_metadata()["help"]},
            "unsupported": dict(UNSUPPORTED_ACTIONS),
            "commands": self.command_metadata(),
            "ownership_model": {
                "runtime": RUNTIME,
                "lease_is_exclusive": False,
                "lease_note": "Hermes's active-session lease is a capacity counter, not "
                              "an exclusive writer lock, and on this machine no cap is "
                              "configured, so it records nothing. Ownership here is "
                              "decided from our own live sessions, the session's own "
                              "ended_at, and the messaging gateway's routing table.",
                "claims": dict(CLAIM_BASIS),
                "attachable": sorted(ATTACHABLE_CLAIMS),
            },
            "gateway_error": self.catalog.error,
            "vault_warnings": self.catalog.vault_warnings[:5],
        }

    def attention(self) -> dict:
        approvals = self.all_approvals()
        return {
            "approvals": approvals,
            "checkpoints": [],
            "rooms": [],
            "note": "this adapter reads no checkpoint, so it invents no urgency; the only "
                    "thing listed here is a request the native runtime is really waiting "
                    "on in a session this adapter has open",
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HermesHandler(BaseHTTPRequestHandler):
    server_version = "Ux46Hermes/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: HermesService

    def log_message(self, fmt: str, *args) -> None:
        if self.service.config.quiet:
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------
    def _headers(self, content_type: str) -> list[tuple[str, str]]:
        return [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store, private"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(int(status))
        for key, value in self._headers(content_type):
            self.send_header(key, value)
        for key, value in extra_headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, code: str, message: str, detail=None) -> None:
        payload = {"error": code, "message": message}
        if detail is not None:
            payload["detail"] = detail
        self._json(status, payload)

    def _consume_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            if (self.headers.get("Transfer-Encoding") or "").strip().casefold() == "chunked":
                self.close_connection = True
                raise AdapterError(HTTPStatus.LENGTH_REQUIRED, "no_length",
                                   "this adapter needs an exact Content-Length")
            return b""
        try:
            length = int(raw)
        except ValueError:
            self.close_connection = True
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_length", "bad Content-Length")
        limit = (NATIVE_FILE_MAX_BYTES + MAX_UPLOAD_OVERHEAD
                 if (self.headers.get("Content-Type") or "").casefold().startswith(
                     "multipart/form-data")
                 else MAX_BODY * 2)
        if length < 0 or length > limit:
            self.close_connection = True
            raise AdapterError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                               "that request body is too large")
        return self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        if not self._raw_body:
            return {}
        try:
            parsed = json.loads(self._raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_json", "that body is not JSON")
        if not isinstance(parsed, dict):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_json",
                               "that body is not an object")
        return parsed

    def _multipart_file(self) -> tuple[str, str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload",
                               "an upload must be multipart/form-data")
        raw = b"Content-Type: " + content_type.encode("utf-8", "replace") + b"\r\n\r\n"
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(raw + self._raw_body)
        except Exception as exc:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload",
                               "that upload could not be read") from exc
        if not parsed.is_multipart():
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload",
                               "that upload carried no file part")
        for part in parsed.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            return (filename, str(part.get_content_type() or ""),
                    part.get_payload(decode=True) or b"")
        raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload",
                           "name a file part in that upload")

    # -- request gate ------------------------------------------------------
    def _check_host(self) -> None:
        """This port answers loopback only, under a name it recognises.

        The connection itself must be loopback, and the `Host` a browser sent
        must be one this adapter is served under — a request that reached this
        socket under somebody else's name is refused rather than answered.
        """
        client = self.client_address[0] if self.client_address else ""
        if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            raise AdapterError(HTTPStatus.FORBIDDEN, "denied",
                               "this adapter answers loopback only")
        host = (self.headers.get("Host") or "").strip().casefold()
        if not host:
            return
        allowed = self.service.allowed_hosts
        if host.rsplit(":", 1)[0].strip("[]") in allowed or host in allowed:
            return
        raise AdapterError(HTTPStatus.FORBIDDEN, "bad_host",
                           "this adapter is not served under that name")

    def _check_mutation(self) -> None:
        token = self.headers.get("X-Atlas-CSRF", "")
        if not secrets.compare_digest(token, self.service.csrf_token):
            raise AdapterError(HTTPStatus.FORBIDDEN, "bad_csrf",
                               "stale page — reload it to get this adapter's token")
        allowed = self.service.config.browser_origin
        if allowed:
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if origin.casefold() != allowed.rstrip("/").casefold():
                raise AdapterError(HTTPStatus.FORBIDDEN, "bad_origin",
                                   "a change must come from the console's own page")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        try:
            self._raw_body = self._consume_body()
            self._check_host()
            parsed = urlparse(self.path)
            path = parsed.path
            prefix = self.service.config.path_prefix
            if prefix and path.startswith(prefix):
                path = path[len(prefix):] or "/"
            if not path.startswith("/api/"):
                raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._check_mutation()
            return self._api(method, path, parse_qs(parsed.query))
        except (AdapterError, agent3.AdapterError) as exc:
            # The reused journal raises the Agent3 adapter's own refusal type;
            # it carries the same three fields and means the same thing.
            self._error(exc.status, exc.code, exc.message, exc.detail)
        except NativeUncertain as exc:
            self._error(HTTPStatus.ACCEPTED, "uncertain", str(exc))
        except NativeUnavailable as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "gateway_unreachable", str(exc))
        except NativeError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, exc.code, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - never leak a traceback
            if not self.service.config.quiet:
                traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "server_error", str(exc)[:200])

    # -- routes ------------------------------------------------------------
    def _api(self, method: str, path: str, query: dict) -> None:
        service = self.service
        get = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731

        if method == "GET" and path == "/api/bootstrap":
            identity = str(self.headers.get(service.config.identity_header, "") or "")
            return self._json(HTTPStatus.OK, service.bootstrap(identity))

        if method == "GET" and path == "/api/workspace":
            service.catalog.refresh()
            return self._json(HTTPStatus.OK, service.workspace())

        if method == "GET" and path == "/api/rooms":
            service.catalog.refresh()
            return self._json(HTTPStatus.OK, service.search_rooms(
                get("query"), int(get("limit", "40") or 40),
                int(get("offset", "0") or 0)))

        if method == "GET" and path == "/api/session-options":
            return self._json(HTTPStatus.OK, service.session_options())
        if method == "POST" and path == "/api/sessions":
            return self._json(HTTPStatus.OK, service.create_session(self._body()))

        if method == "GET" and path == "/api/projects":
            service.catalog.refresh()
            return self._json(HTTPStatus.OK, {"projects": service.project_rows()})

        if method == "GET" and path == "/api/events":
            return self._json(HTTPStatus.OK, service.events.since(
                int(get("after", "0") or 0),
                min(float(get("timeout", "25") or 25), 30.0),
                get("room"), get("epoch")))

        if method == "GET" and path == "/api/attention":
            return self._json(HTTPStatus.OK, service.attention())

        if method == "GET" and path == "/api/approvals":
            return self._json(HTTPStatus.OK, {
                "supported": True, "approvals": service.all_approvals(),
                "note": "only requests the native runtime is really waiting on, in a "
                        "session this adapter has open"})

        if path == "/api/approvals/answer":
            if method != "POST":
                raise AdapterError(HTTPStatus.METHOD_NOT_ALLOWED, "not_found",
                                   "answer a request with POST")
            return self._json(HTTPStatus.OK, service.answer_approval(self._body()))

        if path == "/api/connection/refresh":
            if method != "POST":
                raise AdapterError(HTTPStatus.METHOD_NOT_ALLOWED, "not_found",
                                   "refresh with POST")
            return self._json(HTTPStatus.OK, service.refresh_connection())

        media = re.fullmatch(
            r"/api/atlas/files/([A-Za-z0-9_-]{1,128})/(preview|download)", path)
        if media and method == "GET":
            # Only bytes this adapter's own managed store holds. There is no
            # path here that serves an arbitrary local file.
            try:
                record, data = service.files.open_download(media.group(1))
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            if media.group(2) == "preview":
                header = ("Content-Disposition",
                          f'inline; filename="{files._safe_name(record["name"])}"')
            else:
                header = ("Content-Disposition", files.content_disposition(record["name"]))
            return self._send(HTTPStatus.OK, data,
                              str(record.get("mime") or "application/octet-stream"),
                              (header,))

        submission = re.fullmatch(r"/api/submissions/([A-Za-z0-9_-]{8,64})", path)
        if submission and method == "GET":
            record = service.journal.submission(submission.group(1))
            if record is None:
                raise AdapterError(HTTPStatus.NOT_FOUND, "unknown_submission",
                                   "this adapter never journaled that id")
            return self._json(HTTPStatus.OK, {"submission": record})

        if method == "GET" and path == "/api/submissions":
            return self._json(HTTPStatus.OK, {
                "submissions": service.journal.recent(
                    (query.get("room") or [""])[0], limit=25)})

        pending = re.fullmatch(
            r"/api/room/([^/]+/[^/]+)/pending/([A-Za-z0-9_-]{8,64})", path)
        if pending:
            room = service.require_room(pending.group(1))
            client_id = pending.group(2)
            body = self._body() if method in ("PATCH", "DELETE") else {}
            version = body.get("version")
            if not isinstance(version, int):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_version",
                                   "a queued message is changed at the version it was "
                                   "read at")
            if method == "DELETE":
                record = service.journal.queue_cancel(client_id, version)
                service.events.publish({"type": "queued_message", "room": room.id})
                return self._json(HTTPStatus.OK, {"cancelled": record})
            if method == "PATCH":
                attachments = service.checked_attachments(room, body.get("attachments")) \
                    if "attachments" in body else None
                record = service.journal.queue_update(
                    client_id, version, body.get("body"), attachments)
                service.events.publish({"type": "queued_message", "room": room.id})
                return self._json(HTTPStatus.OK, {"queued": record})
            raise AdapterError(HTTPStatus.METHOD_NOT_ALLOWED, "not_found",
                               "no such queue endpoint")

        room_match = re.fullmatch(
            r"/api/room/([^/]+/[^/]+)"
            r"(?:/(history|search|submit|continue|attach|release|detach|stop|interrupt"
            # The actions this adapter does not implement are matched here on
            # purpose, so each is refused by name with its reason rather than
            # answered "no such endpoint".
            r"|draft|pending|files|command|pref|goal|effort|reasoning|speak))?", path)
        if room_match:
            return self._room(method, room_match.group(1), room_match.group(2) or "", get)

        raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")

    def _room(self, method: str, room_id: str, action: str, get) -> None:
        service = self.service
        if action in UNSUPPORTED_ACTIONS and action != "interrupt":
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported",
                               UNSUPPORTED_ACTIONS[action])
        room = service.require_room(room_id)

        if method == "GET" and not action:
            return self._json(HTTPStatus.OK, service.room_state(room))

        if method == "GET" and action == "history":
            return self._json(HTTPStatus.OK, service.history(
                room, int(get("limit", "40") or 40), get("cursor"),
                get("direction", "desc")))

        if method == "GET" and action == "search":
            kinds = tuple(k for k in (get("kinds", "human,final") or "").split(",") if k)
            return self._json(HTTPStatus.OK, service.search_history(
                room, get("q"), kinds or ("human", "final"),
                int(get("limit", "40") or 40)))

        if method == "GET" and action == "pending":
            return self._json(HTTPStatus.OK,
                              {"pending": service.journal.queue_list(room.id)})

        if method == "POST" and action == "pending":
            return self._json(HTTPStatus.CREATED, service.queue(room, self._body()))

        if method == "POST" and action in ("continue", "attach"):
            return self._json(HTTPStatus.OK, service.attach(room))

        if method == "POST" and action in ("release", "detach"):
            return self._json(HTTPStatus.OK, service.release(room))

        if method == "POST" and action == "submit":
            result = service.submit(room, self._body())
            status = (HTTPStatus.ACCEPTED if result.get("uncertain")
                      else HTTPStatus.BAD_GATEWAY if result.get("failed")
                      else HTTPStatus.OK)
            return self._json(status, result)

        if method == "POST" and action == "command":
            return self._json(HTTPStatus.OK, service.command(room, self._body()))

        if method == "POST" and action in ("stop", "interrupt"):
            return self._json(HTTPStatus.OK, service.stop_turn(room))

        if method == "GET" and action == "draft":
            return self._json(HTTPStatus.OK, service.journal.draft(room.id))

        if method == "PUT" and action == "draft":
            body = self._body()
            text = body.get("body")
            base_version = body.get("base_version", 0)
            if not isinstance(text, str) or len(text) > MAX_BODY:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_draft",
                                   "a draft must be text")
            if not isinstance(base_version, int):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_draft",
                                   "a draft save needs the base_version it was read at")
            try:
                saved = service.journal.save_draft(
                    room.id, text, base_version, str(body.get("device", ""))[:40])
            except DraftConflict as exc:
                raise AdapterError(HTTPStatus.CONFLICT, "draft_conflict",
                                   "this draft was changed on another device",
                                   exc.current)
            service.events.publish({"type": "draft", "room": room.id})
            return self._json(HTTPStatus.OK, saved)

        if method == "POST" and action == "files":
            name, mime, data = self._multipart_file()
            try:
                record = service.files.upload(room.id, name, data, mime,
                                              project=room.project_id)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload", str(exc)) from exc
            try:
                service.check_sendable(record)
                record["sendable"] = True
                record["send_note"] = ""
            except AdapterError as exc:
                record["sendable"] = False
                record["send_note"] = exc.message
            service.events.publish({"type": "file", "room": room.id})
            return self._json(HTTPStatus.CREATED, {"file": record})

        raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such room endpoint")


class HermesServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, service: HermesService):
        self.service = service
        handler = type("BoundHandler", (handler_class,), {"service": service})
        super().__init__(address, handler)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UX46 adapter over the native Hermes TUI gateway")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only by design)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hermes-home", default=DEFAULT_HERMES_HOME,
                        help="HERMES_HOME the native gateway child runs under")
    parser.add_argument("--hermes-repo", default=DEFAULT_HERMES_REPO,
                        help="the installed hermes-agent checkout")
    parser.add_argument("--python", default="",
                        help="interpreter for the gateway child; defaults to the "
                             "hermes-agent venv")
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME,
                        help="the persona this Hermes declares; confirmed against SOUL.md "
                             "and never invented")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="the session source this adapter tags its own sessions with")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help="read-only Session Vault registry; a conversation is filed "
                             "only under a project whose record carries a verified hermes "
                             "origin for its exact session id")
    parser.add_argument("--unfiled-project", default=DEFAULT_UNFILED_PROJECT,
                        help="internal namespace for conversations with no verified Vault "
                             "origin; no project is created for it")
    parser.add_argument("--node", default="user-mac",
                        help="the node id this Hermes runs on")
    parser.add_argument("--state-dir", default="~/.ux46-hermes",
                        help="private journal directory (not Git, Vault or Tell)")
    parser.add_argument("--session-limit", type=int, default=400,
                        help="how many persisted conversations to list")
    parser.add_argument("--include-derived", action="store_true",
                        help="also list sub-agent and tool runs (hundreds of them)")
    parser.add_argument("--new-cwd", default=str(Path.home()),
                        help="working directory for a conversation started with /new")
    parser.add_argument("--send-timeout", type=float, default=45.0)
    parser.add_argument("--attach-timeout", type=float, default=90.0)
    parser.add_argument("--command-timeout", type=float, default=60.0,
                        help="how long to wait for session.create before the result is "
                             "recorded unknown (never repeated)")
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    parser.add_argument("--suggest", type=int, default=2)
    parser.add_argument("--browser-origin", default="",
                        help="require this exact Origin on mutations; the fronting proxy "
                             "normally owns that check")
    parser.add_argument("--allow-host", action="append", default=[],
                        help="an extra Host name this adapter answers under")
    parser.add_argument("--identity-header", default="X-Forwarded-User")
    parser.add_argument("--path-prefix", default="",
                        help="strip this proxy prefix, e.g. /api/agents/pane")
    parser.add_argument("--quiet", action="store_true")
    return parser


def build_gateway(config: argparse.Namespace, on_event) -> NativeGateway:
    """Spawn exactly one native TUI gateway of this adapter's own.

    Hermes resolves its own environment inside that child, exactly as it does
    for the Ink TUI. Nothing here reads a credential file or copies a provider
    secret; only HERMES_HOME is set, and it is a directory path.
    """
    repo = Path(config.hermes_repo).expanduser()
    home = Path(config.hermes_home).expanduser()
    interpreter = config.python or str(repo / "venv" / "bin" / "python")
    if not Path(interpreter).exists():
        raise SystemExit(f"{ADAPTER_NAME}: no interpreter at {interpreter}")
    if not (repo / "tui_gateway" / "entry.py").exists():
        raise SystemExit(f"{ADAPTER_NAME}: no tui_gateway in {repo}")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"
    # The gateway's own session source, so every row this adapter opens is
    # attributable in state.db without any post-hoc relabelling.
    env["HERMES_SESSION_SOURCE"] = config.source
    return NativeGateway(
        [interpreter, "-u", "-m", "tui_gateway.entry"],
        cwd=str(repo), env=env, on_event=on_event,
        ready_timeout=float(config.ready_timeout))


def main(argv: list[str] | None = None) -> int:
    config = build_parser().parse_args(argv)
    if config.host != "127.0.0.1":
        sys.stderr.write(f"{ADAPTER_NAME}: this adapter binds loopback only\n")
        return 2
    holder: dict = {}
    gateway = build_gateway(
        config,
        lambda event, sid, payload: holder["service"].on_native_event(event, sid, payload))
    gateway.start()
    service = HermesService(config, gateway)
    holder["service"] = service
    service.start()
    server = HermesServer((config.host, config.port), HermesHandler, service)
    sys.stderr.write(
        f"{ADAPTER_NAME} {ADAPTER_VERSION} on http://{config.host}:{config.port} — "
        f"native Hermes ({service.identity.get('name')}) under "
        f"{Path(config.hermes_home).expanduser()}, gateway pid "
        f"{gateway.status().get('pid')}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
