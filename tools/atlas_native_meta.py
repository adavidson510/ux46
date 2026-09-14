"""Read-only native session metadata: honest provenance and real recency.

The Codex CLI keeps a small catalogue of its own threads in SQLite. Reading it
answers two questions the Session Vault cannot: whether a native thread was
started by a person or spawned as a sub-agent, and when the runtime last
touched it. Both are read as existing metadata.

What this module deliberately does not do:

  * it never writes, never creates the database and never migrates it — the
    connection is opened read-only, so a missing file is simply "no metadata";
  * it never reads a transcript, a rollout file or a first user message;
  * it never calls a model or the native runtime;
  * it never treats absence as evidence. A thread this catalogue does not
    describe stays ``unknown``, which is neither "a person" nor "a worker".

``has_user_event`` is not used for anything: it is 0 on real human threads, so
reading it as "a person spoke here" would be wrong.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

HUMAN = "human"
WORKER = "worker"
UNKNOWN = "unknown"

TABLE = "threads"
# Columns we would like. Anything missing from an older or newer schema is
# simply not selected, so a schema change degrades to less provenance rather
# than to an exception.
WANTED = (
    "id", "thread_source", "source", "updated_at", "updated_at_ms",
    "archived", "agent_role", "agent_nickname", "title",
)
CHUNK = 400


def default_db_path() -> Path:
    configured = os.environ.get("ATLAS_NATIVE_DB")
    if configured:
        return Path(configured).expanduser()
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "state_5.sqlite"


@dataclass(frozen=True)
class ThreadMeta:
    """What the native catalogue says about one thread.

    ``title`` is the person's own prompt text and stays inside this process:
    it is a ranking hint only, and never reaches the browser, an artifact or a
    Vault record. Use :meth:`mentions` rather than reading it.
    """

    thread_id: str
    role: str
    provenance: str
    updated_ms: int
    archived: bool
    parent_thread_id: str
    title: str = ""

    def mentions(self, needle: str) -> bool:
        needle = (needle or "").strip().casefold()
        return bool(needle) and needle in self.title.casefold()


class NativeMetadata:
    """Bounded, cached, read-only batch lookups over the native catalogue."""

    def __init__(self, db_path: Path | str | None = None, ttl: float = 60.0):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, ThreadMeta]] = {}
        self._columns: tuple[str, ...] | None = None
        self._available: bool | None = None

    # -- availability ------------------------------------------------------
    @property
    def available(self) -> bool:
        if self._available is None:
            self._columns = self._read_columns()
            self._available = bool(self._columns)
        return bool(self._available)

    def status(self) -> dict:
        return {
            "available": self.available,
            "path": str(self.db_path),
            "columns": list(self._columns or ()),
        }

    def _connect(self) -> sqlite3.Connection:
        # mode=ro never creates the file and never writes; a WAL sidecar is
        # read but not checkpointed.
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _read_columns(self) -> tuple[str, ...]:
        if not self.db_path.is_file():
            return ()
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
        except sqlite3.Error:
            return ()
        present = {str(row["name"]) for row in rows}
        if "id" not in present:
            return ()
        return tuple(name for name in WANTED if name in present)

    # -- lookups -----------------------------------------------------------
    def lookup(self, thread_ids) -> dict[str, ThreadMeta]:
        """Metadata for the ids this catalogue knows. Unknown ids are absent."""

        wanted = [str(t) for t in dict.fromkeys(thread_ids) if t]
        if not wanted or not self.available:
            return {}
        now = time.time()
        found: dict[str, ThreadMeta] = {}
        missing: list[str] = []
        with self._lock:
            for thread_id in wanted:
                entry = self._cache.get(thread_id)
                if entry and (now - entry[0]) < self.ttl:
                    found[thread_id] = entry[1]
                else:
                    missing.append(thread_id)
        if not missing:
            return found
        columns = ", ".join(self._columns or ())
        fresh: dict[str, ThreadMeta] = {}
        try:
            with closing(self._connect()) as conn:
                for start in range(0, len(missing), CHUNK):
                    chunk = missing[start:start + CHUNK]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT {columns} FROM {TABLE} WHERE id IN ({marks})", chunk
                    ).fetchall()
                    for row in rows:
                        meta = _meta_from_row(row)
                        fresh[meta.thread_id] = meta
        except sqlite3.Error:
            # A locked, moved or unreadable catalogue means no provenance for
            # this pass, not a wrong one.
            return found
        with self._lock:
            for thread_id, meta in fresh.items():
                self._cache[thread_id] = (now, meta)
        found.update(fresh)
        return found


def _meta_from_row(row: sqlite3.Row) -> ThreadMeta:
    data = {key: row[key] for key in row.keys()}
    parent = _parent_from_source(data.get("source"))
    role, provenance = _classify(data, parent)
    updated_ms = data.get("updated_at_ms")
    if not updated_ms and data.get("updated_at"):
        try:
            updated_ms = int(data["updated_at"]) * 1000
        except (TypeError, ValueError):
            updated_ms = 0
    return ThreadMeta(
        thread_id=str(data.get("id") or ""),
        role=role,
        provenance=provenance,
        updated_ms=int(updated_ms or 0),
        archived=bool(data.get("archived")),
        parent_thread_id=parent,
        title=str(data.get("title") or ""),
    )


def _parent_from_source(source) -> str:
    """A spawned thread names its parent inside its own source record."""

    if not isinstance(source, str) or "{" not in source:
        return ""
    try:
        payload = json.loads(source)
    except (json.JSONDecodeError, ValueError):
        return ""
    spawn = (((payload or {}).get("subagent") or {}).get("thread_spawn") or {})
    if not isinstance(spawn, dict):
        return ""
    return str(spawn.get("parent_thread_id") or "")


def _classify(data: dict, parent: str) -> tuple[str, str]:
    kind = str(data.get("thread_source") or "").strip().casefold()
    if kind == "subagent":
        return WORKER, "native:thread_source=subagent"
    if parent:
        return WORKER, "native:parent_thread_id"
    if kind == "user":
        return HUMAN, "native:thread_source=user"
    if not kind and (data.get("agent_role") or data.get("agent_nickname")):
        # An older catalogue without thread_source still names a spawned role.
        return WORKER, "native:agent_role"
    # guardian_review, a blank column, a value this build has never seen: the
    # honest answer is that we do not know who started it.
    return UNKNOWN, f"native:thread_source={kind}" if kind else ""


__all__ = [
    "NativeMetadata",
    "ThreadMeta",
    "HUMAN",
    "WORKER",
    "UNKNOWN",
    "default_db_path",
]
