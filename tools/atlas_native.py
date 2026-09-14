"""Native runtime adapter for the UX46 console.

An UX46-owned Codex ``app-server`` subprocess speaks JSON-RPC over stdio.
Threads live inside that runtime: Codex keeps its own context, tools, agents
and lifecycle. This module never implements an agent loop, never summarises a
transcript, and never resumes a thread that another process is holding.

One connection object is one process. ``tools/atlas_workers.py`` gives each
attached conversation a process of its own, because Codex keeps a writer on a
resumed thread after ``thread/unsubscribe`` and only process exit frees it.

Documented lifecycle (developers.openai.com/codex/app-server):
``initialize`` then ``initialized``; ``thread/read`` and ``thread/items/list``
for history; ``thread/resume`` only when taking control; ``turn/steer`` needs
an ``expectedTurnId`` precondition.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import atlas_native_profile as native_profile
from ux46_goal import GoalStatus

CLIENT_NAME = "atlas-console"
CLIENT_VERSION = "1.0.0"

# Server -> client requests we answer. Anything else is refused explicitly so a
# native turn fails loudly instead of hanging on a silent client.
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "user_input",
    "execCommandApproval": "command_legacy",
    "applyPatchApproval": "file_change_legacy",
}

ROLLOUT_RE = re.compile(r"rollout-[0-9T:\-]+-([0-9a-f-]{36})\.jsonl$")

# Codex keeps a per-thread writer lock open for a retention window after the
# thread is unsubscribed. A holder of this file is a writer even when it has
# closed the rollout, so ownership must look at both.
WRITER_LOCK_RE = re.compile(r"/thread-writer-locks/([0-9a-f-]{36})\.lock$")

DEFAULT_TIMEOUT = 60.0

# Cursor space for pages we serve ourselves from a flattened thread/read.
FLAT_CURSOR = "flat:"
HISTORY_TTL = 20.0
MAX_FLAT_ITEMS = 20000
OWN_PID_TTL = 3.0
SNIPPET = 220

# The app-server schema exposes account/read {refreshToken: true} for managed
# ChatGPT auth. UX46 never receives, stores, or returns a credential; it only
# asks the running app-server to use its normal managed refresh path.
_AUTH_WORDS = (
    "unauthorized", "access token", "token could not be refreshed", "sign in again",
    "signed in to another account", "logged out",
)
_FAILED_TURN_STATUSES = {"failed", "error", "aborted", "cancelled", "canceled", "timeout"}


class NativeError(RuntimeError):
    """A failure that the console can report truthfully to the person."""

    def __init__(self, message: str, code: str = "native_error", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class UncertainDelivery(NativeError):
    """The request may or may not have reached the runtime.

    Never auto-retried: a repeated turn is a real side effect.
    """

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message, code="uncertain", detail=detail)


def _failure_words(value: Any, depth: int = 0) -> list[str]:
    """Read only small diagnostic fields; never copy arbitrary error payloads."""

    if depth > 4:
        return []
    if isinstance(value, dict):
        words: list[str] = []
        for key in ("code", "message", "type", "kind", "status", "codex_error_info", "codexErrorInfo"):
            field = value.get(key)
            if isinstance(field, (str, int)):
                words.append(str(field)[:240])
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                words.extend(_failure_words(nested, depth + 1))
        return words
    if isinstance(value, list):
        words: list[str] = []
        for nested in value[:20]:
            words.extend(_failure_words(nested, depth + 1))
        return words
    return []


def terminal_failure(value: dict) -> dict | None:
    """Project a terminal native error into safe, actionable state.

    The native error object can contain provider-specific details. The console
    needs only whether a turn stopped and whether usage or authentication
    needs attention, so neither raw error text nor credentials cross this boundary.
    """

    status = str(value.get("status") or "").casefold()
    if value.get("willRetry") is True or status in {"completed", "inprogress", "in_progress", "running"}:
        return None
    words = " ".join(_failure_words(value.get("error", value))).casefold()
    is_usage = any(word in words for word in ("usage_limit_exceeded", "usagelimitexceeded", "hit your usage limit"))
    is_auth = any(word in words for word in _AUTH_WORDS)
    if not is_usage and not is_auth and status not in _FAILED_TURN_STATUSES:
        return None
    if is_usage:
        message = ("Usage limit reached for this agent’s Codex account. Switch its account "
                   "or restore available usage, then refresh this session. Refreshing alone does not reset the limit. "
                   "UX46 will not resend this prompt automatically.")
        kind = "usage_limit"
    elif is_auth:
        message = ("This native turn stopped because Codex needs you to sign in again. "
                   "Refresh connection, then choose whether to send a new prompt.")
        kind = "auth"
    else:
        message = "This native turn ended with an error before an answer was returned."
        kind = "failed"
    return {
        "turn_id": str(value.get("id") or value.get("turnId") or ""),
        "kind": kind,
        "status": status or "failed",
        "message": message,
    }


# ---------------------------------------------------------------------------
# live ownership detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ownership:
    """Who currently holds a native thread, as observed on this machine."""

    thread_id: str
    external_pids: tuple[int, ...]
    atlas_owned: bool
    detected: bool
    detail: str = ""
    # A writer this connection itself still holds: unsubscribing does not
    # end it, so "released" must not be claimed while it is true.
    native_retained: bool = False

    @property
    def held_elsewhere(self) -> bool:
        return bool(self.external_pids)

    @property
    def state(self) -> str:
        if not self.detected:
            return "unknown"
        if self.external_pids:
            return "held_elsewhere"
        if self.atlas_owned:
            return "atlas_owned"
        if self.native_retained:
            return "releasing"
        return "idle"

    def as_json(self) -> dict:
        return {
            "state": self.state,
            "detected": self.detected,
            "external_pids": list(self.external_pids),
            "atlas_owned": self.atlas_owned,
            "detail": self.detail,
            "native_retained": self.native_retained,
        }


class OwnershipProbe:
    """Bounded, read-only detection of processes holding a rollout file.

    ``lsof`` is read-only and never touches the other process. If it cannot be
    run the probe fails closed: control is refused rather than guessed.
    """

    def __init__(self, lsof: str = "/usr/sbin/lsof", ttl: float = 2.0):
        self.lsof = lsof if os.path.exists(lsof) else (shutil.which("lsof") or lsof)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cache: dict[str, set[int]] | None = None
        self._error = ""

    def _scan(self) -> dict[str, set[int]]:
        try:
            completed = subprocess.run(
                [self.lsof, "-nP", "-c", "codex", "-Fpcn"],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # fail closed
            raise NativeError(
                f"cannot determine native session ownership: {exc}",
                code="ownership_unavailable",
            ) from exc
        # lsof exits 1 when nothing matches; that is a valid empty answer.
        if completed.returncode not in (0, 1):
            raise NativeError(
                "cannot determine native session ownership: lsof returned "
                f"{completed.returncode}",
                code="ownership_unavailable",
            )
        owners: dict[str, set[int]] = {}
        pid = 0
        for line in completed.stdout.splitlines():
            if not line:
                continue
            tag, value = line[0], line[1:]
            if tag == "p":
                pid = int(value) if value.isdigit() else 0
            elif tag == "n" and pid:
                match = ROLLOUT_RE.search(value) or WRITER_LOCK_RE.search(value)
                if match:
                    owners.setdefault(match.group(1), set()).add(pid)
        return owners

    def owners(self, refresh: bool = False) -> dict[str, set[int]]:
        with self._lock:
            fresh = self._cache is not None and (time.time() - self._cached_at) < self.ttl
            if fresh and not refresh:
                return self._cache  # type: ignore[return-value]
            self._cache = self._scan()
            self._cached_at = time.time()
            self._error = ""
            return self._cache

    def check(
        self,
        thread_id: str,
        *,
        own_pids: Iterable[int] = (),
        atlas_owned: bool = False,
        refresh: bool = False,
    ) -> Ownership:
        mine = set(int(p) for p in own_pids)
        try:
            table = self.owners(refresh=refresh)
        except NativeError as exc:
            return Ownership(thread_id, (), atlas_owned, False, str(exc))
        holders = table.get(thread_id, set())
        external = sorted(holders - mine)
        return Ownership(thread_id, tuple(external), atlas_owned, True,
                         native_retained=bool(holders & mine))


# ---------------------------------------------------------------------------
# pending native requests (approvals / questions)
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """A native server request waiting for exactly one human response."""

    request_id: Any
    kind: str
    method: str
    thread_id: str
    turn_id: str
    item_id: str
    params: dict
    created_at: float = field(default_factory=time.time)
    answered: bool = False

    @property
    def key(self) -> str:
        return f"{self.thread_id}:{self.request_id}"

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "method": self.method,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "created_at": self.created_at,
            "params": _safe_params(self.kind, self.params),
        }


def _safe_params(kind: str, params: dict) -> dict:
    """Only the fields the console renders, all as plain text."""

    if kind in ("command", "command_legacy"):
        return {
            "command": params.get("command") or params.get("command_line") or "",
            "cwd": params.get("cwd", ""),
            "reason": params.get("reason") or "",
        }
    if kind in ("file_change", "file_change_legacy"):
        changes = params.get("changes") or params.get("fileChanges") or []
        names: list[str] = []
        if isinstance(changes, dict):
            names = list(changes.keys())
        elif isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict):
                    names.append(str(change.get("path") or change.get("file") or ""))
        return {
            "reason": params.get("reason") or "",
            "grant_root": params.get("grantRoot") or "",
            "paths": [n for n in names if n][:20],
        }
    if kind == "permissions":
        return {
            "cwd": params.get("cwd", ""),
            "reason": params.get("reason") or "",
            "permissions": params.get("permissions") or {},
        }
    if kind == "user_input":
        questions = params.get("questions") or []
        out = []
        for q in questions:
            if isinstance(q, dict):
                out.append({
                    "id": str(q.get("id") or q.get("key") or len(out)),
                    "title": str(q.get("title") or q.get("prompt") or ""),
                    "options": [str(o) for o in (q.get("options") or [])],
                })
        return {"questions": out, "blocking": bool(params.get("isBlocking"))}
    return {}


# ---------------------------------------------------------------------------
# the app-server connection
# ---------------------------------------------------------------------------

class AppServer:
    """One UX46-owned ``codex app-server`` stdio subprocess."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        on_event: Callable[[dict], None] | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ):
        self.command = command or [_codex_binary(), "app-server"]
        self.on_event = on_event or (lambda event: None)
        self._cwd = cwd
        self._env = env
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._waiters: dict[int, queue.Queue] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._started_at = 0.0
        self._stderr_tail: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def pids(self) -> tuple[int, ...]:
        return (self.proc.pid,) if self.running and self.proc else ()

    @property
    def expected_name(self) -> str:
        """The executable UX46 was configured to launch, by name only."""

        return os.path.basename(self.command[0]) if self.command else ""

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            env = dict(os.environ)
            if self._env:
                env.update(self._env)
            try:
                self.proc = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=self._cwd,
                    env=env,
                )
            except OSError as exc:
                raise NativeError(
                    f"cannot launch the native runtime: {exc}", code="runtime_unavailable"
                ) from exc
            self._started_at = time.time()
            threading.Thread(target=self._read_loop, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        info = self.request("initialize", {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        self.notify("initialized", {})
        self.on_event({"type": "runtime/started", "info": {
            "codexHome": info.get("codexHome", ""),
            "userAgent": info.get("userAgent", ""),
        }})

    def stop(self) -> None:
        with self._lock:
            proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        except Exception:  # pragma: no cover - best effort shutdown
            try:
                proc.kill()
            except Exception:
                pass

    # -- transport ---------------------------------------------------------
    def _read_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-20]

    def _read_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(message)
            except Exception as exc:  # pragma: no cover - never kill the reader
                self.on_event({"type": "runtime/error", "detail": str(exc)})
        self.on_event({"type": "runtime/exited", "detail": "\n".join(self._stderr_tail[-5:])})

    def _dispatch(self, message: dict) -> None:
        if "id" in message and "method" not in message:
            waiter = self._waiters.pop(message["id"], None)
            if waiter:
                waiter.put(message)
            return
        method = message.get("method", "")
        if "id" in message:  # server -> client request
            self._handle_server_request(message, method)
            return
        self.on_event({
            "type": "notification",
            "method": method,
            "params": message.get("params") or {},
        })

    def _handle_server_request(self, message: dict, method: str) -> None:
        kind = APPROVAL_METHODS.get(method)
        if not kind:
            # Unknown request: refuse explicitly so the turn does not hang.
            self._respond_error(
                message["id"],
                -32601,
                f"{CLIENT_NAME} does not implement {method}",
            )
            self.on_event({"type": "request/unsupported", "method": method})
            return
        params = message.get("params") or {}
        pending = PendingRequest(
            request_id=message["id"],
            kind=kind,
            method=method,
            thread_id=str(params.get("threadId", "")),
            turn_id=str(params.get("turnId", "")),
            item_id=str(params.get("itemId", "")),
            params=params,
        )
        with self._pending_lock:
            self._pending[pending.key] = pending
        self.on_event({"type": "request/pending", "request": pending.as_json()})

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": code, "message": message,
        }})

    def _write(self, payload: dict) -> None:
        proc = self.proc
        if not proc or proc.poll() is not None or not proc.stdin:
            raise NativeError("the native runtime is not running", code="runtime_unavailable")
        line = json.dumps(payload) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise UncertainDelivery(
                    f"the native runtime connection broke while sending: {exc}"
                ) from exc

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._write_lock:
            request_id = self._next_id
            self._next_id += 1
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._waiters[request_id] = waiter
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            self._waiters.pop(request_id, None)
            raise UncertainDelivery(
                f"the native runtime did not answer {method} within {int(timeout)}s",
                detail={"method": method},
            ) from exc
        if "error" in message:
            error = message["error"] or {}
            raise NativeError(
                str(error.get("message") or f"{method} failed"),
                code="native_rejected",
                detail=error,
            )
        return message.get("result") or {}

    # -- pending approvals -------------------------------------------------
    def pending_requests(self, thread_id: str | None = None) -> list[dict]:
        with self._pending_lock:
            items = [p for p in self._pending.values() if not p.answered]
        if thread_id:
            items = [p for p in items if p.thread_id == thread_id]
        return [p.as_json() for p in sorted(items, key=lambda p: p.created_at)]

    def answer_request(self, key: str, response: dict) -> dict:
        """Answer one native request exactly once."""

        with self._pending_lock:
            pending = self._pending.get(key)
            if pending is None:
                raise NativeError("no such native request", code="request_unknown")
            if pending.answered:
                raise NativeError(
                    "that native request was already answered", code="request_answered"
                )
            pending.answered = True
        try:
            self._write({"jsonrpc": "2.0", "id": pending.request_id, "result": response})
        except NativeError:
            with self._pending_lock:
                pending.answered = False
            raise
        with self._pending_lock:
            self._pending.pop(key, None)
        self.on_event({"type": "request/answered", "key": key, "thread_id": pending.thread_id})
        return {"key": key, "thread_id": pending.thread_id, "kind": pending.kind}

    def drop_requests_for(self, thread_id: str) -> None:
        with self._pending_lock:
            for key, pending in list(self._pending.items()):
                if pending.thread_id == thread_id:
                    self._pending.pop(key, None)


APP_SERVER_NAMES = ("codex",)


def _process_table(ps: str = "/bin/ps") -> dict[int, tuple[int, str]]:
    """pid -> (ppid, executable name). No arguments are read or reported."""

    try:
        completed = subprocess.run(
            [ps, "-Ao", "pid=,ppid=,comm="], capture_output=True, text=True,
            timeout=6, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeError(f"cannot read the process table: {exc}",
                          code="ownership_unavailable") from exc
    if completed.returncode != 0:
        raise NativeError("cannot read the process table", code="ownership_unavailable")
    table: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        command = parts[2] if len(parts) > 2 else ""
        table[int(parts[0])] = (int(parts[1]), os.path.basename(command.strip()))
    return table


def app_server_pids(root_pid: int, expected: str = "", ps: str = "/bin/ps",
                    table: dict[int, tuple[int, str]] | None = None) -> set[int]:
    """Exactly the processes that ARE our app-server.

    The process UX46 launched is ours by construction. The only open question
    is whether it is a wrapper: the installed ``codex`` is a Node launcher, so
    the app-server that actually opens rollout files is its native child. That
    one child is ours too; nothing below it is.

    An agent running through UX46 can start another Codex CLI as a worker, and
    that worker owns a different live session. Excusing every descendant would
    let UX46 call that worker's session free and resume a second writer into
    it, so only the launcher chain is excluded.

    Under-claiming is safe — an unrecognised holder is simply treated as
    another writer and UX46 refuses to send. Ambiguity and an unreadable
    process table both fail closed.
    """

    if not root_pid:
        return set()
    rows = table if table is not None else _process_table(ps)
    if root_pid not in rows:
        raise NativeError("the native runtime process is no longer visible",
                          code="ownership_unavailable")
    names = set(APP_SERVER_NAMES)
    wanted = os.path.basename(expected).strip()
    if wanted:
        names.add(wanted)
    candidates = [
        pid for pid, (ppid, name) in rows.items()
        if ppid == root_pid and name and name in names
    ]
    if len(candidates) > 1:
        # Two plausible app-servers under one launcher: we cannot tell which
        # is ours, so we claim neither.
        raise NativeError(
            "cannot identify the native app-server behind the configured launcher "
            f"({len(candidates)} candidates)",
            code="ownership_unavailable",
        )
    return {root_pid} | set(candidates)


def _codex_binary() -> str:
    configured = os.environ.get("ATLAS_CODEX_BIN")
    if configured:
        return configured
    local = Path.home() / ".local" / "bin" / "codex"
    if local.exists():
        return str(local)
    return shutil.which("codex") or "codex"


# ---------------------------------------------------------------------------
# native session facade used by the HTTP layer
# ---------------------------------------------------------------------------

def response_for_decision(kind: str, decision: str, answers: dict | None = None) -> dict:
    """Map a console decision onto the documented native response body."""

    if kind in ("command", "command_legacy"):
        if decision not in ("accept", "decline", "cancel"):
            raise NativeError("unsupported command decision", code="bad_decision")
        if kind == "command_legacy":
            return {"decision": "approved" if decision == "accept" else "denied"}
        return {"decision": decision}
    if kind in ("file_change", "file_change_legacy"):
        if decision not in ("accept", "decline", "cancel"):
            raise NativeError("unsupported file-change decision", code="bad_decision")
        if kind == "file_change_legacy":
            return {"decision": "approved" if decision == "accept" else "denied"}
        return {"decision": decision}
    if kind == "permissions":
        # Accept-once only: never widen a runtime's standing permissions.
        if decision != "accept":
            return {"permissions": {"kind": "denied"}}
        return {"permissions": {"kind": "granted"}, "scope": "once"}
    if kind == "user_input":
        return {"answers": answers or {}}
    raise NativeError("unsupported native request kind", code="bad_decision")


class NativeSessions:
    """Thread-level operations with ownership and serialization rules."""

    def __init__(self, server: AppServer, probe: OwnershipProbe | None = None,
                 execution_policy: str = native_profile.PRESERVE):
        self.server = server
        self.probe = probe or OwnershipProbe()
        # Which permissions this console asks a thread to run under. A host
        # setting, fixed at startup, never inferred from anything a session
        # said and never changed by a message.
        if execution_policy not in native_profile.POLICIES:
            raise ValueError(f"unknown execution policy {execution_policy!r}")
        self.execution_policy = execution_policy
        self._owned: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._active_turns: dict[str, str] = {}
        self._empty_threads: set[str] = set()
        self._history: dict[str, dict] = {}
        self._history_guard = threading.Lock()
        self._terminal: dict[str, tuple[str, dict | None]] = {}
        self._terminal_guard = threading.Lock()
        self._own_pids: set[int] = set()
        self._own_pids_at = 0.0
        # Runtime-owned picker data; a replacement app-server gets a fresh
        # cache, so UX46 never maintains a stale model list of its own.
        self._model_catalog: dict | None = None
        self._model_catalog_lock = threading.Lock()
        self.goal_status = GoalStatus(server)

    # -- ownership ---------------------------------------------------------
    def own_pids(self, refresh: bool = False) -> set[int]:
        """Our app-server process chain — not the work it spawns."""

        roots = self.server.pids
        if not roots:
            return set()
        now = time.time()
        if not refresh and self._own_pids and (now - self._own_pids_at) < OWN_PID_TTL:
            return self._own_pids
        expected = self.server.expected_name
        mine: set[int] = set()
        for root in roots:
            mine |= app_server_pids(root, expected)
        self._own_pids = mine
        self._own_pids_at = now
        return mine

    def ownership(self, thread_id: str, refresh: bool = False) -> Ownership:
        try:
            mine = self.own_pids(refresh=refresh)
        except NativeError as exc:
            # Fail closed: an unknown tree is not permission to write.
            return Ownership(thread_id, (), thread_id in self._owned, False, str(exc))
        return self.probe.check(
            thread_id,
            own_pids=mine,
            atlas_owned=thread_id in self._owned,
            refresh=refresh,
        )

    def owned_threads(self) -> dict[str, dict]:
        return dict(self._owned)

    def mark_owned(self, thread_id: str, cwd: str = "", room: str = "") -> None:
        self._owned[thread_id] = {"cwd": cwd, "room": room, "claimed_at": time.time()}

    def release(self, thread_id: str) -> None:
        self._owned.pop(thread_id, None)
        self._active_turns.pop(thread_id, None)

    def release_to_codex(self, thread_id: str) -> dict:
        """Unload an idle thread from this app-server. Not a proof of release.

        Codex retains the thread's writer after an unsubscribe, so this alone
        can never justify telling the person the session is free. Only
        stopping the process that holds the writer does; see
        ``atlas_workers.SessionWorkerPool.release``.
        """

        with self.lock_for(thread_id):
            self.require_idle_control(thread_id)
            result = self.server.request("thread/unsubscribe", {"threadId": thread_id})
            if result.get("status") not in ("unsubscribed", "notLoaded", "notSubscribed", None):
                raise NativeError("Codex has not confirmed that the session was unloaded", code="release_unconfirmed")
            self.release(thread_id)
            state = self.ownership(thread_id, refresh=True)
            released = state.detected and not state.native_retained and not state.held_elsewhere
            return {"release_state": "released" if released else "releasing",
                    "verified": released,
                    "native_status": result.get("status") or "unsubscribed",
                    "ownership": state.as_json()}

    def lock_for(self, thread_id: str) -> threading.Lock:
        with self._locks_guard:
            if thread_id not in self._locks:
                self._locks[thread_id] = threading.Lock()
            return self._locks[thread_id]

    def require_control(self, thread_id: str) -> Ownership:
        """Admission check. Refuses on unknown ownership (fails closed)."""

        state = self.ownership(thread_id, refresh=True)
        if not state.detected:
            raise NativeError(
                "cannot verify who is holding this native session, so UX46 will not "
                "send into it",
                code="ownership_unavailable",
                detail=state.as_json(),
            )
        if state.held_elsewhere:
            raise NativeError(
                "Open in another terminal — finish or exit there to continue here",
                code="held_elsewhere",
                detail=state.as_json(),
            )
        return state

    # -- reads (no takeover) ----------------------------------------------
    def list_threads(self, **params) -> dict:
        return self.server.request("thread/list", {k: v for k, v in params.items() if v is not None})

    def model_catalog(self) -> dict:
        """Return the app-server-advertised models and their effort options."""

        with self._model_catalog_lock:
            if self._model_catalog is not None:
                return self._model_catalog
            models = self._all_pages("model/list")
            projected = []
            for item in models:
                if not isinstance(item, dict) or not isinstance(item.get("model"), str):
                    raise NativeError("the native runtime returned an invalid model catalog",
                                      code="catalog_unavailable")
                efforts = []
                for option in item.get("supportedReasoningEfforts") or []:
                    effort = option.get("reasoningEffort") if isinstance(option, dict) else None
                    if isinstance(effort, str) and effort:
                        efforts.append(effort)
                model = item["model"]
                projected.append({
                    "id": str(item.get("id") or model),
                    "model": model,
                    "display_name": str(item.get("displayName") or model),
                    "default_effort": (str(item.get("defaultReasoningEffort"))
                                       if item.get("defaultReasoningEffort") else None),
                    "efforts": list(dict.fromkeys(efforts)),
                })
            self._model_catalog = {"available": True, "models": projected}
            return self._model_catalog

    def cached_model_catalog(self) -> dict:
        """Return a cached catalog without adding room-read polling traffic."""

        with self._model_catalog_lock:
            return self._model_catalog or {"available": False, "models": []}

    def require_idle_control(self, thread_id: str) -> None:
        self.require_control(thread_id)
        if self.active_turn(thread_id):
            raise NativeError("This session is still working. Try this action when the current turn finishes; its work has not been interrupted.", code="connection_busy")
        if self.server.pending_requests(thread_id):
            raise NativeError("This session has an approval waiting. Answer it before trying this action again.", code="connection_busy")

    def update_settings(self, thread_id: str, *, model: str | None = None,
                        effort: str | None = None) -> dict:
        """Apply catalog-validated subsequent-turn settings while idle."""

        if model is None and effort is None:
            raise NativeError("choose a model or reasoning effort", code="bad_command")
        self.require_idle_control(thread_id)
        catalog = self.model_catalog()
        choices = {item["model"]: item for item in catalog["models"]}
        selected = choices.get(model) if model else None
        if model and selected is None:
            raise NativeError("that model is not available from this native connection",
                              code="unsupported_model")
        if effort:
            current = self.read_thread(thread_id).get("thread") or {}
            effort_model = selected or choices.get(str(current.get("model") or ""))
            if not effort_model or effort not in effort_model["efforts"]:
                raise NativeError("that reasoning effort is not available for this model",
                                  code="unsupported_effort")
        params = {"threadId": thread_id}
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        self.server.request("thread/settings/update", params)
        thread = self.read_thread(thread_id).get("thread") or {}
        return {"model": thread.get("model"), "reasoning_effort": thread.get("reasoningEffort")}

    def steer_input(self, thread_id: str, text: str, client_message_id: str) -> dict:
        """Steer a known active turn; never silently start a new one."""

        self.require_control(thread_id)
        if thread_id not in self._owned:
            raise NativeError("UX46 is not holding this native session yet — choose Continue here first",
                              code="not_owned")
        with self.lock_for(thread_id):
            active = self.active_turn(thread_id)
            if not active:
                raise NativeError("there is no active native turn to steer", code="no_active_turn")
            if self.server.pending_requests(thread_id):
                raise NativeError("answer the pending native approval before steering", code="connection_busy")
            result = self.server.request("turn/steer", {
                "threadId": thread_id, "expectedTurnId": active,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": client_message_id,
            })
            turn = result.get("turn") or {}
            return {"mode": "steer", "turn_id": str(turn.get("id") or active),
                    "status": str(turn.get("status") or "inProgress")}

    def compact(self, thread_id: str) -> None:
        self.require_idle_control(thread_id)
        self.server.request("thread/compact/start", {"threadId": thread_id})

    def start_fresh_thread(self, cwd: str, *, model: str | None = None,
                           effort: str | None = None, project_id: str | None = None,
                           developer_instructions: str | None = None) -> dict:
        """Create an empty, UX46-owned native thread without a model turn.

        Only current readback settings are carried forward.  No history,
        prompt, shell command, permissions, or approval policy is supplied.
        """

        params: dict[str, object] = {"cwd": cwd}
        if model:
            params["model"] = model
        if project_id:
            params["projectId"] = project_id
        if developer_instructions:
            # This documented thread/start field is durable context for the
            # new native thread. It is specifically not a turn/input call.
            params["developerInstructions"] = developer_instructions
        # A host that has said its sessions run with full tools says so at the
        # start too, or a new conversation would begin under exactly the
        # restrictions the host chose to lift. Nothing else is overridden: no
        # config rides along, so the configured MCP servers and tools are the
        # ones already there.
        profile = None
        if self.execution_policy == native_profile.FULL_ACCESS:
            try:
                profile = native_profile.full_access_start(cwd)
            except native_profile.ProfileError as exc:
                raise NativeError(str(exc), code="execution_profile_unsupported") from exc
            params.update(profile.params)
        result = self.server.request("thread/start", params)
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise NativeError("the native runtime did not identify the new thread",
                              code="runtime_unavailable")
        if profile is not None:
            # Asking is not being granted. A runtime that answered with
            # something narrower must not leave a session behind that this
            # console has claimed and would then write to.
            try:
                profile.verify_start(result)
            except native_profile.ProfileError as exc:
                try:
                    self.server.request("thread/unsubscribe", {"threadId": thread_id})
                except NativeError:
                    pass  # No further writes admitted even if unloading fails.
                raise NativeError(str(exc), code="execution_profile_mismatch",
                                  detail={"source": profile.source}) from exc
        if effort:
            self.server.request("thread/settings/update", {
                "threadId": thread_id, "effort": effort,
            })
        fresh = self.read_thread(thread_id).get("thread") or thread
        self.mark_owned(thread_id, cwd=str(fresh.get("cwd") or cwd))
        self._empty_threads.add(thread_id)
        return {"thread_id": thread_id, "cwd": str(fresh.get("cwd") or cwd),
                "model": fresh.get("model"), "reasoning_effort": fresh.get("reasoningEffort"),
                "execution_profile": self._start_profile(result, profile)}

    def _start_profile(self, result: dict, profile) -> dict:
        """What the new thread is actually running under, and on whose word.

        Under an explicit host policy this is verified: the runtime was asked
        and its answer was checked. Under the default it is only observed —
        the runtime's own startup values, reported as read rather than claimed
        as preserved, because nothing was requested and no saved session
        profile was consulted.
        """

        observed = {
            "sandbox": result.get("sandbox"),
            "approval_policy": result.get("approvalPolicy"),
            "approvals_reviewer": result.get("approvalsReviewer"),
            "active_permission_profile": result.get("activePermissionProfile"),
        }
        if profile is None:
            return dict(observed, policy=self.execution_policy, granted=None,
                        requested=None,
                        source={"policy": self.execution_policy,
                                "chosen_by": "native-default", "thread": "new"})
        return dict(observed, policy=profile.policy, granted=True,
                    requested=profile.as_json()["requested"], source=profile.source)

    def goal(self, thread_id: str) -> dict | None:
        """Read the app-server's goal state without treating limits as access."""

        result = self.server.request("thread/goal/get", {"threadId": thread_id})
        if "goal" not in result or (result["goal"] is not None and not isinstance(result["goal"], dict)):
            raise NativeError("The runtime did not report a goal state", code="goal_unknown")
        goal = result["goal"]
        self.goal_status.observe(thread_id, goal)
        return goal

    def set_goal_status(self, thread_id: str, status: str) -> dict | None:
        if status not in {"active", "paused"}:
            raise NativeError("that goal status is not available in UX46", code="bad_command")
        # The documented goal API supports a pause transition independently
        # of turn interruption.  Pausing a running goal must not discard an
        # active turn; ownership is still required.
        if status == "paused":
            self.require_control(thread_id)
        else:
            self.require_control(thread_id)
            # A second resume can race the goal's first automatically-started
            # turn. Confirm the requested state instead of refusing work that
            # is already running. An active goal with no turn still goes
            # through the explicit set operation, so it can be resumed.
            if self.active_turn(thread_id):
                current = self.goal(thread_id)
                if current and current.get("status") == "active":
                    return current
            self.require_idle_control(thread_id)
        self.server.request("thread/goal/set", {"threadId": thread_id, "status": status})
        return self.goal(thread_id)

    def clear_goal(self, thread_id: str) -> bool:
        self.require_idle_control(thread_id)
        result = self.server.request("thread/goal/clear", {"threadId": thread_id})
        self.goal_status.invalidate(thread_id)
        return bool(result.get("cleared"))

    def _all_pages(
        self, method: str, *, limit: int = 200, extra: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Read every forward page, rejecting a cursor loop as unknown state."""

        cursor = None
        seen: set[str] = set()
        records: list[Any] = []
        while True:
            params: dict[str, Any] = dict(extra or {}, limit=limit)
            if cursor:
                params["cursor"] = cursor
            page = self.server.request(method, params)
            data = page.get("data")
            if not isinstance(data, list):
                raise NativeError(
                    "cannot verify hosted native work before refreshing the connection",
                    code="refresh_safety_unknown",
                )
            records.extend(data)
            nxt = page.get("nextCursor")
            if not nxt:
                return records
            cursor = str(nxt)
            if cursor in seen:
                raise NativeError(
                    "cannot verify every hosted native thread before refreshing the connection",
                    code="refresh_safety_unknown",
                )
            seen.add(cursor)

    def _loaded_thread_ids(self) -> list[str]:
        """Every thread this app-server currently has in memory, across pages."""

        pages = self._all_pages("thread/loaded/list")
        ids = []
        for item in pages:
            # The schema currently returns strings. Accepting an id wrapper is
            # a harmless compatibility aid, not browser-controlled input.
            value = item if isinstance(item, str) else (item.get("id") if isinstance(item, dict) else "")
            if not isinstance(value, str) or not value:
                raise NativeError(
                    "cannot verify hosted native work before refreshing the connection",
                    code="refresh_safety_unknown",
                )
            ids.append(value)
        return list(dict.fromkeys(ids))

    def refresh_connection(self) -> dict:
        """Use the app-server's managed-auth refresh path, without credentials.

        This is deliberately not a login flow. A client-owned token refresh
        request is a different protocol mode and UX46 refuses to implement
        it.
        """

        account = self.server.request("account/read", {"refreshToken": True})
        if account.get("requiresOpenaiAuth") and not account.get("account"):
            return {
                "state": "needs_sign_in",
                "message": "Codex still needs you to sign in before it can run a native turn.",
            }
        return {
            "state": "refreshed",
            "message": ("Codex refreshed the running managed connection. "
                        "It does not prove this process picked up a later account switch or "
                        "that a model turn will succeed."),
        }

    def refresh_safety(self) -> None:
        """Fail closed unless this app-server reports no active work.

        The app-server thread list is the authority after a reconnect; the
        event-derived map supplements it but is never the only signal. This
        admission rule prevents replacement from racing a live send, child
        work, or pending approval.
        """

        try:
            loaded_ids = self._loaded_thread_ids()
        except NativeError as exc:
            if not _unsupported(exc):
                raise
            # Older runtimes lack thread/loaded/list. Paginate every known
            # source kind rather than incorrectly treating page one or only
            # interactive sessions as complete.
            listing = self._all_pages("thread/list", extra={"sourceKinds": [
                "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
            ]})
        else:
            listing = []
            for thread_id in loaded_ids:
                thread = self.read_thread(thread_id).get("thread")
                if not isinstance(thread, dict):
                    raise NativeError(
                        "cannot verify hosted native work before refreshing the connection",
                        code="refresh_safety_unknown",
                    )
                listing.append(thread)
        active = 0
        unknown = 0
        for thread in listing:
            if not isinstance(thread, dict):
                raise NativeError(
                    "cannot verify hosted native work before refreshing the connection",
                    code="refresh_safety_unknown",
                )
            status = (thread.get("status") or {}).get("type")
            if status == "active":
                active += 1
            elif status not in ("idle", "notLoaded"):
                unknown += 1
        pending = len(self.server.pending_requests())
        observed = len(self._active_turns)
        if unknown:
            raise NativeError(
                "cannot verify hosted native work before refreshing the connection",
                code="refresh_safety_unknown",
            )
        if active or pending or observed:
            raise NativeError(
                "a native turn or approval is still active, so UX46 will not refresh the connection yet",
                code="connection_busy",
                detail={"active": active, "pending": pending, "observed": observed},
            )

    def forget_terminal_failure(self, thread_id: str) -> None:
        with self._terminal_guard:
            self._terminal.pop(thread_id, None)

    def latest_terminal_failure(self, thread_id: str, version: object = "") -> dict | None:
        """Read only the newest bounded turn page, cached by native metadata.

        ``thread/read(includeTurns=true)`` hydrates a whole rollout and made
        ordinary room refreshes depend on an arbitrarily long history.  The
        documented ``thread/turns/list`` page contains the same terminal
        state at its newest edge; metadata changes and native notifications
        invalidate this small cache.
        """

        if thread_id in self._empty_threads:
            return None
        key = str(version or "")
        with self._terminal_guard:
            cached = self._terminal.get(thread_id)
            if cached and cached[0] == key:
                return cached[1]
        try:
            result = self.server.request("thread/turns/list", {
                "threadId": thread_id, "limit": 5, "sortDirection": "desc", "itemsView": "summary",
            })
            turns = result.get("data") or []
            newest = next((turn for turn in turns if isinstance(turn, dict)), None)
        except NativeError as exc:
            if not _unsupported(exc):
                raise
            # Some installed runtimes advertise the method but reject it for
            # particular stored threads. Hydrate once as a compatibility
            # fallback; the version-keyed cache above prevents room polling
            # from repeating that expensive read.
            result = self.server.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            turns = (result.get("thread") or {}).get("turns") or []
            newest = next((turn for turn in reversed(turns) if isinstance(turn, dict)), None)
        failure = terminal_failure(newest) if newest else None
        # A newer completed or active turn deliberately clears an old error:
        # the terminal card describes one attempt, never a thread forever.
        with self._terminal_guard:
            self._terminal[thread_id] = (key, failure)
        return failure

    def read_thread(self, thread_id: str) -> dict:
        return self.server.request("thread/read", {"threadId": thread_id})

    def list_items(
        self,
        thread_id: str,
        limit: int = 40,
        cursor: str | None = None,
        direction: str = "desc",
    ) -> dict:
        """One bounded page of native history.

        The installed runtime does not serve ``thread/items/list`` for every
        thread — it answers -32601 "not supported yet" for some — so the reply
        also says which supported source the page came from. The fallback is
        ``thread/read`` with ``includeTurns``, flattened here; no thread is
        ever resumed merely to read it.
        """

        if thread_id in self._empty_threads:
            return {"data": [], "nextCursor": None, "backwardsCursor": None,
                    "source": "new_empty_thread"}
        limit = max(1, min(int(limit), 200))
        descending = direction == "desc"
        if cursor and str(cursor).startswith(FLAT_CURSOR):
            return self._flat_page(thread_id, limit, str(cursor), descending)
        params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": limit,
            "sortDirection": "desc" if descending else "asc",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            page = self.server.request("thread/items/list", params)
        except NativeError as exc:
            if not _unsupported(exc):
                raise
            return self._flat_page(thread_id, limit, None, descending)
        page = dict(page)
        page["source"] = "items_list"
        return page

    # -- supported fallback: thread/read with turns ------------------------
    def full_history(self, thread_id: str, refresh: bool = False) -> tuple[list[dict], str]:
        """Every native item this runtime will give us, in wire order.

        Returns ``(entries, source)`` where each entry is the same
        ``{"turnId": ..., "item": {...}}`` shape the paged API returns.
        """

        with self._history_guard:
            cached = self._history.get(thread_id)
            if cached and not refresh and (time.time() - cached["at"]) < HISTORY_TTL:
                return cached["entries"], cached["source"]
        entries, source = self._read_full(thread_id)
        with self._history_guard:
            self._history[thread_id] = {
                "at": time.time(), "entries": entries, "source": source,
            }
        return entries, source

    def _read_full(self, thread_id: str) -> tuple[list[dict], str]:
        if thread_id in self._empty_threads:
            return [], "new_empty_thread"
        result = self.server.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = result.get("thread") or {}
        entries: list[dict] = []
        for turn in thread.get("turns") or []:
            turn_id = str(turn.get("id") or "")
            for item in turn.get("items") or []:
                if isinstance(item, dict):
                    entries.append({"turnId": turn_id, "item": item})
                if len(entries) >= MAX_FLAT_ITEMS:
                    return entries, "thread_read_truncated"
        return entries, "thread_read"

    def _flat_page(
        self, thread_id: str, limit: int, cursor: str | None, descending: bool
    ) -> dict:
        entries, source = self.full_history(thread_id)
        ordered = list(reversed(entries)) if descending else entries
        start = 0
        if cursor:
            try:
                start = max(0, int(str(cursor)[len(FLAT_CURSOR):]))
            except ValueError:
                raise NativeError("that history cursor is not one of ours",
                                  code="bad_cursor")
        window = ordered[start:start + limit]
        nxt = start + limit
        return {
            "data": window,
            "nextCursor": f"{FLAT_CURSOR}{nxt}" if nxt < len(ordered) else None,
            "backwardsCursor": f"{FLAT_CURSOR}{max(0, start - limit)}" if start else None,
            "source": source,
            "total": len(ordered),
        }

    def search_history(
        self, thread_id: str, query: str, kinds: tuple[str, ...] = ("human", "final"),
        limit: int = 40,
    ) -> dict:
        """Search the person's own words across the whole native history.

        Only what the runtime actually recorded is searched, and every hit
        carries its stable source id so the console can jump to the exact item
        rather than inferring a relationship.
        """

        entries, source = self.full_history(thread_id)
        needle = (query or "").strip().casefold()
        hits: list[dict] = []
        for index, entry in enumerate(entries):
            item = entry.get("item") or {}
            kind = _search_kind(item)
            if kind not in kinds:
                continue
            text = _item_text(item)
            if needle and needle not in text.casefold():
                continue
            hits.append({
                "item_id": str(item.get("id") or ""),
                "turn_id": str(entry.get("turnId") or ""),
                "type": str(item.get("type") or ""),
                "phase": item.get("phase"),
                "kind": kind,
                "index": index,
                "snippet": _snippet(text, needle),
            })
        total = len(hits)
        return {
            "hits": list(reversed(hits))[:max(1, min(int(limit), 200))],
            "total": total,
            "source": source,
            "searched_items": len(entries),
        }

    def page_around(self, thread_id: str, item_id: str, radius: int = 20) -> dict:
        """A bounded page centred on one stable native item id."""

        entries, source = self.full_history(thread_id)
        index = next(
            (i for i, e in enumerate(entries)
             if str((e.get("item") or {}).get("id") or "") == item_id),
            -1,
        )
        if index < 0:
            raise NativeError("that item is not in this session's native history",
                              code="item_unknown")
        radius = max(1, min(int(radius), 100))
        start = max(0, index - radius)
        end = min(len(entries), index + radius + 1)
        return {
            "data": entries[start:end],
            "nextCursor": None,
            "backwardsCursor": f"{FLAT_CURSOR}{start}" if start else None,
            "source": source,
            "total": len(entries),
            "focus_index": index - start,
            "earlier_available": start > 0,
            "later_available": end < len(entries),
        }

    def forget_history(self, thread_id: str) -> None:
        with self._history_guard:
            self._history.pop(thread_id, None)

    # -- control -----------------------------------------------------------
    def continue_here(self, thread_id: str, room: str = "") -> dict:
        """Resume the EXACT native thread. Never a fork, never new settings."""

        self.require_control(thread_id)
        with self.lock_for(thread_id):
            thread = self.read_thread(thread_id).get("thread") or {}
            try:
                # An explicitly chosen host policy replaces the session's saved
                # one, so it only needs the thread identified and located; the
                # default reads and reproduces exactly what the CLI recorded.
                profile = (
                    native_profile.full_access(thread, thread_id)
                    if self.execution_policy == native_profile.FULL_ACCESS
                    else native_profile.from_thread(thread, thread_id)
                )
            except native_profile.ProfileError as exc:
                self.release(thread_id)
                raise NativeError(str(exc), code="execution_profile_unsupported") from exc
            result = self.server.request("thread/resume", {
                # History is loaded separately through the paging APIs. Native
                # full-history hydration can time out on long-lived threads.
                "threadId": thread_id, "excludeTurns": True, **profile.params,
            })
            try:
                profile.verify(result, thread_id)
            except native_profile.ProfileError as exc:
                self.release(thread_id)
                try:
                    self.server.request("thread/unsubscribe", {"threadId": thread_id})
                except NativeError:
                    pass  # No further writes admitted even if unloading fails.
                raise NativeError(str(exc), code="execution_profile_mismatch",
                                  detail={"source": profile.source}) from exc
            resumed = result.get("thread") or {}
            cwd = profile.params["cwd"]
            self.mark_owned(thread_id, cwd=cwd, room=room)
            return {
                "thread_id": str(resumed.get("id") or thread_id),
                "cwd": str(resumed.get("cwd") or cwd),
                "forked": str(resumed.get("id") or thread_id) != thread_id,
                "execution_profile": {
                    "policy": profile.policy,
                    "granted": True,
                    "sandbox": result["sandbox"],
                    "approval_policy": result["approvalPolicy"],
                    "approvals_reviewer": result.get("approvalsReviewer"),
                    "active_permission_profile": result["activePermissionProfile"],
                    "requested": profile.as_json()["requested"],
                    "source": profile.source,
                },
            }

    def start_test_thread(self, cwd: str) -> dict:
        """A dedicated harmless thread: workspace-write, approvals on request."""

        result = self.server.request("thread/start", {
            "cwd": cwd,
            "sandbox": "workspace-write",
            "approvalPolicy": "on-request",
        })
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if thread_id:
            self.mark_owned(thread_id, cwd=cwd, room="")
        return {
            "thread_id": thread_id,
            "cwd": str(result.get("cwd") or cwd),
            "sandbox": result.get("sandbox"),
            "approval_policy": result.get("approvalPolicy"),
        }

    def note_turn(self, thread_id: str, turn_id: str, active: bool) -> None:
        if active:
            self._empty_threads.discard(thread_id)
            self._active_turns[thread_id] = turn_id
        elif self._active_turns.get(thread_id) == turn_id:
            self._active_turns.pop(thread_id, None)
        self.forget_history(thread_id)

    def active_turn(self, thread_id: str) -> str:
        return self._active_turns.get(thread_id, "")

    def send_input(self, thread_id: str, text: str, client_message_id: str,
                   attachments: list[dict] | None = None) -> dict:
        """Start a turn, or steer the active one with its expected turn id."""

        self.require_control(thread_id)
        if thread_id not in self._owned:
            raise NativeError(
                "UX46 is not holding this native session yet — choose Continue here first",
                code="not_owned",
            )
        with self.lock_for(thread_id):
            active = self.active_turn(thread_id)
            input_items: list[dict] = [{"type": "text", "text": text}]
            for attachment in attachments or []:
                path = str(attachment.get("path") or "")
                name = str(attachment.get("name") or "attachment")
                if not path:
                    raise NativeError("managed attachment path is missing", code="bad_attachment")
                if attachment.get("preview_url"):
                    input_items.append({"type": "localImage", "path": path})
                else:
                    # The app-server accepts text input universally.  The
                    # server-managed path lets the native turn inspect a
                    # non-raster file without inventing a browser path or
                    # claiming a non-portable local-file wire type.
                    input_items.append({"type": "text", "text": f"\nAttached file ({name}): {path}"})
            payload = {
                "threadId": thread_id,
                "input": input_items,
                "clientUserMessageId": client_message_id,
            }
            if active:
                result = self.server.request("turn/steer", dict(
                    payload, expectedTurnId=active
                ))
                turn = result.get("turn") or {}
                return {
                    "mode": "steer",
                    "turn_id": str(turn.get("id") or active),
                    "status": str((turn.get("status") or "inProgress")),
                }
            result = self.server.request("turn/start", payload)
            turn = result.get("turn") or {}
            turn_id = str(turn.get("id") or "")
            if turn_id:
                self.note_turn(thread_id, turn_id, True)
            return {
                "mode": "start",
                "turn_id": turn_id,
                "status": str(turn.get("status") or "inProgress"),
            }

    def interrupt(self, thread_id: str) -> dict:
        self.require_control(thread_id)
        return self.server.request("turn/interrupt", {"threadId": thread_id})

    def answer_request(self, key: str, kind: str, decision: str, answers: dict | None = None) -> dict:
        return self.server.answer_request(key, response_for_decision(kind, decision, answers))


def _unsupported(exc: NativeError) -> bool:
    """Did the runtime say it cannot serve this method for this thread?"""

    detail = exc.detail if isinstance(exc.detail, dict) else {}
    if detail.get("code") == -32601:
        return True
    return "not supported" in str(exc).casefold()


def _item_text(item: dict) -> str:
    kind = item.get("type")
    if kind == "userMessage":
        parts = []
        for piece in item.get("content") or []:
            if isinstance(piece, dict) and piece.get("type") == "text":
                parts.append(str(piece.get("text") or ""))
        return "\n".join(parts)
    if kind == "agentMessage":
        return str(item.get("text") or "")
    return ""


def _search_kind(item: dict) -> str:
    kind = item.get("type")
    if kind == "userMessage":
        return "human"
    if kind == "agentMessage":
        # Only a runtime-reported final answer counts as a substantial update;
        # nothing is inferred when the provider omits the phase.
        return "final" if item.get("phase") == "final_answer" else "reply"
    return "work"


def _snippet(text: str, needle: str) -> str:
    flat = " ".join(text.split())
    if not needle:
        return flat[:SNIPPET]
    at = flat.casefold().find(needle)
    if at < 0:
        return flat[:SNIPPET]
    start = max(0, at - 60)
    return ("…" if start else "") + flat[start:start + SNIPPET]


def new_client_id() -> str:
    return uuid.uuid4().hex
