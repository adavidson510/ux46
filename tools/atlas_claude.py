#!/usr/bin/env python3
"""UX46 adapter for CP — the Claude Code CLI already installed on this machine.

CP is a Claude Code runtime, not a new agent. This adapter adds nothing to it:
no wrapper prompt, no second identity, no separate memory. It runs the
installed `claude` binary exactly as a person would, with that binary's own
`~/.claude` configuration, tools, MCP servers and login, and it reads the
transcripts that binary already writes under `~/.claude/projects`.

    UX46 console  ->  this adapter (loopback)  ->  claude -p (one per turn)
                                              ->  ~/.claude/projects/*.jsonl

Three things shape the whole design.

**One process per turn.** A turn is `claude -p --resume <uuid>` with the
message on stdin and the native stream-json on stdout. It starts when a message
is sent and exits when the turn ends, so nothing holds a session between turns
and two views never fight over one transcript.

**Somebody else may be in there.** Before starting a turn this adapter asks the
operating system, by name, whether another process has that exact transcript
open. If one does, the turn is refused and says so. It never kills, never
steals, and never guesses: an answer it cannot verify is reported as held, not
assumed free.

**Nothing is invented.** A conversation is filed under a Session Vault project
only when that project's own origins file names `runtime: claude` and a session
id that is a real UUID with a transcript on this disk. A reference that is not
resolvable is reported as exactly that — a portable reference, unavailable
here — and never quietly pointed at some other conversation.

Run it:

    python3 tools/atlas_claude.py --port 8882

It binds loopback only. See architecture/claude-cp-adapter-1.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid as uuidlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atlas_creation as creation  # noqa: E402
import atlas_files as files  # noqa: E402
import atlas_journal as journal_store  # noqa: E402

ADAPTER_NAME = "ux46-claude"
ADAPTER_VERSION = "1"
DEFAULT_PORT = 8882
DEFAULT_NODE = "user-mac"
DEFAULT_AGENT_ID = "cp"
DEFAULT_AGENT_NAME = "CP"
DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_REGISTRY = "~/.codex/projects/registry.json"
DEFAULT_STATE_DIR = "~/.ux46-claude"
DEFAULT_UNFILED_PROJECT = "unfiled-claude"

RUNTIME = "claude"
CAPABILITY = "claude-code-local"
CAPABILITY_SHORT = "CP · Claude Code"
CAPABILITY_LABEL = ("the Claude Code CLI on this machine — its own transcripts, "
                    "read here, and sendable one turn at a time")

MAX_BODY = 64 * 1024
MAX_ATTACH_BYTES = 5 * 1024 * 1024        # what fits in one base64 image block
HISTORY_MAX = 200
SCAN_LIMIT = 600                          # newest transcripts considered per refresh
CATALOG_TTL = 4.0
TURN_TIMEOUT = 1800.0

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

PENDING = journal_store.PENDING
DISPATCHING = journal_store.DISPATCHING
ACCEPTED = journal_store.ACCEPTED
FAILED = journal_store.FAILED
UNCERTAIN = journal_store.UNCERTAIN
# What a command reports; a command is not a submission and is not journaled
# as one.
COMPLETED = "completed"

# What this adapter can actually do, said once and reported everywhere.
CAPABILITIES = {
    "read_history": True,
    "search_history": True,
    "send": True,
    "stream": True,
    "attachments": True,
    "images": True,
    "new_conversation": True,
    "stop_turn": True,
    "queue": True,
    "drafts": True,
    "voice": False,
    "approvals": False,
    "goal": False,
    "compact": False,
    "steer": False,
}

UNSUPPORTED_ACTIONS = {
    "goal": "Claude Code has no goal object this adapter can read or set",
    "speak": "local speech belongs to the Codex console, not to this adapter",
    "refresh": "this adapter never touches the Claude Code login",
    "approvals": "turns run with the permission mode this adapter was started with; "
                 "there is no approval to answer here",
    "attach": "a Claude Code session is not held between turns, so there is nothing "
              "to take; selecting the conversation is enough",
}

COMMANDS_SUPPORTED = (
    "/help", "/refresh", "/new", "/status", "/model", "/effort", "/reasoning", "/reasononing",
)
COMMAND_HELP = (
    {"name": "/refresh", "usage": "/refresh", "description": "refresh CP conversation metadata; each turn already starts a fresh CLI process"},
    {"name": "/help", "usage": "/help",
     "description": "show the commands this CP adapter can carry out"},
    {"name": "/new", "usage": "/new [title]",
     "description": "start a fresh CP conversation in this project and open it here"},
    {"name": "/status", "usage": "/status",
     "description": "show the model, effort, working directory and permission mode "
                    "the next turn will really use"},
    {"name": "/model", "usage": "/model [name]",
     "description": "choose the model the next turn is launched with; the running "
                    "turn is untouched"},
    {"name": "/effort", "usage": "/effort [low|medium|high|xhigh|max]",
     "description": "choose the effort the next turn is launched with"},
    {"name": "/reasoning", "usage": "/reasoning [low|medium|high|xhigh|max]",
     "description": "alias for /effort"},
    {"name": "/reasononing", "usage": "/reasononing [low|medium|high|xhigh|max]",
     "description": "alias for /effort"},
)
COMMAND_UNSUPPORTED = {
    "/steer": "a turn here is one process with its message already sent; stop it and "
              "send again",
    "/compact": "compaction is something the CLI does inside an interactive session, "
                "and this adapter runs one turn at a time",
    "/goal": UNSUPPORTED_ACTIONS["goal"],
}

# Which canonical item a tool call becomes. The console draws these; anything
# unrecognised stays `dynamicToolCall` rather than pretending to be a shell.
TOOL_KINDS = {
    "Bash": "commandExecution", "BashOutput": "commandExecution",
    "KillShell": "commandExecution",
    "Edit": "fileChange", "Write": "fileChange", "NotebookEdit": "fileChange",
    "MultiEdit": "fileChange",
    "WebSearch": "webSearch", "WebFetch": "webSearch",
    "TodoWrite": "plan", "ExitPlanMode": "plan",
}


class AdapterError(Exception):
    def __init__(self, status, code: str, message: str, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# the event log (long-poll), same shape the console already reads
# ---------------------------------------------------------------------------

class EventLog:
    def __init__(self, limit: int = 500):
        self.limit = limit
        self._events: list[dict] = []
        self._seq = 0
        self._cond = threading.Condition()

    def publish(self, event: dict) -> int:
        with self._cond:
            self._seq += 1
            event = dict(event, seq=self._seq, at=time.time())
            self._events.append(event)
            del self._events[: max(0, len(self._events) - self.limit)]
            self._cond.notify_all()
            return self._seq

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def since(self, after: int, timeout: float = 25.0, room: str = "") -> dict:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                found = [e for e in self._events if e["seq"] > after]
                if room:
                    found = [e for e in found if e.get("room") in (room, None, "")]
                if found or time.time() >= deadline:
                    return {"seq": self._seq, "events": found[-100:]}
                self._cond.wait(timeout=max(0.1, deadline - time.time()))


# ---------------------------------------------------------------------------
# native transcripts
# ---------------------------------------------------------------------------

def _read_records(path: Path, limit: int = 0):
    """Every JSON line this file holds, skipping ones that will not parse.

    A half-written last line is normal: the CLI may be appending as we read.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    count = 0
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            yield record
            count += 1
            if limit and count >= limit:
                return


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _tool_kind(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcpToolCall"
    return TOOL_KINDS.get(name, "dynamicToolCall")


def transcript_items(records) -> list[dict]:
    """Project native records into the items the console draws.

    One item per content block, with the native record uuid as the id, so
    every line on screen traces back to a line in the transcript. Sidechain
    records — a subagent's own conversation — are left out: they belong to a
    turn, not to the conversation a person is reading.
    """
    items: list[dict] = []
    for record in records:
        kind = str(record.get("type") or "")
        if record.get("isSidechain"):
            continue
        if kind not in ("user", "assistant", "system"):
            continue
        uid = str(record.get("uuid") or "")
        at = record.get("timestamp")
        turn_id = str(record.get("requestId") or record.get("parentUuid") or "")
        if kind == "system":
            # Most system records are the CLI talking to itself — turn timings,
            # local command echoes. The ones that carry a warning or a
            # compaction boundary change what a person is reading, so those are
            # kept and nothing else is.
            text = str(record.get("content") or "")
            subtype = str(record.get("subtype") or "")
            level = str(record.get("level") or "")
            if not text or (level not in ("warning", "error")
                            and subtype != "compact_boundary"):
                continue
            items.append({"id": uid or f"system-{len(items)}", "type": "unknown",
                          "role": "system", "at": at, "turn_id": turn_id,
                          "subtype": subtype, "level": level, "text": text[:4000]})
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or kind)
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        model = message.get("model")
        for position, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            item_id = uid if len(blocks) == 1 else f"{uid}:{position}"
            base = {"id": item_id or f"item-{len(items)}", "message_id": uid,
                    "block_index": position, "turn_id": turn_id, "role": role, "at": at}
            if btype == "text" and role == "user":
                text = str(block.get("text") or "")
                if record.get("isMeta") or text.startswith("<local-command-caveat>"):
                    # The CLI's own bookkeeping, not something a person said.
                    continue
                base.update({"type": "userMessage", "text": text})
            elif btype == "text":
                base.update({"type": "agentMessage", "text": str(block.get("text") or ""),
                             "phase": "final_answer", "questions": [], "model": model})
            elif btype == "thinking":
                summary = str(block.get("thinking") or "")
                base.update({"type": "reasoning", "summary": [summary[:2000]] if summary else []})
            elif btype == "tool_use":
                name = str(block.get("name") or "")
                base.update({"type": _tool_kind(name), "tool": name, "server": "",
                             "status": "called", "duration_ms": None,
                             "call_id": str(block.get("id") or ""),
                             "arguments": block.get("input") if isinstance(
                                 block.get("input"), dict) else {}})
                if base["type"] == "commandExecution":
                    args = base["arguments"]
                    base["command"] = str(args.get("command") or "")
                elif base["type"] == "fileChange":
                    args = base["arguments"]
                    base["path"] = str(args.get("file_path") or args.get("path")
                                       or args.get("notebook_path") or "")
                    base["changes"] = ([{"path": base["path"], "kind": name}]
                                       if base["path"] else [])
            elif btype == "tool_result":
                result = block.get("content")
                base.update({"type": "functionCallOutput",
                             "name": "", "call_id": str(block.get("tool_use_id") or ""),
                             "output": (result if isinstance(result, str)
                                        else _text_of(result) or json.dumps(result)[:4000])[:8000],
                             "is_error": bool(block.get("is_error"))})
            elif btype == "image":
                base.update({"type": "attachment",
                             "mime": str((block.get("source") or {}).get("media_type") or ""),
                             "note": "an image in the native transcript; this adapter "
                                     "lists it rather than serving it back"})
            else:
                base.update({"type": "unknown", "kind": btype,
                             "raw_keys": sorted(k for k in block if k != "type")[:12]})
            items.append(base)
    return items


def _settle_tool_items(items: list[dict]) -> None:
    """Join each named result to its call, retaining both native source IDs."""
    by_call = {}
    joined = set()
    for item in items:
        if item.get("call_id") and item["type"] != "functionCallOutput":
            by_call[item["call_id"]] = item
    for item in items:
        if item["type"] != "functionCallOutput":
            continue
        call = by_call.get(item.get("call_id"))
        if call is None:
            continue
        call["status"] = "failed" if item.get("is_error") else "completed"
        call["output"] = item.get("output", "")
        call["result_id"] = item["id"]
        call["result_message_id"] = item.get("message_id")
        call["is_error"] = bool(item.get("is_error"))
        joined.add(item["id"])
    # Unmatched results remain visible; a missing call is never grounds to
    # discard output. Join before pagination so page edges cannot split pairs.
    items[:] = [item for item in items if item["id"] not in joined]


class Session:
    """One native transcript, and what can honestly be said about it."""

    def __init__(self, path: Path, native_id: str, cwd: str, title: str,
                 updated: float, model: str, subagent: bool, count: int):
        self.path = path
        self.native_id = native_id
        self.cwd = cwd
        self.title = title
        self.updated = updated
        self.model = model
        self.subagent = subagent
        self.count = count


def _metadata_records(path: Path):
    """Bound listing IO even when one native line contains a large image."""
    try:
        with path.open("rb") as handle:
            head = handle.read(256 * 1024)
        lines = head.splitlines(keepends=True)
        for line in lines[:60]:
            if not line.endswith(b"\n"):
                continue  # never parse a record truncated by the byte budget
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield record
    except OSError:
        return


def scan_session(path: Path) -> Session | None:
    """Read enough of a transcript to list it, and no more.

    The head names the working directory and whether this is a subagent's own
    conversation; the first thing a person typed is the best title there is.
    """
    native_id = path.stem
    if not UUID_RE.match(native_id):
        return None
    cwd = ""
    title = ""
    model = ""
    subagent = False
    seen = 0
    for record in _metadata_records(path):
        seen += 1
        if record.get("isSidechain"):
            subagent = True
        if not cwd and record.get("cwd"):
            cwd = str(record["cwd"])
        if not model:
            message = record.get("message")
            if isinstance(message, dict) and message.get("model"):
                model = str(message["model"])
        if not title:
            if record.get("type") == "user" and not record.get("isMeta"):
                message = record.get("message")
                if isinstance(message, dict):
                    text = _text_of(message.get("content")).strip()
                    if text and not text.startswith("<local-command-caveat>"):
                        title = " ".join(text.split())[:80]
        if record.get("aiTitle"):
            title = str(record["aiTitle"])[:80]
    if not seen:
        return None
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0.0
    return Session(path, native_id, cwd, title or "Untitled conversation", updated,
                   model, subagent, seen)


# ---------------------------------------------------------------------------
# the Session Vault, read only
# ---------------------------------------------------------------------------

def read_projects(registry_path: Path):
    """Registry projects, verified claude origins, and the ones that do not resolve.

    An origin links a conversation only when it names this runtime, a real
    UUID, and a transcript that exists here. Anything else is returned
    separately so it can be reported honestly rather than remapped.
    """
    projects: dict[str, dict] = {}
    links: dict[str, dict] = {}
    unresolved: list[dict] = []
    warnings: list[str] = []
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"the registry could not be read ({exc}); every conversation "
                        "will show as unfiled")
        return projects, links, unresolved, warnings
    for entry in registry.get("projects") or []:
        project_id = str(entry.get("id") or "")
        if not PROJECT_ID_RE.match(project_id):
            continue
        projects[project_id] = {"id": project_id,
                                "name": str(entry.get("name") or project_id),
                                "root": str(entry.get("root") or "")}
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
                if str(origin.get("runtime") or "") != RUNTIME:
                    continue
                native = str(origin.get("session_id") or "")
                record = {"project_id": project_id, "session": session,
                          "record_path": str(sidecar), "native_id": native,
                          "cwd": str(origin.get("cwd") or ""),
                          "node": str(origin.get("node") or ""),
                          "primary": bool(origin.get("primary"))}
                if not UUID_RE.match(native):
                    # A reference kept for people and other machines. It names
                    # no conversation this CLI can resume, and this adapter
                    # will not pick a different one and call it the same.
                    unresolved.append(dict(record, reason=(
                        "this reference is not a Claude Code session id, so nothing "
                        "here can be resumed from it")))
                    continue
                if native in links and links[native]["project_id"] != project_id:
                    warnings.append(
                        f"{native} is claimed by both {links[native]['project_id']} and "
                        f"{project_id}; showing it under {links[native]['project_id']}")
                    continue
                links.setdefault(native, record)
    return projects, links, unresolved, warnings


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------

class Room:
    def __init__(self, project_id: str, project_name: str, project_root: str,
                 session: str, native_id: str, node: str, unfiled: bool,
                 transcript: Path | None, cwd: str, title: str, updated: float,
                 model: str, started: bool):
        self.project_id = project_id
        self.project_name = project_name
        self.project_root = project_root
        self.session = session
        self.native_id = native_id
        self.node = node
        self.unfiled = unfiled
        self.transcript = transcript
        self.cwd = cwd
        self.title = title
        self.updated = updated
        self.model = model
        self.started = started

    @property
    def id(self) -> str:
        return f"{self.project_id}/{self.session}"

    @property
    def last_active(self) -> str:
        if not self.updated:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated))

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "session": self.session,
            "title": self.title,
            "status": "active",
            "status_source": "vault_record" if not self.unfiled else "none",
            "updated": self.last_active,
            "capability": CAPABILITY,
            "capability_label": CAPABILITY_LABEL,
            "capability_short": CAPABILITY_SHORT,
            "controllable": True,
            "runtime": RUNTIME,
            "node": self.node,
            "origin_count": 0 if self.unfiled else 1,
            "origins_status": "unlinked" if self.unfiled else "linked",
            "worker": False,
            "worker_provenance": "",
            "identity_key": f"{RUNTIME}@{self.node}:{self.native_id}",
            "conversation_key": f"{self.project_id}/{RUNTIME}@{self.node}:{self.native_id}",
            "last_active": self.last_active,
            "updated_ms": int(self.updated * 1000) if self.updated else None,
            "recency_source": "transcript" if self.started else "none",
            "native_role": "human",
            "native_provenance": "claude-code",
            "record_aliases": [],
            "attention": {"state": "none", "source": "adapter",
                          "basis": "this adapter reads no checkpoint"},
            "checkpoint": None,
            "session_id": self.native_id,
            "cwd": self.cwd,
            "started": self.started,
        }


class Catalog:
    """Every local Claude Code conversation, refreshed on a short timer."""

    def __init__(self, config):
        self.config = config
        self.projects_dir = Path(config.projects_dir).expanduser()
        self.registry = Path(config.registry).expanduser()
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {}
        self._by_native: dict[str, Room] = {}
        self._at = 0.0
        self.unresolved: list[dict] = []
        self.warnings: list[str] = []
        self.projects: dict[str, dict] = {}
        self.error = ""
        # Conversations minted here that have not had a turn yet, so they have
        # no transcript to be found by.
        self._reserved: dict[str, Room] = {}
        self._scan_cache = {}
        self.fresh_path = Path(config.state_dir).expanduser()/'fresh-sessions.json'
        if getattr(config,'fresh_only',False) and self.fresh_path.exists():
            for data in json.loads(self.fresh_path.read_text()):
                data['transcript']=None
                room=Room(**data);self._reserved[room.native_id]=room

    def reserve(self, room: Room) -> None:
        with self._lock:
            self._reserved[room.native_id] = room
            if getattr(self.config,'fresh_only',False):
                from ux46_setup import save
                save(self.fresh_path,[dict(vars(r),transcript=None) for r in self._reserved.values()])
            self._at = 0.0

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._at < CATALOG_TTL:
                return
            self._at = time.time()
        projects, links, unresolved, warnings = read_projects(self.registry)
        rooms: dict[str, Room] = {}
        by_native: dict[str, Room] = {}
        error = ""
        found: list[Path] = []
        try:
            for directory in self.projects_dir.iterdir():
                if not directory.is_dir():
                    continue
                candidates=directory.glob("*.jsonl")
                if getattr(self.config,'fresh_only',False):
                    allowed=set(links)|set(self._reserved)
                    candidates=(p for p in candidates if p.stem in allowed)
                found.extend(candidates)
        except OSError as exc:
            if not (isinstance(exc,FileNotFoundError) and getattr(self.config,'fresh_only',False)):
                error = f"the Claude Code transcript directory could not be read: {exc}"
        found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for path in found[:SCAN_LIMIT]:
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
            cached = self._scan_cache.get(str(path))
            session = cached[1] if cached and cached[0] == stamp else scan_session(path)
            self._scan_cache[str(path)] = (stamp, session)
            if session is None or session.subagent:
                continue
            link = links.get(session.native_id)
            if link:
                project = projects.get(link["project_id"], {})
                room = Room(link["project_id"], project.get("name", link["project_id"]),
                            project.get("root", ""), link["session"], session.native_id,
                            self.config.node, False, path, session.cwd, session.title,
                            session.updated, session.model, True)
            else:
                room = Room(self.config.unfiled_project, "Unfiled", "",
                            session.native_id, session.native_id, self.config.node,
                            True, path, session.cwd, session.title, session.updated,
                            session.model, True)
            rooms[room.id] = room
            by_native[room.native_id] = room
        with self._lock:
            for native, room in self._reserved.items():
                if native in by_native:
                    continue
                rooms[room.id] = room
                by_native[native] = room
            self._rooms = rooms
            self._by_native = by_native
            self.projects = projects
            self.unresolved = unresolved
            self.warnings = warnings
            self.error = error
            self._at = time.time()  # expiry starts after the scan completes

    def rooms(self) -> list[Room]:
        self.refresh()
        with self._lock:
            return sorted(self._rooms.values(), key=lambda r: r.updated, reverse=True)

    def room(self, room_id: str) -> Room | None:
        self.refresh()
        with self._lock:
            return self._rooms.get(room_id)

    def by_project(self):
        grouped: dict[str, list[Room]] = {}
        for room in self.rooms():
            grouped.setdefault(room.project_id, []).append(room)
        out = []
        for project_id, members in grouped.items():
            if project_id == self.config.unfiled_project:
                continue
            project = self.projects.get(project_id, {"id": project_id,
                                                     "name": project_id, "root": ""})
            out.append((project, members))
        out.sort(key=lambda pair: pair[1][0].updated if pair[1] else 0, reverse=True)
        if self.config.unfiled_project in grouped:
            out.append(({"id": self.config.unfiled_project, "name": "Unfiled",
                         "root": ""}, grouped[self.config.unfiled_project]))
        return out


# ---------------------------------------------------------------------------
# who else is in this transcript
# ---------------------------------------------------------------------------

class Held:
    def __init__(self, held: bool, detail: str, pids=(), unverified: bool = False):
        self.held = held
        self.detail = detail
        self.pids = list(pids)
        self.unverified = unverified


class Guard:
    """Whether somebody else is in this conversation, on evidence.

    Three questions, strongest answer first. The CLI keeps its own inventory of
    live sessions — `claude agents --json` names every one it is running,
    interactive contexts included, with the session id and the pid — so that is
    asked first and it is the one that sees a session sitting idle between
    turns. Then: does any process have the transcript open right now, and is
    there a `claude` process whose command line names this exact session.

    Any yes is a refusal. So is an inventory that cannot be read: an adapter
    that cannot see who is working is not entitled to guess.
    """

    INVENTORY_TTL = 2.0

    def __init__(self, cli: str, lsof_path: str = ""):
        self.cli = cli
        self.lsof = lsof_path or shutil.which("lsof") or "/usr/sbin/lsof"
        self.available = bool(self.lsof) and Path(self.lsof).exists()
        self.ps = shutil.which("ps") or "/bin/ps"
        self._lock = threading.Lock()
        self._inventory: list[dict] | None = None
        self._inventory_at = 0.0
        self._inventory_error = ""

    def _run(self, argv: list[str]) -> str:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout or ""

    def inventory(self, force: bool = False):
        """What the CLI says it is running, cached for a couple of seconds.

        Returns (entries, error). An error is never an empty inventory: the
        difference between "nothing is running" and "this could not be asked"
        is the whole point of the check.
        """
        with self._lock:
            fresh = (not force and self._inventory is not None
                     and time.time() - self._inventory_at < self.INVENTORY_TTL)
            if fresh:
                return list(self._inventory), self._inventory_error
        entries: list[dict] | None = None
        error = ""
        try:
            done = subprocess.run([self.cli, "agents", "--json"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"the Claude Code session inventory could not be read: {exc}"
        else:
            if done.returncode != 0:
                error = ("the Claude Code session inventory could not be read: "
                         + ((done.stderr or "").strip()[:200]
                            or f"`{Path(self.cli).name} agents --json` exited "
                               f"{done.returncode}"))
            else:
                try:
                    found = json.loads(done.stdout or "null")
                except ValueError as exc:
                    error = f"the Claude Code session inventory was not JSON: {exc}"
                else:
                    if isinstance(found, list):
                        entries = [e for e in found if isinstance(e, dict)]
                    else:
                        error = ("the Claude Code session inventory was not the list "
                                 "this adapter knows how to read")
        with self._lock:
            self._inventory = entries if entries is not None else []
            self._inventory_at = time.time()
            self._inventory_error = error
            return list(self._inventory), error

    def live_for(self, native_id: str, mine: set[int]):
        """Entries in the CLI's own inventory for this exact session."""
        entries, error = self.inventory()
        if error:
            return [], error
        found = []
        for entry in entries:
            if str(entry.get("sessionId") or "") != native_id:
                continue
            try:
                pid = int(entry.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and (pid in mine or pid == os.getpid()):
                continue
            found.append(entry)
        return found, ""

    def open_by(self, path: Path, mine: set[int]) -> list[int]:
        pids = []
        for line in self._run([self.lsof, "-t", "--", str(path)]).split():
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid in mine or pid == os.getpid():
                continue
            pids.append(pid)
        return pids

    def running_for(self, native_id: str, mine: set[int]) -> list[int]:
        if not native_id:
            return []
        pids = []
        for line in self._run([self.ps, "-Ao", "pid=,command="]).splitlines():
            line = line.strip()
            if native_id not in line or "claude" not in line:
                continue
            head = line.split(None, 1)[0]
            try:
                pid = int(head)
            except ValueError:
                continue
            if pid in mine or pid == os.getpid():
                continue
            pids.append(pid)
        return pids

    def inspect(self, path: Path | None, native_id: str, mine: set[int]) -> Held:
        if not self.available:
            return Held(True, "this adapter cannot check who has that conversation open "
                              "(lsof was not found), so it will not write into it",
                        unverified=True)
        live, error = self.live_for(native_id, mine)
        if error:
            # Fail closed. Not knowing is not the same as nobody being there.
            return Held(True, error + ", so UX46 will not write into this conversation",
                        unverified=True)
        if live:
            named = ", ".join(
                f"{e.get('kind') or 'session'} {e.get('pid')}"
                + (f" ({e.get('name')})" if e.get("name") else "") for e in live)
            return Held(True, f"Claude Code is already running this session — {named} — "
                              "so UX46 will not write into it",
                        [e.get("pid") for e in live if e.get("pid")])
        pids = self.open_by(path, mine) if path and path.exists() else []
        if pids:
            return Held(True, f"another process ({', '.join(str(p) for p in pids)}) has "
                              "this transcript open, so UX46 will not write into it", pids)
        pids = self.running_for(native_id, mine)
        if pids:
            return Held(True, f"a Claude Code process ({', '.join(str(p) for p in pids)}) "
                              "is running this exact session, so UX46 will not write "
                              "into it", pids)
        return Held(False, "the CLI is not running this session and nothing has its "
                           "transcript open, so a turn can start")


# ---------------------------------------------------------------------------
# one turn, one process
# ---------------------------------------------------------------------------

class Turn:
    """A single `claude -p` run: start it, read its stream, let it finish."""

    def __init__(self, service, room: Room, client_id: str, blocks: list[dict],
                 resume: bool):
        self.service = service
        self.room = room
        self.client_id = client_id
        self.blocks = blocks
        self.resume = resume
        self.process: subprocess.Popen | None = None
        self.native_id = room.native_id
        self.model = ""
        self.result: dict = {}
        self.stopped = False
        self.finished = False
        self.error = ""
        self.outcome: dict = {}
        self._stderr: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def argv(self) -> list[str]:
        config = self.service.config
        args = [self.service.cli, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--permission-mode", config.permission_mode]
        if self.resume:
            args += ["--resume", self.native_id]
        else:
            args += ["--session-id", self.native_id]
        model = self.service.preference(self.room.id, "model") or config.model
        if model:
            args += ["--model", model]
        effort = self.service.preference(self.room.id, "effort")
        if effort:
            args += ["--effort", effort]
        return args

    def start(self) -> dict:
        """Hand the message to the CLI. This is what acceptance means.

        It returns as soon as the process has the message: `delivered` once
        stdin is written and closed, `failed` if the process never started at
        all, and `uncertain` if it started but the message may or may not have
        reached it. The answer itself arrives later, on its own time.
        """
        service = self.service
        room = self.room
        payload = json.dumps({"type": "user",
                              "message": {"role": "user", "content": self.blocks}})
        cwd = service.turn_cwd(room)
        try:
            self.process = subprocess.Popen(
                self.argv(), cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=os.environ.copy(), start_new_session=True)
        except OSError as exc:
            self.error = f"the Claude Code CLI could not be started: {exc}"
            return {"delivered": False, "state": FAILED, "detail": self.error}
        service.register_turn(room.id, self)
        # Read stderr as it comes. A CLI that writes more than a pipe holds
        # would otherwise block forever waiting for somebody to drain it.
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        try:
            self.process.stdin.write(payload + "\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except OSError as exc:
            self.error = f"the message could not be handed to the CLI: {exc}"
            self.stop()
            service.forget_turn(room.id, self)
            return {"delivered": False, "state": UNCERTAIN, "detail": self.error}
        service.events.publish({"type": "native", "room": room.id,
                                "method": "turn/started", "client_id": self.client_id})
        return {"delivered": True, "state": ACCEPTED,
                "detail": "handed to Claude Code; the answer is being written"}

    def _drain_stderr(self) -> None:
        try:
            for line in self.process.stderr:
                self._stderr.append(line)
                del self._stderr[:-40]
        except (OSError, ValueError):
            pass

    def stream(self) -> dict:
        """Read the turn to its end. Whatever this says, it was delivered."""
        service = self.service
        room = self.room
        watchdog = threading.Timer(TURN_TIMEOUT, self.stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._observe(event)
        except (OSError, ValueError):
            pass
        finally:
            watchdog.cancel()
            code = self.process.wait()
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
            service.forget_turn(room.id, self)
            service.catalog.refresh(force=True)
        stderr = "".join(self._stderr)[-2000:].strip()
        self.finished = True
        if self.stopped:
            outcome = {"native": "stopped", "detail":
                       "the turn was stopped here. Whatever it had already done stands, "
                       "and nothing was sent again."}
        elif self.result.get("subtype") == "success" and not self.result.get("is_error"):
            outcome = {"native": "success", "detail": "", "model": self.model}
        elif self.result:
            outcome = {"native": str(self.result.get("subtype") or "error"),
                       "detail": "the message was delivered; the turn then reported "
                                 + str(self.result.get("subtype") or "an error")
                                 + (": " + str(self.result.get("result"))[:400]
                                    if self.result.get("result") else "")}
        elif code == 0:
            outcome = {"native": "no_result", "detail":
                       "the message was delivered, and the CLI exited without reporting "
                       "a result, so this adapter cannot say the turn finished. Nothing "
                       "was resent."}
        else:
            outcome = {"native": "exit_" + str(code), "detail":
                       "the message was delivered; the CLI then exited with status "
                       f"{code}" + (f": {stderr}" if stderr else "")}
        self.outcome = outcome
        return outcome

    def run_to_end(self) -> dict:
        handoff = self.start()
        if not handoff["delivered"]:
            return handoff
        outcome = self.stream()
        return dict(handoff, outcome=outcome)

    def _observe(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        room = self.room
        session_id = str(event.get("session_id") or "")
        if session_id and UUID_RE.match(session_id):
            self.native_id = session_id
        if kind == "system" and event.get("subtype") == "init":
            self.model = str(event.get("model") or self.model)
            self.service.note_runtime(room.id, self.model,
                                      list(event.get("tools") or []),
                                      list(event.get("mcp_servers") or []))
            return
        if kind == "assistant":
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                self.model = str(message["model"])
        if kind == "result":
            self.result = event
            return
        if kind in ("assistant", "user", "stream_event"):
            # The transcript on disk is the record; this only tells the console
            # something moved, so it re-reads at its own pace.
            self.service.events.publish({"type": "native", "room": room.id,
                                         "method": "turn/updated",
                                         "client_id": self.client_id})

    def stop(self) -> None:
        self.stopped = True
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class ClaudeService:
    def __init__(self, config):
        self.config = config
        self.state_dir = Path(config.state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.journal = journal_store.Journal(self.state_dir / "claude-console.sqlite3")
        self.files = files.FileStore(self.state_dir / "attachments")
        self.catalog = Catalog(config)
        self.events = EventLog()
        self.csrf_token = secrets.token_urlsafe(32)
        self.started_at = time.time()
        self.cli = shutil.which(config.cli) or config.cli
        self.guard = Guard(self.cli, config.lsof)
        self._turns: dict[str, Turn] = {}
        self._turn_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._prefs: dict[str, dict] = {}
        self._runtime: dict[str, dict] = {}
        # When a turn of ours last finished, for the room payload only.
        self._own_write: dict[str, float] = {}
        self._stop = threading.Event()
        self._queue_wake = threading.Event()
        self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True)
        self._queue_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._queue_wake.set()

    # -- small state -------------------------------------------------------
    def preference(self, room_id: str, key: str) -> str:
        return str((self._prefs.get(room_id) or {}).get(key) or "")

    def set_preference(self, room_id: str, key: str, value: str) -> None:
        self._prefs.setdefault(room_id, {})[key] = value

    def note_runtime(self, room_id: str, model: str, tools: list, servers: list) -> None:
        self._runtime[room_id] = {"model": model, "tools": tools[:80],
                                  "mcp_servers": servers[:40], "at": time.time()}

    def register_turn(self, room_id: str, turn: Turn) -> None:
        with self._turn_lock:
            self._turns[room_id] = turn

    def forget_turn(self, room_id: str, turn: Turn) -> None:
        with self._turn_lock:
            if self._turns.get(room_id) is turn:
                del self._turns[room_id]

    def running(self, room_id: str) -> Turn | None:
        with self._turn_lock:
            return self._turns.get(room_id)

    def my_pids(self) -> set[int]:
        with self._turn_lock:
            return {t.process.pid for t in self._turns.values()
                    if t.process is not None and t.process.poll() is None}

    def turn_cwd(self, room: Room) -> Path:
        """Where a turn runs: the conversation's own directory, checked.

        Never a path from a request. An existing conversation keeps the working
        directory its transcript recorded; a new one uses its project root.
        """
        for candidate in (room.cwd, room.project_root, str(Path.home())):
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
        return Path.home()

    def require_room(self, room_id: str) -> Room:
        if not ROOM_ID_RE.match(room_id or ""):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_room", "that is not a room id")
        room = self.catalog.room(room_id)
        if room is None:
            raise AdapterError(HTTPStatus.NOT_FOUND, "room_unknown",
                               "this machine has no Claude Code conversation by that name")
        return room

    # -- reading -----------------------------------------------------------
    def room_state(self, room: Room) -> dict:
        payload = room.as_json()
        turn = self.running(room.id)
        found = self.guard.inspect(room.transcript, room.native_id, self.my_pids())
        held = bool(found.held) and turn is None
        # This console can send whenever no other process is writing that
        # transcript. There is nothing to hold between turns, so "connected"
        # here means exactly that: the way in is clear.
        payload["ownership"] = {
            "state": "held_elsewhere" if held else "atlas_owned",
            "atlas_owned": not held,
            "detected": self.guard.available and not found.unverified,
            "scope": "transcript",
            "exclusive": False,
            "adapter_state": "turn_running" if turn else ("held" if held else "free"),
            "detail": ("UX46 is running a turn here" if turn else found.detail),
            "holders": found.pids,
            "also_open_as": [],
        }
        payload["submissions"] = [s.as_json() for s in self.journal.recent(room.id, limit=10)]
        payload["pending"] = [q.as_json() for q in self.journal.queue_list(room.id)]
        payload["draft"] = self.journal.draft(room.id)
        payload["approvals"] = []
        runtime = self._runtime.get(room.id) or {}
        payload["native"] = {
            "thread_id": room.native_id,
            "session_id": room.native_id,
            "model": runtime.get("model") or room.model or "",
            "requested_model": self.preference(room.id, "model") or self.config.model,
            "reasoning_effort": self.preference(room.id, "effort"),
            "permission_mode": self.config.permission_mode,
            "cwd": str(self.turn_cwd(room)),
            "source": "cli",
            "transcript": str(room.transcript) if room.transcript else "",
            "started": room.started,
            "active_turn": turn.client_id if turn else "",
            "active_run": bool(turn),
            "tools": runtime.get("tools") or [],
            "mcp_servers": runtime.get("mcp_servers") or [],
        }
        payload["capabilities"] = CAPABILITIES
        payload["unsupported"] = dict(UNSUPPORTED_ACTIONS)
        payload["commands"] = [dict(entry) for entry in COMMAND_HELP]
        return payload

    def history(self, room: Room, limit: int, cursor: str, direction: str) -> dict:
        size = max(1, min(int(limit), HISTORY_MAX))
        if room.transcript is None or not room.transcript.exists():
            return {"room": room.id, "thread_id": room.native_id, "items": [],
                    "next_cursor": None, "complete": True, "page_size": size,
                    "direction": direction,
                    "source": "unstarted" if not room.started else "unavailable",
                    "note": ("this conversation has not had a turn yet, so Claude Code "
                             "has not written a transcript for it"
                             if not room.started else
                             "the transcript for this conversation could not be read"),
                    "unavailable": room.started}
        items = transcript_items(_read_records(room.transcript))
        _settle_tool_items(items)
        offset = 0
        if cursor:
            try:
                offset = max(0, int(cursor))
            except ValueError:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_cursor",
                                   "a history cursor is an offset this adapter issued")
        total = len(items)
        end = max(0, total - offset)
        start = max(0, end - size)
        window = items[start:end]
        return {
            "room": room.id,
            "thread_id": room.native_id,
            "items": list(reversed(window)) if direction == "desc" else window,
            "next_cursor": str(offset + len(window)) if start > 0 else None,
            "backwards_cursor": None,
            "complete": start == 0,
            "page_size": size,
            "direction": direction,
            "source": "transcript",
            "total": total,
            "note": "" if items else "this conversation has no visible messages yet",
        }

    def search_history(self, room: Room, query: str, kinds, limit: int) -> dict:
        needle = (query or "").strip().casefold()
        if not needle:
            return {"room": room.id, "query": "", "hits": [], "total": 0}
        wanted = set(kinds or ())
        hits = []
        if room.transcript and room.transcript.exists():
            items = transcript_items(_read_records(room.transcript))
            for item in items:
                if item["type"] == "userMessage" and "human" not in wanted and wanted:
                    continue
                if item["type"] == "agentMessage" and "final" not in wanted and wanted:
                    continue
                if item["type"] not in ("userMessage", "agentMessage"):
                    continue
                text = str(item.get("text") or "")
                if needle in text.casefold():
                    hits.append({"id": item["id"], "type": item["type"], "at": item.get("at"),
                                 "text": text[:400]})
        hits.reverse()
        return {"room": room.id, "query": query, "hits": hits[:max(1, min(limit, 200))],
                "total": len(hits)}

    def workspace(self) -> dict:
        projects = []
        prefs = self.journal.room_prefs()
        for project, members in self.catalog.by_project():
            unfiled = project["id"] == self.config.unfiled_project
            pinned = []
            suggested = []
            hidden_total = 0
            for room in members:
                pref = prefs.get(room.id, {})
                is_pinned = bool(pref.get("pinned"))
                is_hidden = bool(pref.get("hidden"))
                if is_hidden and is_pinned:
                    is_hidden = pref.get("updated_at", 0) > pref.get("pinned_at", 0)
                if is_hidden:
                    hidden_total += 1
                    continue
                if not is_pinned and len(suggested) >= max(0, int(self.config.suggest)):
                    continue
                index = len(suggested)
                entry = room.as_json()
                entry.update({"pinned": is_pinned, "hidden": False, "aliases": [],
                              "alias_count": 0, "group_role": "human",
                              "reason": "pinned" if is_pinned else "recent" if index == 0 else "also_recent",
                              "reason_label": ("pinned to this project" if is_pinned else
                                               "most recent conversation here" if index == 0
                                               else "recently active here")})
                (pinned if is_pinned else suggested).append(entry)
            projects.append({
                "id": project["id"], "name": project["name"], "root": project["root"],
                "last_active": members[0].last_active if members else "",
                "updated_ms": int(members[0].updated * 1000) if members else None,
                "recency_source": "transcript" if members else "none",
                "pinned": pinned, "suggested": suggested,
                "more_total": max(0, len(members) - len(suggested) - len(pinned)),
                "agent_work_total": 0, "hidden_total": hidden_total,
                "record_total": len(members), "conversation_total": len(members),
                "unfiled": unfiled, "vault_project": not unfiled,
                "note": ("Claude Code conversations on this machine with no verified "
                         "Vault origin; no project was created for them" if unfiled else ""),
            })
        return {"projects": projects, "suggest_limit": int(self.config.suggest),
                "native_metadata": not self.catalog.error, "prefs": prefs,
                "gateway_error": self.catalog.error,
                "vault_warnings": self.catalog.warnings[:5],
                "unresolved_references": self.catalog.unresolved[:20]}

    def project_rows(self) -> list[dict]:
        rows = []
        for project, members in self.catalog.by_project():
            unfiled = project["id"] == self.config.unfiled_project
            rows.append({"id": project["id"], "name": project["name"],
                         "root": project["root"], "sessions": len(members),
                         "controllable": len(members), "needs_person": 0,
                         "updated": members[0].last_active if members else "",
                         "updated_ms": int(members[0].updated * 1000) if members else None,
                         "unfiled": unfiled, "vault_project": not unfiled})
        return rows

    def search_rooms(self, query: str, limit: int, offset: int) -> dict:
        rooms = self.catalog.rooms()
        needle = (query or "").strip().casefold()
        if needle:
            rooms = [r for r in rooms
                     if needle in " ".join([r.id, r.title, r.native_id]).casefold()]
        size = max(1, min(int(limit), 200))
        window = rooms[offset:offset + size]
        return {"total": len(rooms), "offset": offset, "returned": len(window),
                "grouped": False, "rooms": [r.as_json() for r in window],
                "truncated": offset + len(window) < len(rooms)}

    # -- writing -----------------------------------------------------------
    def checked_attachments(self, room: Room, wanted) -> list[dict]:
        if not wanted:
            return []
        if not isinstance(wanted, list) or len(wanted) > 8:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_attachments",
                               "attach up to eight files to one message")
        out = []
        for entry in wanted:
            file_id = str((entry or {}).get("id") or "") if isinstance(entry, dict) else str(entry)
            try:
                record, data = self.files.open_download(file_id, room=room.id)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "file_unknown", str(exc)) from exc
            if not str(record["mime"]).startswith("image/"):
                raise AdapterError(
                    HTTPStatus.BAD_REQUEST, "unsupported_attachment",
                    f"{record['name']} is {record['mime']}; this adapter can only send "
                    "images to Claude Code, as native image blocks")
            if len(data) > MAX_ATTACH_BYTES:
                raise AdapterError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                                   f"{record['name']} is larger than this adapter will "
                                   "put in one message")
            out.append({"record": record, "data": data})
        return out

    def message_blocks(self, text: str, attachments: list[dict]) -> list[dict]:
        import base64
        blocks: list[dict] = []
        for entry in attachments:
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": entry["record"]["mime"],
                "data": base64.b64encode(entry["data"]).decode("ascii")}})
        if text.strip():
            blocks.append({"type": "text", "text": text})
        return blocks

    def submit(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id", ""))
        text = body.get("body")
        target = str(body.get("thread_id") or body.get("session_id") or "")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a submission needs a stable client id")
        if not isinstance(text, str) or (not text.strip() and not body.get("attachments")):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "empty", "there is nothing to send")
        if len(text) > MAX_BODY:
            raise AdapterError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                               "that is too long")
        if target and target != room.native_id:
            raise AdapterError(HTTPStatus.CONFLICT, "wrong_target",
                               "that draft was written for a different conversation",
                               {"expected": room.native_id})
        if text.lstrip().startswith("/"):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "is_command",
                               "that looks like a command; send it through the command "
                               "endpoint so it is never delivered as chat text")
        attachments = self.checked_attachments(room, body.get("attachments"))
        return self.dispatch(room, client_id, text, attachments)

    def room_lock(self, room_id: str) -> threading.Lock:
        """One lock per conversation, so two of them never wait on each other."""
        with self._locks_lock:
            lock = self._locks.get(room_id)
            if lock is None:
                lock = self._locks[room_id] = threading.Lock()
            return lock

    def dispatch(self, room: Room, client_id: str, text: str,
                 attachments: list[dict], background: bool = True,
                 reserved: bool = False) -> dict:
        """Reserve, hand over, and answer.

        The lock is held only for as long as it takes to decide and to start
        the process — never for the length of a turn, which is a model's time
        and not a request's. Two conversations do not queue behind each other.
        """
        with self.room_lock(room.id):
            if self.running(room.id) is not None:
                raise AdapterError(HTTPStatus.CONFLICT, "busy",
                                   "a turn is already running in this conversation")
            found = self.guard.inspect(room.transcript, room.native_id, self.my_pids())
            if found.held:
                # Somebody else is in there. Refused before anything is
                # reserved, so the same message can simply be sent again later.
                raise AdapterError(HTTPStatus.CONFLICT, "held_elsewhere", found.detail)
            if reserved:
                # A queued message reserved its own id when it was queued.
                record, created = self.journal.get(client_id), True
            else:
                try:
                    record, created = self.journal.reserve(client_id, room.id,
                                                           room.native_id, text)
                except journal_store.DuplicateMismatch as exc:
                    raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                                       str(exc), exc.detail) from exc
            if not created:
                # This exact submission is already journaled. Whatever became
                # of it, it is not handed over a second time.
                return {"submission": record.as_json(), "duplicate": True,
                        "state": record.status, "delivered": record.status == ACCEPTED,
                        "message": "this message was already handed to Claude Code; "
                                   "UX46 did not send it again"}
            turn = Turn(self, room, client_id, self.message_blocks(text, attachments),
                        resume=room.started)
            handoff = turn.start()
        if not handoff["delivered"]:
            settled = self.journal.settle(client_id, handoff["state"],
                                          detail=handoff["detail"],
                                          native_turn_id=turn.native_id)
            payload = {"submission": settled.as_json(), "state": handoff["state"],
                       "delivered": False, "message": handoff["detail"]}
            payload["uncertain" if handoff["state"] == UNCERTAIN else "failed"] = True
            return payload
        # Delivered. That is what acceptance means here, and nothing the turn
        # reports afterwards takes it back.
        settled = self.journal.settle(client_id, ACCEPTED, detail=handoff["detail"],
                                      native_turn_id=turn.native_id, mode="running")
        if background:
            worker = threading.Thread(target=self._finish, args=(turn,), daemon=True)
            worker.start()
        else:
            self._finish(turn)
            settled = self.journal.get(client_id) or settled
        return {"submission": settled.as_json(), "state": ACCEPTED, "delivered": True,
                "running": background, "message": handoff["detail"],
                "model": turn.model}

    def _finish(self, turn: Turn) -> None:
        """Record how the turn ended, without unsaying that it was delivered."""
        try:
            outcome = turn.stream()
        except Exception as exc:  # noqa: BLE001 - a background thread reports, never dies
            outcome = {"native": "adapter_error", "detail": str(exc)[:200]}
        self._own_write[turn.room.id] = time.time()
        with contextlib.suppress(journal_store.JournalError):
            self.journal.settle(turn.client_id, ACCEPTED,
                                detail=outcome.get("detail", ""),
                                native_turn_id=turn.native_id,
                                mode=outcome.get("native", ""))
        self.events.publish({"type": "native", "room": turn.room.id,
                             "method": "turn/completed", "client_id": turn.client_id,
                             "native_state": outcome.get("native", "")})

    def queue(self, room: Room, body: dict) -> dict:
        client_id = str(body.get("client_id", ""))
        text = body.get("body")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "a queued message needs a stable client id")
        if not isinstance(text, str) or len(text) > MAX_BODY or not text.strip():
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_message",
                               "a queued message needs short text")
        try:
            # A short editing window, then this adapter sends it once, when the
            # conversation is free. It is the same reservation a direct send
            # makes, so a queued message can never also be sent by hand.
            item, created = self.journal.enqueue(
                client_id, room.id, room.native_id, text, [], time.time() + 5.0)
        except journal_store.DuplicateMismatch as exc:
            raise AdapterError(HTTPStatus.CONFLICT, "duplicate_mismatch",
                               str(exc), exc.detail) from exc
        self._queue_wake.set()
        self.events.publish({"type": "queued_message", "room": room.id,
                             "client_id": client_id, "state": item.status})
        return {"pending": item.as_json(), "duplicate": not created}

    def _queue_loop(self) -> None:
        """Send what was queued, one message at a time, when nothing is running.

        Never a replay: each queued message carries the same reservation a
        direct send would have made, so it goes exactly once or not at all.
        """
        while not self._stop.is_set():
            self._queue_wake.wait(timeout=2.0)
            self._queue_wake.clear()
            for item in self.journal.queue_due(time.time()):
                if self._stop.is_set():
                    return
                room = self.catalog.room(item.room)
                if room is None:
                    self.journal.queue_mark(item.client_id, FAILED,
                                            "this machine no longer has that conversation")
                    continue
                if self.running(room.id) is not None:
                    continue
                self.journal.queue_mark(item.client_id, DISPATCHING)
                try:
                    result = self.dispatch(room, item.client_id, item.body, [],
                                           background=False, reserved=True)
                except AdapterError as exc:
                    # Still queued, still exactly once: it waits for the next
                    # pass rather than being retried into a second send.
                    self.journal.queue_mark(item.client_id, PENDING, exc.message)
                    continue
                except Exception as exc:  # noqa: BLE001 - report, never crash the loop
                    self.journal.queue_mark(item.client_id, FAILED, str(exc)[:200])
                    continue
                self.journal.queue_mark(item.client_id, result["state"],
                                        result.get("message", ""))
                self.events.publish({"type": "queued_message", "room": room.id,
                                     "client_id": item.client_id,
                                     "state": result["state"]})

    def stop_turn(self, room: Room) -> dict:
        turn = self.running(room.id)
        if turn is None:
            return {"stopped": False, "state": "idle",
                    "message": "no turn started by UX46 is running in this conversation"}
        turn.stop()
        return {"stopped": True, "state": "stopping", "client_id": turn.client_id,
                "message": "UX46 stopped the turn it started. The transcript keeps "
                           "everything that already happened."}

    def attach(self, room: Room) -> dict:
        """There is nothing to take, so this only reports whether the way is clear."""
        state = self.room_state(room)
        own = state["ownership"]
        return {"room": state, "attached": own["atlas_owned"],
                "release_state": "", "ownership": own,
                "message": own["detail"]}

    def release(self, room: Room) -> dict:
        """Stop this adapter's own turn, if it has one. Never anybody else's."""
        turn = self.running(room.id)
        if turn is None:
            return {"release_state": "released", "room": room.id,
                    "message": "UX46 had nothing running here; the conversation and its "
                               "transcript are untouched."}
        turn.stop()
        return {"release_state": "released", "room": room.id, "stopped_turn": True,
                "message": "UX46 stopped the turn it had started here. Nothing else was "
                           "touched and the transcript is intact."}

    # -- commands ----------------------------------------------------------
    def command(self, room: Room, body: dict) -> dict:
        raw = str(body.get("command") or "").strip()
        name = raw.split()[0].casefold() if raw else ""
        argument = raw[len(name):].strip()
        if name == "/streer":
            name = "/steer"
        if name == "/reasononing":
            name = "/effort"
        elif name == "/reasoning":
            name = "/effort"
        if name not in COMMANDS_SUPPORTED:
            reason = COMMAND_UNSUPPORTED.get(name)
            if not name:
                return {"command": {"name": "/help", "state": COMPLETED,
                                    "supported": list(COMMANDS_SUPPORTED),
                                    "help": [dict(e) for e in COMMAND_HELP],
                                    "unsupported": dict(COMMAND_UNSUPPORTED)}}
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported_command",
                               reason or f"{name} is not a command this adapter runs, "
                               "and it will never be sent as ordinary text")
        if name == "/help":
            return {"command": {"name": "/help", "state": COMPLETED,
                                "supported": list(COMMANDS_SUPPORTED),
                                "help": [dict(e) for e in COMMAND_HELP],
                                "unsupported": dict(COMMAND_UNSUPPORTED)}}
        if name == "/refresh":
            self.catalog.refresh(force=True)
            return {"command": {"name": name, "state": COMPLETED, "message": "CP conversation metadata refreshed. The next turn starts a fresh CLI process with CP’s current login; no message was replayed."}}
        if name == "/status":
            runtime = self._runtime.get(room.id) or {}
            return {"command": {"name": "/status", "state": COMPLETED, "native": {
                "model": runtime.get("model") or room.model or "",
                "requested_model": self.preference(room.id, "model") or self.config.model
                                   or "(the CLI's own default)",
                "reasoning_effort": self.preference(room.id, "effort") or "(unset)",
                "permission_mode": self.config.permission_mode,
                "cwd": str(self.turn_cwd(room)),
                "session_id": room.native_id,
                "active_turn": bool(self.running(room.id)),
                "tools": runtime.get("tools") or [],
            }, "message": ("Model and effort are what the next turn will be launched "
                           "with. A model shown here was reported by a turn that ran.")}}
        if name in ("/model", "/effort"):
            key = "model" if name == "/model" else "effort"
            if not argument:
                return {"command": {"name": name, "state": COMPLETED,
                                    "value": self.preference(room.id, key),
                                    "message": f"the next turn uses "
                                               f"{self.preference(room.id, key) or 'the default'}"}}
            if key == "effort" and argument not in ("low", "medium", "high", "xhigh", "max"):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_effort",
                                   "effort is one of low, medium, high, xhigh, max")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", argument):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_model",
                                   "that is not a model name this adapter will pass on")
            self.set_preference(room.id, key, argument)
            return {"command": {"name": name, "state": COMPLETED, "value": argument,
                                "message": f"the next turn in this conversation will be "
                                           f"launched with {argument}; the CLI decides "
                                           f"whether it accepts it"}}
        client_id = str(body.get("client_id") or "")
        if not CLIENT_ID_RE.match(client_id):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_client_id",
                               "/new needs a stable client id")
        wanted = dict(body)
        wanted["_source_room"] = room.id
        # A slash command originates in a room. Keep the new conversation in
        # that room's project rather than silently putting it under Unfiled.
        if not room.unfiled:
            wanted["project_id"] = room.project_id
        result = self.new_conversation(wanted, title=argument)
        return {**result, "command": {"name": "/new", "state": COMPLETED, "new_room": result["new_room"], "message": result["message"]}}

    # -- creation ----------------------------------------------------------
    def session_options(self) -> dict:
        projects, _, _, _ = read_projects(Path(self.config.registry).expanduser())
        return creation.options(projects)

    def create_session(self, body: dict) -> dict:
        return self.new_conversation(body)

    def new_conversation(self, body: dict, title: str = "") -> dict:
        projects, _, _, _ = read_projects(Path(self.config.registry).expanduser())
        wanted = dict(body)
        if title:
            wanted["title"] = title
        wanted.setdefault("client_id", secrets.token_urlsafe(12).replace("-", "_")[:24])
        try:
            client, chosen_title, project = creation.request(wanted, projects)
        except ValueError as exc:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_session", str(exc)) from exc
        # Reserve the intent durably. Pure metadata creation can be recovered
        # with the same UUID; a reused key with different input is a conflict.
        intent = json.dumps({"title": chosen_title, "project": project["id"] if project else None,
                             "source": body.get("_source_room", "")}, sort_keys=True)
        try:
            self.journal.reserve(client, "ux46/new-conversation", "claude-new", intent)
        except journal_store.DuplicateMismatch as exc:
            raise AdapterError(409, "duplicate_mismatch", "This new-conversation id was already used with different details.") from exc
        # The id the CLI will use for the first turn, minted here so the Vault
        # origin and the conversation are the same thing from the start.
        # A retried /new reuses its stable client id. Deriving the first
        # Claude Code session id from that id makes the operation idempotent
        # across an adapter restart without sending a model turn.
        native_id = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"ux46-cp:{client}"))
        if project:
            root = Path(project["root"]).expanduser()
            session = re.sub(r"[^a-z0-9]+", "-", chosen_title.casefold()).strip("-")[:42]
            session = (session or "exploration") + "-" + hashlib.sha256(
                client.encode()).hexdigest()[:16]
            try:
                creation.link(project, chosen_title, client, RUNTIME, native_id,
                              self.config.node, str(root))
            except Exception as exc:  # noqa: BLE001 - the Vault is not this adapter's
                raise AdapterError(HTTPStatus.BAD_GATEWAY, "vault_error",
                                   f"the Session Vault record could not be written: {exc}")
            room = Room(project["id"], project["name"], project["root"], session,
                        native_id, self.config.node, False, None, str(root),
                        chosen_title, time.time(), "", False)
        else:
            room = Room(self.config.unfiled_project, "Unfiled", "", native_id, native_id,
                        self.config.node, True, None, str(Path.home()), chosen_title,
                        time.time(), "", False)
        self.catalog.reserve(room)
        self.catalog.refresh(force=True)
        self.events.publish({"type": "session", "room": room.id, "state": "created"})
        self.journal.settle(client, journal_store.ACCEPTED, mode="command", detail="New CP conversation: " + room.id)
        return {"state": "created", "new_room": room.id, "room": room.id,
                "session_id": native_id,
                "message": ("Started a CP conversation. Claude Code writes its transcript "
                            "when the first message runs, so it is empty until then.")}

    # -- bootstrap ---------------------------------------------------------
    def bootstrap(self, identity: str) -> dict:
        self.catalog.refresh()
        return {
            "csrf": self.csrf_token,
            "mode": "local",
            "identity": identity,
            "node": self.config.node,
            "public_origin": "",
            "runtime_started": False,
            "started_at": self.started_at,
            "seq": self.events.seq,
            "state_dir": str(self.state_dir),
            "vault_errors": self.catalog.warnings[:5],
            "remembered_owned": {},
            "voice": {"enabled": False, "loaded": False, "voices": [],
                      "reason": UNSUPPORTED_ACTIONS["speak"]},
            "voice_default": "",
            "agent": {
                "id": self.config.agent_id,
                "name": self.config.agent_name,
                "emoji": "",
                "runtime": RUNTIME,
                "identity_source": "adapter configuration",
                "identity_error": "",
                "cli": self.cli,
                "cli_version": self.cli_version(),
                "permission_mode": self.config.permission_mode,
            },
            "adapter": {
                "name": ADAPTER_NAME,
                "version": ADAPTER_VERSION,
                "unfiled_project": self.config.unfiled_project,
                "projects": [row["id"] for row in self.project_rows()],
                "transcripts": str(self.catalog.projects_dir),
                "conversations": len(self.catalog.rooms()),
                "unresolved_references": len(self.catalog.unresolved),
                "ownership_detection": self.guard.available,
                "stream": True,
            },
            "capabilities": dict(CAPABILITIES),
            "unsupported": dict(UNSUPPORTED_ACTIONS),
            "commands": {"supported": list(COMMANDS_SUPPORTED),
                         "help": [dict(e) for e in COMMAND_HELP],
                         "unsupported": dict(COMMAND_UNSUPPORTED)},
        }

    def cli_version(self) -> str:
        cached = getattr(self, "_cli_version", None)
        if cached is not None:
            return cached
        version = ""
        try:
            done = subprocess.run([self.cli, "--version"], capture_output=True,
                                  text=True, timeout=10)
            version = (done.stdout or "").strip()[:80]
        except (OSError, subprocess.SubprocessError):
            version = ""
        self._cli_version = version
        return version

    def attention(self) -> dict:
        return {"approvals": [], "needs": [], "watching": [], "suppressed": [],
                "note": "this adapter reports no attention state; it reads no checkpoint"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{ADAPTER_NAME}/{ADAPTER_VERSION}"
    sys_version = ""
    service: ClaudeService = None       # bound per server

    def log_message(self, fmt, *args):
        if not self.service.config.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------
    def _json(self, status, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send(self, status, data: bytes, mime: str, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, code, message, detail=None) -> None:
        payload = {"error": code, "message": message}
        if detail is not None:
            payload["detail"] = detail
        self._json(status, payload)

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            found = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_json", "that was not JSON")
        if not isinstance(found, dict):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_json", "expected a JSON object")
        return found

    def _consume_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_length", "bad Content-Length")
        if length < 0 or length > MAX_BODY + MAX_ATTACH_BYTES:
            raise AdapterError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                               "that request is too large")
        return self.rfile.read(length) if length else b""

    def _check_host(self) -> None:
        """Loopback only, addressed as loopback. Nothing else is served."""
        client = self.client_address[0]
        if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            raise AdapterError(HTTPStatus.FORBIDDEN, "denied", "this adapter is loopback only")
        host = (self.headers.get("Host") or "").casefold()
        port = self.service.config.port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if host not in allowed:
            raise AdapterError(HTTPStatus.FORBIDDEN, "bad_host",
                               "this adapter answers on its own loopback address only")

    def _check_mutation(self) -> None:
        token = self.headers.get("X-Atlas-CSRF", "")
        if not secrets.compare_digest(token, self.service.csrf_token):
            raise AdapterError(HTTPStatus.FORBIDDEN, "bad_csrf",
                               "this change needs the token from /api/bootstrap")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self):  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._check_host()
            self._raw_body = self._consume_body()
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
        except AdapterError as exc:
            self._error(exc.status, exc.code, exc.message, exc.detail)
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
        if method == "GET" and path == "/api/projects":
            return self._json(HTTPStatus.OK, {"projects": service.project_rows()})
        if method == "GET" and path == "/api/rooms":
            return self._json(HTTPStatus.OK, service.search_rooms(
                get("query"), int(get("limit", "40") or 40), int(get("offset", "0") or 0)))
        if method == "GET" and path == "/api/events":
            return self._json(HTTPStatus.OK, service.events.since(
                int(get("after", "0") or 0),
                min(float(get("timeout", "25") or 25), 30.0), get("room")))
        if method == "GET" and path == "/api/attention":
            return self._json(HTTPStatus.OK, service.attention())
        if method == "GET" and path == "/api/approvals":
            return self._json(HTTPStatus.OK, {"approvals": [], "supported": False,
                                              "message": UNSUPPORTED_ACTIONS["approvals"]})
        if method == "GET" and path == "/api/session-options":
            return self._json(HTTPStatus.OK, service.session_options())
        if method == "POST" and path == "/api/sessions":
            return self._json(HTTPStatus.OK, service.create_session(self._body()))
        if method == "GET" and path == "/api/sessions":
            return self._json(HTTPStatus.OK, {
                "sessions": [r.as_json() for r in service.catalog.rooms()],
                "unresolved_references": service.catalog.unresolved})
        if path == "/api/connection/refresh":
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported",
                               UNSUPPORTED_ACTIONS["refresh"])
        if method == "GET" and path == "/api/submissions":
            return self._json(HTTPStatus.OK, {
                "submissions": [s.as_json() for s in service.journal.recent(get("room"), 50)],
                "unsettled": [s.as_json() for s in service.journal.unsettled()]})

        one = re.fullmatch(r"/api/submissions/([A-Za-z0-9_-]{8,64})", path)
        if method == "GET" and one:
            found = service.journal.get(one.group(1))
            if found is None:
                return self._json(HTTPStatus.NOT_FOUND, {
                    "error": "unknown_submission", "client_id": one.group(1),
                    "message": "this adapter never journaled that submission, so it was "
                               "never handed to Claude Code"})
            return self._json(HTTPStatus.OK, {"submission": found.as_json()})

        download = re.fullmatch(
            r"/api/atlas/files/([A-Za-z0-9_-]{1,128})/(preview|download)", path)
        if method == "GET" and download:
            try:
                record, data = service.files.open_download(download.group(1),
                                                           room=get("room") or None)
            except files.FileStoreError as exc:
                raise AdapterError(HTTPStatus.NOT_FOUND, "file_unknown", str(exc)) from exc
            disposition = files.content_disposition(record["name"])
            if download.group(2) == "preview":
                if record["preview_url"] is None:
                    raise AdapterError(HTTPStatus.NOT_FOUND, "preview_unavailable",
                                       "this attachment is download-only")
                return self._send(HTTPStatus.OK, data, record["mime"],
                                  (("Content-Disposition", "inline; " + disposition[12:]),))
            return self._send(HTTPStatus.OK, data, record["mime"],
                              (("Content-Disposition", disposition),))

        room_match = re.fullmatch(
            r"/api/room/([^/]+/[^/]+)"
            r"(?:/(history|search|continue|attach|submit|release|detach|pending|stop|"
            r"interrupt|command|draft|files|goal|speak|effort|reasoning|pref))?", path)
        if room_match:
            return self._room(method, room_match.group(1), room_match.group(2) or "", get)
        raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")

    def _room(self, method: str, room_id: str, action: str, get) -> None:
        service = self.service
        if action in ("goal", "speak", "effort", "reasoning"):
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported",
                               UNSUPPORTED_ACTIONS.get(action, COMMAND_UNSUPPORTED.get(
                                   "/" + action, "this adapter does not do that")))
        room = service.require_room(room_id)

        if method == "GET" and not action:
            return self._json(HTTPStatus.OK, service.room_state(room))
        if method == "GET" and action == "pref":
            return self._json(HTTPStatus.OK, {"pref": service.journal.room_pref(room.id)})
        if method == "POST" and action == "pref":
            body = self._body()
            pinned, hidden = body.get("pinned"), body.get("hidden")
            if any(value is not None and not isinstance(value, bool)
                   for value in (pinned, hidden)) or (pinned is None and hidden is None):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_pref",
                                   "say which of pinned or hidden to change, using true or false")
            members = [r.id for r in service.catalog.rooms() if r.native_id == room.native_id]
            prefs = service.journal.set_group_pref(
                members, selected=room.id, pinned=pinned, hidden=hidden)
            service.events.publish({"type": "workspace", "room": room.id, "global": True})
            return self._json(HTTPStatus.OK, {"pref": prefs[room.id], "conversation": prefs})
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
                              {"pending": [q.as_json() for q in
                                           service.journal.queue_list(room.id)]})
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
        if method == "POST" and action in ("stop", "interrupt"):
            return self._json(HTTPStatus.OK, service.stop_turn(room))
        if method == "POST" and action == "command":
            return self._json(HTTPStatus.OK, service.command(room, self._body()))
        if method == "GET" and action == "draft":
            return self._json(HTTPStatus.OK, service.journal.draft(room.id))
        if method == "PUT" and action == "draft":
            body = self._body()
            text = body.get("body")
            base_version = body.get("base_version", 0)
            if not isinstance(text, str) or len(text) > MAX_BODY:
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_draft", "a draft is text")
            if not isinstance(base_version, int):
                raise AdapterError(HTTPStatus.BAD_REQUEST, "bad_draft",
                                   "a draft save needs the base_version it was read at")
            try:
                saved = service.journal.save_draft(room.id, text, base_version,
                                                   str(body.get("device", ""))[:40])
            except journal_store.DraftConflict as exc:
                raise AdapterError(HTTPStatus.CONFLICT, "draft_conflict",
                                   "this draft was changed on another device", exc.current)
            service.events.publish({"type": "draft", "room": room.id})
            return self._json(HTTPStatus.OK, saved)
        if method == "POST" and action == "files":
            raise AdapterError(HTTPStatus.BAD_REQUEST, "unsupported_upload",
                               "upload files through the console's own store; this "
                               "adapter sends images it can already read")
        raise AdapterError(HTTPStatus.NOT_FOUND, "not_found", "no such room endpoint")


class ClaudeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: ClaudeService):
        self.service = service
        handler = type("BoundHandler", (Handler,), {"service": service})
        super().__init__(address, handler)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UX46 adapter over the installed Claude Code CLI (CP)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only by design)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME)
    parser.add_argument("--node", default=DEFAULT_NODE)
    parser.add_argument("--cli", default="claude",
                        help="the Claude Code binary; resolved once at startup and "
                             "never taken from a request")
    parser.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR,
                        help="where Claude Code writes its transcripts")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help="read-only Session Vault registry")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="private journal and attachments (not Git, Vault or Tell)")
    parser.add_argument("--unfiled-project", default=DEFAULT_UNFILED_PROJECT,
                        help="namespace for conversations with no verified Vault origin")
    parser.add_argument("--fresh-only", action="store_true", help="Read only explicitly linked or newly created sessions")
    parser.add_argument("--permission-mode", default="default",
                        choices=("default", "acceptEdits", "auto", "bypassPermissions", "manual",
                                 "dontAsk", "plan"),
                        help="passed to the CLI for every turn")
    parser.add_argument("--model", default="",
                        help="model for turns that have not chosen one; empty means the "
                             "CLI's own configured default")
    parser.add_argument("--lsof", default="",
                        help="path to lsof; used only to see who has a transcript open")
    parser.add_argument("--identity-header", default="X-Forwarded-User")
    parser.add_argument("--path-prefix", default="",
                        help="strip this proxy prefix, e.g. /api/agents/cp")
    parser.add_argument("--suggest", type=int, default=2)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    config = build_parser().parse_args(argv)
    if config.host != "127.0.0.1":
        sys.stderr.write("ux46-claude: this adapter binds loopback only\n")
        return 2
    service = ClaudeService(config)
    if not shutil.which(config.cli) and not Path(config.cli).exists():
        sys.stderr.write(f"ux46-claude: no Claude Code CLI at {config.cli!r}\n")
        return 2
    if not service.catalog.projects_dir.is_dir():
        sys.stderr.write(f"ux46-claude: no transcripts at "
                         f"{service.catalog.projects_dir}\n")
        return 2
    server = ClaudeServer((config.host, config.port), service)
    if not config.quiet:
        service.catalog.refresh(force=True)
        print(f"{ADAPTER_NAME} on {config.host}:{server.server_address[1]} — "
              f"{len(service.catalog.rooms())} local Claude Code conversations, "
              f"{config.permission_mode}, {service.cli}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
