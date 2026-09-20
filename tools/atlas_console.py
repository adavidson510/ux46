#!/usr/bin/env python3
"""UX46 console — a local management surface over existing native sessions.

This is a distinct entrypoint from the older spatial/tmux UI in
``tools/atlas_ui.py``; that file is untouched. The console adds nothing to the
runtime: Codex keeps its context, tools and lifecycle, and UX46 supplies
discovery, reading, targeting, an input journal and honest state.

Run it:

    python3 tools/atlas_console.py --port 8878

It listens on 127.0.0.1 only. A private HTTPS front (Tailscale Serve) can be
pointed at that loopback port; see docs/atlas-console-run-1.md.
"""

from __future__ import annotations

import argparse
import contextlib
from email import policy
from email.parser import BytesParser
import html
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atlas_desktops as desktops  # noqa: E402
import atlas_discovery as discovery  # noqa: E402
import atlas_files as files  # noqa: E402
import atlas_journal as journal  # noqa: E402
import atlas_native as native  # noqa: E402
import atlas_native_profile as native_profile  # noqa: E402
import ux46_execution_policy as execution_policies  # noqa: E402
import atlas_remote as remote  # noqa: E402
import atlas_voice as voice  # noqa: E402
import atlas_workers as workers  # noqa: E402
import atlas_tell as tell  # noqa: E402
import ux46_skills as skill_basket  # noqa: E402
import ux46_usage as usage_meter  # noqa: E402
import ux46_chapters as chapters  # noqa: E402
import ux46_recovery as recovery  # noqa: E402

APP_DIR = HERE.parent / "app" / "console"
STATIC_FILES = {
    "/modules.js": ("modules.js", "application/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/workspace.js": ("workspace.js", "application/javascript; charset=utf-8"),
    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
    "/tell.js": ("tell.js", "application/javascript; charset=utf-8"),
    "/tell.css": ("tell.css", "text/css; charset=utf-8"),
    "/efficiency.js": ("efficiency.js", "application/javascript; charset=utf-8"),
}

MAX_BODY = 64 * 1024
MAX_UPLOAD_OVERHEAD = 128 * 1024
ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
AGENT_PREFIX_RE = re.compile(r"/api/agents/([^/]+)(/.*)?")
EVENT_LIMIT = 500

COMMAND_HELP = (
    {"name": "/help", "usage": "/help", "description": "show commands available through this native connection"},
    {"name": "/status", "usage": "/status", "description": "show current native model and reasoning setting"},
    {"name": "/model", "usage": "/model [model]", "description": "show or set a runtime-advertised model for later turns"},
    {"name": "/reasoning", "usage": "/reasoning [effort]", "description": "show or set a supported reasoning effort for later turns"},
    {"name": "/effort", "usage": "/effort [effort]", "description": "alias for /reasoning"},
    {"name": "/steer", "usage": "/steer message", "description": "steer the current active turn; never starts a turn"},
    {"name": "/compact", "usage": "/compact", "description": "ask the native runtime to compact this idle thread"},
    {"name": "/new", "usage": "/new [blank|parallel] [title]", "description": "start the next compact chapter, a blank conversation, or a parallel continuation"},
    {"name": "/refresh", "usage": "/refresh", "description": "reload the current Codex login when all hosted work is idle"},
    {"name": "/goal", "usage": "/goal [resume|pause|clear]", "description": "read, resume, pause, or clear the native thread goal"},
)

DEFAULT_PORT = 8878
DEFAULT_PUBLIC_ORIGIN = "https://workspace.example.com"
DEFAULT_PUBLIC_USER = "user@example.com"


# ---------------------------------------------------------------------------
# configuration and the auth boundary
# ---------------------------------------------------------------------------

class AuthDecision:
    def __init__(self, ok: bool, mode: str, identity: str = "", reason: str = ""):
        self.ok = ok
        self.mode = mode
        self.identity = identity
        self.reason = reason


class AuthBoundary:
    """Pluggable identity check.

    ``local`` mode trusts a direct loopback request to the bound port.
    ``tailscale`` mode additionally accepts the configured public host when a
    trusted loopback proxy supplies an exact user identity header. Tailscale is
    therefore a deployment choice, not a dependency of the product.
    """

    def __init__(
        self,
        port: int,
        public_origin: str = "",
        public_user: str = "",
        identity_header: str = "Tailscale-User-Login",
    ):
        self.port = port
        self.public_origin = public_origin.rstrip("/")
        self.public_user = public_user
        self.identity_header = identity_header
        parsed = urlparse(self.public_origin) if self.public_origin else None
        self.public_host = parsed.netloc.casefold() if parsed else ""
        self.local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    @property
    def public_enabled(self) -> bool:
        return bool(self.public_host and self.public_user)

    def origin_for(self, host: str) -> str:
        host = (host or "").casefold()
        if self.public_host and host == self.public_host:
            return self.public_origin
        return f"http://{host}"

    def check(self, host: str, headers, client_ip: str) -> AuthDecision:
        """Decide who is asking, from facts the caller cannot forge.

        When a public origin is configured this listener is shared with a
        private HTTPS front, so `Host` is attacker-controlled: a tailnet peer
        can send `Host: localhost:8878` through the proxy. There is therefore
        no local-owner exemption in that mode — every request must match the
        configured origin and carry the identity the proxy verified. The
        laptop uses the same private URL as the phone.
        """

        host = (host or "").casefold()
        loopback = client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        if not loopback:
            # The listener is loopback-only; anything else is a
            # misconfiguration and must never be trusted with forwarded
            # identity headers.
            return AuthDecision(False, "denied", reason="non-loopback connection")

        if self.public_enabled:
            if host != self.public_host:
                return AuthDecision(
                    False, "denied",
                    reason="this console is served at its private origin only",
                )
            supplied = (headers.get(self.identity_header) or "").strip()
            if not supplied:
                return AuthDecision(
                    False, "denied", reason="missing private-network identity"
                )
            if supplied.casefold() != self.public_user.casefold():
                return AuthDecision(False, "denied", reason="identity not allowed")
            return AuthDecision(True, "private-network", identity=supplied)

        # Local-only deployment: nothing but this machine can reach the port.
        if host in self.local_hosts:
            return AuthDecision(True, "local-owner", identity="local")
        return AuthDecision(False, "denied", reason="unknown host")


# ---------------------------------------------------------------------------
# event log (long-poll)
# ---------------------------------------------------------------------------

from ux46_events import EventLog


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class ConsoleService:
    def __init__(self, config: argparse.Namespace):
        self.config = config
        self.recovery_gate = recovery.Gate(getattr(config, "recovery_root", None))
        self.state_dir = Path(config.state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _own_only(self.state_dir, 0o700)
        self.journal = journal.Journal(self.state_dir / "atlas-console.sqlite3")
        self.desktops = desktops.DesktopStore(self.state_dir / "desktops.sqlite3")
        self.files = files.FileStore(self.state_dir / "attachments")
        self.usage = usage_meter.UsageStore(self.state_dir / "usage",
            tell_receipts_dir=Path.home() / ".local/state/tell-boards-v1/review-receipts")
        self.exploration_root = self.state_dir / "explorations"
        (self.exploration_root / "sessions").mkdir(parents=True, exist_ok=True, mode=0o700)
        configured_generated = str(getattr(config, "generated_root", "") or "")
        self.generated_root = Path(configured_generated).expanduser().resolve() if configured_generated else None
        self.discovery = discovery.Discovery(
            Path(config.registry).expanduser() if config.registry else None,
            native_db=(Path(native_db).expanduser()
                       if (native_db := getattr(config, "native_db", "")) else None),
            exploration_root=self.exploration_root,
        )
        self.events = EventLog()
        self.csrf_token = secrets.token_urlsafe(32)
        self.auth = AuthBoundary(
            port=config.port,
            public_origin=config.public_origin,
            public_user=config.public_user,
        )
        self.started_at = time.time()
        self._account_recoveries = {}
        self._account_recovery_lock = threading.Lock()
        # The configured agents this console can speak for. Only trusted local
        # configuration names a host, port or identity file; a request names an
        # agent id and nothing else.
        self.agents = remote.AgentRegistry(
            local_label=str(getattr(config, "local_agent_label", "") or "Local"),
            local_node=self.discovery.node,
            config_path=str(getattr(config, "agents_config", "") or ""),
        )
        self.voice = (
            voice.VoiceService(config.voice_model_dir, self.state_dir / "voice-cache")
            if getattr(config, "voice_model_dir", "") else None
        )
        self._codex_command = list(config.codex_command)
        # One native process per attached conversation, plus one read/catalog
        # runtime that never resumes a thread. Switching rooms therefore never
        # stops anyone's work, and Release stops exactly one process.
        # The host's execution policy: what every session this console starts
        # or resumes is asked to run under. A saved UI choice takes precedence
        # over the initial launch default. Existing workers keep their policy.
        self.access_defaults = execution_policies.ExecutionPolicy(
            self.state_dir / "execution-policy.json",
            getattr(config, "execution_policy", native_profile.PRESERVE))
        self.execution_policy = self.access_defaults.read()["policy"]
        self.workers = workers.SessionWorkerPool(
            command=self._codex_command, on_event=self._on_native_event,
            execution_policy=self.execution_policy,
        )
        # A Refresh and a submission must never race across the same
        # connection. The browser cannot influence which native operation runs.
        self._connection_lock = threading.Lock()
        self._turn_state: dict[str, dict] = {}
        self._queue_wake = threading.Event()
        self._queue_stop = threading.Event()
        self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True)
        self._queue_thread.start()

    def _queue_loop(self) -> None:
        """Dispatch expired editable messages without replaying settled work."""
        while not self._queue_stop.is_set():
            self._queue_wake.wait(timeout=0.35)
            self._queue_wake.clear()
            seen_threads: set[str] = set()
            for item in self.journal.queue_due(time.time()):
                if item.thread_id in seen_threads:
                    continue
                seen_threads.add(item.thread_id)
                self._dispatch_queued(item)

    def _dispatch_queued(self, item: journal.QueuedMessage) -> None:
        try:
            with self.recovery_gate.admit():
                # Pause may have held this row after queue_due read it.
                current = self.journal.queue_get(item.client_id)
                if current and current.status == journal.PENDING:
                    self._dispatch_admitted_queue(current)
        except recovery.RecoveryBusy:
            return

    def _dispatch_admitted_queue(self, item: journal.QueuedMessage) -> None:
        """One FIFO attempt. Ownership and approvals keep the item queued."""
        try:
            room = self.require_room(item.room)
            if not room.controllable or room.thread_id != item.thread_id:
                self.journal.queue_mark(item.client_id, journal.PENDING, "native target is unavailable")
                return
            if self.workers.pending_requests(item.thread_id):
                self.journal.queue_mark(item.client_id, journal.PENDING, "waiting for a native approval")
                return
            if item.thread_id not in self.workers.attached():
                remembered = next((saved for saved in self.journal.remembered_owned()
                                   if saved["thread_id"] == item.thread_id and saved["room"] == item.room), None)
                if not remembered:
                    self.journal.queue_mark(item.client_id, journal.PENDING,
                                            "Continue here before this message can dispatch")
                    return
                # Startup recovery: a remembered conversation gets its own
                # process back, and only that conversation.
                self.workers.attach(item.thread_id, item.room)
            self.journal.queue_mark(item.client_id, journal.DISPATCHING)
            attachments = self.native_attachments(room, item.attachments)
            result = self.writer_for(item.thread_id).send_input(
                item.thread_id, item.body, item.client_id, attachments)
        except native.UncertainDelivery as exc:
            self.journal.settle(item.client_id, journal.UNCERTAIN, detail=str(exc))
            self.journal.queue_mark(item.client_id, journal.UNCERTAIN, "delivery is unknown; UX46 will not replay it")
        except native.NativeError as exc:
            if exc.code in {"held_elsewhere", "ownership_unavailable", "not_owned", "connection_busy"}:
                self.journal.queue_mark(item.client_id, journal.PENDING, str(exc))
                return
            self.journal.settle(item.client_id, journal.FAILED, detail=str(exc))
            self.journal.queue_mark(item.client_id, journal.FAILED, str(exc))
        else:
            self.journal.settle(item.client_id, journal.ACCEPTED,
                                native_turn_id=result.get("turn_id", ""), mode=result.get("mode", ""))
            self.journal.queue_mark(item.client_id, journal.ACCEPTED)
            self.events.publish({"type": "queued_message", "room": item.room,
                                 "client_id": item.client_id, "state": journal.ACCEPTED})

    def queue_message(self, room: discovery.Room, client_id: str, text: str,
                      attachments: list[dict] | None = None) -> dict:
        if not CLIENT_ID_RE.match(client_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_client_id", "a queued message needs a stable client id")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_BODY:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_message", "a queued message needs short text")
        self.require_controllable(room)
        clean = self.checked_attachments(room, attachments)
        try:
            item, created = self.journal.enqueue(client_id, room.id, room.thread_id, text, clean,
                                                  time.time() + 1.75)
        except journal.DuplicateMismatch as exc:
            raise ApiError(HTTPStatus.CONFLICT, exc.code, str(exc), exc.detail) from exc
        self._queue_wake.set()
        return {"pending": item.as_json(), "duplicate": not created}

    def update_queued_message(self, room: discovery.Room, client_id: str, version: int,
                              body: str | None, attachments: list[dict] | None,
                              editing: bool | None, position: float | None = None) -> dict:
        item = self.journal.queue_get(client_id)
        if not item or item.room != room.id or item.thread_id != room.thread_id:
            raise ApiError(HTTPStatus.NOT_FOUND, "queue_unknown", "that queued message is not in this room")
        if body is not None and (not body.strip() or len(body) > MAX_BODY):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_message", "a queued message needs short text")
        if attachments is not None:
            attachments = self.checked_attachments(room, attachments)
        try:
            updated = self.journal.queue_update(client_id, version, body=body, attachments=attachments,
                                                position=position,
                                                editable_until=(time.time() + 1.75 if editing is False else None),
                                                editing=editing)
        except journal.JournalError as exc:
            raise ApiError(HTTPStatus.CONFLICT, exc.code, str(exc), exc.detail) from exc
        self.events.publish({"type": "queued_message", "room": room.id, "client_id": client_id,
                             "state": updated.status})
        if editing is False: self._queue_wake.set()
        return {"pending": updated.as_json()}

    def cancel_queued_message(self, room: discovery.Room, client_id: str, version: int) -> dict:
        item = self.journal.queue_get(client_id)
        if not item or item.room != room.id or item.thread_id != room.thread_id:
            raise ApiError(HTTPStatus.NOT_FOUND, "queue_unknown", "that queued message is not in this room")
        try:
            cancelled = self.journal.queue_cancel(client_id, version)
        except journal.JournalError as exc:
            raise ApiError(HTTPStatus.CONFLICT, exc.code, str(exc), exc.detail) from exc
        self.events.publish({"type": "queued_message", "room": room.id, "client_id": client_id,
                             "state": cancelled.status})
        return {"pending": cancelled.as_json()}

    def checked_attachments(self, room: discovery.Room, attachments: list[dict] | None) -> list[dict]:
        clean = attachments if isinstance(attachments, list) else []
        if any(not isinstance(item, dict) or set(item) != {"file_id"} or not isinstance(item["file_id"], str)
               for item in clean):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_attachment", "attachments must name managed file ids")
        try:
            for item in clean:
                self.files.get(item["file_id"], room=room.id)
        except files.FileStoreError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_attachment", str(exc)) from exc
        return [{"file_id": item["file_id"]} for item in clean]

    def native_attachments(self, room: discovery.Room, attachments: list[dict]) -> list[dict]:
        try:
            resolved = []
            for item in attachments:
                record, path = self.files.native_path(item["file_id"], room=room.id)
                resolved.append(dict(record, path=str(path)))
            return resolved
        except files.FileStoreError as exc:
            raise native.NativeError(str(exc), code="bad_attachment") from exc

    def native_item_attachment(self, room: discovery.Room, item_id: str, index: int) -> dict:
        """Copy one path that the native runtime itself recorded on this item."""

        self.require_controllable(room)
        entries, _source = self.sessions_for(room.thread_id).full_history(room.thread_id)
        item = next((entry.get("item") for entry in entries
                     if str((entry.get("item") or {}).get("id") or "") == item_id), None)
        if not isinstance(item, dict):
            raise ApiError(HTTPStatus.NOT_FOUND, "item_unknown", "that item is not in this room's native history")
        candidates = _item_attachment_refs(item)
        if index < 0 or index >= len(candidates):
            raise ApiError(HTTPStatus.NOT_FOUND, "attachment_unknown", "that native item has no such attachment")
        candidate = candidates[index]
        try:
            return self.files.register_local(
                room.id, candidate["path"], project_root=room.project_root,
                generated_root=self.generated_root, native_reference_validated=True,
                name=candidate.get("name"), mime=candidate.get("mime"), project=room.project_id,
            )
        except files.FileStoreError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_native_attachment", str(exc)) from exc

    def project_pref(self, project_id: str) -> dict:
        if not any(item["id"] == project_id for item in self.discovery.projects()):
            raise ApiError(HTTPStatus.NOT_FOUND, "project_unknown", "that project is not in this UX46 registry")
        return self.journal.project_pref(project_id)

    # -- native runtime ----------------------------------------------------
    @property
    def sessions(self) -> native.NativeSessions:
        """The read/catalog runtime: metadata, history, search, model list.

        It refuses every writer method, so opening a room to read it never
        takes a native writer that a later Release would have to free.
        """

        return self.workers.reader_sessions()

    def sessions_for(self, thread_id: str) -> native.NativeSessions:
        """Whichever runtime speaks for this conversation right now."""

        return self.workers.sessions_for(thread_id)

    def writer_for(self, thread_id: str) -> native.NativeSessions:
        """The conversation's own worker, or an honest refusal."""

        return self.workers.writer_for(thread_id)

    @property
    def runtime_started(self) -> bool:
        return self.workers.any_running()

    def _on_native_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "notification":
            method = event.get("method", "")
            params = event.get("params") or {}
            thread_id = str(params.get("threadId") or "")
            rooms = self._rooms_for_thread(thread_id)
            room = rooms[0] if rooms else ""
            # Turn bookkeeping belongs to the worker that emitted the event;
            # the pool has already applied it to that worker alone.
            terminal = native.terminal_failure(
                (params.get("turn") if isinstance(params.get("turn"), dict) else params)
            ) if method in {"error", "turn/completed"} else None
            if thread_id and terminal:
                self._turn_state[thread_id] = terminal
            elif thread_id and method == "turn/completed":
                self._turn_state.pop(thread_id, None)
            self.events.publish({
                "type": "native",
                "method": method,
                "thread_id": thread_id,
                "room": room,
                "rooms": rooms,
                "worker": event.get("worker", ""),
                "agent": event.get("agent", ""),
                "item_type": ((params.get("item") or {}).get("type")
                              if isinstance(params.get("item"), dict) else None),
                "terminal": terminal,
            })
            return
        if kind == "request/pending":
            request = event.get("request") or {}
            self.events.publish({
                "type": "approval",
                "state": "pending",
                "thread_id": request.get("thread_id", ""),
                "room": self._room_for_thread(str(request.get("thread_id", ""))),
                "rooms": self._rooms_for_thread(str(request.get("thread_id", ""))),
                "global": True,
            })
            return
        if kind in ("runtime/exited", "runtime/error", "request/unsupported"):
            self.events.publish({"type": kind, "detail": event.get("detail", ""),
                                 "method": event.get("method", ""),
                                 "worker": event.get("worker", ""),
                                 "worker_role": event.get("worker_role", ""),
                                 "global": True})
            return
        self.events.publish({"type": str(kind), "global": True})

    def _room_for_thread(self, thread_id: str) -> str:
        rooms = self._rooms_for_thread(thread_id)
        return rooms[0] if rooms else ""

    def _rooms_for_thread(self, thread_id: str) -> list[str]:
        if not thread_id:
            return []
        return [room.id for room in self.discovery.rooms_for_thread(thread_id)]

    def refresh_connection(self, room: discovery.Room | None = None) -> dict:
        """Reload the current Codex login without disturbing other work.

        With a room, exactly that conversation's process is replaced: every
        other attached conversation keeps running and keeps its writer. With
        no room, only the read/catalog runtime is replaced — UX46 will not
        silently restart someone's live session to refresh a login.
        """

        if not self._connection_lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "connection_busy",
                           "a send or connection refresh is already in progress")
        try:
            result = (self._refresh_conversation(room) if room is not None
                      else self._refresh_read_runtime())
            self.events.publish({"type": "connection", "state": result["state"],
                                 "room": room.id if room is not None else "",
                                 "global": room is None})
            return result
        finally:
            self._connection_lock.release()

    def _unsettled_for(self, thread_id: str = "") -> list:
        rows = self.journal.unsettled()
        return [row for row in rows if not thread_id or row.thread_id == thread_id]

    def _refresh_conversation(self, room: discovery.Room) -> dict:
        self.require_controllable(room)
        if self._unsettled_for(room.thread_id):
            raise ApiError(
                HTTPStatus.CONFLICT, "connection_busy",
                "a previous dispatch for this session is still unsettled, so UX46 "
                "will not refresh it yet",
            )
        refreshed = self.workers.refresh_worker(room.thread_id, room.id)
        self.journal.remember_owned(
            room.thread_id, room.id, str((refreshed.get("continued") or {}).get("cwd") or ""))
        others = len([tid for tid in self.workers.attached() if tid != room.thread_id])
        message = refreshed.get("message") or ""
        if refreshed.get("state") == "refreshed":
            message = (
                "UX46 restarted this conversation's own native process with the current "
                "Codex login and resumed the exact same session. "
                f"{others} other attached conversation{'s were' if others != 1 else ' was'} "
                "left running. It does not prove a model turn will succeed."
            )
        return dict(refreshed, message=message, scope="conversation", room=room.id,
                    reconnect_needed=0, others_running=others)

    def _refresh_read_runtime(self) -> dict:
        if self._unsettled_for():
            raise ApiError(
                HTTPStatus.CONFLICT, "connection_busy",
                "a previous dispatch is still unsettled, so UX46 will not refresh yet",
            )
        # The read runtime is replaced only when it reports no hosted work.
        self.sessions.refresh_safety()
        self.workers.stop_reader()
        refreshed = self.sessions.refresh_connection()
        attached = len(self.workers.attached())
        result = dict(refreshed, scope="connection", reconnected=0, reconnect_needed=0,
                      attached=attached)
        if result["state"] == "refreshed":
            result["message"] = (
                "UX46 loaded the current Codex login in a fresh read connection. "
                f"{attached} attached conversation{'s keep' if attached != 1 else ' keeps'} "
                "the login it started with; refresh one from its own room. "
                "It does not prove a model turn will succeed."
            )
        return result


    # -- release -----------------------------------------------------------
    RELEASE_MESSAGES = {
        "released": ("Released. UX46 stopped this conversation's own native process and "
                     "confirmed nothing still holds its native writer — Codex can take it "
                     "up again. Other sessions kept running."),
        "releasing": ("UX46 stopped this conversation's process, but a process still holds "
                      "the native writer, so UX46 will not call it released yet."),
        "unverified": ("UX46 stopped this conversation's process but could not check the "
                       "native writer, so it will not claim the session is free."),
        "not_attached": ("UX46 was not holding this session, so there was no process to "
                         "stop. Any remembered ownership has been cleared."),
    }

    def release_conversation(self, room: discovery.Room) -> dict:
        """Stop exactly one conversation's worker and report what that freed.

        No other conversation is touched, nothing UX46 did not launch is
        signalled, and no lock file is unlinked. If the writer is still held
        or cannot be checked, this says so instead of claiming a release.
        """

        self.require_controllable(room)
        unsettled = self._unsettled_for(room.thread_id)
        if unsettled:
            raise ApiError(
                HTTPStatus.CONFLICT, "connection_busy",
                "a previous dispatch for this session is still unsettled, so UX46 "
                "will not release it yet",
                {"unsettled": [item.as_json() for item in unsettled]},
            )
        result = self.workers.release(room.thread_id)
        self.journal.forget_owned(room.thread_id)
        state = str(result.get("release_state") or "unverified")
        result["message"] = self.RELEASE_MESSAGES.get(
            state, "UX46 stopped this conversation's process.")
        result["room"] = room.id
        self.events.publish({"type": "ownership", "room": room.id, "state": state})
        return result

    # -- helpers -----------------------------------------------------------
    def session_options(self) -> dict:
        self.discovery.refresh()
        # The policy is what this console will *ask* for. What a session was
        # actually granted is reported per room, from the runtime's own answer.
        return {"available": True, "blank": True,
                "execution_policy": self.execution_policy, "projects": [
            {"id": p.id, "name": p.name} for p in self.discovery._projects
            if p.id != "ux46-explorations" and p.root.is_dir()]}

    def create_session(self, body: dict) -> dict:
        client_id = body.get("client_id")
        title = body.get("title", "")
        project_id = body.get("project_id")
        if (not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id)
                or not isinstance(title, str) or len(title) > 120
                or (project_id is not None and not isinstance(project_id, str))):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_session", "Choose an agent, a project or blank, and a short name.")
        self.discovery.refresh()
        wanted = project_id or "ux46-explorations"
        project = next((p for p in self.discovery._projects if p.id == wanted), None)
        if project is None or not project.root.is_dir():
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown_project", "That project is not available on this agent.")
        (project.sessions_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        title = title.strip() or ("Exploration" if not project_id else f"New {project.name} conversation")
        # A creation destination, not an existing conversation. No current
        # thread, model override, transcript or approval profile is inherited.
        destination = discovery.Room(project.id, project.name, str(project.root), "new",
            title, "active", "", discovery.CONTROLLABLE, "", str(project.root), "codex",
            self.discovery.node, 0, "unlinked", None, {}, "", "", "")
        raw = json.dumps({"action": "create_session", "project_id": project_id, "title": title}, sort_keys=True)
        result = self._journal_new(destination, raw, client_id, title, mode="blank")
        command = result.get("command", {})
        return {"state": "created" if command.get("state") == "created" else command.get("state", "failed"),
                "message": command.get("message", ""), "new_room": command.get("new_room"),
                "duplicate": bool(result.get("duplicate"))}

    def workspace(self) -> dict:
        """The working set: what this person would plausibly reopen.

        Preferences are read here and merged into a reading projection. A GET
        never writes one.
        """

        prefs = self.journal.room_prefs()
        # Pinned and hidden come back resolved per conversation, not per
        # record: the projection owns that, so one alias cannot contradict
        # another on the way to the browser.
        payload = self.discovery.workspace(prefs)
        payload["prefs"] = prefs
        return payload

    def require_room(self, room_id: str) -> discovery.Room:
        if not ROOM_ID_RE.match(room_id or ""):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_room", "that is not a room id")
        room = self.discovery.room(room_id)
        if room is None:
            self.discovery.refresh(force=True)
            room = self.discovery.room(room_id)
        if room is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown_room", "no such room")
        return room

    def require_controllable(self, room: discovery.Room) -> discovery.Room:
        if not room.controllable:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "not_controllable",
                f"{room.id} is {discovery.CAPABILITY_LABEL.get(room.capability, room.capability)}",
            )
        return room

    def _chapter_record(self, room: discovery.Room, project) -> discovery.sv.Record:
        """Read the current durable record without touching native history."""

        path = Path(room.record_path)
        try:
            resolved = path.resolve(strict=True)
            sessions = project.sessions_dir.resolve(strict=True)
            resolved.relative_to(sessions)
            record = discovery.sv.parse_record(project, resolved)
        except (OSError, ValueError, discovery.sv.VaultError) as exc:
            raise ApiError(HTTPStatus.CONFLICT, "chapter_context_unavailable",
                           "UX46 cannot read this room's durable continuation record; use /new blank or repair the record.") from exc
        if record.identity != room.id:
            raise ApiError(HTTPStatus.CONFLICT, "chapter_context_unavailable",
                           "UX46 could not match this room to one durable continuation record; use /new blank.")
        return record

    def _rolling_admission(self, room: discovery.Room) -> str:
        """Return an actionable refusal reason, or an empty string when idle."""

        if self._unsettled_for(room.thread_id):
            return "A previous input has an unsettled delivery state; resolve it before starting the next chapter."
        if any(item.status not in {journal.ACCEPTED, "cancelled"}
               for item in self.journal.queue_list(room.id)):
            return "This room has queued input; send, edit, or cancel it before starting the next chapter."
        try:
            speaker = self.sessions_for(room.thread_id)
            thread = speaker.read_thread(room.thread_id).get("thread") or {}
            status = (thread.get("status") or {}).get("type")
            if status == "active" or speaker.active_turn(room.thread_id):
                return "A native turn is still active; wait for it to finish before starting the next chapter."
            if status not in {"idle", "notLoaded"}:
                return "UX46 cannot verify that this native thread is idle; use /new blank or refresh its connection first."
            if self.workers.pending_requests(room.thread_id):
                return "A native approval or question is waiting; answer it before starting the next chapter."
            goal = speaker.goal(room.thread_id)
            if goal is not None:
                status = str(goal.get("status") or "").casefold()
                # A dormant goal remains recorded on its original thread. It
                # is neither copied nor resumed by the new chapter. An active
                # or unrecognised status cannot safely make that distinction.
                if status not in {"paused", "blocked", "usagelimited", "completed", "complete"}:
                    return ("This native thread has an active or unverified goal; pause or finish it "
                            "before starting the next chapter. The new chapter will not copy it.")
        except native.NativeError:
            return "UX46 cannot verify native turn, goal, and pending-input state; use /new blank or restore the connection first."
        return ""

    def chapter_preview(self, room: discovery.Room) -> dict:
        self.require_controllable(room)
        projects = [p for p in self.discovery._projects if p.id == room.project_id]
        if len(projects) != 1 or not projects[0].root.is_dir():
            raise ApiError(HTTPStatus.CONFLICT, "chapter_context_unavailable",
                           "UX46 cannot resolve this room's project; use /new blank.")
        record = self._chapter_record(room, projects[0])
        package = chapters.build_brief(projects[0], record)
        reason = self._rolling_admission(room)
        return {"chapter": package, "eligible": not bool(reason), "reason": reason}

    def other_room_owner(self, room: discovery.Room) -> str:
        """Shared native ids must not create two writers."""

        owned = self.workers.owned_threads().get(room.thread_id)
        if owned and owned.get("room") and owned["room"] != room.id:
            return str(owned["room"])
        return ""

    def command(self, room: discovery.Room, raw: str, client_id: str = "") -> dict:
        """Run one explicitly supported native command, never TUI text.

        App-server exposes RPC capabilities, not Codex's terminal slash parser.
        This small dispatcher is intentionally closed: browser input can select
        a documented operation but never its RPC method, executable, cwd, or
        arbitrary settings payload.
        """

        match = re.fullmatch(r"/([A-Za-z]+)(?:\s+(.*))?", raw.strip(), flags=re.S)
        if not match:
            return self._command_result("unknown", "unsupported",
                                        "Use /help to see commands available through UX46.")
        name = match.group(1).casefold()
        argument = (match.group(2) or "").strip()
        aliases = {"streer": "steer", "reasononing": "reasoning", "effort": "reasoning"}
        name = aliases.get(name, name)
        if name not in {"help", "status", "model", "reasoning", "steer", "compact", "new", "refresh", "goal"}:
            return self._command_result(name, "unsupported",
                                        "That Codex terminal command is not exposed by this app-server connection.")
        self.require_controllable(room)
        if name in {"model", "reasoning", "steer", "compact", "new", "goal"}:
            other = self.other_room_owner(room)
            if other:
                raise ApiError(HTTPStatus.CONFLICT, "held_by_room",
                               f"the same native session is open here as {other}")
        if name == "refresh":
            # An attached conversation refreshes its own process; an unattached
            # room refreshes only the shared read runtime.
            scoped = room if room.thread_id in self.workers.attached() else None
            connection = self.refresh_connection(scoped)
            return self._command_result("refresh", connection.get("state", "unknown"),
                                        connection.get("message", "Connection refresh finished."),
                                        native=connection)
        if name == "goal":
            if not argument:
                return self._command_result(
                    "goal", "ready", "Current native goal state.",
                    native={"goal": self.sessions_for(room.thread_id).goal(room.thread_id)})
            if argument not in {"resume", "pause", "clear"}:
                return self._command_result("goal", "unsupported", "Use /goal, /goal resume, /goal pause, or /goal clear.")
            if not CLIENT_ID_RE.match(client_id):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "this goal command needs a stable client id")
            if not self._connection_lock.acquire(blocking=False):
                raise ApiError(HTTPStatus.CONFLICT, "connection_busy",
                               "a send or connection refresh is already in progress")
            try:
                writer = self.writer_for(room.thread_id)
                if argument in {"resume", "pause"}:
                    requested = "active" if argument == "resume" else "paused"
                    goal = writer.set_goal_status(room.thread_id, requested)
                    actual = str(goal.get("status") or "") if isinstance(goal, dict) else ""
                    if actual == requested:
                        message = (f"Native goal now reports {actual}. "
                                   "This does not change account usage limits or permissions.")
                        state = "updated"
                    else:
                        message = ("UX46 could not confirm the requested goal state; native readback reports "
                                   f"{actual or 'no goal'}. Account usage limits and permissions are unchanged.")
                        state = "unconfirmed"
                else:
                    cleared = writer.clear_goal(room.thread_id)
                    goal = writer.goal(room.thread_id)
                    if cleared and goal is None:
                        message = "Native goal cleared. This does not change account usage limits or permissions."
                        state = "cleared"
                    else:
                        actual = str(goal.get("status") or "") if isinstance(goal, dict) else "no goal"
                        message = ("UX46 could not confirm that the native goal cleared; readback reports "
                                   f"{actual}. Account usage limits and permissions are unchanged.")
                        state = "unconfirmed"
            finally:
                self._connection_lock.release()
            self.events.publish({"type": "goal", "room": room.id, "state": state})
            return self._command_result("goal", state, message, native={"goal": goal})
        if name == "status":
            speaker = self.sessions_for(room.thread_id)
            thread = speaker.read_thread(room.thread_id).get("thread") or {}
            return self._command_result("status", "ready", "Current native thread settings.", native={
                "model": thread.get("model"), "reasoning_effort": thread.get("reasoningEffort"),
                "active_turn": speaker.active_turn(room.thread_id),
                "attached": room.thread_id in self.workers.attached(),
            }, catalog=self._catalog())
        if name in {"help", "model", "reasoning"} and not argument:
            catalog = self._catalog()
            message = ("Choose a runtime-advertised model or reasoning effort."
                       if name in {"model", "reasoning"} else "UX46 commands use documented native operations.")
            return self._command_result(name, "ready", message, catalog=catalog)
        if name == "help":
            return self._command_result("help", "ready", "UX46 commands use documented native operations.",
                                        catalog=self._catalog())

        if name in {"steer", "compact", "new"} and not CLIENT_ID_RE.match(client_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                           "this command needs a stable client id before UX46 can start it")
        if name == "steer" and not argument:
            return self._command_result("steer", "needs_input", "Add the instruction to steer the active turn.")
        if name in {"model", "reasoning", "compact", "new", "steer"}:
            if not self._connection_lock.acquire(blocking=False):
                raise ApiError(HTTPStatus.CONFLICT, "connection_busy",
                               "a send or connection refresh is already in progress")
            try:
                if name == "model":
                    state = self.writer_for(room.thread_id).update_settings(
                        room.thread_id, model=argument)
                    self.events.publish({"type": "settings", "room": room.id})
                    return self._command_result("model", "updated", "Model updated for later turns.", native=state,
                                                catalog=self._catalog())
                if name == "reasoning":
                    state = self.writer_for(room.thread_id).update_settings(
                        room.thread_id, effort=argument)
                    self.events.publish({"type": "settings", "room": room.id})
                    return self._command_result("reasoning", "updated", "Reasoning effort updated for later turns.", native=state,
                                                catalog=self._catalog())
                if name == "steer":
                    return self._journal_steer(room, raw.strip(), client_id, argument)
                if name == "compact":
                    return self._journal_compact(room, raw.strip(), client_id)
                # A retry must return the original journaled result before it
                # re-checks live source state. A successful rolling chapter
                # may already have released that source worker.
                if self.journal.get(client_id) is not None:
                    submission, _created = self._reserve_command(room, raw.strip(), client_id)
                    return self._duplicate_command(submission, "new") or {}
                mode, title = chapters.parse_new_argument(argument)
                if mode != "blank":
                    preview = self.chapter_preview(room)
                    if not preview["eligible"]:
                        return self._command_result("new", "needs_idle", preview["reason"],
                                                    chapter={"previous_room": room.id, "mode": mode,
                                                             **preview["chapter"], "lineage": {"previous_room": room.id}})
                    project = next(p for p in self.discovery._projects if p.id == room.project_id)
                    return self._journal_new(room, raw.strip(), client_id,
                                             chapters.chapter_title(room.project_name, mode, title), mode=mode,
                                             source_record=self._chapter_record(room, project),
                                             chapter_brief=preview["chapter"])
                return self._journal_new(room, raw.strip(), client_id,
                                         chapters.chapter_title(room.project_name, mode, title), mode=mode)
            finally:
                self._connection_lock.release()
        return self._command_result(name, "unsupported", "That command is not available here.")

    def _catalog(self) -> dict:
        try:
            return self.sessions.model_catalog()
        except native.NativeError as exc:
            return {"available": False, "models": [], "message": str(exc)}

    @staticmethod
    def _command_result(name: str, state: str, message: str, *, native=None, catalog=None,
                        new_room=None, chapter=None) -> dict:
        command = {"name": name, "state": state, "message": message}
        if native is not None:
            command["native"] = native
        if catalog is not None:
            command["catalog"] = catalog
        if new_room is not None:
            command["new_room"] = new_room
        if chapter is not None:
            command["chapter"] = chapter
        return {"command": command}

    def _duplicate_command(self, submission: journal.Submission, name: str) -> dict | None:
        if submission.status == journal.PENDING or submission.status == journal.UNCERTAIN:
            return self._command_result(name, "uncertain",
                                        "UX46 cannot prove the earlier command outcome, so it will not repeat it.")
        try:
            saved = json.loads(submission.detail or "{}")
        except json.JSONDecodeError:
            saved = {}
        command = saved.get("command") if isinstance(saved, dict) else None
        if isinstance(command, dict):
            return {"command": command, "duplicate": True, "dispatched": False}
        return self._command_result(name, "failed", "The earlier command has no recoverable result.")

    def _reserve_command(self, room: discovery.Room, raw: str, client_id: str):
        try:
            return self.journal.reserve(client_id, room.id, room.thread_id or "new-session", raw)
        except journal.DuplicateMismatch as exc:
            raise ApiError(HTTPStatus.CONFLICT, exc.code, str(exc), exc.detail) from exc

    def _settle_command(self, client_id: str, status: str, result: dict) -> dict:
        self.journal.settle(client_id, status, mode="command",
                            detail=json.dumps(result, separators=(",", ":")))
        return result

    def _journal_steer(self, room: discovery.Room, raw: str, client_id: str, text: str) -> dict:
        submission, created = self._reserve_command(room, raw, client_id)
        if not created:
            return self._duplicate_command(submission, "steer") or {}
        try:
            result = self.writer_for(room.thread_id).steer_input(room.thread_id, text, client_id)
        except native.UncertainDelivery:
            return self._settle_command(client_id, journal.UNCERTAIN, self._command_result(
                "steer", "uncertain", "UX46 cannot prove whether the active turn received that steering instruction."))
        except native.NativeError as exc:
            return self._settle_command(client_id, journal.FAILED, self._command_result(
                "steer", "failed", str(exc)))
        self.events.publish({"type": "command", "room": room.id, "state": "steered"})
        return self._settle_command(client_id, journal.ACCEPTED, self._command_result(
            "steer", "accepted", "Steering instruction accepted by the active native turn.", native=result))

    def _journal_compact(self, room: discovery.Room, raw: str, client_id: str) -> dict:
        submission, created = self._reserve_command(room, raw, client_id)
        if not created:
            return self._duplicate_command(submission, "compact") or {}
        try:
            self.writer_for(room.thread_id).compact(room.thread_id)
        except native.UncertainDelivery:
            return self._settle_command(client_id, journal.UNCERTAIN, self._command_result(
                "compact", "uncertain", "UX46 cannot prove whether native compaction started."))
        except native.NativeError as exc:
            return self._settle_command(client_id, journal.FAILED, self._command_result(
                "compact", "failed", str(exc)))
        return self._settle_command(client_id, journal.ACCEPTED, self._command_result(
            "compact", "accepted", "Native compaction started."))

    def _journal_new(self, room: discovery.Room, raw: str, client_id: str, title: str, *,
                     mode: str = "blank", source_record=None, chapter_brief=None) -> dict:
        submission, created = self._reserve_command(room, raw, client_id)
        if not created:
            return self._duplicate_command(submission, "new") or {}
        stem = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:48] or "new"
        session = f"{stem}-{client_id.casefold()[-12:]}"
        identity = f"{room.project_id}/{session}"
        projects = [p for p in self.discovery._projects if p.id == room.project_id]
        if (len(projects) != 1 or not projects[0].root.is_dir()
                or (projects[0].sessions_dir / f"{session}.md").exists()):
            return self._settle_command(client_id, journal.FAILED, self._command_result(
                "new", "failed", "UX46 could not reserve a new Vault session name."))
        vault = discovery.sv
        rolling = mode in {"replace", "parallel"}
        args = argparse.Namespace(identity=identity, title=title or f"New {room.project_name} session",
            summary=(f"A UX46 rolling chapter from {room.id}; native transcript and goal were not inherited."
                     if rolling else "A fresh native session created in UX46; no prior transcript was inherited."),
            keywords=f"{room.project_id}, {session}", status="active", no_link_current=True,
            json=True, primary=False, runtime=None, session_id=None, session_name=None, node=None, cwd=None)
        native_attempted = False
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                vault.command_start(args, projects, self.discovery.node)
            record = vault.parse_record(projects[0], projects[0].sessions_dir / f"{session}.md")
            if rolling:
                vault.update_record(record.path, {
                    "chapter_parent": room.id,
                    "chapter_mode": mode,
                    "chapter_brief_chars": chapter_brief["size_chars"],
                    "chapter_source_record": source_record.identity,
                }, section_title="Rolling chapter", section_content=(
                    f"Continuation parent: {room.id}.\n\n"
                    "UX46 supplied a bounded durable entry brief at native thread start. "
                    "No transcript, goal, permission, or approval state was copied."
                ))
            source = (self.sessions_for(room.thread_id).read_thread(room.thread_id).get("thread") or {}) if room.thread_id else {}
            catalog = self._catalog() if source else {}
            choices = {item.get("model"): item for item in catalog.get("models", [])}
            source_model = source.get("model") if source.get("model") in choices else None
            source_effort = source.get("reasoningEffort")
            if not source_model or source_effort not in choices[source_model].get("efforts", []):
                source_effort = None
            # A new project session starts at its canonical project root, not
            # a generic hub cwd inherited from the source thread. No history
            # or instructions are passed to the app-server.
            native_attempted = True
            fresh = self.workers.start_fresh(str(projects[0].root), room=identity,
                model=source_model, effort=source_effort, project_id=source.get("projectId"),
                developer_instructions=(chapters.developer_instructions(chapter_brief["brief"], identity=identity)
                                        if rolling else None))
            self.journal.remember_owned(fresh["thread_id"], identity, fresh["cwd"])
            stamp = time.strftime("%Y-%m-%d")
            vault.link_origin(record, {"runtime": "codex", "node": self.discovery.node,
                "session_id": fresh["thread_id"], "session_name": "", "cwd": fresh["cwd"],
                "captured": stamp, "last_seen": stamp, "primary": True}, make_primary=True)
            self.discovery.refresh(force=True)
            created_room = self.require_room(identity)
        except native.UncertainDelivery:
            return self._settle_command(client_id, journal.UNCERTAIN, self._command_result(
                "new", "uncertain", "UX46 cannot prove whether the fresh native thread was created."))
        except native.NativeError as exc:
            return self._settle_command(client_id, journal.FAILED, self._command_result("new", "failed", str(exc)))
        except (discovery.sv.VaultError, OSError) as exc:
            status = journal.UNCERTAIN if native_attempted else journal.FAILED
            return self._settle_command(client_id, status, self._command_result("new", status,
                "The native conversation may exist, but its project record could not be saved. Check before creating another."
                if native_attempted else str(exc)))
        except Exception as exc:
            status = journal.UNCERTAIN if native_attempted else journal.FAILED
            message = ("The new conversation could not be fully recorded. Check this attempt before creating another."
                       if native_attempted else
                       "The new conversation could not be prepared. Nothing was started; your current conversation is intact.")
            return self._settle_command(client_id, status, self._command_result(
                "new", status, message + " (" + type(exc).__name__ + ")"))
        self.events.publish({"type": "workspace", "room": identity, "global": True})
        lineage = {"previous_room": room.id, "previous_thread": room.thread_id,
                   "mode": mode, "source_release": {"state": "not_attached"}}
        if mode == "replace" and room.thread_id in self.workers.attached():
            try:
                released = self.workers.release(room.thread_id)
                self.journal.forget_owned(room.thread_id)
                lineage["source_release"] = {"state": released.get("release_state", "unknown"),
                                             "verified": bool(released.get("verified"))}
            except native.NativeError as exc:
                lineage["source_release"] = {"state": "not_released", "reason": str(exc)}
        chapter = ({"previous_room": room.id, "mode": mode, **chapter_brief,
                    "lineage": lineage} if rolling else
                   {"previous_room": room.id, "mode": "blank", "brief": "",
                    "size_chars": 0, "sources": [], "lineage": lineage})
        return self._settle_command(client_id, journal.ACCEPTED, self._command_result(
            "new", "created", ("Created the next native chapter with a compact durable handoff."
                               if rolling else "Created an empty native thread with no inherited transcript."), native=fresh,
            new_room=created_room.as_json(full=False), chapter=chapter))

    def _account_recovery(self, room, worker, terminal):
        """Refresh changed logins only for our idle workers, without replaying input."""
        if worker is None:
            return {"state": "current"}
        identity = workers.login_identity()
        changed = identity is not None and identity != worker.login_identity
        failed_auth = terminal and terminal.get("kind") == "auth"
        if not changed and not failed_auth:
            return {"state": "current"}
        key = (identity, terminal.get("turn_id") if failed_auth else "login")
        with self._account_recovery_lock:
            prior = self._account_recoveries.get(room.thread_id)
            if prior and prior["key"] == key:
                if prior["state"] == "refreshed" or prior.get("pending") or prior["attempts"] >= 3 or time.monotonic() - prior["at"] < 60:
                    return {"state": prior["state"], "message": prior.get("message", "")}
            if self.workers.busy(room.thread_id) or self._unsettled_for(room.thread_id):
                return {"state": "deferred", "message": "Login changed; this session will refresh when its current work finishes."}
            record = {"key": key, "state": "deferred", "pending": True, "at": time.monotonic(),
                      "attempts": prior["attempts"] + 1 if prior and prior["key"] == key else 1,
                      "message": "Refreshing this session with the current login…"}
            self._account_recoveries[room.thread_id] = record
        def recover():
            try:
                with self.recovery_gate.admit():
                    result = self.refresh_connection(room)
                record.update(state="refreshed" if result.get("state") == "refreshed" else "failed",
                              message=("Session refreshed with the current login. Previous prompts were not resent."
                                       if result.get("state") == "refreshed" else "The current Codex login is unavailable. Sign in on this agent’s host, then refresh this session."))
            except Exception:
                record.update(state="failed", message="Automatic refresh could not finish. Use the session refresh control to retry.")
            finally:
                record["pending"] = False
        threading.Thread(target=recover, name="ux46-login-recovery", daemon=True).start()
        return {"state": "deferred", "message": record["message"]}

    def room_state(self, room: discovery.Room, refresh: bool = False, allow_recovery: bool = True) -> dict:
        payload = room.as_json(full=False)
        payload["draft"] = self.journal.draft(room.id)
        payload["submissions"] = [s.as_json() for s in self.journal.recent(room.id, limit=10)]
        if not room.controllable:
            payload["ownership"] = {
                "state": "not_controllable",
                "detected": True,
                "detail": discovery.CAPABILITY_LABEL.get(room.capability, ""),
            }
            payload["capability_short"] = discovery.CAPABILITY_SHORT.get(room.capability, "")
            payload["native"] = None
            return payload
        try:
            speaker = self.sessions_for(room.thread_id)
            state = speaker.ownership(room.thread_id, refresh=refresh)
            payload["ownership"] = state.as_json()
            connecting = self.workers.attaching_worker(room.thread_id)
            if connecting:
                pending = connecting.sessions.ownership(room.thread_id, refresh=refresh)
                if pending.detected and not pending.external_pids:
                    payload["ownership"] = dict(pending.as_json(), state="connecting",
                                                atlas_owned=False,
                                                detail="Connecting this conversation in UX46. Your devices share this connection.")
            payload["ownership"]["also_open_as"] = [
                other.id for other in self.discovery.rooms_for_thread(room.thread_id)
                if other.id != room.id
            ]
            other = self.other_room_owner(room)
            if other:
                payload["ownership"]["held_by_room"] = other
            thread = speaker.read_thread(room.thread_id).get("thread") or {}
            worker = self.workers.worker(room.thread_id)
            payload["native"] = {
                "thread_id": str(thread.get("id") or room.thread_id),
                "cwd": str(thread.get("cwd") or room.cwd),
                "model": thread.get("model"),
                # Native metadata explicitly calls this a configured or
                # persisted value, not per-turn execution telemetry. Null is
                # shown as unknown by the client; UX46 never guesses it.
                "reasoning_effort": thread.get("reasoningEffort"),
                "source": thread.get("source"),
                "name": thread.get("name"),
                "updated_at": thread.get("updatedAt"),
                "created_at": thread.get("createdAt"),
                "worker_provenance": discovery.worker_from_thread(thread),
                "active_turn": speaker.active_turn(room.thread_id),
                # Whether this conversation has a native process of its own.
                "worker": worker.as_json() if worker else None,
                # What this session was verified to be running under, when
                # UX46 is holding it. Null when nothing is held, because
                # nothing has been asked for or answered.
                "execution_profile": worker.profile if worker else None,
                "execution_policy": self.execution_policy,
                # ``model/list`` is fetched only by an explicit command and
                # cached for this app-server; room polling never triggers it.
                "catalog": self.sessions.cached_model_catalog(),
            }
            failure = speaker.latest_terminal_failure(room.thread_id, thread.get("updatedAt"))
            if failure:
                self._turn_state[room.thread_id] = failure
            else:
                self._turn_state.pop(room.thread_id, None)
            payload["native_terminal"] = self._turn_state.get(room.thread_id)
            payload["goal_status"] = speaker.goal_status.read(room.thread_id)
            payload["approvals"] = self.workers.pending_requests(room.thread_id)
            observer = worker or self.workers.reader()
            payload["account_status"] = observer.account_status.read()
            payload["connection_recovery"] = self._account_recovery(room, worker, failure) if allow_recovery else {"state": "inspection_only"}
            if payload["connection_recovery"]["state"] == "deferred":
                payload["account_status"] = {"state": "unknown", "checked_at": None}
        except native.NativeError as exc:
            payload["ownership"] = {"state": "unknown", "detected": False, "detail": str(exc)}
            payload["native"] = None
            payload["approvals"] = []
        return payload


def _own_only(path: Path, mode: int) -> None:
    """Keep private state readable by its owner alone."""

    try:
        os.chmod(path, mode)
    except OSError:
        pass


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "AtlasConsole/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: ConsoleService  # set on the server instance

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # no tokens, no bodies
        if self.service.config.quiet:
            return
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), fmt % args)
        )

    def _security_headers(self, content_type: str) -> list[tuple[str, str]]:
        return [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store, private"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            # media-src 'self' lets the console play the WAV it just made from
            # a stored reply. Same origin only: no data:, no blob:, no remote.
            ("Content-Security-Policy",
             "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
             "media-src 'self'; connect-src 'self'; form-action 'none'; "
             "frame-ancestors 'none'; base-uri 'none'"),
        ]

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(int(status))
        for key, value in self._security_headers(content_type):
            self.send_header(key, value)
        for key, value in extra_headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            # We could not frame this request's body, so the connection cannot
            # be reused safely. Say so on the wire.
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, code: str, message: str, detail=None) -> None:
        payload = {"error": code, "message": message}
        if detail is not None:
            payload["detail"] = detail
        self._json(status, payload)

    # -- request gate ------------------------------------------------------
    def _gate(self) -> AuthDecision:
        service = self.service
        host = self.headers.get("Host", "")
        client_ip = self.client_address[0] if self.client_address else ""
        decision = service.auth.check(host, self.headers, client_ip)
        if not decision.ok:
            raise ApiError(HTTPStatus.FORBIDDEN, "denied", decision.reason or "denied")
        return decision

    def _check_mutation(self, decision: AuthDecision) -> None:
        service = self.service
        host = self.headers.get("Host", "")
        expected = service.auth.origin_for(host)
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin.casefold() != expected.casefold():
            raise ApiError(HTTPStatus.FORBIDDEN, "bad_origin",
                           "a change must come from this console's own page")
        token = self.headers.get("X-Atlas-CSRF", "")
        if not secrets.compare_digest(token, service.csrf_token):
            raise ApiError(HTTPStatus.FORBIDDEN, "bad_csrf", "stale console page — reload it")

    def _consume_body(self) -> None:
        """Read exactly this request's body, before any route runs.

        HTTP framing is the transport's job, not each handler's. A route that
        ignores its body would otherwise leave those bytes in the socket and
        the next request on this keep-alive connection would be parsed from
        them — which is how a Continue press turned the following Send into
        501 "Unsupported method ('{}POST')". Anything we cannot frame exactly
        ends the connection instead of guessing.
        """

        self._raw_body = b""
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().casefold()
        if encoding and encoding != "identity":
            self.close_connection = True
            raise ApiError(HTTPStatus.NOT_IMPLEMENTED, "unsupported_encoding",
                           f"this console does not accept {encoding} bodies")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            length = int(str(raw_length).strip())
        except ValueError:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_length", "bad content length")
        if length < 0:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_length", "bad content length")
        upload_match = re.fullmatch(
            r"(?:/api/agents/[a-z][a-z0-9-]{0,31})?/api/room/[^/]+/[^/]+/files",
            urlparse(self.path).path)
        limit = (self.service.files.max_upload_bytes + MAX_UPLOAD_OVERHEAD) if upload_match else MAX_BODY
        if length > limit:
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                           "that submission is too large for the console")
        if not length:
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "short_body",
                           "the request body ended early")
        self._raw_body = body

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"") or b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "unreadable request body")
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "request body must be an object")
        return payload

    def _multipart_file(self) -> tuple[str, str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_upload", "file uploads use multipart/form-data")
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + getattr(self, "_raw_body", b"")
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_upload", "unreadable multipart upload") from exc
        found: list[tuple[str, str, bytes]] = []
        for part in message.iter_parts() if message.is_multipart() else ():
            if part.get_content_disposition() != "form-data":
                continue
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = part.get_filename()
            data = part.get_payload(decode=True)
            if not filename or data is None:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_upload", "the file part needs a name and bytes")
            found.append((filename, part.get_content_type(), data))
        if len(found) != 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_upload", "send exactly one multipart file field")
        return found[0]

    # -- verbs -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._consume_body()      # exact framing for every route
            decision = self._gate()
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if method == "GET" and path in STATIC_FILES:
                return self._serve_static(path)
            if not path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such page")
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._check_mutation(decision)
            scoped = AGENT_PREFIX_RE.fullmatch(path)
            target_agent = scoped.group(1) if scoped else "local"
            suffix = scoped.group(2) if scoped else path
            # Drafts, queue editing/cancellation, reads and recovery receipts
            # stay usable while new native work is held.
            launches = method == "POST" and (suffix in {"/api/sessions", "/api/test-thread", "/api/approvals/answer", "/api/connection/refresh"}
                or bool(re.fullmatch(r"/api/room/[^/]+/[^/]+/(submit|pending|command|continue|attach)", suffix)))
            if method == "PATCH" and re.fullmatch(r"/api/room/[^/]+/[^/]+/pending/[^/]+", suffix):
                launches = self._body().get("editing") is False
            restoring = suffix == "/api/connection/refresh" or suffix.endswith(("/continue", "/attach"))
            gate = self.service.recovery_gate.admit(target_agent, self.headers.get("X-UX46-Recovery-ID") if restoring else None) if launches else contextlib.nullcontext()
            with gate:
                if scoped:
                    return self._agent_api(method, scoped.group(1), scoped.group(2) or "",
                                           parsed.query, query, decision)
                return self._api(method, path, query, decision)
        except recovery.RecoveryBusy as exc:
            self._error(HTTPStatus.CONFLICT, "recovery_in_progress", str(exc))
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message, exc.detail)
        except native.NativeError as exc:
            status = {
                "held_elsewhere": HTTPStatus.CONFLICT,
                "ownership_unavailable": HTTPStatus.CONFLICT,
                "not_owned": HTTPStatus.CONFLICT,
                "connection_busy": HTTPStatus.CONFLICT,
                "refresh_safety_unknown": HTTPStatus.CONFLICT,
                "release_unconfirmed": HTTPStatus.CONFLICT,
                "agent_unsupported": HTTPStatus.BAD_REQUEST,
                "read_only_runtime": HTTPStatus.CONFLICT,
                "runtime_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
                "uncertain": HTTPStatus.ACCEPTED,
            }.get(exc.code, HTTPStatus.BAD_GATEWAY)
            self._error(status, exc.code, str(exc), exc.detail)
        except remote.RemoteError as exc:
            status = {
                "unknown_agent": HTTPStatus.NOT_FOUND,
                "forbidden_path": HTTPStatus.FORBIDDEN,
                "forbidden_method": HTTPStatus.FORBIDDEN,
                "forbidden_query": HTTPStatus.FORBIDDEN,
                "not_a_remote_agent": HTTPStatus.BAD_REQUEST,
                "agent_response_too_large": HTTPStatus.BAD_GATEWAY,
                "agent_protocol": HTTPStatus.BAD_GATEWAY,
                "agent_request_timeout": HTTPStatus.GATEWAY_TIMEOUT,
            }.get(exc.code, HTTPStatus.SERVICE_UNAVAILABLE)
            # Unavailable is the answer. This console never answers for another
            # agent, so there is no local fallback here.
            self._error(status, exc.code, str(exc), exc.detail)
        except BrokenPipeError:  # client went away mid-poll
            pass
        except Exception as exc:  # pragma: no cover - never leak a traceback
            if not self.service.config.quiet:
                traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "server_error", str(exc)[:200])

    def _serve_static(self, path: str) -> None:
        name, content_type = STATIC_FILES[path]
        file_path = APP_DIR / name  # fixed map: no path joining from the request
        try:
            body = file_path.read_bytes()
        except OSError:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "console asset missing")
        self._send(HTTPStatus.OK, body, content_type)

    # -- agents ------------------------------------------------------------
    def _agent_api(self, method: str, agent_id: str, suffix: str, raw_query: str,
                   query: dict, decision: AuthDecision) -> None:
        """One console operation, addressed to one configured agent.

        The outer request has already been authenticated and, for a mutation,
        CSRF-checked. What arrives here is an agent id and an API suffix — never
        a URL, a host or a path the browser composed. The suffix must match the
        allowlist in ``atlas_remote`` for the local agent and the remote ones
        alike, so both branches reach exactly the same set of operations.
        """

        service = self.service
        agent = service.agents.get(agent_id)
        if not suffix:
            if method != "GET":
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such agent endpoint")
            return self._json(HTTPStatus.OK, {"agent": agent.as_json()})
        remote.check_allowed(method, suffix, raw_query)
        if agent.is_local:
            return self._api(method, suffix, query, decision)
        speech_match = re.fullmatch(r"/api/room/(.+)/speak", suffix)
        if method == "POST" and speech_match and service.voice:
            body = self._body()
            item_id = str(body.get("item_id") or "")
            if not item_id or len(item_id) > 256:
                raise ApiError(HTTPStatus.BAD_REQUEST, "no_item", "name the message to read")
            item = agent.console.message_for_speech(speech_match.group(1), item_id)
            text = native._item_text(item)
            try:
                speech = service.voice.speak(text, str(body.get("voice") or voice.DEFAULT_VOICE))
            except voice.VoiceError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
            payload = speech.as_json()
            payload.update(room=speech_match.group(1), item_id=item_id, type=item.get("type"), agent=agent.id)
            return self._json(HTTPStatus.OK, payload)
        result = service.agents.proxy(
            agent, method, suffix, raw_query,
            headers={name: self.headers.get(name, "")
                     for name in remote.FORWARDED_REQUEST_HEADERS},
            body=(getattr(self, "_raw_body", b"") or None),
        )
        if method == "GET" and suffix == "/api/bootstrap" and result.status == 200 and service.voice:
            payload = json.loads(result.body)
            payload["voice"] = service.voice.status()
            payload["voice_default"] = getattr(service.config, "voice_default", voice.DEFAULT_VOICE)
            payload["voice_location"] = "workspace"
            return self._json(HTTPStatus.OK, payload)
        return self._send(result.status, result.body, result.content_type, result.headers)

    # -- API ---------------------------------------------------------------
    def _api(self, method: str, path: str, query: dict, decision: AuthDecision) -> None:
        service = self.service
        get = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731

        if path.startswith("/api/tell/"):
            if query:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_query", "Tell does not accept query parameters")
            status, payload = tell.request(method, path.removeprefix("/api/tell"),
                                           getattr(self, "_raw_body", b"") or None)
            return self._json(status, payload)

        if path == "/api/execution-policy" and method in {"GET", "POST"}:
            if method == "GET":
                return self._json(HTTPStatus.OK, service.access_defaults.read())
            body = self._body()
            if not isinstance(body, dict) or set(body) != {"policy", "base_revision"}:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_policy", "Choose an access default.")
            with service._connection_lock:
                try:
                    result = service.access_defaults.set(body["policy"], body["base_revision"])
                except execution_policies.PolicyConflict as exc:
                    raise ApiError(HTTPStatus.CONFLICT, "policy_conflict", str(exc)) from exc
                except (ValueError, TypeError) as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_policy", str(exc)) from exc
                service.execution_policy = result["policy"]
                service.workers.execution_policy = result["policy"]
            return self._json(HTTPStatus.OK, result)

        if path.startswith("/api/recovery/"):
            try:
                status, result = recovery.api(service.recovery_gate.root, method, path,
                    self._body() if method == "POST" else None, get("request_id") or None)
            except recovery.RecoveryBusy as exc:
                raise ApiError(HTTPStatus.CONFLICT, "recovery_in_progress", str(exc))
            except (ValueError, OSError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_recovery", "Check the recovery mode, configured agent and request ID")
            return self._json(status, result)

        if method == "GET" and path == "/api/service/activity":
            hosted = service.workers.attached()
            return self._json(HTTPStatus.OK, {"active_turns": sum(bool(w.sessions.active_turn(tid)) for tid, w in hosted.items()),
                "pending_inputs": len(service.workers.pending_requests()) + len(service.journal.unsettled()) + int(service._connection_lock.locked()),
                "workers": len(hosted)})

        if method == "GET" and path == "/api/bootstrap":
            return self._json(HTTPStatus.OK, {
                "csrf": service.csrf_token,
                "mode": decision.mode,
                "identity": decision.identity,
                "node": service.discovery.node,
                "public_origin": service.auth.public_origin if service.auth.public_enabled else "",
                "recovery": {"admission": bool(service.recovery_gate.root), "external_preparation": bool(service.recovery_gate.root), "held_queue": True},
                "runtime_started": service.runtime_started,
                "started_at": service.started_at,
                "seq": service.events.seq,
                "state_dir": str(service.state_dir),
                "vault_errors": service.discovery.errors[:5],
                "remembered_owned": service.journal.remembered_owned(),
                "voice": (service.voice.status() if service.voice
                          else {"enabled": False, "loaded": False, "voices": [],
                                "reason": "the server was started without --voice-model-dir"}),
                "voice_default": getattr(service.config, "voice_default", voice.DEFAULT_VOICE),
            })

        if path == "/api/desktop-state":
            if method == "GET":
                return self._json(HTTPStatus.OK, service.desktops.read())
            if method == "PUT":
                body = self._body()
                try:
                    result = service.desktops.save(body.get("base_version"), body.get("state"))
                except desktops.Conflict as exc:
                    raise ApiError(HTTPStatus.CONFLICT, "desktop_conflict",
                                   "This desktop was changed elsewhere. Your edits are kept.", exc.current)
                except ValueError as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_desktop", str(exc))
                return self._json(HTTPStatus.OK, result)

        if method == "GET" and path == "/api/agents":
            # Identity, capability and availability only. No host, account,
            # port, key path or token is ever published here.
            listing = service.agents.listing()
            if service.voice and service.voice.status().get("enabled"):
                for listed in listing["agents"]:
                    if "voice" not in listed["capabilities"]:
                        listed["capabilities"].append("voice")
            return self._json(HTTPStatus.OK, listing)

        if method == "GET" and path == "/api/skills":
            return self._json(HTTPStatus.OK, {"skills": skill_basket.catalog()})
        skill_match = re.fullmatch(r"/api/skills/([A-Za-z0-9_-]{1,64})", path)
        if method == "GET" and skill_match:
            try:
                return self._json(HTTPStatus.OK, skill_basket.read(skill_match.group(1),
                    path=(query.get("file") or ["SKILL.md"])[0]))
            except ValueError as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, "skill_unknown", str(exc))
        usage_match = re.fullmatch(r"/api/room/([A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96})/usage", path)
        if method == "GET" and (usage_match or path == "/api/usage"):
            day = (query.get("day") or [None])[0]
            if day is not None:
                try:
                    import datetime
                    if datetime.date.fromisoformat(day).isoformat() != day: raise ValueError()
                except ValueError:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_day", "Use a date in YYYY-MM-DD format.")
            if usage_match:
                room = service.require_controllable(service.require_room(usage_match.group(1)))
                thread = service.sessions_for(room.thread_id).read_thread(room.thread_id).get("thread") or {}
                rollout = thread.get("path")
                if not rollout or not Path(rollout).is_file():
                    return self._json(HTTPStatus.OK, {"state": "unavailable", "coverage": "Native usage receipts are unavailable on this connector."})
                result = service.usage.room(rollout, room.id, agent=service.agents.local_id,
                                            project=room.project_id, day=day)
            else:
                result = service.usage.summary(project=(query.get("project") or [None])[0], day=day)
                result["background"] = service.usage.tell_receipts(day=day)
            return self._json(HTTPStatus.OK, result)

        if method == "GET" and path == "/api/projects":
            return self._json(HTTPStatus.OK, {"projects": service.discovery.projects()})

        if method == "GET" and path == "/api/session-options":
            return self._json(HTTPStatus.OK, service.session_options())
        if method == "POST" and path == "/api/sessions":
            return self._json(HTTPStatus.OK, service.create_session(self._body()))

        project_pref_match = re.fullmatch(r"/api/projects/([A-Za-z0-9._-]{1,64})/pref", path)
        if project_pref_match:
            project_id = project_pref_match.group(1)
            if method == "GET":
                return self._json(HTTPStatus.OK, {"pref": service.project_pref(project_id)})
            if method == "PATCH":
                body = self._body()
                base_version = body.get("base_version")
                if not isinstance(base_version, int):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref", "project preferences need a base_version")
                wanted = {key: body[key] for key in ("display_name", "appearance", "icon_file_id") if key in body}
                if not wanted:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref", "name a project preference to change")
                display_name = wanted.get("display_name")
                appearance = wanted.get("appearance")
                icon_file_id = wanted.get("icon_file_id", ...)
                if display_name is not None and (not isinstance(display_name, str) or len(display_name.strip()) > 120):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref", "display_name must be short text")
                if appearance is not None and (not isinstance(appearance, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", appearance)):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref", "appearance must be a short lower-case name")
                if icon_file_id is not ... and icon_file_id is not None and not isinstance(icon_file_id, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref", "icon_file_id must be an uploaded raster id or null")
                service.project_pref(project_id)
                if isinstance(icon_file_id, str):
                    try:
                        service.files.associate_project(icon_file_id, project_id)
                    except files.FileStoreError as exc:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_project_icon", str(exc)) from exc
                try:
                    pref = service.journal.save_project_pref(
                        project_id, base_version, display_name=display_name.strip() if isinstance(display_name, str) else None,
                        appearance=appearance, icon_file_id=icon_file_id,
                    )
                except journal.ProjectPrefConflict as exc:
                    raise ApiError(HTTPStatus.CONFLICT, exc.code, str(exc), exc.current) from exc
                service.events.publish({"type": "project_pref", "project": project_id, "global": True})
                return self._json(HTTPStatus.OK, {"pref": pref})

        file_match = re.fullmatch(r"/api/atlas/files/([A-Za-z0-9_-]{1,128})/(preview|download)", path)
        if method == "GET" and file_match:
            room = get("room") or None
            try:
                record, data = service.files.open_download(file_match.group(1), room=room)
            except files.FileStoreError as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            if file_match.group(2) == "preview":
                if record["preview_url"] is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "preview_unavailable", "this attachment is download-only")
                return self._send(HTTPStatus.OK, data, record["mime"],
                                  (("Content-Disposition", "inline; " + files.content_disposition(record["name"])[12:]),))
            return self._send(HTTPStatus.OK, data, record["mime"],
                              (("Content-Disposition", files.content_disposition(record["name"])),))

        if method == "GET" and path == "/api/rooms":
            workers = get("workers")
            wanted = [rid for rid in (get("ids") or "").split(",") if ROOM_ID_RE.match(rid)][:20]
            result = service.discovery.search(
                query=get("query"),
                project=get("project"),
                ids=wanted,
                include_workers=workers in ("1", "only"),
                only_workers=workers == "only",
                include_complete=get("complete", "1") != "0",
                group=get("group") == "1",
                limit=int(get("limit", "40") or 40),
                offset=int(get("offset", "0") or 0),
            )
            return self._json(HTTPStatus.OK, result)

        if method == "GET" and path == "/api/workspace":
            # A reading projection only: no preference is written on a GET.
            return self._json(HTTPStatus.OK, service.workspace())

        if method == "GET" and path == "/api/attention":
            return self._json(HTTPStatus.OK, self._attention())

        if method == "GET" and path == "/api/events":
            after = int(get("after", "0") or 0)
            room = get("room")
            timeout = min(float(get("timeout", "25") or 25), 30.0)
            return self._json(HTTPStatus.OK, service.events.since(after, timeout, room, get("epoch")))

        if method == "GET" and path == "/api/approvals":
            try:
                pending = service.workers.pending_requests()
            except native.NativeError:
                pending = []
            for item in pending:
                item["room"] = service._room_for_thread(item.get("thread_id", ""))
            return self._json(HTTPStatus.OK, {"approvals": pending})

        if method == "POST" and path == "/api/connection/refresh":
            # No browser-selected method, command, path, account, or token is
            # accepted here. An optional room narrows the refresh to that one
            # conversation's own process; every other worker keeps running.
            body = self._body()
            wanted = str((body or {}).get("room") or "")
            scoped = service.require_room(wanted) if wanted else None
            return self._json(HTTPStatus.OK,
                              {"connection": service.refresh_connection(scoped)})

        upload_match = re.fullmatch(r"/api/room/([^/]+/[^/]+)/files", path)
        if method == "POST" and upload_match:
            room = service.require_room(upload_match.group(1))
            name, mime, data = self._multipart_file()
            try:
                record = service.files.upload(room.id, name, data, mime, project=room.project_id)
            except files.FileStoreError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_upload", str(exc)) from exc
            service.events.publish({"type": "file", "room": room.id})
            return self._json(HTTPStatus.CREATED, {"file": record})

        native_attachment_match = re.fullmatch(r"/api/room/([^/]+/[^/]+)/item/([A-Za-z0-9_-]{1,128})/attachments", path)
        if method == "POST" and native_attachment_match:
            body = self._body()
            index = body.get("index", 0)
            if not isinstance(index, int):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_attachment", "native attachment index must be a number")
            room = service.require_room(native_attachment_match.group(1))
            record = service.native_item_attachment(room, native_attachment_match.group(2), index)
            service.events.publish({"type": "file", "room": room.id})
            return self._json(HTTPStatus.CREATED, {"file": record})

        if method == "POST" and path == "/api/approvals/answer":
            body = self._body()
            key = str(body.get("key", ""))
            kind = str(body.get("kind", ""))
            decision_value = str(body.get("decision", ""))
            answers = body.get("answers") if isinstance(body.get("answers"), dict) else None
            result = service.workers.answer_request(key, kind, decision_value, answers)
            service.events.publish({"type": "approval", "state": "answered",
                                    "room": service._room_for_thread(result["thread_id"]),
                                    "global": True})
            return self._json(HTTPStatus.OK, {"answered": result})

        pending_match = re.fullmatch(r"/api/room/([^/]+/[^/]+)/pending/([A-Za-z0-9_-]{8,64})", path)
        if pending_match:
            return self._pending_api(method, pending_match.group(1), pending_match.group(2))

        room_match = re.fullmatch(r"/api/room/([^/]+/[^/]+)(?:/(history|search|continue|attach|submit|draft|interrupt|release|detach|refresh|speak|pref|command|pending|chapter-preview))?", path)
        if room_match:
            room_id = room_match.group(1)
            action = room_match.group(2) or ""
            return self._room_api(method, room_id, action, query, get)

        submission_match = re.fullmatch(r"/api/submissions/([A-Za-z0-9_-]{8,64})", path)
        if method == "GET" and submission_match:
            # Recovering one attempt by its exact client id. Read-only: it
            # reports what was journaled, and never dispatches anything.
            submission = service.journal.get(submission_match.group(1))
            if submission is None:
                return self._json(HTTPStatus.NOT_FOUND, {
                    "error": "unknown_submission",
                    "message": "this console never journaled that submission, so it "
                               "was not dispatched",
                    "client_id": submission_match.group(1),
                })
            return self._json(HTTPStatus.OK, {"submission": submission.as_json()})

        if method == "GET" and path == "/api/submissions":
            room = get("room")
            return self._json(HTTPStatus.OK, {
                "submissions": [s.as_json() for s in service.journal.recent(room or None, 50)],
                "unsettled": [s.as_json() for s in service.journal.unsettled()],
            })

        audio_match = re.fullmatch(r"/api/audio/([0-9a-f]{32,64})\.wav", path)
        if method == "GET" and audio_match:
            if not service.voice:
                raise ApiError(HTTPStatus.NOT_FOUND, "voice_off", "local speech is not enabled")
            try:
                audio_path = service.voice.path_for(audio_match.group(1))
            except voice.VoiceError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
            if not audio_path.is_file():
                raise ApiError(HTTPStatus.NOT_FOUND, "audio_gone",
                               "that clip is no longer cached — ask for it again")
            return self._send(HTTPStatus.OK, audio_path.read_bytes(), "audio/wav")

        if method == "POST" and path == "/api/test-thread":
            if not service.config.allow_test_thread:
                raise ApiError(HTTPStatus.FORBIDDEN, "disabled",
                               "test threads are disabled; start with --allow-test-thread")
            self._body()  # drained; the browser cannot choose a directory
            cwd = str(service.config.test_thread_cwd or "")
            if not cwd or not Path(cwd).is_dir():
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_cwd",
                               "start the console with --test-thread-cwd pointing at an "
                               "existing directory")
            result = service.workers.start_test_thread(cwd)
            service.journal.remember_owned(result["thread_id"], "", result["cwd"])
            return self._json(HTTPStatus.OK, result)

        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")

    def _room_api(self, method: str, room_id: str, action: str, query: dict, get) -> None:
        service = self.service
        room = service.require_room(room_id)

        if method == "GET" and action == "pending":
            return self._json(HTTPStatus.OK, {"pending": [item.as_json() for item in service.journal.queue_list(room.id)]})

        if method == "GET" and action == "chapter-preview":
            return self._json(HTTPStatus.OK, service.chapter_preview(room))

        if method == "POST" and action == "pending":
            body = self._body()
            return self._json(HTTPStatus.CREATED, service.queue_message(
                room, str(body.get("client_id", "")), body.get("body", ""), body.get("attachments")))

        if method == "GET" and not action:
            return self._json(HTTPStatus.OK, service.room_state(room, refresh=True, allow_recovery=get("inspect") != "1"))

        if method == "GET" and action == "history":
            service.require_controllable(room)
            limit = int(get("limit", "40") or 40)
            cursor = get("cursor") or None
            direction = get("direction", "desc")
            around = get("around") or ""
            try:
                speaker = service.sessions_for(room.thread_id)
                if around:
                    page = speaker.page_around(room.thread_id, around, radius=20)
                else:
                    page = speaker.list_items(room.thread_id, limit, cursor, direction)
            except native.NativeError as exc:
                if exc.code == "item_unknown":
                    raise ApiError(HTTPStatus.NOT_FOUND, exc.code, str(exc))
                # Both supported reads failed. That is an unavailable history,
                # not an empty one: saying "no messages" about a thread we
                # could not read would be a lie the person cannot see through.
                return self._json(HTTPStatus.OK, {
                    "room": room.id,
                    "thread_id": room.thread_id,
                    "items": [],
                    "next_cursor": None,
                    "backwards_cursor": None,
                    "complete": False,
                    "page_size": limit,
                    "direction": direction,
                    "source": "unavailable",
                    "unavailable": True,
                    "retryable": True,
                    "error_code": exc.code,
                    "message": str(exc),
                })
            items = page.get("data") or []
            empty_session = (
                not items and not cursor and not around
                and page.get("source", "").startswith("thread_read")
            )
            return self._json(HTTPStatus.OK, {
                "room": room.id,
                "thread_id": room.thread_id,
                "items": [_project_item(entry) for entry in items],
                "next_cursor": page.get("nextCursor"),
                "backwards_cursor": page.get("backwardsCursor"),
                # Never claim a full history when the runtime paged it.
                "complete": not bool(page.get("nextCursor")),
                "page_size": limit,
                "direction": direction,
                "source": "empty" if empty_session else page.get("source", "items_list"),
                "note": ("this native session has no saved history yet"
                         if empty_session else ""),
                "total": page.get("total"),
                "focus_index": page.get("focus_index"),
                "earlier_available": page.get("earlier_available"),
                "later_available": page.get("later_available"),
            })

        if method == "GET" and action == "search":
            service.require_controllable(room)
            kinds = tuple(k for k in (get("kinds", "human,final") or "").split(",") if k)
            try:
                result = service.sessions_for(room.thread_id).search_history(
                    room.thread_id, get("q"), kinds or ("human", "final"),
                    limit=int(get("limit", "40") or 40),
                )
            except native.NativeError as exc:
                return self._json(HTTPStatus.OK, {
                    "hits": [], "total": 0, "source": "unavailable",
                    "searched_items": 0, "retryable": True, "error_code": exc.code,
                    "note": "this session's history could not be read just now: "
                            + str(exc),
                })
            return self._json(HTTPStatus.OK, result)

        if method == "GET" and action == "pref":
            return self._json(HTTPStatus.OK, {"pref": service.journal.room_pref(room.id)})

        if method == "POST" and action == "pref":
            body = self._body()
            pinned = body.get("pinned")
            hidden = body.get("hidden")
            for value in (pinned, hidden):
                if value is not None and not isinstance(value, bool):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref",
                                   "pinned and hidden are true or false")
            if pinned is None and hidden is None:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_pref",
                               "say which of pinned or hidden to change")
            # A preference belongs to the conversation, not to whichever of
            # its records the person happened to be looking at. Hiding from one
            # alias hides all of them; pinning names this record as the face.
            members = service.discovery.conversation(room)
            try:
                prefs = service.journal.set_group_pref(
                    [member.id for member in members],
                    selected=room.id, pinned=pinned, hidden=hidden,
                )
            except journal.JournalError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
            service.events.publish({"type": "workspace", "room": room.id, "global": True})
            return self._json(HTTPStatus.OK, {
                "pref": prefs.get(room.id, service.journal.room_pref(room.id)),
                "conversation": prefs,
            })

        if method == "GET" and action == "draft":
            return self._json(HTTPStatus.OK, service.journal.draft(room.id))

        if method == "PUT" and action == "draft":
            body = self._body()
            text = body.get("body")
            if not isinstance(text, str) or len(text) > MAX_BODY:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_draft", "a draft must be text")
            try:
                saved = service.journal.save_draft(
                    room.id, text, int(body.get("base_version", 0) or 0),
                    device=str(body.get("device", ""))[:40],
                )
            except journal.DraftConflict as exc:
                raise ApiError(HTTPStatus.CONFLICT, "draft_conflict",
                               "this draft was changed on another device", exc.current)
            service.events.publish({"type": "draft", "room": room.id})
            return self._json(HTTPStatus.OK, saved)

        # "attach" is the agent-neutral name for the same operation the
        # console has always called Continue here.
        if method == "POST" and action in ("continue", "attach"):
            service.require_controllable(room)
            other = service.other_room_owner(room)
            if other:
                raise ApiError(HTTPStatus.CONFLICT, "held_by_room",
                               f"the same native session is already open here as {other}")
            result = service.workers.attach(room.thread_id, room.id)
            service.journal.remember_owned(room.thread_id, room.id, result.get("cwd", ""))
            service.events.publish({"type": "ownership", "room": room.id, "state": "atlas_owned"})
            return self._json(HTTPStatus.OK, {"continued": result, "attached": result,
                                              "room": service.room_state(room, refresh=True)})

        # "detach" is the agent-neutral name for Release.
        if method == "POST" and action in ("release", "detach"):
            service.require_controllable(room)
            return self._release(room)

        if method == "POST" and action == "refresh":
            service.require_controllable(room)
            return self._json(HTTPStatus.OK,
                              {"connection": service.refresh_connection(room)})

        if method == "POST" and action == "interrupt":
            service.require_controllable(room)
            service.writer_for(room.thread_id).interrupt(room.thread_id)
            return self._json(HTTPStatus.OK, {"interrupted": room.thread_id})

        if method == "POST" and action == "speak":
            return self._speak(room)

        if method == "POST" and action == "submit":
            return self._submit(room)

        if method == "POST" and action == "command":
            body = self._body()
            raw = body.get("command")
            if not isinstance(raw, str) or len(raw) > MAX_BODY:
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_command", "a command must be short text")
            target = str(body.get("thread_id", ""))
            if target and target != room.thread_id:
                raise ApiError(HTTPStatus.CONFLICT, "wrong_target",
                               "that command was written for a different native session",
                               {"expected": room.thread_id})
            result = service.command(room, raw, str(body.get("client_id", "")))
            command = result.get("command") or {}
            if command.get("name") not in {"help", "status", "refresh"}:
                result["room"] = service.room_state(room, refresh=True)
            return self._json(HTTPStatus.OK, result)

        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such room endpoint")

    def _pending_api(self, method: str, room_id: str, client_id: str) -> None:
        room = self.service.require_room(room_id)
        body = self._body()
        version = body.get("version")
        if not isinstance(version, int):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_version", "name the queued message version")
        if method == "DELETE":
            return self._json(HTTPStatus.OK, self.service.cancel_queued_message(room, client_id, version))
        if method == "PATCH":
            editing = body.get("editing")
            if not isinstance(editing, bool):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_edit", "say whether editing is true or false")
            text = body.get("body")
            attachments = body.get("attachments")
            if text is not None and not isinstance(text, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_message", "a queued message must be text")
            if attachments is not None and not isinstance(attachments, list):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_attachment", "attachments must be a list")
            return self._json(HTTPStatus.OK, self.service.update_queued_message(
                room, client_id, version, text, attachments, editing, body.get("position")))
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "no such queued message endpoint")

    def _release(self, room: discovery.Room) -> None:
        """Answer Release truthfully, including when it is not yet complete."""

        result = self.service.release_conversation(room)
        # ``released`` stays the thread id the request named, so an existing
        # client keeps working; ``release_state`` is the honest outcome.
        payload = dict(result, released=result["thread_id"])
        return self._json(HTTPStatus.OK, payload)

    def _speak(self, room: discovery.Room) -> None:
        """Read one existing native message aloud, locally.

        The text is looked up server side from the item id, so the browser
        cannot ask the console to speak arbitrary words.
        """

        service = self.service
        if not service.voice:
            raise ApiError(HTTPStatus.NOT_FOUND, "voice_off",
                           "this console was started without local speech")
        body = self._body()
        item_id = str(body.get("item_id", ""))
        wanted = str(body.get("voice") or getattr(service.config, "voice_default",
                                                  voice.DEFAULT_VOICE))
        if not item_id:
            raise ApiError(HTTPStatus.BAD_REQUEST, "no_item", "name the message to read")
        service.require_controllable(room)
        entries, _source = service.sessions_for(room.thread_id).full_history(room.thread_id)
        found = next(
            (e for e in entries if str((e.get("item") or {}).get("id") or "") == item_id),
            None,
        )
        if not found:
            raise ApiError(HTTPStatus.NOT_FOUND, "item_unknown",
                           "that message is not in this session's native history")
        item = found.get("item") or {}
        text = native._item_text(item)
        if not text.strip():
            raise ApiError(HTTPStatus.CONFLICT, "nothing_to_read",
                           "that item has no text to read aloud")
        try:
            speech = service.voice.speak(text, wanted)
        except voice.VoiceError as exc:
            status = (HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "voice_unavailable"
                      else HTTPStatus.BAD_REQUEST)
            raise ApiError(status, exc.code, str(exc))
        payload = speech.as_json()
        payload.update({"room": room.id, "item_id": item_id, "type": item.get("type")})
        return self._json(HTTPStatus.OK, payload)

    def _submit(self, room: discovery.Room) -> None:
        """Serialize a submission against the fixed managed-auth refresh."""

        with self.service._connection_lock:
            return self._submit_locked(room)

    def _submit_locked(self, room: discovery.Room) -> None:
        """Journal first, dispatch once, and never invent a delivery state."""

        service = self.service
        body = self._body()
        client_id = str(body.get("client_id", ""))
        text = body.get("body")
        target = str(body.get("thread_id", ""))
        if not CLIENT_ID_RE.match(client_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                           "a submission needs a stable client id")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty", "there is nothing to send")
        if len(text) > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "that is too long")
        service.require_controllable(room)
        # The browser may confirm the target it believes it is typing into, but
        # the authoritative id always comes from the server-side record.
        if target and target != room.thread_id:
            raise ApiError(HTTPStatus.CONFLICT, "wrong_target",
                           "that draft was written for a different native session",
                           {"expected": room.thread_id})
        other = service.other_room_owner(room)
        if other:
            raise ApiError(HTTPStatus.CONFLICT, "held_by_room",
                           f"the same native session is open here as {other}")

        try:
            submission, created = service.journal.reserve(
                client_id, room.id, room.thread_id, text
            )
        except journal.DuplicateMismatch as exc:
            raise ApiError(HTTPStatus.CONFLICT, exc.code,
                           "that submission id was already used for different text or target",
                           exc.detail)
        if not created:
            # Retry of a known id: report the journaled outcome, call nothing.
            return self._json(HTTPStatus.OK, {
                "submission": submission.as_json(),
                "duplicate": True,
                "dispatched": False,
            })

        # send_input serialises per native thread itself; taking that lock here
        # too would deadlock the dispatch.
        try:
            result = service.writer_for(room.thread_id).send_input(room.thread_id, text, client_id)
        except native.UncertainDelivery as exc:
            settled = service.journal.settle(
                client_id, journal.UNCERTAIN, detail=str(exc)
            )
            service.events.publish({"type": "submission", "room": room.id,
                                    "status": journal.UNCERTAIN})
            return self._json(HTTPStatus.ACCEPTED, {
                "submission": settled.as_json(),
                "dispatched": True,
                "uncertain": True,
            })
        except native.NativeError as exc:
            settled = service.journal.settle(client_id, journal.FAILED, detail=str(exc))
            service.events.publish({"type": "submission", "room": room.id,
                                    "status": journal.FAILED})
            status = HTTPStatus.CONFLICT if exc.code in (
                "held_elsewhere", "not_owned", "ownership_unavailable"
            ) else HTTPStatus.BAD_GATEWAY
            return self._error(status, exc.code, str(exc), {
                "submission": settled.as_json(),
            })
        settled = service.journal.settle(
            client_id,
            journal.ACCEPTED,
            native_turn_id=result.get("turn_id", ""),
            mode=result.get("mode", ""),
        )
        service.events.publish({"type": "submission", "room": room.id,
                                "status": journal.ACCEPTED})
        return self._json(HTTPStatus.OK, {
            "submission": settled.as_json(),
            "dispatched": True,
            "native": result,
        })

    def _attention(self) -> dict:
        """Human-action first, then reported activity. No invented urgency.

        Only the live runtime can put a room in the first list. A saved
        checkpoint is a dated report by whoever wrote it: it is kept, shown and
        counted separately, and one that belongs to finished work, to agent
        work, or to weeks ago is not presented as something waiting on User
        now. Nothing here claims such a report was resolved.
        """

        service = self.service
        rooms = service.discovery.rooms()
        needs, suppressed, watching = [], [], []
        for room in rooms:
            state = str(room.attention.get("state", "none"))
            if state in ("needs_now", "review_ready"):
                reason = _suppression_reason(room)
                (suppressed if reason else needs).append((room, reason))
            elif state == "needs_soon" and not _suppression_reason(room):
                watching.append(room)
        needs.sort(key=lambda pair: (pair[0].attention.get("rank", 0), pair[0].last_active),
                   reverse=True)
        suppressed.sort(key=lambda pair: pair[0].last_active, reverse=True)
        watching.sort(key=lambda room: room.last_active, reverse=True)
        try:
            approvals = service.workers.pending_requests()
        except native.NativeError:
            approvals = []
        for item in approvals:
            item["room"] = service._room_for_thread(item.get("thread_id", ""))
        return {
            "approvals": approvals,
            # The only live obligations UX46 can prove.
            "needs_person": [_reported(room, reason) for room, reason in needs[:20]],
            "needs_person_total": len(needs),
            "reported_suppressed": [_reported(room, reason) for room, reason in suppressed[:20]],
            "reported_suppressed_total": len(suppressed),
            "stale_after_days": discovery.STALE_CHECKPOINT_DAYS,
            "watching": [r.as_json() for r in watching[:10]],
            "watching_total": len(watching),
            "unsettled": [s.as_json() for s in service.journal.unsettled()],
        }


def _suppression_reason(room: discovery.Room) -> str:
    """Why a saved report is history rather than a live request."""

    if room.worker_provenance:
        return "agent work"
    if room.status in ("complete", "archived"):
        return f"the record is marked {room.status}"
    age = discovery.checkpoint_age_days(room)
    if age is not None and age > discovery.STALE_CHECKPOINT_DAYS:
        return f"reported {int(age)} days ago"
    return ""


def _reported(room: discovery.Room, reason: str) -> dict:
    payload = room.as_json()
    age = discovery.checkpoint_age_days(room)
    payload["reported_age_days"] = None if age is None else round(age, 1)
    payload["suppressed_reason"] = reason
    return payload


def _item_attachment_refs(item: dict) -> list[dict]:
    """Only app-server attachment objects recorded on this exact native item.

    A browser cannot contribute a path to this list.  File-change records and
    message text are deliberately not interpreted as file references.
    """

    found: list[dict] = []
    candidates = list(item.get("content") or []) + list(item.get("attachments") or [])
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("type") not in {"localImage", "localFile"}:
            continue
        path = candidate.get("path")
        if not isinstance(path, str) or not path:
            continue
        entry = {"path": path}
        if isinstance(candidate.get("name"), str):
            entry["name"] = candidate["name"]
        if isinstance(candidate.get("mime"), str):
            entry["mime"] = candidate["mime"]
        found.append(entry)
    return found


def _project_item(entry: dict) -> dict:
    """Project one native item into what the console renders.

    No semantics are inferred from text: the shape comes from the documented
    item type, and unknown types are surfaced as themselves.
    """

    item = entry.get("item") if isinstance(entry, dict) else None
    if not isinstance(item, dict):
        return {"type": "unknown", "turn_id": str((entry or {}).get("turnId", "")), "id": ""}
    kind = str(item.get("type") or "unknown")
    out = {
        "type": kind,
        "id": str(item.get("id") or ""),
        "turn_id": str(entry.get("turnId") or ""),
    }
    if kind == "userMessage":
        parts = []
        for piece in item.get("content") or []:
            if isinstance(piece, dict) and piece.get("type") == "text":
                parts.append(str(piece.get("text") or ""))
            elif isinstance(piece, dict):
                parts.append(f"[{piece.get('type', 'attachment')}]")
        out["text"] = "\n".join(parts)
        out["client_id"] = str(item.get("clientId") or "")
    elif kind == "agentMessage":
        out["text"] = str(item.get("text") or "")
        # Phase is only reported when the provider supplies it.
        out["phase"] = item.get("phase")
        out["questions"] = [
            {"title": str(q.get("title", "")), "options": [str(o) for o in (q.get("options") or [])]}
            for q in (item.get("questions") or []) if isinstance(q, dict)
        ]
    elif kind == "reasoning":
        out["summary"] = [str(s) for s in (item.get("summary") or [])][:8]
    elif kind == "commandExecution":
        out.update({
            "command": str(item.get("command") or ""),
            "cwd": str(item.get("cwd") or ""),
            "status": str(item.get("status") or ""),
            "exit_code": item.get("exitCode"),
            "duration_ms": item.get("durationMs"),
            "output": str(item.get("aggregatedOutput") or ""),
        })
    elif kind == "fileChange":
        changes = []
        for change in item.get("changes") or []:
            if isinstance(change, dict):
                changes.append({
                    "path": str(change.get("path") or change.get("file") or ""),
                    "kind": str(change.get("kind") or change.get("type") or ""),
                })
        out.update({"status": str(item.get("status") or ""), "changes": changes[:50]})
    elif kind in ("mcpToolCall", "dynamicToolCall"):
        out.update({
            "tool": str(item.get("tool") or ""),
            "server": str(item.get("server") or item.get("namespace") or ""),
            "status": str(item.get("status") or ""),
            "duration_ms": item.get("durationMs"),
        })
    elif kind == "functionCallOutput":
        output = item.get("output")
        out.update({
            "name": str(item.get("name") or ""),
            "output": output if isinstance(output, str) else json.dumps(output)[:4000],
        })
    elif kind == "webSearch":
        out["query"] = str(item.get("query") or "")
    elif kind == "plan":
        out["text"] = str(item.get("text") or "")
    else:
        out["raw_keys"] = sorted(k for k in item.keys() if k != "type")[:12]
    return out


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            # SQLite's transaction context does not close a connection. Each
            # HTTP thread owns a Journal connection; retire it with the thread.
            self.service.journal.close()

    def __init__(self, address, handler_class, service: ConsoleService):
        self.service = service
        handler = type("BoundHandler", (handler_class,), {"service": service})
        super().__init__(address, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UX46 console over native sessions")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only by design)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(journal.default_state_dir()),
                        help="private journal/draft directory (not Git, Vault or Tell)")
    parser.add_argument("--registry", default="", help="override the UX46 registry path")
    parser.add_argument("--native-db", default="",
                        help="override the read-only native session catalogue "
                             "(default: $CODEX_HOME/state_5.sqlite)")
    parser.add_argument("--generated-root", default="",
                        help="optional dedicated server-managed generated asset directory; native attachment "
                             "copies remain confined here or to the selected project root")
    parser.add_argument("--public-origin", default="",
                        help="exact private HTTPS origin served by a loopback proxy. When set, "
                             "that origin and its verified identity are the ONLY way in, on the "
                             f"laptop too (suggested: {DEFAULT_PUBLIC_ORIGIN})")
    parser.add_argument("--public-user", default=DEFAULT_PUBLIC_USER,
                        help="exact identity accepted on the public origin")
    parser.add_argument("--codex-command", nargs="+",
                        default=[os.environ.get("ATLAS_CODEX_BIN")
                                 or "codex", "app-server"],
                        help="command that speaks the app-server protocol on stdio")
    parser.add_argument("--agents-config", default="",
                        help="trusted JSON file (schema_version 1) naming the remote agents "
                             "this console can reach: agent id, label, runtime, node and the "
                             "ssh target/port/identity that carries its account-local loopback "
                             "console. Omit it for only the local agent")
    parser.add_argument("--recovery-root", help="Private installation admission/receipt directory; configured by the launcher")
    parser.add_argument("--local-agent-label", default="Codex",
                        help="the name shown for this machine's own agent")
    parser.add_argument(
        "--execution-policy", choices=list(native_profile.POLICIES),
        default=native_profile.PRESERVE,
        help="Initial access default (a saved UI choice takes precedence): "
             "workspace-write restricts writes to the project and asks for broader access; "
             "full-access uses this OS account without routine approvals; preserve keeps "
             "CLI/session settings. Existing workers change only on reconnect.")
    parser.add_argument("--allow-test-thread", action="store_true",
                        help="allow creating one dedicated harmless native thread")
    parser.add_argument("--test-thread-cwd", default="",
                        help="directory for that test thread")
    parser.add_argument("--start-runtime", action="store_true",
                        help="launch the native runtime at startup instead of on first use")
    parser.add_argument("--voice-model-dir", default="",
                        help="enable local speech from Kokoro model files in this directory")
    parser.add_argument("--voice-default", default=voice.DEFAULT_VOICE,
                        choices=list(voice.VOICES), help="voice used when none is chosen")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("atlas console refuses to bind a non-loopback address", file=sys.stderr)
        return 2
    service = ConsoleService(args)
    server = ConsoleServer((args.host, args.port), ConsoleHandler, service)
    if args.start_runtime:
        try:
            service.sessions  # noqa: B018 - starts the subprocess
        except native.NativeError as exc:
            print(f"native runtime unavailable: {exc}", file=sys.stderr)
    origin = f"http://{args.host}:{args.port}/"
    if service.auth.public_enabled:
        print(f"UX46 console bound to {origin} (loopback only)")
        print(f"reachable at {service.auth.public_origin} for "
              f"{service.auth.public_user} — that origin and identity are the only way in, "
              f"including from this machine")
    else:
        print(f"UX46 console on {origin} — local owner mode, no public origin configured")
    print(f"state: {service.state_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.workers.shutdown()
        service.agents.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
