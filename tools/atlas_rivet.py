#!/usr/bin/env python3
"""UX46 adapter for the existing Agent3 OpenClaw gateway.

This is a *reading and sending* surface over a gateway that is already running
and already authenticated. It starts nothing, configures nothing and owns
nothing on the agent side: Agent3 keeps its own process, its own login, its own
sessions and its own other clients.

Run it beside that gateway, on the same machine, as the account that owns it:

    python3 tools/atlas_rivet.py --port 8879

It binds 127.0.0.1 only. The UX46 console front end reaches it through the
local gateway proxy at ``/api/agents/agent3/...``; it is never exposed publicly.

What it deliberately is not
---------------------------
* Not a second agent. It attaches to the configured OpenClaw agent identity
  (``--agent-id``, default ``main``) and refuses every other session key. The
  gateway's own default agent id is reported, never silently adopted.
* Not an RPC tunnel. Exactly eight gateway methods are reachable, named in
  ``ALLOWED_METHODS``; nothing a browser sends can widen that.
* Not a lifecycle manager. "Release" detaches this view. It never aborts a
  run, never stops the gateway, never disturbs another client, and says so.
* Not a Codex console. Commands, goal/effort control and approvals are
  reported ``unsupported`` rather than forwarded as user text.
* Not a filing clerk. A conversation is shown under a real Session Vault
  project only when a record there carries a verified ``openclaw`` origin for
  its exact session key. Everything else is Unfiled — an internal namespace,
  not a project, and nothing is created on disk for it.
* Not a publisher. Every send carries ``deliver: false``, so a reply is
  answered on the gateway's internal webchat channel and is never republished
  into the session's Discord or other origin channel.

The gateway wire shape this maps onto is documented at
https://docs.openclaw.ai/gateway/protocol and was verified against the
installed build (see ``architecture/agent3-gateway-adapter-1.md``).
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
import subprocess
import sys
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

ADAPTER_NAME = "ux46-agent3"
ADAPTER_VERSION = "1"

DEFAULT_PORT = 8879
DEFAULT_AGENT_ID = "main"
DEFAULT_UNFILED_PROJECT = "unfiled"
UNFILED_PROJECT_NAME = "Unfiled"
DEFAULT_REGISTRY = "~/.codex/projects/registry.json"
DEFAULT_SIDECAR = str(HERE / "ux46_rivet_gateway.mjs")

MAX_BODY = 64 * 1024
EVENT_LIMIT = 500
CATALOG_TTL = 10.0
HISTORY_SCAN_CAP = 1000

# Read out of the installed OpenClaw build (2026.7.1-2), not guessed:
# `parseMessageWithAttachments` rejects an image over MAX_IMAGE_BYTES outright
# and any attachment over `agents.defaults.mediaMaxMb` (default 20 MB), while
# the gateway socket refuses a frame over MAX_PAYLOAD_BYTES. All three are
# enforced here so a person is told before a turn starts, not after it fails.
GATEWAY_IMAGE_MAX_BYTES = 6 * 1024 * 1024
GATEWAY_MEDIA_MAX_BYTES = 20 * 1024 * 1024
GATEWAY_FRAME_MAX_BYTES = 25 * 1024 * 1024
# Attachments ride as base64 inside one JSON frame, which inflates them by 4/3.
# This is the largest raw payload that still fits the frame with room for the
# message and the rest of the envelope; it is the binding limit in practice.
GATEWAY_ATTACHMENT_MAX_BYTES = 17 * 1024 * 1024
# An image at or under this size is carried inline; larger images and every
# non-image attachment are offloaded by the gateway to `media://inbound/<id>`
# and staged into the agent's workspace as `MediaPaths`.
GATEWAY_INLINE_IMAGE_BYTES = 2 * 1000 * 1000
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_UPLOAD_OVERHEAD = 256 * 1024

# The gateway's own documented acknowledgement statuses for chat.send: a fresh
# admission answers `accepted`, and a replay of a known run answers
# `in_flight`. The installed 2026.7.1-2 chat handler answers `started`.
ACCEPTED_RUN_STATUSES = frozenset({"accepted", "in_flight", "started"})

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SLUG_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# The whole of what this adapter may ask the gateway for. A browser names a
# room and an action; it never names a method.
ALLOWED_METHODS = frozenset({
    "agent.identity.get",
    "agents.list",
    "sessions.list",
    "sessions.create",
    "sessions.patch",
    "sessions.compact",
    "chat.history",
    "chat.send",
    "chat.abort",
    "sessions.messages.subscribe",
    "sessions.messages.unsubscribe",
})

CAPABILITY = "openclaw-gateway"
CAPABILITY_SHORT = "Agent3 · OpenClaw gateway"
CAPABILITY_LABEL = (
    "the Agent3 OpenClaw gateway on this machine — readable, and sendable "
    "through the gateway's own session"
)

# Submission lifecycle. These words mean exactly what the console's do, and
# "accepted" is only ever written after the gateway said so.
PENDING = "pending"
DISPATCHING = "dispatching"
COMPLETED = "completed"
ACCEPTED = "accepted"
FAILED = "failed"
UNCERTAIN = "uncertain"
CANCELLED = "cancelled"

# The slash commands this adapter can complete without forwarding command text
# into a Agent3 conversation.  Their implementation maps only to the gateway
# calls listed in ``ALLOWED_METHODS`` or to this adapter's own bridge.
COMMANDS_SUPPORTED = ("/new", "/help", "/status", "/refresh", "/model", "/effort", "/compact")
COMMAND_HELP = (
    {"name": "/model", "usage": "/model [name]", "description": "read or set this session’s model through the native gateway"},
    {"name": "/effort", "usage": "/effort [level]", "description": "read or set native thinking effort for this session"},
    {"name": "/compact", "usage": "/compact", "description": "run the gateway’s native compaction for this session"},
    {"name": "/new", "usage": "/new [title]",
     "description": "start a fresh Agent3 conversation on this gateway and open it here; "
                    "the conversation you are in is left exactly as it is. A linked "
                    "project is retained for the new conversation"},
    {"name": "/help", "usage": "/help",
     "description": "show the commands this adapter can actually perform and why the "
                    "others are unavailable"},
    {"name": "/status", "usage": "/status",
     "description": "read the current gateway session state without starting a turn"},
    {"name": "/refresh", "usage": "/refresh",
     "description": "reconnect this UX46 adapter's gateway bridge and refresh its "
                    "session catalog; it does not restart Agent3 or replay a request"},
)
# Named so the refusal can say what would be needed, not just that it is missing.
COMMAND_UNSUPPORTED = {
    "/steer": "this gateway adapter has no native steering operation; Stop and send a new instruction",
    "/goal": "OpenClaw exposes no native goal object to this adapter",
}

STATUS_MEANING = {
    PENDING: "journaled here, not yet accepted by the gateway",
    DISPATCHING: "handed to the gateway; no answer yet",
    ACCEPTED: "accepted by the OpenClaw gateway — not a completed answer",
    FAILED: "the gateway refused it; nothing was delivered",
    UNCERTAIN: "delivery is unknown; this adapter will not resend on its own",
    CANCELLED: "cancelled here before it was ever handed to the gateway",
}

# Every console action this adapter does not implement, and the honest reason.
# These are answered with an explicit refusal, never forwarded as chat text.
UNSUPPORTED_ACTIONS = {
    "goal": "the OpenClaw gateway exposes no goal object, so there is nothing to read or set",
    "effort": "use /effort to change the native session setting",
    "reasoning": "use /reasoning or /effort to change native thinking effort",
    "approvals": "exec and plugin approvals need the gateway approvals scope and a "
                 "resolution surface this adapter does not implement",
    "speak": "local speech belongs to the Codex console, not to this adapter",
    "refresh": "use /refresh to re-check the gateway connection",
    "interrupt": "use Stop, which maps to the gateway's own chat.abort for this session",
}

CAPABILITIES = {
    "read_history": True,
    "search_history": True,
    "send": True,
    "stop": True,
    "stream": True,
    "queue": True,
    "attach_scope": "view_only",
    # Managed uploads are stored here and sent as genuine gateway attachments.
    "attachments": True,
    "attachment_images": True,
    "attachment_files": True,
    "attachment_previews": True,
    "attachment_limits": {
        "image_max_bytes": GATEWAY_IMAGE_MAX_BYTES,
        "file_max_bytes": GATEWAY_ATTACHMENT_MAX_BYTES,
        "message_total_bytes": GATEWAY_ATTACHMENT_MAX_BYTES,
        "inline_image_bytes": GATEWAY_INLINE_IMAGE_BYTES,
        "per_message": MAX_ATTACHMENTS_PER_MESSAGE,
        "basis": "gateway image cap, media cap, and the 25 MiB socket frame that "
                 "base64 has to fit inside",
    },
    "drafts": True,
    "commands": True,
    "commands_supported": list(COMMANDS_SUPPORTED),
    "command_help": [dict(entry) for entry in COMMAND_HELP],
    "goal": False,
    "effort": True,
    "approvals": False,
    "voice": False,
    "notes": [
        "Attach and Release move only this UX46 view. Agent3's gateway, its other "
        "clients and any run in flight are untouched by either.",
        "Send is idempotent on the client id: a repeat of a known id reports the "
        "journaled outcome and calls the gateway zero more times.",
        "A send whose outcome the gateway never confirmed is reported uncertain "
        "and is never resent automatically.",
        "Attachments are stored here as managed bytes and delivered as real "
        "gateway attachments. Images ride inline under 2 MB; larger images and "
        "every other file type are offloaded by the gateway and staged into the "
        "agent's workspace. Nothing is base64-guessed and no file is dropped.",
        "Drafts are this adapter's own compare-and-set store. They are never "
        "written into a gateway session.",
        "Commands use native gateway operations. /new calls the gateway's own "
        "sessions.create with a fresh key and no message, so no model run starts "
        "and the conversation it was typed in is untouched. Model and effort use sessions.patch; "
        "compaction uses sessions.compact. Unsupported commands are never sent as chat text.",
        "Every send carries deliver: false, so a reply is answered on the "
        "gateway's internal webchat channel and is never published to the "
        "session's Discord or other origin channel.",
    ],
}

# Verified against the installed build, not assumed. In `chat.send`,
# `resolveChatSendOriginatingRoute` returns the internal channel unless
# `deliver === true`, and the reply pipeline is built on
# `INTERNAL_MESSAGE_CHANNEL` ("webchat"). Routing a reply anywhere else needs
# either `deliver: true` on a session that carries a delivery context, or the
# `originatingChannel`/`originatingTo` fields — which the gateway itself
# refuses without admin scope. This adapter sends `deliver: false` explicitly
# and never sends an originating route, so a send from the browser cannot
# publish into Discord or any other channel.
DELIVERY_BOUNDARY = {
    "deliver": False,
    "reply_channel": "webchat",
    "publishes_to_origin_channel": False,
    "originating_route_sent": False,
    "verified_against": "openclaw 2026.7.1-2 resolveChatSendOriginatingRoute",
    "note": "a message sent from this console is answered on the gateway's internal "
            "webchat channel; it is never republished to the session's Discord or "
            "other origin channel, and this adapter never sends to another person",
}


class AdapterError(Exception):
    """A refusal this adapter can explain to the person who caused it."""

    def __init__(self, status: HTTPStatus, code: str, message: str, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


class GatewayError(Exception):
    """The gateway answered, and the answer was a refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code or "gateway_error"


class GatewayUnavailable(GatewayError):
    """The gateway could not be reached at all. Nothing was delivered."""


class GatewayUncertain(GatewayError):
    """The call went out and no outcome came back. Never retried here."""


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------

class Transport:
    """One authenticated conversation with the gateway."""

    name = "none"

    def start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        raise NotImplementedError

    def status(self) -> dict:
        return {"transport": self.name, "connected": False, "detail": ""}

    def reconnect(self) -> dict:
        """Reconnect this adapter's transport, if it has one.

        This is deliberately an adapter operation. It does not restart the
        gateway, send chat text, or retry any prior call.
        """

        return {**self.status(), "reconnected": False,
                "note": "this transport opens a fresh gateway call for each request"}


class SidecarTransport(Transport):
    """A persistent gateway connection held by the Node bridge.

    The bridge resolves the gateway's own auth in its own process. This class
    never sees, stores or forwards a credential; it exchanges newline JSON with
    a child process over a pipe.
    """

    name = "sidecar"

    def __init__(self, command: list[str], on_event=None, ready_timeout: float = 30.0):
        self.command = list(command)
        self._on_event = on_event
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._waiters: dict[str, dict] = {}
        self._cond = threading.Condition()
        self._seq = 0
        self._ready: dict = {}
        self._error = ""
        self._reader: threading.Thread | None = None
        self._connected = False
        self._generation = 0

    def start(self) -> None:
        with self._cond:
            # A previous ready frame must not satisfy a replacement bridge's
            # handshake when ``reconnect`` starts this object again.
            self._ready = {}
            self._error = ""
            self._connected = False
        self._proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        deadline = time.time() + self._ready_timeout
        with self._cond:
            while not self._ready and not self._error and time.time() < deadline:
                self._cond.wait(timeout=max(0.05, deadline - time.time()))
            if self._error:
                raise GatewayUnavailable("gateway_unreachable", self._error)
            if not self._ready:
                raise GatewayUnavailable(
                    "gateway_unreachable",
                    "the gateway bridge did not report a connection in time",
                )
        self._connected = True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._connected = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

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
            kind = str(frame.get("type") or "")
            if kind == "ready":
                with self._cond:
                    if frame.get("ok"):
                        self._ready = frame.get("gateway") or {"connected": True}
                    else:
                        self._error = str((frame.get("error") or {}).get("message") or "unknown")
                    self._cond.notify_all()
            elif kind == "response":
                key = str(frame.get("id") or "")
                with self._cond:
                    waiter = self._waiters.get(key)
                    if waiter is not None:
                        waiter["frame"] = frame
                        self._cond.notify_all()
            elif kind == "event":
                if self._on_event:
                    self._on_event(str(frame.get("event") or ""), frame.get("payload") or {})
            elif kind == "transport":
                # The bridge reconnects itself; the adapter reports the gap
                # rather than presenting stale history as live.
                self._connected = frame.get("state") == "connected"
                self._generation += 1
                if self._on_event:
                    self._on_event("transport", dict(frame))
        with self._cond:
            # A retired reader must not mark a replacement bridge offline.
            if self._proc is proc:
                self._connected = False
                if not self._ready and not self._error:
                    self._error = "the gateway bridge exited before it connected"
                self._cond.notify_all()

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if method not in ALLOWED_METHODS:
            raise GatewayError("method_not_allowed", f"this adapter never calls {method}")
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise GatewayUnavailable("gateway_unreachable",
                                     "the gateway bridge is not running")
        with self._lock:
            self._seq += 1
            key = str(self._seq)
            payload = json.dumps({"id": key, "method": method,
                                  "params": params or {},
                                  "timeout_ms": int(timeout * 1000)})
            with self._cond:
                self._waiters[key] = {"frame": None}
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except OSError as exc:
                with self._cond:
                    self._waiters.pop(key, None)
                raise GatewayUnavailable("gateway_unreachable", str(exc)) from exc
        deadline = time.time() + timeout + 5.0
        with self._cond:
            while self._waiters[key]["frame"] is None and time.time() < deadline:
                self._cond.wait(timeout=max(0.05, deadline - time.time()))
            frame = self._waiters.pop(key)["frame"]
        if frame is None:
            raise GatewayUncertain(
                "uncertain",
                "the gateway bridge gave no answer in time; this adapter will not "
                "repeat the call",
            )
        if frame.get("ok"):
            return frame.get("result") or {}
        error = frame.get("error") or {}
        message = str(error.get("message") or "the gateway refused the call")
        if any(word in message.lower() for word in (
                "timeout", "timed out", "disconnected", "connection closed", "socket closed")):
            raise GatewayUncertain(str(error.get("code") or "uncertain"), message)
        raise GatewayError(str(error.get("code") or "gateway_error"),
                           message)

    def status(self) -> dict:
        proc = self._proc
        running = bool(proc and proc.poll() is None)
        return {
            "transport": self.name,
            "connected": bool(running and self._connected),
            "detail": self._error,
            "gateway": dict(self._ready),
        }

    def reconnect(self) -> dict:
        """Replace only the local bridge process and await its new handshake."""

        self.stop()
        self.start()
        return {**self.status(), "reconnected": True}


class CliTransport(Transport):
    """One `openclaw gateway call` per request: the deterministic fallback.

    This spends a process per call and cannot stream, so history polling is
    bounded and the adapter reports ``stream: false``. It is here because it
    needs nothing but the CLI that is already installed and already logged in.
    """

    name = "cli"

    def __init__(self, command: list[str]):
        self.command = list(command)
        self._detail = ""

    def start(self) -> None:
        # A read-only call proves the CLI resolves its own auth.
        self.call("agent.identity.get", {}, timeout=15.0)

    def stop(self) -> None:
        return None

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if method not in ALLOWED_METHODS:
            raise GatewayError("method_not_allowed", f"this adapter never calls {method}")
        argv = [*self.command, "gateway", "call", method, "--json",
                "--params", json.dumps(params or {}),
                "--timeout", str(int(timeout * 1000))]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout + 10.0)
        except FileNotFoundError as exc:
            self._detail = str(exc)
            raise GatewayUnavailable("gateway_unreachable",
                                     "the openclaw CLI is not on PATH here") from exc
        except subprocess.TimeoutExpired as exc:
            raise GatewayUncertain(
                "uncertain",
                "the openclaw CLI did not return in time; this adapter will not "
                "repeat the call",
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            self._detail = detail
            raise GatewayError("gateway_error", detail or "the gateway call failed")
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError as exc:
            raise GatewayError("bad_response",
                               "the gateway returned something that is not JSON") from exc

    def status(self) -> dict:
        return {"transport": self.name, "connected": True, "detail": self._detail,
                "gateway": {"stream": False}}


# ---------------------------------------------------------------------------
# durable journal
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    client_id      TEXT PRIMARY KEY,
    room           TEXT NOT NULL,
    session_key    TEXT NOT NULL,
    body_hash      TEXT NOT NULL,
    body           TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    attachments    TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    attempt_id     TEXT NOT NULL DEFAULT '',
    ack_status     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    client_id   TEXT PRIMARY KEY,
    room        TEXT NOT NULL,
    session_key TEXT NOT NULL,
    body        TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attached (
    room        TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    since       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    client_id       TEXT PRIMARY KEY,
    room            TEXT NOT NULL,
    session_key     TEXT NOT NULL,
    command         TEXT NOT NULL,
    name            TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    status          TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    created_key     TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    room       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    device     TEXT NOT NULL DEFAULT ''
);
"""


class DraftConflict(Exception):
    """Another device advanced this draft; the caller must merge, not clobber."""

    def __init__(self, current: dict):
        super().__init__("this draft was changed on another device")
        self.current = current


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DuplicateMismatch(Exception):
    def __init__(self, detail: dict):
        super().__init__("that submission id was already used for different text or target")
        self.detail = detail


class Journal:
    """Durable per-client-id record of every send this adapter ever attempted.

    It is written *before* the gateway is called and settled after, so a crash
    between the two leaves a `dispatching` row that is reported as unknown
    rather than replayed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._local = threading.local()
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # -- submissions -------------------------------------------------------
    def reserve(self, client_id: str, room: str, session_key: str, body: str,
                idempotency_key: str, attachments: list[dict] | None = None) -> tuple[dict, bool]:
        # The attachment ids are part of what makes a submission that
        # submission: reusing an id with a different file set is a mismatch,
        # not a retry.
        digest = body_hash(body + "\x00" + json.dumps(
            [str(a.get("file_id", "")) for a in (attachments or [])]))
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE client_id = ?",
                               (client_id,)).fetchone()
            if row is not None:
                if row["body_hash"] != digest or row["session_key"] != session_key:
                    raise DuplicateMismatch({
                        "client_id": client_id,
                        "recorded_session_key": row["session_key"],
                        "recorded_status": row["status"],
                    })
                return self._submission_json(row), False
            conn.execute(
                "INSERT INTO submissions(client_id, room, session_key, body_hash, body,"
                " idempotency_key, attachments, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (client_id, room, session_key, digest, body, idempotency_key,
                 json.dumps(attachments or []), DISPATCHING, now, now),
            )
            row = conn.execute("SELECT * FROM submissions WHERE client_id = ?",
                               (client_id,)).fetchone()
            return self._submission_json(row), True

    def settle(self, client_id: str, status: str, detail: str = "", run_id: str = "",
               attempt_id: str = "", ack_status: str = "") -> dict:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE submissions SET status = ?, detail = ?, run_id = ?, attempt_id = ?,"
                " ack_status = ?, updated_at = ? WHERE client_id = ?",
                (status, detail[:400], run_id, attempt_id, ack_status,
                 time.time(), client_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE client_id = ?",
                               (client_id,)).fetchone()
        return self._submission_json(row)

    def submission(self, client_id: str) -> dict | None:
        row = self._connect().execute(
            "SELECT * FROM submissions WHERE client_id = ?", (client_id,)).fetchone()
        return self._submission_json(row) if row else None

    def recent(self, room: str = "", limit: int = 10) -> list[dict]:
        sql = "SELECT * FROM submissions"
        args: tuple = ()
        if room:
            sql += " WHERE room = ?"
            args = (room,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._connect().execute(sql, (*args, int(limit))).fetchall()
        return [self._submission_json(row) for row in rows]

    def unsettled(self, session_key: str = "") -> list[dict]:
        rows = self._connect().execute(
            "SELECT * FROM submissions WHERE status = ?", (DISPATCHING,)).fetchall()
        out = [self._submission_json(row) for row in rows]
        return [row for row in out if not session_key or row["session_key"] == session_key]

    @staticmethod
    def _submission_json(row) -> dict:
        return {
            "client_id": row["client_id"],
            "room": row["room"],
            # The exact gateway session key, never a reconstructed one.
            "session_key": row["session_key"],
            "thread_id": row["session_key"],
            "body": row["body"],
            "status": row["status"],
            "detail": row["detail"],
            "native_turn_id": row["run_id"],
            "run_id": row["run_id"],
            "attempt_id": row["attempt_id"],
            # The gateway's own word for what it did with the run.
            "ack_status": row["ack_status"],
            "idempotency_key": row["idempotency_key"],
            "attachments": json.loads(row["attachments"] or "[]"),
            "mode": "gateway",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "meaning": STATUS_MEANING.get(row["status"], ""),
        }

    # -- queue -------------------------------------------------------------
    def enqueue(self, client_id: str, room: str, session_key: str, body: str,
                reason: str, attachments: list[dict] | None = None) -> tuple[dict, bool]:
        now = time.time()
        payload = json.dumps(attachments or [])
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
            if row is not None:
                if (row["body"] != body or row["session_key"] != session_key
                        or row["attachments"] != payload):
                    raise DuplicateMismatch({"client_id": client_id,
                                             "recorded_status": row["status"]})
                return self._queue_json(row), False
            conn.execute(
                "INSERT INTO queue(client_id, room, session_key, body, attachments, status,"
                " reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (client_id, room, session_key, body, payload, PENDING, reason, now, now),
            )
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
            return self._queue_json(row), True

    def queue_get(self, client_id: str) -> dict | None:
        row = self._connect().execute("SELECT * FROM queue WHERE client_id = ?",
                                      (client_id,)).fetchone()
        return self._queue_json(row) if row else None

    def queue_list(self, room: str) -> list[dict]:
        rows = self._connect().execute(
            "SELECT * FROM queue WHERE room = ? AND status IN (?, ?) ORDER BY created_at",
            (room, PENDING, DISPATCHING)).fetchall()
        return [self._queue_json(row) for row in rows]

    def queue_ready(self) -> list[dict]:
        rows = self._connect().execute(
            "SELECT * FROM queue WHERE status = ? ORDER BY created_at", (PENDING,)).fetchall()
        return [self._queue_json(row) for row in rows]

    def queue_mark(self, client_id: str, status: str, reason: str = "") -> dict:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE queue SET status = ?, reason = ?, version = version + 1,"
                " updated_at = ? WHERE client_id = ?",
                (status, reason[:400], time.time(), client_id),
            )
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
        return self._queue_json(row)

    def queue_cancel(self, client_id: str, version: int) -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
            if row is None:
                raise AdapterError(HTTPStatus.NOT_FOUND, "queue_unknown",
                                   "that queued message is not in this room")
            if row["version"] != version:
                raise AdapterError(HTTPStatus.CONFLICT, "queue_stale",
                                   "that queued message changed since you read it",
                                   self._queue_json(row))
            if row["status"] != PENDING:
                raise AdapterError(HTTPStatus.CONFLICT, "queue_dispatching",
                                   "that message is already with the gateway, so it "
                                   "cannot be cancelled here",
                                   self._queue_json(row))
            conn.execute(
                "UPDATE queue SET status = ?, version = version + 1, updated_at = ?"
                " WHERE client_id = ?", (CANCELLED, time.time(), client_id))
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
        return self._queue_json(row)

    @staticmethod
    def _queue_json(row) -> dict:
        return {
            "client_id": row["client_id"],
            "room": row["room"],
            "session_key": row["session_key"],
            "thread_id": row["session_key"],
            "body": row["body"],
            # The exact file ids the person attached, carried unchanged from
            # queueing through editing to delivery.
            "attachments": json.loads(row["attachments"] or "[]"),
            "status": row["status"],
            "version": row["version"],
            "queued_reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def queue_update(self, client_id: str, version: int, body: str | None,
                     attachments: list[dict] | None) -> dict:
        """Edit a queued message under compare-and-set, ids intact."""

        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
            if row is None:
                raise AdapterError(HTTPStatus.NOT_FOUND, "queue_unknown",
                                   "that queued message is not in this room")
            if row["version"] != version:
                raise AdapterError(HTTPStatus.CONFLICT, "queue_stale",
                                   "that queued message changed since you read it",
                                   self._queue_json(row))
            if row["status"] != PENDING:
                raise AdapterError(HTTPStatus.CONFLICT, "queue_dispatching",
                                   "that message is already with the gateway, so it "
                                   "cannot be edited here", self._queue_json(row))
            conn.execute(
                "UPDATE queue SET body = ?, attachments = ?, version = version + 1,"
                " updated_at = ? WHERE client_id = ?",
                (row["body"] if body is None else body,
                 row["attachments"] if attachments is None else json.dumps(attachments),
                 time.time(), client_id))
            row = conn.execute("SELECT * FROM queue WHERE client_id = ?",
                               (client_id,)).fetchone()
        return self._queue_json(row)

    # -- commands ----------------------------------------------------------
    def reserve_command(self, client_id: str, room: str, session_key: str, command: str,
                        name: str, payload_hash: str,
                        destination_key: str) -> tuple[dict, bool]:
        """Write down what a command intends *before* the gateway is called.

        The destination key is persisted here, so a restart, a crash or a
        concurrent duplicate can never end up asking the gateway to create a
        second conversation: the same client id always resolves to the same key,
        and a row that already exists is reconciled rather than reissued.
        """

        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM commands WHERE client_id = ?",
                               (client_id,)).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash or row["session_key"] != session_key:
                    raise DuplicateMismatch({
                        "client_id": client_id,
                        "recorded_command": row["command"],
                        "recorded_session_key": row["session_key"],
                        "recorded_status": row["status"],
                    })
                return self._command_json(row), False
            conn.execute(
                "INSERT INTO commands(client_id, room, session_key, command, name,"
                " payload_hash, destination_key, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (client_id, room, session_key, command, name, payload_hash,
                 destination_key, DISPATCHING, now, now))
            row = conn.execute("SELECT * FROM commands WHERE client_id = ?",
                               (client_id,)).fetchone()
            return self._command_json(row), True

    def settle_command(self, client_id: str, status: str, detail: str = "",
                       created_key: str = "", session_id: str = "") -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM commands WHERE client_id = ?",
                               (client_id,)).fetchone()
            conn.execute(
                "UPDATE commands SET status = ?, detail = ?, created_key = ?,"
                " session_id = ?, updated_at = ? WHERE client_id = ?",
                (status, detail[:400],
                 created_key or (row["created_key"] if row else ""),
                 session_id or (row["session_id"] if row else ""),
                 time.time(), client_id))
            row = conn.execute("SELECT * FROM commands WHERE client_id = ?",
                               (client_id,)).fetchone()
        if row is None:
            raise AdapterError(HTTPStatus.CONFLICT, "command_unknown",
                               "that command's record is gone, so its outcome cannot "
                               "be reported; nothing was resent")
        return self._command_json(row)

    def command_record(self, client_id: str) -> dict | None:
        row = self._connect().execute("SELECT * FROM commands WHERE client_id = ?",
                                      (client_id,)).fetchone()
        return self._command_json(row) if row else None

    def unsettled_commands(self) -> list[dict]:
        rows = self._connect().execute(
            "SELECT * FROM commands WHERE status IN (?, ?)",
            (DISPATCHING, UNCERTAIN)).fetchall()
        return [self._command_json(row) for row in rows]

    @staticmethod
    def _command_json(row) -> dict:
        return {
            "client_id": row["client_id"],
            "room": row["room"],
            "session_key": row["session_key"],
            "command": row["command"],
            "name": row["name"],
            "payload_hash": row["payload_hash"],
            # Generated before the call, and never regenerated for this id.
            "destination_key": row["destination_key"],
            "created_key": row["created_key"],
            "session_id": row["session_id"],
            "status": row["status"],
            "detail": row["detail"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # -- drafts ------------------------------------------------------------
    def draft(self, room: str) -> dict:
        row = self._connect().execute(
            "SELECT * FROM drafts WHERE room = ?", (room,)).fetchone()
        if not row:
            return {"room": room, "body": "", "version": 0, "updated_at": 0.0, "device": ""}
        return {"room": row["room"], "body": row["body"], "version": int(row["version"]),
                "updated_at": float(row["updated_at"]), "device": row["device"]}

    def save_draft(self, room: str, body: str, base_version: int, device: str = "") -> dict:
        """Compare-and-set. A stale base version never overwrites newer text.

        This is the adapter's own store, deterministic and local. It never
        writes anything into a gateway session.
        """

        now = time.time()
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT version, body, updated_at, device FROM drafts WHERE room = ?",
                    (room,)).fetchone()
                current = int(row["version"]) if row else 0
                if int(base_version) != current:
                    conn.execute("ROLLBACK")
                    raise DraftConflict({
                        "room": room,
                        "body": row["body"] if row else "",
                        "version": current,
                        "updated_at": float(row["updated_at"]) if row else 0.0,
                        "device": row["device"] if row else "",
                    })
                version = current + 1
                conn.execute(
                    "INSERT INTO drafts(room, body, version, updated_at, device)"
                    " VALUES(?,?,?,?,?)"
                    " ON CONFLICT(room) DO UPDATE SET body=excluded.body,"
                    " version=excluded.version, updated_at=excluded.updated_at,"
                    " device=excluded.device",
                    (room, body, version, now, device))
                conn.execute("COMMIT")
            except DraftConflict:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"room": room, "body": body, "version": version, "updated_at": now,
                "device": device}

    # -- attachment (this view only) --------------------------------------
    def attach(self, room: str, session_key: str) -> dict:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO attached(room, session_key, since) VALUES(?,?,?)"
                " ON CONFLICT(room) DO UPDATE SET session_key=excluded.session_key",
                (room, session_key, time.time()))
            row = conn.execute("SELECT * FROM attached WHERE room = ?", (room,)).fetchone()
        return {"room": row["room"], "session_key": row["session_key"], "since": row["since"]}

    def detach(self, room: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM attached WHERE room = ?", (room,))
        return cur.rowcount > 0

    def attached(self) -> dict[str, str]:
        rows = self._connect().execute("SELECT * FROM attached").fetchall()
        return {row["room"]: row["session_key"] for row in rows}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

class Events:
    def __init__(self):
        self._cond = threading.Condition()
        self._events: list[dict] = []
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, event: dict) -> None:
        with self._cond:
            self._seq += 1
            self._events.append(dict(event, seq=self._seq, at=time.time()))
            del self._events[:-EVENT_LIMIT]
            self._cond.notify_all()

    def since(self, after: int, timeout: float = 25.0, room: str = "") -> dict:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                events = [e for e in self._events if e["seq"] > after]
                if room:
                    events = [e for e in events
                              if e.get("room") in (room, None, "") or e.get("global")]
                if events or time.time() >= deadline:
                    return {"seq": self._seq, "events": events[-100:]}
                self._cond.wait(timeout=max(0.1, deadline - time.time()))


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------

def session_slug(key: str, agent_id: str) -> str:
    """A room-id-safe name for one exact gateway session key.

    The slug is presentation only. Every lookup goes back through the catalog
    map built from ``sessions.list``, so a slug can never be turned into a
    session key this gateway did not report.
    """

    core = key
    prefix = f"agent:{agent_id}:"
    if core.startswith(prefix):
        core = core[len(prefix):]
    slug = SLUG_STRIP_RE.sub("-", core).strip("-._")
    if not slug or not slug[0].isalnum():
        slug = f"s-{slug}" if slug else "session"
    return slug[:95]


class Room:
    """One gateway session, as UX46 addresses it."""

    def __init__(self, *, project_id: str, project_name: str, project_root: str,
                 session: str, session_key: str, entry: dict, node: str,
                 record: dict | None, unfiled: bool):
        self.project_id = project_id
        self.project_name = project_name
        self.project_root = project_root
        self.session = session
        self.session_key = session_key
        self.entry = entry
        self.node = node
        self.record = record
        # True when no Vault record anywhere carries a verified openclaw origin
        # for this exact key. Such a conversation is shown under the internal
        # Unfiled namespace rather than filed under somebody's real project.
        self.unfiled = unfiled

    @property
    def id(self) -> str:
        return f"{self.project_id}/{self.session}"

    @property
    def updated_ms(self) -> int:
        value = self.entry.get("updatedAt")
        return int(value) if isinstance(value, (int, float)) else 0

    @property
    def last_active(self) -> str:
        if not self.updated_ms:
            return ""
        try:
            return time.strftime("%Y-%m-%d", time.localtime(self.updated_ms / 1000))
        except (OverflowError, OSError, ValueError):
            return ""

    @property
    def title(self) -> str:
        if self.record and self.record.get("title"):
            return str(self.record["title"])
        origin = self.entry.get("origin") or {}
        label = str(self.entry.get("label") or self.entry.get("displayName")
                    or origin.get("label") or "")
        return label or self.session

    def as_json(self) -> dict:
        origin = self.entry.get("origin") or {}
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "session": self.session,
            "title": self.title,
            # Not a lifecycle claim: this adapter has no Vault record for a
            # session unless one carries a verified openclaw origin.
            "status": str((self.record or {}).get("status") or "unrecorded"),
            "status_source": "vault_record" if self.record else "none",
            "updated": self.last_active,
            "capability": CAPABILITY,
            "capability_label": CAPABILITY_LABEL,
            "capability_short": CAPABILITY_SHORT,
            "controllable": True,
            "runtime": "openclaw",
            "node": self.node,
            "origin_count": 1 if self.record else 0,
            "origins_status": "linked" if self.record else "unlinked",
            "worker": False,
            "worker_provenance": "",
            "identity_key": f"openclaw@{self.node}:{self.session_key}",
            "conversation_key": f"{self.project_id}/openclaw@{self.node}:{self.session_key}",
            "last_active": self.last_active,
            "recency_source": "gateway" if self.updated_ms else "none",
            "native_role": "human",
            "native_provenance": str(origin.get("provider") or ""),
            "native_archived": bool(self.entry.get("archived")),
            "record_aliases": [],
            "attention": {"state": "none", "source": "adapter",
                          "basis": "this adapter reads no checkpoint"},
            "checkpoint": (self.record or {}).get("checkpoint"),
            # The exact key, said plainly, with where it came from.
            "session_key": self.session_key,
            "session_key_source": "gateway.sessions.list",
            "thread_id": self.session_key,
            "record_linked": bool(self.record),
            "record_path": str((self.record or {}).get("record_path") or ""),
            # `unfiled` is an internal namespace of this adapter, not a project
            # in anyone's Session Vault, and no Vault project was created for it.
            "unfiled": self.unfiled,
            "vault_project": not self.unfiled,
            "project_source": "vault_origin" if self.record else "unfiled",
        }


class Catalog:
    """The exact set of rooms, rebuilt from ``sessions.list`` and nothing else.

    Where a room *lives* is a separate question from whether it exists. A
    conversation is filed under a real Session Vault project only when some
    record in that project carries a verified `openclaw` origin naming this
    exact session key. Everything else lands in the internal Unfiled namespace,
    which is not a project and is never written to disk as one.
    """

    def __init__(self, transport: Transport, *, agent_id: str,
                 projects: dict[str, dict], links: dict[str, dict],
                 unfiled_project: str, node: str, ttl: float = CATALOG_TTL):
        self.transport = transport
        self.agent_id = agent_id
        self.projects = projects
        self.links = links
        self.unfiled_project = unfiled_project
        self.node = node
        self.ttl = ttl
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {}
        self._loaded_at = 0.0
        self._error = ""
        self.defaults: dict = {}

    def _place(self, key: str) -> tuple[dict, dict | None, str]:
        """Which project a session belongs to, and the record that proves it."""

        link = self.links.get(key)
        if link is None:
            return ({"id": self.unfiled_project, "name": UNFILED_PROJECT_NAME, "root": ""},
                    None, "")
        project = self.projects.get(link["project_id"]) or {
            "id": link["project_id"], "name": link["project_id"], "root": ""}
        return project, link, str(link.get("session") or "")

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._loaded_at < self.ttl and self._rooms:
                return
            try:
                result = self.transport.call("sessions.list", {}, timeout=20.0)
            except GatewayError as exc:
                self._error = str(exc)
                self._loaded_at = time.time()
                return
            self._error = ""
            self.defaults = result.get("defaults") or {}
            prefix = f"agent:{self.agent_id}:"
            rooms: dict[str, Room] = {}
            taken: dict[tuple[str, str], str] = {}
            for entry in result.get("sessions") or []:
                key = str((entry or {}).get("key") or "")
                # One configured agent identity only. A session belonging to
                # any other agent is not addressable through this adapter.
                if not key or (key != self.agent_id and not key.startswith(prefix)):
                    continue
                project, link, wanted = self._place(key)
                slug = wanted or session_slug(key, self.agent_id)
                if not ROOM_ID_RE.match(f"{project['id']}/{slug}"):
                    slug = session_slug(key, self.agent_id)
                if taken.get((project["id"], slug), key) != key:
                    slug = f"{slug[:86]}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"
                taken[(project["id"], slug)] = key
                rooms[f"{project['id']}/{slug}"] = Room(
                    project_id=project["id"], project_name=project["name"],
                    project_root=project["root"], session=slug, session_key=key,
                    entry=entry or {}, node=self.node, record=link,
                    unfiled=link is None,
                )
            self._rooms = rooms
            self._loaded_at = time.time()

    @property
    def error(self) -> str:
        return self._error

    def rooms(self) -> list[Room]:
        self.refresh()
        return sorted(self._rooms.values(), key=lambda room: room.updated_ms, reverse=True)

    def by_project(self) -> list[tuple[dict, list[Room]]]:
        """Rooms grouped by the project they actually belong to.

        Real projects first, most recently active first; the Unfiled namespace
        always last so it never displaces someone's own work.
        """

        grouped: dict[str, list[Room]] = {}
        for room in self.rooms():
            grouped.setdefault(room.project_id, []).append(room)
        out: list[tuple[dict, list[Room]]] = []
        for project_id, members in grouped.items():
            project = self.projects.get(project_id) or {
                "id": project_id,
                "name": (UNFILED_PROJECT_NAME if project_id == self.unfiled_project
                         else project_id),
                "root": "",
            }
            out.append((project, members))
        real = [pair for pair in out if pair[0]["id"] != self.unfiled_project]
        unfiled = [pair for pair in out if pair[0]["id"] == self.unfiled_project]
        real.sort(key=lambda pair: (pair[1][0].last_active if pair[1] else "",
                                    pair[0]["name"]), reverse=True)
        return real + unfiled

    def room(self, room_id: str) -> Room | None:
        self.refresh()
        found = self._rooms.get(room_id)
        if found is None:
            self.refresh(force=True)
            found = self._rooms.get(room_id)
        return found

    def room_for_key(self, session_key: str) -> Room | None:
        self.refresh()
        for room in self._rooms.values():
            if room.session_key == session_key:
                return room
        return None


def read_projects(registry_path: Path) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Read every registry project and every *verified* openclaw origin.

    A session is linked to a project only when that project's own
    ``*.origins.json`` names ``runtime: "openclaw"`` and an exact session key.
    Nothing is written, no project is created, and an unlinked conversation is
    never filed under somebody's real project.
    """

    projects: dict[str, dict] = {}
    links: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
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
                if str(origin.get("runtime") or "") != "openclaw":
                    continue
                key = str(origin.get("session_key") or origin.get("session_id") or "")
                if not key:
                    continue
                if key in links and links[key]["project_id"] != project_id:
                    # Two projects claim one conversation. Neither is silently
                    # preferred; the first reading wins and the clash is named.
                    warnings.append(
                        f"{key} is claimed by both {links[key]['project_id']} and "
                        f"{project_id}; showing it under {links[key]['project_id']}")
                    continue
                links[key] = {
                    "project_id": project_id,
                    "session": session,
                    "record_path": str(sidecar),
                    "checkpoint": None,
                    "status": "",
                    "title": "",
                }
    return projects, links, warnings


# ---------------------------------------------------------------------------
# history projection
# ---------------------------------------------------------------------------

def _block_items(message: dict, index_hint: int) -> list[dict]:
    """Project one gateway chat message into console-shaped items.

    One item per content block. Ids are the exact native message id, suffixed
    with the block index only when a message really carries several blocks, so
    every id here traces back to something the gateway reported.
    """

    meta = message.get("__openclaw") or {}
    message_id = str(meta.get("id") or message.get("id") or "")
    mirror = str(meta.get("mirrorIdentity") or "")
    turn_id = mirror.split(":", 1)[0] if mirror else ""
    seq = meta.get("seq")
    role = str(message.get("role") or "")
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    out: list[dict] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        item_id = message_id if len(blocks) == 1 else f"{message_id}:{position}"
        base = {
            "id": item_id,
            "message_id": message_id,
            "block_index": position,
            "turn_id": turn_id,
            "seq": seq,
            "role": role,
            "at": meta.get("recordTimestampMs") or message.get("timestamp"),
        }
        if kind == "text" and role == "user":
            base.update({"type": "userMessage", "text": str(block.get("text") or "")})
            idempotency = str(meta.get("idempotencyKey") or message.get("idempotencyKey") or "")
            # Only an id this adapter minted is reported back as ours.
            if idempotency.startswith(f"{ADAPTER_NAME}:"):
                # OpenClaw appends :user to its transcript mirror identity.
                base["client_id"] = idempotency.split(":", 1)[1].removesuffix(":user")
        elif kind == "text":
            base.update({"type": "agentMessage", "text": str(block.get("text") or ""),
                         "phase": block.get("phase") or (
                             "final_answer" if message.get("stopReason") == "stop" else None),
                         "questions": [],
                         "model": message.get("model"), "provider": message.get("provider")})
        elif kind == "reasoning":
            summary = block.get("summary")
            base.update({"type": "reasoning",
                         "summary": [str(s) for s in summary][:8]
                         if isinstance(summary, list)
                         else [str(block.get("text") or "")][:1]})
        elif kind == "toolCall":
            base.update({"type": "mcpToolCall", "tool": str(block.get("name") or ""),
                         "server": "", "status": "called", "duration_ms": None,
                         "call_id": str(block.get("id") or block.get("toolCallId") or ""),
                         "arguments": block.get("arguments") or {}})
        elif kind == "toolResult":
            text = block.get("text")
            base.update({"type": "functionCallOutput",
                         "name": str(block.get("toolName") or block.get("name") or ""),
                         "output": text if isinstance(text, str)
                         else json.dumps(block.get("content"))[:4000],
                         "call_id": str(block.get("toolCallId") or message.get("toolCallId")
                                        or block.get("id") or ""),
                         "is_error": bool(message.get("isError") or block.get("isError"))})
        elif kind == "image":
            base.update({"type": "attachment", "mime": str(block.get("mimeType") or ""),
                         "note": "image attachments are listed, not served, by this adapter"})
        else:
            base.update({"type": kind or "unknown",
                         "raw_keys": sorted(k for k in block if k != "type")[:12]})
        out.append(base)
    if not out:
        out.append({"id": message_id or f"message-{index_hint}", "message_id": message_id,
                    "block_index": 0, "turn_id": turn_id, "seq": seq, "role": role,
                    "type": "unknown", "raw_keys": []})
    return out


def _settle_tool_items(items: list[dict]) -> None:
    """A matching native result completes a call; age alone proves nothing."""
    results = {(item.get("turn_id"), item.get("call_id")): item for item in items
               if item.get("type") == "functionCallOutput" and item.get("call_id")}
    for item in items:
        result = results.get((item.get("turn_id"), item.get("call_id")))
        if item.get("type") == "mcpToolCall" and result is not None:
            item["status"] = "failed" if result.get("is_error") else "completed"


def _item_text(item: dict) -> str:
    for field in ("text", "output"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value
    summary = item.get("summary")
    if isinstance(summary, list):
        return "\n".join(str(s) for s in summary)
    return ""


def _search_kind(item: dict) -> str:
    kind = item.get("type")
    if kind == "userMessage":
        return "human"
    if kind == "agentMessage":
        return "final"
    return "other"


def _snippet(text: str, needle: str, width: int = 160) -> str:
    if not needle:
        return text[:width]
    position = text.casefold().find(needle)
    if position < 0:
        return text[:width]
    start = max(0, position - width // 3)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class RivetService:
    def __init__(self, config: argparse.Namespace, transport: Transport):
        self.config = config
        self.transport = transport
        self.state_dir = Path(config.state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.journal = Journal(self.state_dir / "ux46-agent3.sqlite3")
        self.files = files.FileStore(self.state_dir / "attachments")
        self.events = Events()
        self.csrf_token = secrets.token_urlsafe(32)
        self.started_at = time.time()
        registry = Path(config.registry).expanduser()
        projects, links, warnings = read_projects(registry)
        if config.unfiled_project in projects:
            raise SystemExit(
                f"the registry already has a project called '{config.unfiled_project}'; "
                "choose another --unfiled-project so real work is never mixed with the "
                "internal unfiled namespace")
        self.projects = projects
        self.vault_warnings = warnings
        self.catalog = Catalog(
            transport, agent_id=config.agent_id, projects=projects, links=links,
            unfiled_project=config.unfiled_project, node=config.node,
        )
        self.identity: dict = {}
        self.agents: dict = {}
        self._send_lock = threading.Lock()
        # One command at a time, so two identical calls cannot both create.
        self._command_lock = threading.Lock()
        self._queue_wake = threading.Event()
        self._stop = threading.Event()
        self._subscribed: set[str] = set()
        # Run state the gateway reported on its own stream, per session key.
        # Nothing here is inferred; an entry appears only when a push carried it.
        self._active_runs: dict[str, dict] = {}
        self._worker = threading.Thread(target=self._queue_loop, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            self.identity = self.transport.call("agent.identity.get", {}, timeout=15.0)
        except GatewayError as exc:
            self.identity = {"error": str(exc)}
        try:
            # Read-only, and only to *report* which agent the gateway treats as
            # default. The configured identity is never silently changed to it.
            self.agents = self.transport.call("agents.list", {}, timeout=15.0)
        except GatewayError as exc:
            self.agents = {"error": str(exc)}
        # A command left mid-flight by a restart is named, never replayed: the
        # next identical request reconciles it from the gateway's session list.
        pending = self.journal.unsettled_commands()
        if pending and not self.config.quiet:
            sys.stderr.write(
                f"ux46-agent3: {len(pending)} command(s) have no confirmed outcome; "
                "each will be re-checked, never reissued: "
                + ", ".join(f"{row['name']}->{row['destination_key']}" for row in pending)
                + "\n")
        default_id = str(self.agents.get("defaultId") or "")
        if default_id and default_id != self.config.agent_id and not self.config.quiet:
            sys.stderr.write(
                f"ux46-agent3: staying on the configured agent '{self.config.agent_id}'; "
                f"the gateway's default is '{default_id}'\n")
        self.catalog.refresh(force=True)
        # Re-subscribe whatever this adapter was showing before it restarted.
        for session_key in self.journal.attached().values():
            self._subscribe(session_key)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue_wake.set()

    def on_gateway_event(self, event: str, payload: dict) -> None:
        """Relay one gateway push as an event the console already understands.

        The console's poll loop refreshes a transcript on ``type: "native"``
        and nothing else, so a push is normalised to that shared type. The
        gateway's own event name is kept verbatim in ``method`` and
        ``gateway_event``: nothing is renamed into a turn lifecycle, and no
        ``turn/started`` or ``turn/completed`` is ever manufactured here.
        """

        if event == "transport":
            self._subscribed.clear()
            self._active_runs.clear()
            self.events.publish({"type": "connection", "state": str(payload.get("state") or ""),
                                 "global": True,
                                 "detail": "the gateway stream dropped; history may be behind"})
            return
        session_key = str(payload.get("sessionKey") or "")
        # This callback runs on the sidecar's response reader. Never make a
        # gateway call here: waiting for that reader's own response deadlocks
        # it and delays every concurrent send. Catalog misses are picked up by
        # the normal workspace/history refresh path.
        room = next((r for r in list(self.catalog._rooms.values())
                     if r.session_key == session_key), None)
        if room is None:
            return
        if event not in ("session.message", "sessions.changed"):
            return
        # Run state, only when the push actually carried it.
        run_state = None
        if "hasActiveRun" in payload or "activeRunIds" in payload:
            run_state = {"active": bool(payload.get("hasActiveRun")),
                         "run_ids": list(payload.get("activeRunIds") or []),
                         "observed_at": time.monotonic()}
            self._active_runs[session_key] = run_state
        self.events.publish({
            "type": "native",
            # The gateway's own name for what happened, unchanged.
            "method": event,
            "gateway_event": event,
            "runtime": "openclaw",
            "room": room.id,
            "session_key": session_key,
            "message_id": str(payload.get("messageId") or ""),
            "message_seq": payload.get("messageSeq"),
            "phase": str(payload.get("phase") or ""),
            # True when this push also changed room state the console reads
            # separately (whether a run is in flight). A client that does not
            # know this key simply refreshes the transcript, which is correct.
            "lifecycle": run_state is not None,
            "active_run": None if run_state is None else run_state["active"],
        })

    # -- rooms -------------------------------------------------------------
    def require_room(self, room_id: str) -> Room:
        if not ROOM_ID_RE.match(room_id or ""):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_room", "that is not a room id")
        room = self.catalog.room(room_id)
        if room is None:
            raise AdapterError(
                HTTPStatus.NOT_FOUND, "room_unknown",
                "the gateway's session list has no such session for this agent",
                {"agent_id": self.config.agent_id, "gateway_error": self.catalog.error},
            )
        return room

    def room_state(self, room: Room) -> dict:
        payload = room.as_json()
        attached = room.id in self.journal.attached()
        # The console's own vocabulary, so Continue here and Send actually
        # work: it reads `idle` to offer Continue and `atlas_owned` to enable
        # the composer. Here those words mean only whether this UX46 view is
        # following the session. They are not a claim of exclusive ownership —
        # `scope`, `exclusive` and `adapter_state` say what is really true, and
        # Agent3's gateway and every other client are unaffected either way.
        payload["ownership"] = {
            "state": "atlas_owned" if attached else "idle",
            "atlas_owned": attached,
            "detected": True,
            "scope": "ux46_view",
            "exclusive": False,
            "adapter_state": "attached" if attached else "detached",
            "detail": ("this UX46 view is following the session; Agent3's gateway and its "
                       "other clients are unaffected either way"),
            "also_open_as": [],
        }
        payload["submissions"] = self.journal.recent(room.id, limit=10)
        payload["pending"] = self.journal.queue_list(room.id)
        payload["draft"] = self.journal.draft(room.id)
        payload["approvals"] = []
        live = self._active_runs.get(room.session_key) or {}
        run_ids = [str(value) for value in (live.get("run_ids") or []) if value]
        running = bool(live["active"] if "active" in live
                       else room.entry.get("hasActiveRun"))
        payload["native"] = {
            # The console addresses a conversation by `native.thread_id`; for
            # this runtime that *is* the exact gateway session key, and the
            # submit path checks the two agree.
            "thread_id": room.session_key,
            "session_key": room.session_key,
            "session_id": str(room.entry.get("sessionId") or ""),
            "model": room.entry.get("model"),
            "model_provider": room.entry.get("modelProvider"),
            "agent_runtime": room.entry.get("agentRuntime"),
            "reasoning_effort": room.entry.get("thinkingLevel"),
            "context_tokens": room.entry.get("contextTokens"),
            "total_tokens": room.entry.get("totalTokens"),
            "status": room.entry.get("status"),
            "source": str((room.entry.get("origin") or {}).get("provider") or ""),
            # A run id only when the gateway actually reported one on its own
            # stream. No turn id is ever invented to make an indicator move.
            "active_turn": run_ids[0] if (running and run_ids) else "",
            "active_run": running,
            "active_run_ids": run_ids,
            "updated_at": room.entry.get("updatedAt"),
            "started_at": room.entry.get("startedAt"),
            "origin": room.entry.get("origin"),
            "archived": bool(room.entry.get("archived")),
        }
        payload["capabilities"] = CAPABILITIES
        payload["unsupported"] = dict(UNSUPPORTED_ACTIONS)
        payload["commands"] = [dict(entry) for entry in COMMAND_HELP]
        return payload

    def workspace(self) -> dict:
        """One row per project a conversation actually belongs to.

        Unfiled is a namespace of this adapter, listed last and labelled as
        such; it is never presented as a Session Vault project.
        """

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
                    "group_role": "human",
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
                "root": project["root"],
                "last_active": members[0].last_active if members else "",
                "recency_source": "gateway" if members else "none",
                "pinned": [],
                "suggested": suggested,
                "more_total": max(0, len(members) - len(suggested)),
                "agent_work_total": 0,
                "hidden_total": 0,
                "record_total": len(members),
                "conversation_total": len(members),
                "unfiled": unfiled,
                "vault_project": not unfiled,
                "note": ("conversations on this gateway with no verified Vault origin; "
                         "no project was created for them" if unfiled else ""),
            })
        return {
            "projects": projects,
            "suggest_limit": int(self.config.suggest),
            "native_metadata": not self.catalog.error,
            "prefs": {},
            "gateway_error": self.catalog.error,
            "vault_warnings": self.vault_warnings[:5],
        }

    def project_rows(self) -> list[dict]:
        rows = []
        for project, members in self.catalog.by_project():
            unfiled = project["id"] == self.config.unfiled_project
            rows.append({
                "id": project["id"], "name": project["name"], "root": project["root"],
                "sessions": len(members), "controllable": len(members),
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
                     if needle in " ".join([room.id, room.title, room.session_key]).casefold()]
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
        requested_at = time.monotonic()
        size = max(1, min(int(limit), 200))
        params: dict = {"sessionKey": room.session_key, "limit": size,
                        "agentId": self.config.agent_id}
        if cursor:
            try:
                params["offset"] = max(0, int(cursor))
            except ValueError:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_cursor",
                                   "a history cursor is an offset this adapter issued")
        try:
            page = self.transport.call("chat.history", params, timeout=25.0)
        except GatewayError as exc:
            # An unreadable history is not an empty one, and is never shown as
            # "no messages".
            return {
                "room": room.id, "session_key": room.session_key,
                "thread_id": room.session_key, "items": [], "next_cursor": None,
                "backwards_cursor": None, "complete": False, "page_size": size,
                "direction": direction, "source": "unavailable", "unavailable": True,
                "retryable": True, "error_code": exc.code, "message": str(exc),
            }
        info = page.get("sessionInfo") or {}
        previous = self._active_runs.get(room.session_key) or {}
        # A successful history read repairs a missed lifecycle push. A newer
        # event received while this read was in flight takes precedence.
        if ("hasActiveRun" in info and
                previous.get("observed_at", 0) <= requested_at):
            self._active_runs[room.session_key] = {
                "active": bool(info["hasActiveRun"]),
                "run_ids": list(info.get("activeRunIds") or []),
                "observed_at": requested_at,
            }
        messages = page.get("messages") or []
        items: list[dict] = []
        for index, message in enumerate(messages):
            if isinstance(message, dict):
                items.extend(_block_items(message, index))
        self.attach_sent_files(items)
        _settle_tool_items(items)
        next_offset = page.get("nextOffset")
        total = page.get("totalMessages")
        offset = page.get("offset")
        return {
            "room": room.id,
            "session_key": room.session_key,
            "thread_id": room.session_key,
            # Gateway messages arrive chronologically. UX46's shared API
            # requests newest first and reverses them for reading order.
            "items": list(reversed(items)) if direction == "desc" else items,
            "next_cursor": str(next_offset) if isinstance(next_offset, int) else None,
            "backwards_cursor": (str(max(0, int(offset) - size))
                                 if isinstance(offset, int) and offset > 0 else None),
            "complete": not bool(page.get("hasMore")),
            "page_size": size,
            "direction": direction,
            "source": "empty" if not messages and not cursor else "chat.history",
            "note": ("this gateway session has no visible history yet"
                     if not messages and not cursor else ""),
            "total": total,
            "earlier_available": bool(offset) if isinstance(offset, int) else None,
            "later_available": bool(page.get("hasMore")),
            "session_info": page.get("sessionInfo") or {},
        }

    def attach_sent_files(self, items: list[dict]) -> None:
        """Show the files that went with a message this adapter sent.

        The gateway's own history does not carry the console's managed file
        ids, so the association is made the only honest way available: through
        the idempotency key this adapter minted, which is the client id it
        journaled the attachments under. A message from any other client is
        left exactly as the gateway reported it.
        """

        for item in items:
            client_id = str(item.get("client_id") or "")
            if not client_id or item.get("type") != "userMessage":
                continue
            submission = self.journal.submission(client_id)
            if not submission or not submission.get("attachments"):
                continue
            records = []
            for entry in submission["attachments"]:
                try:
                    records.append(self.files.get(str(entry.get("file_id") or ""),
                                                  room=submission["room"]))
                except files.FileStoreError:
                    records.append({"id": str(entry.get("file_id") or ""),
                                    "missing": True})
            item["attachments"] = records
            item["attachments_source"] = "adapter_journal"

    def search_history(self, room: Room, query: str, kinds: tuple[str, ...],
                       limit: int) -> dict:
        try:
            page = self.transport.call("chat.history", {
                "sessionKey": room.session_key, "limit": HISTORY_SCAN_CAP,
                "agentId": self.config.agent_id,
            }, timeout=30.0)
        except GatewayError as exc:
            return {"hits": [], "total": 0, "source": "unavailable", "searched_items": 0,
                    "retryable": True, "error_code": exc.code,
                    "note": f"this session's history could not be read just now: {exc}"}
        items: list[dict] = []
        for index, message in enumerate(page.get("messages") or []):
            if isinstance(message, dict):
                items.extend(_block_items(message, index))
        needle = (query or "").strip().casefold()
        hits = []
        for index, item in enumerate(items):
            kind = _search_kind(item)
            if kind not in kinds:
                continue
            text = _item_text(item)
            if needle and needle not in text.casefold():
                continue
            hits.append({"item_id": item["id"], "turn_id": item.get("turn_id", ""),
                         "type": item.get("type", ""), "phase": item.get("phase"),
                         "kind": kind, "index": index, "snippet": _snippet(text, needle)})
        return {
            "hits": list(reversed(hits))[: max(1, min(int(limit), 200))],
            "total": len(hits),
            "source": "chat.history",
            "searched_items": len(items),
            # Whole-history search is bounded by one page; say so rather than
            # implying every message was read.
            "complete": len(page.get("messages") or []) < HISTORY_SCAN_CAP,
            "scan_cap": HISTORY_SCAN_CAP,
        }

    # -- attach / release --------------------------------------------------
    def _subscribe(self, session_key: str) -> bool:
        if session_key in self._subscribed:
            return True
        try:
            self.transport.call("sessions.messages.subscribe",
                                {"key": session_key, "agentId": self.config.agent_id},
                                timeout=15.0)
        except GatewayError:
            return False
        self._subscribed.add(session_key)
        return True

    def attach(self, room: Room) -> dict:
        streaming = self._subscribe(room.session_key)
        record = self.journal.attach(room.id, room.session_key)
        self._queue_wake.set()
        self.events.publish({"type": "ownership", "room": room.id, "state": "attached"})
        return {
            "attached": {**record, "streaming": streaming},
            "continued": {**record, "streaming": streaming},
            "scope": "ux46_view",
            "message": ("This UX46 view now follows Agent3's session. Nothing was started, "
                        "taken over or reconfigured on the gateway."
                        + ("" if streaming else
                           " The live stream could not be subscribed, so history is polled.")),
            "room": self.room_state(room),
        }

    def release(self, room: Room) -> dict:
        unsettled = self.journal.unsettled(room.session_key)
        if unsettled:
            raise AdapterError(
                HTTPStatus.CONFLICT, "send_unsettled",
                "a send for this session has no recorded outcome yet, so this view will "
                "not be detached while it is unknown",
                {"unsettled": unsettled},
            )
        if room.session_key in self._subscribed:
            try:
                self.transport.call("sessions.messages.unsubscribe",
                                    {"key": room.session_key,
                                     "agentId": self.config.agent_id}, timeout=15.0)
            except GatewayError:
                pass
            self._subscribed.discard(room.session_key)
        was_attached = self.journal.detach(room.id)
        self.events.publish({"type": "ownership", "room": room.id, "state": "detached"})
        return {
            "released": room.session_key,
            "session_key": room.session_key,
            "thread_id": room.session_key,
            "room": room.id,
            "release_state": "detached" if was_attached else "not_attached",
            "gateway_untouched": True,
            "native_work_continues": True,
            "message": ("This UX46 view stopped following the session. Agent3's gateway "
                        "kept running, no run was aborted, and every other client kept "
                        "its connection."
                        if was_attached else
                        "This view was not following that session, so nothing changed. "
                        "Agent3's gateway was not touched."),
        }

    def stop_turn(self, room: Room) -> dict:
        try:
            result = self.transport.call(
                "chat.abort", {"sessionKey": room.session_key,
                               "agentId": self.config.agent_id}, timeout=20.0)
        except GatewayUncertain as exc:
            raise AdapterError(HTTPStatus.ACCEPTED, "uncertain", str(exc))
        except GatewayError as exc:
            raise AdapterError(HTTPStatus.BAD_GATEWAY, exc.code, str(exc))
        self.events.publish({"type": "turn", "room": room.id, "state": "abort_requested"})
        return {"aborted": result.get("aborted"), "run_ids": result.get("runIds") or [],
                "room": room.id, "session_key": room.session_key,
                "message": "The gateway was asked to stop this session's current run."}

    # -- sending -----------------------------------------------------------
    def idempotency_key(self, client_id: str) -> str:
        return f"{ADAPTER_NAME}:{client_id}"

    def dispatch(self, room: Room, client_id: str, text: str,
                 attachments: list[dict] | None = None) -> dict:
        """Journal first, call once, and never invent a delivery state."""

        key = self.idempotency_key(client_id)
        wanted = attachments or []
        try:
            submission, created = self.journal.reserve(
                client_id, room.id, room.session_key, text, key, wanted)
        except DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch", str(exc), exc.detail)
        if not created:
            # A known id: report what was journaled and call the gateway zero
            # more times, whatever that outcome was.
            return {"submission": submission, "duplicate": True, "dispatched": False}
        try:
            payload = self.native_attachments(room, wanted)
        except AdapterError:
            # The bytes were never handed over, so nothing was dispatched and
            # nothing was dropped: the files stay downloadable here.
            self.journal.settle(client_id, FAILED,
                                detail="the attachments were refused before sending")
            raise
        params = {
            "sessionKey": room.session_key,
            "agentId": self.config.agent_id,
            "message": text,
            # The gateway's own dedupe key. One client id is one run id,
            # for the life of the journal row.
            "idempotencyKey": key,
            # Explicit, on every send: answer on the internal webchat channel
            # and never republish into this session's origin channel.
            "deliver": False,
        }
        if payload:
            params["attachments"] = payload
        try:
            result = self.transport.call(
                "chat.send", params, timeout=float(self.config.send_timeout))
        except GatewayUncertain as exc:
            settled = self.journal.settle(client_id, UNCERTAIN, detail=str(exc))
            self.events.publish({"type": "submission", "room": room.id, "status": UNCERTAIN})
            return {"submission": settled, "dispatched": True, "uncertain": True}
        except GatewayError as exc:
            settled = self.journal.settle(client_id, FAILED, detail=str(exc))
            self.events.publish({"type": "submission", "room": room.id, "status": FAILED})
            return {"submission": settled, "dispatched": False, "failed": True,
                    "error_code": exc.code}
        ack = self.read_acknowledgement(result, key)
        if ack is None:
            # The call returned, but not with the run acknowledgement the
            # gateway documents. That is an unknown outcome, not a success:
            # it is recorded uncertain and is never resent.
            detail = ("the gateway answered chat.send without an accepted run "
                      f"acknowledgement (got {json.dumps(result)[:200]})")
            settled = self.journal.settle(client_id, UNCERTAIN, detail=detail)
            self.events.publish({"type": "submission", "room": room.id, "status": UNCERTAIN})
            return {"submission": settled, "dispatched": True, "uncertain": True,
                    "reason": "no_run_acknowledgement"}
        settled = self.journal.settle(
            client_id, ACCEPTED, run_id=ack["run_id"],
            attempt_id=ack["attempt_id"], ack_status=ack["status"])
        self.events.publish({"type": "submission", "room": room.id, "status": ACCEPTED})
        return {"submission": settled, "dispatched": True, "native": ack,
                "delivery": DELIVERY_BOUNDARY}

    def read_acknowledgement(self, result: dict, expected_key: str) -> dict | None:
        """The documented accepted-run ack, or None when it is not one.

        A fresh admission answers ``status: "accepted"`` with the run id; a
        replay of a known run answers ``in_flight``. An empty body, a missing
        run id, an unknown status, or a run id belonging to some other send is
        not an acknowledgement and must never be journaled as accepted.
        """

        if not isinstance(result, dict):
            return None
        run_id = result.get("runId")
        status = result.get("status")
        if not isinstance(run_id, str) or not run_id:
            return None
        if not isinstance(status, str) or status not in ACCEPTED_RUN_STATUSES:
            return None
        if run_id != expected_key:
            return None
        return {
            "run_id": run_id,
            "status": status,
            "attempt_id": str(result.get("attemptId") or ""),
            "session_key": str(result.get("sessionKey") or ""),
            "expires_at_ms": result.get("expiresAtMs"),
            "turn_kind": str(result.get("turnKind") or ""),
        }

    # -- commands ----------------------------------------------------------
    def destination_key(self, client_id: str) -> str:
        """The one key this command attempt may ever create.

        Derived from the client id rather than drawn at random, so a lost
        journal row still cannot produce a second conversation: the same
        attempt always resolves to the same key.
        """

        digest = hashlib.sha256(f"{ADAPTER_NAME}:{client_id}".encode()).hexdigest()[:12]
        return f"agent:{self.config.agent_id}:{ADAPTER_NAME}-new-{digest}"

    def command(self, room: Room, body: dict) -> dict:
        """Run one supported slash command, or refuse it by name.

        Nothing here is ever forwarded to the model as text. An unsupported
        command is answered `unsupported` with the reason. /new accepts an
        optional title and preserves the linked project.
        """

        raw = body.get("command")
        client_id = str(body.get("client_id", ""))
        target = str(body.get("thread_id") or body.get("session_key") or "")
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_BODY:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_command",
                               "a command must be short text")
        # The source is checked before anything is written down, so a command
        # aimed at another conversation never reserves a key here.
        if target and target != room.session_key:
            raise AdapterError(HTTPStatus.CONFLICT, "wrong_target",
                               "that command was written for a different gateway session",
                               {"expected": room.session_key})
        text = raw.strip()
        parts = text.split(None, 1)
        name = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        name = {"/reasoning": "/effort", "/reasononing": "/effort", "/streer": "/steer"}.get(name, name)
        if name in ("/model", "/effort", "/compact"):
            if name == "/compact" and args:
                return {"command": {"name": name, "state": "needs_input", "message": "Use /compact on its own; this gateway does not accept a focus prompt."}}
            if name != "/compact" and not args:
                self.catalog.refresh(force=True)
                if self.catalog.error:
                    raise AdapterError(503, "gateway_unreachable", self.catalog.error)
                current = self.catalog.room(room.id) or room
                return {"command": {"name": name, "state": COMPLETED,
                        "native": self.room_state(current)["native"],
                        "message": str(current.entry.get("model" if name == "/model" else "thinkingLevel") or "Not reported")}}
            if not CLIENT_ID_RE.match(client_id):
                raise AdapterError(400, "bad_client_id", "A command needs a stable client id.")
            with self._command_lock:
                try:
                    record, created = self.journal.reserve_command(client_id, room.id, room.session_key, text, name, body_hash(text), "")
                except DuplicateMismatch as exc:
                    raise AdapterError(409, "duplicate_mismatch", "This command id was already used.") from exc
                if not created:
                    return {"command": {"name": name, "state": record["status"] if record["status"] in (COMPLETED, FAILED) else UNCERTAIN,
                            "message": record["detail"] or "The earlier command was not repeated; its outcome is unconfirmed."}}
                method = "sessions.compact" if name == "/compact" else "sessions.patch"
                params = {"key": room.session_key, "agentId": self.config.agent_id}
                if name != "/compact":
                    params["model" if name == "/model" else "thinkingLevel"] = args
                try:
                    result = self.transport.call(method, params, timeout=120.0 if name == "/compact" else 20.0)
                    state = COMPLETED if result.get("ok") is True else UNCERTAIN
                    message = ("Native compaction completed." if result.get("compacted") else "Native compaction: " + str(result.get("reason") or "completed")) if name == "/compact" else "Native session setting updated."
                    if state == UNCERTAIN:
                        message = "The gateway did not confirm this command. It will not be repeated automatically."
                except GatewayUncertain:
                    state, message = UNCERTAIN, "The gateway did not answer in time. The command was not repeated."
                except GatewayError as exc:
                    state, message = FAILED, str(exc)
                self.journal.settle_command(client_id, state, detail=message)
                return {"command": {"name": name, "state": state, "message": message, "sent_as_text": False}}
        if name == "/help":
            return {"command": {"name": name, "state": COMPLETED,
                    "supported": list(COMMANDS_SUPPORTED), "help": list(COMMAND_HELP),
                    "unsupported": dict(COMMAND_UNSUPPORTED), "sent_as_text": False}}
        if name in ("/status", "/refresh"):
            if args:
                return {"command": {"name": name, "state": "needs_input", "message": name + " takes no arguments."}}
            if name == "/refresh":
                # Re-reading over the existing bridge preserves all in-flight
                # receipts and subscriptions. A dead bridge may be replaced.
                if not self.transport.status().get("connected"):
                    self.transport.reconnect()
            self.catalog.refresh(force=True)
            if self.catalog.error:
                raise AdapterError(503, "gateway_unreachable", self.catalog.error)
            current = self.catalog.room(room.id)
            if current is None:
                raise AdapterError(404, "unknown_room", "This conversation is no longer in the gateway catalog.")
            native = self.room_state(current)["native"]
            return {"command": {"name": name, "state": COMPLETED, "native": native,
                    "message": "Gateway connection and conversation state refreshed." if name == "/refresh" else "Gateway status read.",
                    "sent_as_text": False}}
        if name != "/new":
            reason = COMMAND_UNSUPPORTED.get(name, "No native operation is exposed by this adapter.")
            return {"command": {"name": name, "state": "unsupported", "message":
                    f"{name} is not available through Agent3's gateway connection: {reason}. It was not sent as a message.",
                    "supported": list(COMMANDS_SUPPORTED), "sent_as_text": False}}
        if len(args) > 120:
            raise AdapterError(400, "bad_title", "Use a conversation name of 120 characters or fewer.")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a command needs a stable client id")

        # One command at a time. Two identical calls arriving together must not
        # both reach sessions.create, and the second must see the first's row.
        with self._command_lock:
            return self._new_conversation(room, client_id, text, args,
                project=({"id": room.project_id, "name": room.project_name, "root": room.project_root} if not room.unfiled else None))

    def _new_conversation(self, room: Room, client_id: str, text: str, title: str = "", project=None) -> dict:
        destination = self.destination_key(client_id)
        payload = body_hash(f"/new\x00{title}\x00{room.session_key}")
        try:
            record, created = self.journal.reserve_command(
                client_id, room.id, room.session_key, text, "/new", payload, destination)
        except DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                               "that command id was already used for a different command "
                               "or a different conversation", exc.detail)
        if not created:
            if record["status"] == COMPLETED:
                # Known success: the same new conversation, never a second one.
                return self._new_result(room, record, recovered=True)
            if record["status"] == FAILED:
                return {"command": {
                    "name": "/new", "state": "failed",
                    "message": ("The gateway refused to start a new conversation: "
                                + (record["detail"] or "no reason given")
                                + ". This conversation is untouched."),
                    "source_room": room.id, "source_intact": True}}
            # Dispatching or uncertain: a first attempt may already have
            # created it. Reconcile from the gateway's own session list and
            # never call sessions.create again for this id.
            return self._reconcile_new(room, record)

        try:
            result = self.transport.call("sessions.create", {
                "key": destination,
                "agentId": self.config.agent_id,
                "label": title or "New Agent3 conversation",
                # No task, no message, no parent, no fork, no worktree and no
                # command hooks: a fresh context, no model run, nothing touched.
            }, timeout=float(self.config.command_timeout))
        except GatewayUncertain as exc:
            self.journal.settle_command(client_id, UNCERTAIN, detail=str(exc))
            return self._reconcile_new(room, self.journal.command_record(client_id))
        except GatewayError as exc:
            self.journal.settle_command(client_id, FAILED, detail=str(exc))
            return {"command": {
                "name": "/new", "state": "failed",
                "message": ("The gateway refused to start a new conversation: "
                            f"{exc}. This conversation is untouched."),
                "error_code": exc.code, "source_room": room.id, "source_intact": True}}

        created_key = str(result.get("key") or "") if isinstance(result, dict) else ""
        if not (isinstance(result, dict) and result.get("ok") is True and created_key):
            # A reply that is not the documented create acknowledgement proves
            # nothing either way, so it is recorded unknown and never retried.
            detail = ("the gateway answered sessions.create without an ok acknowledgement "
                      f"({json.dumps(result)[:200] if result else 'empty'})")
            self.journal.settle_command(client_id, UNCERTAIN, detail=detail)
            return self._reconcile_new(room, self.journal.command_record(client_id))
        # The key the gateway reports is authoritative; it may be a canonical
        # form of the one that was asked for.
        record = self.journal.settle_command(
            client_id, DISPATCHING, created_key=created_key,
            session_id=str(result.get("sessionId") or ""))
        record["run_started"] = bool(result.get("runStarted"))
        if project:
            creation.link(project, title, client_id, "openclaw", created_key,
                          self.config.node, str(Path(project['root']).expanduser()))
            projects, links, _ = read_projects(Path(self.config.registry).expanduser())
            self.catalog.projects, self.catalog.links = projects, links
        return self._reconcile_new(room, record, fresh=True)

    def session_options(self) -> dict:
        projects, _, _ = read_projects(Path(self.config.registry).expanduser())
        return creation.options(projects)

    def create_session(self, body: dict) -> dict:
        projects, _, _ = read_projects(Path(self.config.registry).expanduser())
        try:
            client, title, project = creation.request(body, projects)
        except ValueError as exc:
            raise AdapterError(400, "bad_session", str(exc)) from exc
        dest = project or {"id":self.config.unfiled_project,"name":"Unfiled","root":""}
        token = hashlib.sha256((dest['id']+'\0'+title).encode()).hexdigest()[:20]
        room = Room(project_id=dest['id'],project_name=dest['name'],project_root=dest['root'],
                    session='new-'+token,session_key='new-'+token,entry={},
                    node=self.config.node,record=None,unfiled=not project)
        result = self._new_conversation(room, client, '/new '+title, title, project)
        command = result.get('command', {})
        return {"state":"created" if command.get('state')==COMPLETED else command.get('state','failed'),
                "message":command.get('message',''),"new_room":command.get('new_room')}

    def _reconcile_new(self, room: Room, record: dict, fresh: bool = False) -> dict:
        """Decide what really happened by reading the gateway's session list.

        This is the only recovery path, and it never calls sessions.create. A
        conversation is reported started only once the gateway lists it and this
        view is following it; anything else stays uncertain.
        """

        wanted = record.get("created_key") or record["destination_key"]
        self.catalog.refresh(force=True)
        found = self.catalog.room_for_key(wanted)
        if found is None and record.get("created_key"):
            found = self.catalog.room_for_key(record["destination_key"])
        if found is None:
            unreadable = bool(self.catalog.error)
            self.journal.settle_command(
                record["client_id"], UNCERTAIN,
                detail=(f"the gateway's session list does not show {wanted} yet"
                        if not unreadable else
                        f"the gateway's session list could not be read: "
                        f"{self.catalog.error}"))
            return {"command": {
                "name": "/new", "state": "uncertain",
                "message": ("UX46 asked the gateway for a new conversation and cannot "
                            "confirm the result yet"
                            + (" — its session list could not be read."
                               if unreadable else " — it is not listed yet.")
                            + " Nothing was resent and no second conversation was asked "
                            "for. Run /new again to re-check the same request; this "
                            "conversation is untouched."),
                "session_key": wanted,
                "list_unavailable": unreadable,
                "retry_is_safe": True,
                "source_room": room.id, "source_intact": True}}
        settled = self.journal.settle_command(
            record["client_id"], COMPLETED, created_key=found.session_key,
            session_id=record.get("session_id", ""))
        settled["run_started"] = record.get("run_started", False)
        return self._new_result(room, settled, recovered=not fresh)

    def _new_result(self, room: Room, record: dict, recovered: bool = False) -> dict:
        """Report one started conversation, attaching this view to it.

        Attaching is what makes the new room usable straight away. The
        conversation the command was typed in is never released: it keeps its
        subscription, its history and its attachments.
        """

        key = record.get("created_key") or record["destination_key"]
        self.catalog.refresh()
        created = self.catalog.room_for_key(key)
        if created is None:
            return self._reconcile_new(room, record)
        attached = self.attach(created)
        message = "Started a new Agent3 conversation."
        if recovered:
            message = ("That new conversation was already started, so UX46 opened the "
                       "same one rather than making another.")
        if record.get("run_started"):
            message += (" The gateway reports a run already started in it, which UX46 "
                        "did not ask for.")
        return {
            "command": {
                "name": "/new",
                "state": COMPLETED,
                "new_room": {"id": created.id, "session_key": created.session_key,
                             "session_id": str(created.entry.get("sessionId") or "")},
                "message": message,
                "attached": True,
                "run_started": bool(record.get("run_started")),
                "recovered": recovered,
                # The conversation it was typed in keeps running and stays open.
                "source_room": room.id,
                "source_intact": True,
                "source_released": False,
            },
            "room": attached["room"],
        }

    # -- attachments -------------------------------------------------------
    def checked_attachments(self, room: Room, raw) -> list[dict]:
        """Validate the file ids a browser named, without reading the bytes yet."""

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
            # The exact id the browser named, unchanged. Nothing is rewritten
            # on the way through the queue or an edit.
            clean.append({"file_id": record["id"]})
        if total > GATEWAY_ATTACHMENT_MAX_BYTES:
            raise AdapterError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachments_too_large",
                f"those attachments total {total} bytes; base64 of that does not fit "
                f"the gateway's {GATEWAY_FRAME_MAX_BYTES}-byte frame. Send them across "
                "more than one message; every file stays stored here.")
        return clean

    @staticmethod
    def check_sendable(record: dict) -> None:
        """Refuse before sending what the gateway would refuse mid-turn."""

        size = int(record.get("size") or 0)
        mime = str(record.get("mime") or "")
        if size <= 0:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "attachment_empty",
                               f"{record.get('name')} is empty, and the gateway "
                               "refuses an empty attachment payload")
        if mime.startswith("image/") and size > GATEWAY_IMAGE_MAX_BYTES:
            raise AdapterError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachment_too_large",
                f"{record.get('name')} is {size} bytes; the gateway refuses an image "
                f"over {GATEWAY_IMAGE_MAX_BYTES} bytes. It stays stored and "
                "downloadable here.")
        if size > GATEWAY_ATTACHMENT_MAX_BYTES:
            raise AdapterError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachment_too_large",
                f"{record.get('name')} is {size} bytes; base64 of that does not fit "
                f"the gateway's {GATEWAY_FRAME_MAX_BYTES}-byte frame, so this adapter "
                f"sends at most {GATEWAY_ATTACHMENT_MAX_BYTES} bytes. It stays stored "
                "and downloadable here.")

    def native_attachments(self, room: Room, wanted: list[dict]) -> list[dict]:
        """Turn managed file ids into the gateway's documented attachment form.

        The wire shape is the one the installed build accepts:
        ``{mimeType, fileName, content}`` with base64 content. The managed
        bytes are sent verbatim — nothing is re-encoded, resized or truncated.
        """

        out: list[dict] = []
        total = 0
        for item in wanted:
            file_id = str(item.get("file_id") or "")
            try:
                record, data = self.files.open_download(file_id, room=room.id)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            self.check_sendable(record)
            total += len(data)
            if total > GATEWAY_ATTACHMENT_MAX_BYTES:
                raise AdapterError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "attachments_too_large",
                    "those attachments do not fit one gateway frame; every file is "
                    "still stored here")
            out.append({
                "mimeType": record["mime"],
                "fileName": record["name"],
                # The managed bytes, verbatim. Nothing is re-encoded or resized.
                "content": base64.b64encode(data).decode("ascii"),
            })
        return out

    def submit(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id", ""))
        text = body.get("body")
        target = str(body.get("thread_id") or body.get("session_key") or "")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a submission needs a stable client id")
        if not isinstance(text, str) or (not text.strip() and not body.get("attachments")):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "empty", "there is nothing to send")
        if len(text) > MAX_BODY:
            raise AdapterError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                               "that is too long")
        attachments = self.checked_attachments(room, body.get("attachments"))
        if target and target != room.session_key:
            raise AdapterError(HTTPStatus.CONFLICT, "wrong_target",
                               "that draft was written for a different gateway session",
                               {"expected": room.session_key})
        with self._send_lock:
            return self.dispatch(room, client_id, text, attachments)

    def queue(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id", ""))
        text = body.get("body")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a queued message needs a stable client id")
        if not isinstance(text, str) or len(text) > MAX_BODY or (
                not text.strip() and not body.get("attachments")):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_message",
                               "a queued message needs short text or an attachment")
        attachments = self.checked_attachments(room, body.get("attachments"))
        attached = room.id in self.journal.attached()
        reason = "" if attached else "attach this view before a queued message can send"
        try:
            item, created = self.journal.enqueue(
                client_id, room.id, room.session_key, text, reason, attachments)
        except DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch", str(exc), exc.detail)
        self._queue_wake.set()
        self.events.publish({"type": "queued_message", "room": room.id,
                             "client_id": client_id, "state": item["status"]})
        return {"pending": item, "duplicate": not created}

    def _queue_loop(self) -> None:
        while not self._stop.is_set():
            self._queue_wake.wait(timeout=2.0)
            self._queue_wake.clear()
            attached = self.journal.attached()
            for item in self.journal.queue_ready():
                if self._stop.is_set():
                    return
                if item["room"] not in attached:
                    continue
                room = self.catalog.room(item["room"])
                if room is None:
                    self.journal.queue_mark(item["client_id"], PENDING,
                                            "the gateway no longer lists that session")
                    continue
                self.journal.queue_mark(item["client_id"], DISPATCHING)
                with self._send_lock:
                    try:
                        result = self.dispatch(room, item["client_id"], item["body"],
                                               item["attachments"])
                    except AdapterError as exc:
                        self.journal.queue_mark(item["client_id"], FAILED, exc.message)
                        self.events.publish({"type": "queued_message", "room": room.id,
                                             "client_id": item["client_id"],
                                             "state": FAILED})
                        continue
                status = result["submission"]["status"]
                self.journal.queue_mark(item["client_id"], status,
                                        result["submission"].get("detail", ""))
                self.events.publish({"type": "queued_message", "room": room.id,
                                     "client_id": item["client_id"], "state": status})

    # -- bootstrap ---------------------------------------------------------
    def bootstrap(self, identity: str) -> dict:
        status = self.transport.status()
        return {
            "csrf": self.csrf_token,
            "mode": "proxy",
            "identity": identity,
            "node": self.config.node,
            "public_origin": "",
            "runtime_started": False,
            "started_at": self.started_at,
            "seq": self.events.seq,
            "state_dir": str(self.state_dir),
            "vault_errors": [],
            "remembered_owned": self.journal.attached(),
            "voice": {"enabled": False, "loaded": False, "voices": [],
                      "reason": UNSUPPORTED_ACTIONS["speak"]},
            "voice_default": "",
            "agent": {
                "id": self.config.agent_id,
                # Exactly what the gateway calls itself. Never a name this
                # adapter chose.
                "name": str(self.identity.get("name") or ""),
                "emoji": str(self.identity.get("emoji") or ""),
                "runtime": "openclaw",
                "identity_source": "gateway.agent.identity.get",
                "identity_error": str(self.identity.get("error") or ""),
                # Reported, never acted on: the configured identity is what
                # this adapter addresses, whatever the gateway defaults to.
                "gateway_default_id": str(self.agents.get("defaultId") or ""),
                "is_gateway_default": (str(self.agents.get("defaultId") or "")
                                       == self.config.agent_id),
                "other_agent_ids": [str(a.get("id") or "")
                                    for a in (self.agents.get("agents") or [])
                                    if str(a.get("id") or "") != self.config.agent_id],
            },
            "adapter": {
                "name": ADAPTER_NAME,
                "version": ADAPTER_VERSION,
                "unfiled_project": self.config.unfiled_project,
                "projects": [row["id"] for row in self.project_rows()],
                **status,
                "stream": status.get("transport") == "sidecar",
            },
            "capabilities": {**CAPABILITIES,
                             "stream": status.get("transport") == "sidecar"},
            "unsupported": dict(UNSUPPORTED_ACTIONS),
            "commands": {"supported": list(COMMANDS_SUPPORTED),
                         "help": [dict(entry) for entry in COMMAND_HELP],
                         "unsupported": {name: reason for name, reason
                                         in COMMAND_UNSUPPORTED.items() if reason}},
            "gateway_error": self.catalog.error,
            "vault_warnings": self.vault_warnings[:5],
            # What a browser-originated send can and cannot reach, verified
            # against the installed build rather than assumed.
            "delivery": DELIVERY_BOUNDARY,
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class RivetHandler(BaseHTTPRequestHandler):
    server_version = "Ux46Rivet/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: RivetService

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
        limit = (GATEWAY_MEDIA_MAX_BYTES + MAX_UPLOAD_OVERHEAD
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
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_json", "that body is not an object")
        return parsed

    def _multipart_file(self) -> tuple[str, str, bytes]:
        """Read exactly one uploaded file out of a multipart body.

        The browser supplies the name and content type; both are treated as
        untrusted labels and re-checked by the file store, which sniffs raster
        signatures itself.
        """

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
            data = part.get_payload(decode=True) or b""
            return (filename, str(part.get_content_type() or ""), data)
        raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_upload",
                           "name a file part in that upload")

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
            parsed = urlparse(self.path)
            path = parsed.path
            proxy_prefix = self.service.config.path_prefix
            if proxy_prefix and path.startswith(proxy_prefix):
                path = path[len(proxy_prefix):] or "/"
            if not path.startswith("/api/"):
                raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._check_mutation()
            return self._api(method, path, parse_qs(parsed.query))
        except AdapterError as exc:
            self._error(exc.status, exc.code, exc.message, exc.detail)
        except GatewayUncertain as exc:
            self._error(HTTPStatus.ACCEPTED, "uncertain", str(exc))
        except GatewayUnavailable as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "gateway_unreachable", str(exc))
        except GatewayError as exc:
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
            return self._json(HTTPStatus.OK, service.workspace())

        if method == "GET" and path == "/api/rooms":
            return self._json(HTTPStatus.OK, service.search_rooms(
                get("query"), int(get("limit", "40") or 40), int(get("offset", "0") or 0)))

        if method == "GET" and path == "/api/session-options":
            return self._json(HTTPStatus.OK, service.session_options())
        if method == "POST" and path == "/api/sessions":
            return self._json(HTTPStatus.OK, service.create_session(self._body()))

        if method == "GET" and path == "/api/projects":
            return self._json(HTTPStatus.OK, {"projects": service.project_rows()})

        if method == "GET" and path == "/api/events":
            return self._json(HTTPStatus.OK, service.events.since(
                int(get("after", "0") or 0),
                min(float(get("timeout", "25") or 25), 30.0),
                get("room")))

        if method == "GET" and path == "/api/attention":
            # Nothing here reads a checkpoint, so nothing here claims urgency.
            return self._json(HTTPStatus.OK, {
                "approvals": [], "needs": [], "watching": [], "suppressed": [],
                "note": "this adapter reports no attention state; it reads no checkpoint",
            })

        if method == "GET" and path == "/api/approvals":
            return self._json(HTTPStatus.OK, {
                "approvals": [], "supported": False,
                "message": UNSUPPORTED_ACTIONS["approvals"]})

        if path == "/api/approvals/answer":
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported",
                               UNSUPPORTED_ACTIONS["approvals"])

        if path == "/api/connection/refresh":
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported",
                               UNSUPPORTED_ACTIONS["refresh"])

        file_match = re.fullmatch(
            r"/api/atlas/files/([A-Za-z0-9_-]{1,128})/(preview|download)", path)
        if method == "GET" and file_match:
            room = get("room") or None
            try:
                record, data = service.files.open_download(file_match.group(1), room=room)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            if file_match.group(2) == "preview":
                if record["preview_url"] is None:
                    raise AdapterError(HTTPStatus.NOT_FOUND, "preview_unavailable",
                                       "this attachment is download-only")
                # Inline, so the console can show it as a thumbnail.
                return self._send(HTTPStatus.OK, data, record["mime"], (
                    ("Content-Disposition",
                     "inline; " + files.content_disposition(record["name"])[12:]),))
            return self._send(HTTPStatus.OK, data, record["mime"],
                              (("Content-Disposition",
                                files.content_disposition(record["name"])),))

        submission_match = re.fullmatch(r"/api/submissions/([A-Za-z0-9_-]{8,64})", path)
        if method == "GET" and submission_match:
            found = service.journal.submission(submission_match.group(1))
            if found is None:
                return self._json(HTTPStatus.NOT_FOUND, {
                    "error": "unknown_submission",
                    "message": "this adapter never journaled that submission, so it was "
                               "never sent to the gateway",
                    "client_id": submission_match.group(1)})
            return self._json(HTTPStatus.OK, {"submission": found})

        if method == "GET" and path == "/api/submissions":
            return self._json(HTTPStatus.OK, {
                "submissions": service.journal.recent(get("room"), 50),
                "unsettled": service.journal.unsettled()})

        pending_match = re.fullmatch(
            r"/api/room/([^/]+/[^/]+)/pending/([A-Za-z0-9_-]{8,64})", path)
        if pending_match:
            room = service.require_room(pending_match.group(1))
            body = self._body()
            version = body.get("version")
            if not isinstance(version, int):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_version",
                                   "name the queued message version")
            item = service.journal.queue_get(pending_match.group(2))
            if item is None or item["room"] != room.id:
                raise AdapterError(HTTPStatus.NOT_FOUND, "queue_unknown",
                                   "that queued message is not in this room")
            if method == "DELETE":
                cancelled = service.journal.queue_cancel(pending_match.group(2), version)
                service.events.publish({"type": "queued_message", "room": room.id,
                                        "client_id": cancelled["client_id"],
                                        "state": cancelled["status"]})
                return self._json(HTTPStatus.OK, {"pending": cancelled})
            if method == "PATCH":
                text = body.get("body")
                if text is not None and (not isinstance(text, str) or len(text) > MAX_BODY):
                    raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_message",
                                       "a queued message must be short text")
                attachments = (None if body.get("attachments") is None
                               else service.checked_attachments(room, body["attachments"]))
                updated = service.journal.queue_update(
                    pending_match.group(2), version, text, attachments)
                service.events.publish({"type": "queued_message", "room": room.id,
                                        "client_id": updated["client_id"],
                                        "state": updated["status"]})
                return self._json(HTTPStatus.OK, {"pending": updated})
            raise AdapterError(HTTPStatus.NOT_FOUND, "not_found",
                               "no such queued message endpoint")

        room_match = re.fullmatch(
            r"/api/room/([^/]+/[^/]+)"
            r"(?:/(history|search|continue|attach|submit|release|detach|pending|stop|"
            r"interrupt|command|draft|refresh|files|speak|goal|effort|reasoning))?", path)
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
                room, int(get("limit", "40") or 40), get("cursor"), get("direction", "desc")))

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
            # Say at upload time whether this file can actually be sent, so a
            # person is never surprised at send time.
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


class RivetServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, service: RivetService):
        self.service = service
        handler = type("BoundHandler", (handler_class,), {"service": service})
        super().__init__(address, handler)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UX46 adapter over the existing Agent3 OpenClaw gateway")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only by design)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID,
                        help="the one OpenClaw agent identity this adapter may address")
    parser.add_argument("--unfiled-project", default=DEFAULT_UNFILED_PROJECT,
                        help="internal namespace for gateway conversations with no "
                             "verified Vault origin; no project is created for it")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help="read-only Session Vault registry; every project in it is "
                             "read, and a conversation is filed under the project whose "
                             "record carries a verified openclaw origin for its key")
    parser.add_argument("--node", default="server-agent3",
                        help="the node id this gateway runs on")
    parser.add_argument("--state-dir", default="~/.ux46-agent3",
                        help="private journal directory (not Git, Vault or Tell)")
    parser.add_argument("--transport", choices=("auto", "sidecar", "cli"), default="auto",
                        help="auto prefers the persistent bridge and falls back to the CLI")
    parser.add_argument("--sidecar", default=DEFAULT_SIDECAR,
                        help="path to ux46_rivet_gateway.mjs")
    parser.add_argument("--node-bin", default="node")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--gateway-url", default="",
                        help="optional explicit ws:// url for the bridge")
    parser.add_argument("--openclaw-root", default="",
                        help="installed openclaw package root, when a global install is "
                             "not resolvable from this directory")
    parser.add_argument("--send-timeout", type=float, default=45.0)
    parser.add_argument("--command-timeout", type=float, default=30.0,
                        help="how long to wait for sessions.create before the result "
                             "is recorded unknown (never resent)")
    parser.add_argument("--suggest", type=int, default=2)
    parser.add_argument("--browser-origin", default="",
                        help="require this exact Origin on mutations; the fronting proxy "
                             "normally owns that check")
    parser.add_argument("--identity-header", default="X-Forwarded-User",
                        help="header the fronting proxy uses to name the person")
    parser.add_argument("--path-prefix", default="",
                        help="strip this proxy prefix, e.g. /api/agents/agent3")
    parser.add_argument("--quiet", action="store_true")
    return parser


def build_transport(config: argparse.Namespace, on_event) -> Transport:
    """Prefer the persistent stream; fall back to the CLI and say which."""

    wanted = config.transport
    if wanted in ("auto", "sidecar"):
        command = [config.node_bin, str(Path(config.sidecar).expanduser())]
        if config.gateway_url:
            command += ["--url", config.gateway_url]
        if getattr(config, "openclaw_root", ""):
            command += ["--openclaw-root", config.openclaw_root]
        sidecar = SidecarTransport(command, on_event=on_event)
        try:
            sidecar.start()
            return sidecar
        except (GatewayError, OSError) as exc:
            sidecar.stop()
            if wanted == "sidecar":
                raise SystemExit(f"the gateway bridge could not connect: {exc}")
            sys.stderr.write(
                f"ux46-agent3: the persistent bridge is unavailable ({exc}); "
                "falling back to one openclaw CLI call per request, with no live stream\n")
    cli = CliTransport([config.openclaw_bin])
    cli.start()
    return cli


def main(argv: list[str] | None = None) -> int:
    config = build_parser().parse_args(argv)
    if config.host != "127.0.0.1":
        sys.stderr.write("ux46-agent3: this adapter binds loopback only\n")
        return 2
    holder: dict = {}
    transport = build_transport(
        config, lambda event, payload: holder["service"].on_gateway_event(event, payload))
    service = RivetService(config, transport)
    holder["service"] = service
    service.start()
    server = RivetServer((config.host, config.port), RivetHandler, service)
    agent_name = str(service.identity.get("name") or "(unnamed)")
    sys.stderr.write(
        f"ux46-agent3 {ADAPTER_VERSION} on http://{config.host}:{config.port} — "
        f"agent {config.agent_id} ({agent_name}) over the {transport.name} transport\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        transport.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
