"""One native runtime process per attached conversation.

Codex keeps a *writer* on a thread after ``thread/unsubscribe``: the lock file
under ``$CODEX_HOME/thread-writer-locks/<thread>.lock`` stays held by the
app-server process for a retention window measured in tens of minutes. One
shared app-server therefore cannot hand a session back when the person asks
for it, and it cannot isolate one conversation's auth or restart from another.

So UX46 hosts each attached conversation in its own app-server subprocess and
keeps one extra read/catalog runtime that never resumes a thread. The lifecycle
is deterministic and process-shaped:

* attach   - start a process for this thread, resume the exact thread in it;
* release  - refuse while work is unsettled, unsubscribe, then stop *that*
             process and verify from the writer lock that nothing still holds
             it. UX46 never reports "released" while a holder is visible;
* refresh  - stop and restart one conversation's process, leaving every other
             conversation running.

Nothing here kills a process UX46 did not launch and nothing unlinks a lock
file. An unverifiable state is reported as unverified, never as released.

The adapter boundary below exists so a second agent runtime could be hosted
the same way. Only Codex has an adapter today; every other name is refused
rather than pretended.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import atlas_native as native
from ux46_account import login_identity, AccountStatus

CODEX = "codex"

# Methods that make a runtime take, or keep, a writer on a thread. The
# read/catalog runtime refuses all of them locally, so a viewed history can
# never quietly become a second writer.
WRITER_METHODS = frozenset({
    "thread/resume",
    "thread/start",
    "thread/unsubscribe",
    "thread/settings/update",
    "thread/compact/start",
    "thread/goal/set",
    "thread/goal/clear",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
})

READER = "reader"
CONVERSATION = "conversation"

STOP_GRACE = 3.0


class ReadOnlyAppServer(native.AppServer):
    """An app-server connection UX46 will only ever read through."""

    def request(self, method: str, params: dict, timeout: float = native.DEFAULT_TIMEOUT) -> dict:
        if method in WRITER_METHODS:
            raise native.NativeError(
                f"the UX46 read runtime does not perform {method}",
                code="read_only_runtime",
            )
        return super().request(method, params, timeout)

    def notify(self, method: str, params: dict) -> None:
        if method in WRITER_METHODS:
            raise native.NativeError(
                f"the UX46 read runtime does not perform {method}",
                code="read_only_runtime",
            )
        super().notify(method, params)


@dataclass(frozen=True)
class AgentAdapter:
    """How one agent runtime is launched and spoken to.

    ``agent`` is the name UX46 may report. An adapter exists only when UX46
    can really host that runtime; see ``adapter_for``.
    """

    agent: str
    command: tuple[str, ...]

    def launch(self, on_event: Callable[[dict], None], *, read_only: bool = False) -> native.AppServer:
        factory = ReadOnlyAppServer if read_only else native.AppServer
        return factory(command=list(self.command), on_event=on_event)


def adapter_for(agent: str, command: list[str] | tuple[str, ...]) -> AgentAdapter:
    """Only a runtime UX46 actually implements can be hosted."""

    name = (agent or CODEX).strip().casefold()
    if name != CODEX:
        raise native.NativeError(
            f"UX46 has no native worker adapter for {agent!r}; only Codex app-server "
            "sessions can be hosted here",
            code="agent_unsupported",
        )
    return AgentAdapter(agent=CODEX, command=tuple(command))


def _alive(pids: set[int]) -> set[int]:
    live = set()
    for pid in pids:
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            live.add(pid)
        except OSError:
            continue
        else:
            live.add(pid)
    return live


@dataclass
class SessionWorker:
    """One runtime process, and what it is for."""

    role: str
    agent: str
    server: native.AppServer
    sessions: native.NativeSessions
    thread_id: str = ""
    room: str = ""
    cwd: str = ""
    # What this worker's thread was verified to be running under, as reported
    # by the runtime when it was resumed or started.
    profile: dict | None = None
    started_at: float = field(default_factory=time.time)
    stopped_at: float = 0.0
    login_identity: str | None = None
    account_status: Any = None

    @property
    def running(self) -> bool:
        return self.server.running

    @property
    def label(self) -> str:
        return f"{self.role}:{self.thread_id}" if self.thread_id else self.role

    def as_json(self) -> dict:
        return {
            "role": self.role,
            "agent": self.agent,
            "thread_id": self.thread_id,
            "room": self.room,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "running": self.running,
            "pids": list(self.server.pids),
            "execution_profile": self.profile,
        }

    def stop(self, grace: float = STOP_GRACE) -> tuple[int, ...]:
        """Stop this worker's own process chain and report what survived.

        Only processes UX46 launched are signalled, and only with SIGTERM
        after the documented stdin close and terminate have been tried. A
        process that outlives that is reported, never force-killed and never
        described as gone.
        """

        try:
            pids = set(self.sessions.own_pids(refresh=True))
        except native.NativeError:
            pids = set(self.server.pids)
        self.server.stop()
        remaining = self._settle(pids, grace)
        if remaining:
            for pid in sorted(remaining):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            remaining = self._settle(remaining, grace)
        self.stopped_at = time.time()
        return tuple(sorted(remaining))

    @staticmethod
    def _settle(pids: set[int], grace: float) -> set[int]:
        deadline = time.time() + max(0.0, grace)
        remaining = _alive(pids)
        while remaining and time.time() < deadline:
            time.sleep(0.05)
            remaining = _alive(remaining)
        return remaining


class SessionWorkerPool:
    """Every native runtime process this console owns, and nothing else."""

    def __init__(
        self,
        *,
        command: list[str],
        on_event: Callable[[dict], None],
        probe: Any | None = None,
        agent: str = CODEX,
        execution_policy: str = native.native_profile.PRESERVE,
    ):
        self.adapter = adapter_for(agent, command)
        self._on_event = on_event
        # The host's execution policy, given to every runtime this pool
        # starts. A worker adopts it when it is spawned, which is why a
        # per-room refresh is what picks up a changed setting: no already
        # running conversation is reached into and altered.
        if execution_policy not in native.native_profile.POLICIES:
            raise ValueError(f"unknown execution policy {execution_policy!r}")
        self.execution_policy = execution_policy
        self.probe = probe or native.OwnershipProbe()
        self._guard = threading.RLock()
        self._reader_lock = threading.Lock()
        self._workers: dict[str, SessionWorker] = {}
        self._attaching: dict[str, SessionWorker] = {}
        self._thread_locks: dict[str, threading.Lock] = {}
        self._reader: SessionWorker | None = None
        self._closed = False

    # -- probe -------------------------------------------------------------
    def set_probe(self, probe: Any) -> None:
        """Use one ownership probe for every runtime this pool owns."""

        with self._guard:
            self.probe = probe
            for worker in self._all_workers():
                worker.sessions.probe = probe

    def _all_workers(self) -> list[SessionWorker]:
        with self._guard:
            workers = list(self._workers.values()) + list(self._attaching.values())
            if self._reader is not None:
                workers.append(self._reader)
            return workers

    # -- locks -------------------------------------------------------------
    def _thread_lock(self, thread_id: str) -> threading.Lock:
        with self._guard:
            if thread_id not in self._thread_locks:
                self._thread_locks[thread_id] = threading.Lock()
            return self._thread_locks[thread_id]

    # -- spawning ----------------------------------------------------------
    def _spawn(self, role: str, thread_id: str = "", room: str = "") -> SessionWorker:
        if self._closed:
            raise native.NativeError("this console is shutting down", code="runtime_unavailable")
        cell: dict[str, SessionWorker | None] = {"worker": None}

        def on_event(event: dict, cell=cell) -> None:
            self._runtime_event(cell.get("worker"), event)

        server = self.adapter.launch(on_event, read_only=(role == READER))
        sessions = native.NativeSessions(server, probe=self.probe,
                                         execution_policy=self.execution_policy)
        worker = SessionWorker(
            role=role, agent=self.adapter.agent, server=server, sessions=sessions,
            thread_id=thread_id, room=room, login_identity=login_identity(),
            account_status=AccountStatus(server),
        )
        cell["worker"] = worker
        server.start()
        return worker

    def _runtime_event(self, worker: SessionWorker | None, event: dict) -> None:
        """Keep the emitting worker's own turn bookkeeping, then forward."""

        if worker is not None and event.get("type") == "notification":
            if event.get("method") in {"account/updated", "account/rateLimits/updated", "error", "turn/completed"} and worker.account_status:
                worker.account_status.invalidate(clear=event.get("method") != "turn/completed")
            method = str(event.get("method") or "")
            params = event.get("params") or {}
            thread_id = str(params.get("threadId") or "")
            if thread_id and (method.startswith("thread/goal/") or method in ("turn/started", "turn/completed", "error")):
                worker.sessions.goal_status.invalidate(thread_id)
            if thread_id and method in ("turn/started", "turn/completed"):
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                turn_id = str((turn or {}).get("id") or params.get("turnId") or "")
                if turn_id:
                    worker.sessions.note_turn(thread_id, turn_id, method == "turn/started")
                worker.sessions.forget_terminal_failure(thread_id)
        payload = dict(event)
        payload["worker"] = worker.label if worker else ""
        payload["worker_role"] = worker.role if worker else ""
        payload["agent"] = worker.agent if worker else self.adapter.agent
        self._on_event(payload)

    # -- the read / catalog runtime ---------------------------------------
    def reader(self) -> SessionWorker:
        """The runtime used for history, search, metadata and the catalog.

        It refuses every writer method locally, so browsing a conversation
        never takes a writer that a later Release would have to free.
        """

        with self._guard:
            existing = self._reader
        identity = login_identity()
        if existing is not None and existing.running and (identity is None or existing.login_identity == identity):
            return existing
        with self._reader_lock:
            with self._guard:
                existing = self._reader
            identity = login_identity()
            if existing is not None and existing.running and (identity is None or existing.login_identity == identity):
                return existing
            if existing is not None:
                existing.stop()
            worker = self._spawn(READER)
            with self._guard:
                self._reader = worker
            return worker

    def reader_sessions(self) -> native.NativeSessions:
        return self.reader().sessions

    def reader_pids(self) -> set[int]:
        with self._guard:
            reader = self._reader
        if reader is None or not reader.running:
            return set()
        try:
            return set(reader.sessions.own_pids(refresh=True))
        except native.NativeError:
            return set(reader.server.pids)

    def stop_reader(self) -> tuple[int, ...]:
        with self._reader_lock:
            with self._guard:
                reader, self._reader = self._reader, None
        return reader.stop() if reader is not None else ()

    # -- lookups -----------------------------------------------------------
    def worker(self, thread_id: str) -> SessionWorker | None:
        with self._guard:
            worker = self._workers.get(thread_id)
        if worker is not None and not worker.running:
            # The process died on its own; UX46 is not holding anything.
            with self._guard:
                if self._workers.get(thread_id) is worker:
                    self._workers.pop(thread_id, None)
            return None
        return worker

    def attached(self) -> dict[str, SessionWorker]:
        with self._guard:
            live = {tid: w for tid, w in self._workers.items() if w.running}
            self._workers = live
            return dict(live)

    def owned_threads(self) -> dict[str, dict]:
        """Exactly the conversations UX46 is holding a writer process for."""

        owned: dict[str, dict] = {}
        for thread_id, worker in self.attached().items():
            record = worker.sessions.owned_threads().get(thread_id) or {}
            owned[thread_id] = {
                "cwd": record.get("cwd", worker.cwd),
                "room": record.get("room", worker.room),
                "claimed_at": record.get("claimed_at", worker.started_at),
                "agent": worker.agent,
            }
        return owned

    def sessions_for(self, thread_id: str) -> native.NativeSessions:
        """The runtime that speaks for this thread: its worker, else reading."""

        worker = self.worker(thread_id)
        return worker.sessions if worker else self.reader_sessions()

    def writer_for(self, thread_id: str) -> native.NativeSessions:
        """The runtime allowed to change this thread, or an honest refusal."""

        worker = self.worker(thread_id)
        if worker is None:
            raise native.NativeError(
                "UX46 is not holding this native session yet — choose Continue here first",
                code="not_owned",
            )
        return worker.sessions

    def any_running(self) -> bool:
        with self._guard:
            reader = self._reader
            workers = list(self._workers.values())
        return bool((reader and reader.running) or any(w.running for w in workers))

    def as_json(self) -> list[dict]:
        return [w.as_json() for w in self._all_workers()]

    # -- approvals ---------------------------------------------------------
    def pending_requests(self, thread_id: str | None = None) -> list[dict]:
        """Approvals waiting across every conversation worker."""

        if thread_id:
            worker = self.worker(thread_id)
            return worker.sessions.server.pending_requests(thread_id) if worker else []
        pending: list[dict] = []
        for worker in self.attached().values():
            pending.extend(worker.sessions.server.pending_requests())
        pending.sort(key=lambda item: item.get("created_at", 0.0))
        return pending

    def answer_request(self, key: str, kind: str, decision: str,
                       answers: dict | None = None) -> dict:
        """Route one answer to the worker that actually asked."""

        for worker in self.attached().values():
            if any(item.get("key") == key for item in worker.sessions.server.pending_requests()):
                return worker.sessions.answer_request(key, kind, decision, answers)
        raise native.NativeError("no such native request", code="request_unknown")

    def busy(self, thread_id: str) -> str:
        """Why this conversation cannot be stopped right now, if it cannot."""

        worker = self.worker(thread_id)
        if worker is None:
            return ""
        if worker.sessions.active_turn(thread_id):
            return "a native turn is still running"
        if worker.sessions.server.pending_requests(thread_id):
            return "a native approval is still waiting"
        return ""

    # -- attach ------------------------------------------------------------
    def attaching_worker(self, thread_id: str) -> SessionWorker | None:
        """An owned connection attempt, not yet a usable native writer."""
        with self._guard:
            return self._attaching.get(thread_id)

    def attach(self, thread_id: str, room: str = "", agent: str = CODEX) -> dict:
        """Hold this exact thread in a process of its own.

        Two Vault records that name the same thread deduplicate here: the
        second attach finds the first worker and never starts a second writer.
        """

        if (agent or CODEX).strip().casefold() != self.adapter.agent:
            raise native.NativeError(
                f"this console hosts {self.adapter.agent} sessions, not {agent!r}",
                code="agent_unsupported",
            )
        with self._thread_lock(thread_id):
            existing = self.worker(thread_id)
            if existing is not None:
                return {
                    "thread_id": thread_id,
                    "cwd": existing.cwd,
                    "forked": False,
                    "room": existing.room,
                    "agent": existing.agent,
                    "duplicate": True,
                    "execution_profile": getattr(existing, "profile", None),
                }
            worker = self._spawn(CONVERSATION, thread_id=thread_id, room=room)
            with self._guard:
                self._attaching[thread_id] = worker
            try:
                result = self._resume_in(worker, thread_id, room)
                worker.cwd = str(result.get("cwd") or "")
                worker.profile = result.get("execution_profile")
                with self._guard:
                    self._workers[thread_id] = worker
            except BaseException:
                worker.stop()
                raise
            finally:
                with self._guard:
                    self._attaching.pop(thread_id, None)
            result["room"] = room
            result["agent"] = worker.agent
            result["duplicate"] = False
            return result

    def _resume_in(self, worker: SessionWorker, thread_id: str, room: str) -> dict:
        """Resume with the thread's own profile, retrying past our own reader.

        A read runtime that the installed Codex chose to load can look like
        another terminal. Stopping UX46's own read process is a bounded,
        honest recovery; a holder that is not ours is still refused.
        """

        try:
            return worker.sessions.continue_here(thread_id, room)
        except native.NativeError as exc:
            if exc.code != "held_elsewhere":
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            holders = {int(pid) for pid in (detail.get("external_pids") or [])}
            reader_pids = self.reader_pids()
            if not holders or not holders <= reader_pids:
                raise
            self.stop_reader()
            return worker.sessions.continue_here(thread_id, room)

    def start_fresh(self, cwd: str, *, room: str = "", model: str | None = None,
                    effort: str | None = None, project_id: str | None = None,
                    developer_instructions: str | None = None) -> dict:
        """Create an empty thread inside a process dedicated to it."""

        worker = self._spawn(CONVERSATION, room=room)
        try:
            fresh = worker.sessions.start_fresh_thread(
                cwd, model=model, effort=effort, project_id=project_id,
                developer_instructions=developer_instructions)
        except BaseException:
            worker.stop()
            raise
        thread_id = str(fresh["thread_id"])
        worker.thread_id = thread_id
        worker.cwd = str(fresh.get("cwd") or cwd)
        worker.profile = fresh.get("execution_profile")
        worker.sessions.mark_owned(thread_id, cwd=worker.cwd, room=room)
        with self._guard:
            self._workers[thread_id] = worker
        return dict(fresh, room=room, agent=worker.agent)

    def start_test_thread(self, cwd: str) -> dict:
        worker = self._spawn(CONVERSATION)
        try:
            result = worker.sessions.start_test_thread(cwd)
        except BaseException:
            worker.stop()
            raise
        thread_id = str(result.get("thread_id") or "")
        if not thread_id:
            worker.stop()
            raise native.NativeError("the native runtime did not identify the new thread",
                                     code="runtime_unavailable")
        worker.thread_id = thread_id
        worker.cwd = str(result.get("cwd") or cwd)
        with self._guard:
            self._workers[thread_id] = worker
        return dict(result, agent=worker.agent)

    # -- release -----------------------------------------------------------
    def release(self, thread_id: str) -> dict:
        """Stop only this conversation's process and prove what that freed."""

        with self._thread_lock(thread_id):
            worker = self.worker(thread_id)
            if worker is None:
                state = self._probe(thread_id, own_pids=())
                return {
                    "thread_id": thread_id,
                    "release_state": "not_attached",
                    "verified": True,
                    "native_status": "",
                    "holders": [],
                    "ownership": state.as_json(),
                }
            reason = self.busy(thread_id)
            if reason:
                raise native.NativeError(
                    f"{reason}, so UX46 will not release this session yet",
                    code="connection_busy",
                    detail={"thread_id": thread_id, "reason": reason},
                )
            native_status = ""
            try:
                unloaded = worker.sessions.server.request(
                    "thread/unsubscribe", {"threadId": thread_id})
                native_status = str(unloaded.get("status") or "unsubscribed")
            except native.NativeError as exc:
                # Unsubscribe is the documented unload, but it is not what
                # frees the writer. Stopping our process is; say so plainly.
                native_status = f"unsubscribe_failed: {exc}"
            worker.sessions.release(thread_id)
            survivors = worker.stop()
            with self._guard:
                if self._workers.get(thread_id) is worker:
                    self._workers.pop(thread_id, None)
            result = self._verify_release(thread_id, survivors)
            result["native_status"] = native_status
            return result

    def _probe(self, thread_id: str, own_pids=()) -> native.Ownership:
        try:
            return self.probe.check(thread_id, own_pids=own_pids, refresh=True)
        except native.NativeError as exc:
            return native.Ownership(thread_id, (), False, False, str(exc))

    def _verify_release(self, thread_id: str, survivors: tuple[int, ...]) -> dict:
        state = self._probe(thread_id, own_pids=())
        holders = sorted(set(state.external_pids) | set(survivors))
        if not state.detected:
            return {
                "thread_id": thread_id,
                "release_state": "unverified",
                "verified": False,
                "holders": holders,
                "ownership": state.as_json(),
            }
        if holders:
            return {
                "thread_id": thread_id,
                "release_state": "releasing",
                "verified": False,
                "holders": holders,
                "ownership": state.as_json(),
            }
        return {
            "thread_id": thread_id,
            "release_state": "released",
            "verified": True,
            "holders": [],
            "ownership": state.as_json(),
        }

    # -- refresh -----------------------------------------------------------
    def refresh_worker(self, thread_id: str, room: str = "") -> dict:
        """Reload one conversation's login by replacing only its process."""

        with self._thread_lock(thread_id):
            worker = self.worker(thread_id)
            if worker is None:
                raise native.NativeError(
                    "UX46 is not holding this native session yet — choose Continue here first",
                    code="not_owned",
                )
            reason = self.busy(thread_id)
            if reason:
                raise native.NativeError(
                    f"{reason}, so UX46 will not refresh this session yet",
                    code="connection_busy",
                    detail={"thread_id": thread_id, "reason": reason},
                )
            worker.sessions.refresh_safety()
            wanted_room = room or worker.room
            try:
                worker.sessions.server.request("thread/unsubscribe", {"threadId": thread_id})
            except native.NativeError:
                pass
            worker.sessions.release(thread_id)
            survivors = worker.stop()
            with self._guard:
                if self._workers.get(thread_id) is worker:
                    self._workers.pop(thread_id, None)
            if survivors:
                # The old writer is still visible; resuming now would be a
                # second writer on the same thread.
                raise native.NativeError(
                    "the previous native process for this session has not exited yet, "
                    "so UX46 will not start a second one",
                    code="release_unconfirmed",
                    detail={"holders": list(survivors)},
                )
            replacement = self._spawn(CONVERSATION, thread_id=thread_id, room=wanted_room)
            with self._guard:
                self._attaching[thread_id] = replacement
            try:
                account = replacement.sessions.refresh_connection()
                result = self._resume_in(replacement, thread_id, wanted_room)
                replacement.cwd = str(result.get("cwd") or "")
                replacement.profile = result.get("execution_profile")
                with self._guard:
                    self._workers[thread_id] = replacement
            except BaseException:
                replacement.stop()
                raise
            finally:
                with self._guard:
                    self._attaching.pop(thread_id, None)
            return {
                "thread_id": thread_id,
                "room": wanted_room,
                "state": account.get("state", "refreshed"),
                "message": account.get("message", ""),
                "reconnected": 1,
                "continued": result,
            }

    # -- shutdown ----------------------------------------------------------
    def shutdown(self) -> None:
        self._closed = True
        with self._guard:
            workers = list(self._workers.values()) + list(self._attaching.values())
            self._workers = {}
            self._attaching = {}
            reader, self._reader = self._reader, None
        for worker in workers:
            try:
                worker.stop()
            except Exception:  # pragma: no cover - best effort shutdown
                pass
        if reader is not None:
            try:
                reader.stop()
            except Exception:  # pragma: no cover
                pass
