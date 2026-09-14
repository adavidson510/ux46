"""Private input journal and drafts for the UX46 console.

A small SQLite file under the configured state directory. It is deliberately
outside Git, the Session Vault and Tell: it holds what the person typed and
what UX46 did with it, nothing else.

Rules encoded here:
  * a submission is journaled durably BEFORE it is dispatched;
  * a repeated client id with the same target and body never dispatches twice;
  * a repeated client id with a different target or body is refused;
  * an uncertain side effect stays uncertain — nothing is auto-replayed;
  * drafts are versioned, so a phone cannot silently overwrite a laptop;
  * a pin or a hide is one row for one room, so two devices never fight over a
    whole preferences document.

Pins and hides live here, on the host, rather than in a Vault record: they are
how one person likes to see their own console, not durable shared memory, and
nothing here reclassifies or removes a record.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3

PENDING = "pending"
DISPATCHING = "dispatching"
EDITING = "editing"
ACCEPTED = "accepted"
FAILED = "failed"
UNCERTAIN = "uncertain"
TERMINAL = (ACCEPTED, FAILED, UNCERTAIN)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    client_id      TEXT PRIMARY KEY,
    room           TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    body           TEXT NOT NULL,
    body_hash      TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    status         TEXT NOT NULL,
    native_turn_id TEXT,
    mode           TEXT,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS submissions_room ON submissions(room, created_at);
CREATE TABLE IF NOT EXISTS queued_messages (
    client_id      TEXT PRIMARY KEY,
    room           TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    body           TEXT NOT NULL,
    attachments    TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    position       REAL NOT NULL,
    editable_until REAL NOT NULL,
    queued_reason  TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS queued_messages_thread ON queued_messages(thread_id, status, position);
CREATE TABLE IF NOT EXISTS drafts (
    room       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    device     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS owned_threads (
    thread_id  TEXT PRIMARY KEY,
    room       TEXT NOT NULL DEFAULT '',
    cwd        TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_prefs (
    room       TEXT PRIMARY KEY,
    pinned     INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0,
    pinned_at  REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_prefs (
    project      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    appearance   TEXT NOT NULL DEFAULT 'purple',
    icon_file_id TEXT,
    version      INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS read_updates (
    item_key   TEXT PRIMARY KEY,
    read_at    REAL NOT NULL
);
"""


class JournalError(RuntimeError):
    def __init__(self, message: str, code: str = "journal_error", detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


class DuplicateMismatch(JournalError):
    """Same client id, different target or body: refuse rather than guess."""

    def __init__(self, message: str, detail=None):
        super().__init__(message, code="duplicate_mismatch", detail=detail)


class DraftConflict(JournalError):
    """Another device advanced this draft; the caller must merge, not clobber."""

    def __init__(self, current: dict):
        super().__init__(
            "this draft was changed on another device", code="draft_conflict", detail=current
        )
        self.current = current


class ProjectPrefConflict(JournalError):
    """A phone or tablet changed project presentation first."""

    def __init__(self, current: dict):
        super().__init__("this project preference changed on another device",
                         code="project_pref_conflict", detail=current)
        self.current = current


@dataclass(frozen=True)
class Submission:
    client_id: str
    room: str
    thread_id: str
    body: str
    status: str
    created_at: float
    updated_at: float
    native_turn_id: str = ""
    mode: str = ""
    detail: str = ""

    def as_json(self) -> dict:
        return {
            "client_id": self.client_id,
            "room": self.room,
            "thread_id": self.thread_id,
            "body": self.body,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "native_turn_id": self.native_turn_id,
            "mode": self.mode,
            "detail": self.detail,
            # "accepted" means the native adapter took the input, never that
            # the agent finished anything.
            "meaning": {
                PENDING: "journaled here, not yet accepted by the runtime",
                ACCEPTED: "accepted by the native runtime — not a completed answer",
                FAILED: "the runtime refused it; nothing was delivered",
                UNCERTAIN: "delivery is unknown; UX46 will not resend on its own",
            }.get(self.status, ""),
        }


@dataclass(frozen=True)
class QueuedMessage:
    client_id: str
    room: str
    thread_id: str
    body: str
    attachments: list[dict]
    status: str
    version: int
    position: float
    editable_until: float
    queued_reason: str
    created_at: float
    updated_at: float

    def as_json(self) -> dict:
        return {"client_id": self.client_id, "room": self.room, "thread_id": self.thread_id,
                "body": self.body, "attachments": self.attachments, "status": self.status,
                "version": self.version, "position": self.position,
                "editable_until": self.editable_until, "queued_reason": self.queued_reason,
                "created_at": self.created_at, "updated_at": self.updated_at}


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_state_dir() -> Path:
    configured = os.environ.get("ATLAS_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".atlas-console"


class Journal:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict(self.path.parent, 0o700)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        # what the person typed stays readable by the owner alone
        for suffix in ("", "-wal", "-shm"):
            self._restrict(Path(str(self.path) + suffix), 0o600)

    @staticmethod
    def _restrict(path: Path, mode: int) -> None:
        try:
            if path.exists():
                os.chmod(path, mode)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            for suffix in ("", "-wal", "-shm"):
                self._restrict(Path(str(self.path) + suffix), 0o600)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- submissions -------------------------------------------------------
    def reserve(self, client_id: str, room: str, thread_id: str, body: str) -> tuple[Submission, bool]:
        """Atomically claim a client id.

        Returns (submission, created). ``created`` False means this exact
        submission was already journaled, so the runtime must not be called
        again for it.
        """

        if not client_id or not room or not thread_id:
            raise JournalError("a submission needs a client id, room and native target",
                               code="bad_submission")
        digest = body_hash(body)
        now = time.time()
        conn = self._connect()
        with self._write_lock:
            try:
                conn.execute(
                    "INSERT INTO submissions(client_id, room, thread_id, body, body_hash,"
                    " created_at, updated_at, status, native_turn_id, mode, detail)"
                    " VALUES(?,?,?,?,?,?,?,?,'','','')",
                    (client_id, room, thread_id, body, digest, now, now, PENDING),
                )
                return self.get(client_id), True  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                existing = self.get(client_id)
                if existing is None:  # pragma: no cover - defensive
                    raise JournalError("submission vanished during reservation")
                if (
                    existing.room != room
                    or existing.thread_id != thread_id
                    or body_hash(existing.body) != digest
                ):
                    raise DuplicateMismatch(
                        "that submission id was already used for a different target or text",
                        detail=existing.as_json(),
                    )
                return existing, False

    def get(self, client_id: str) -> Submission | None:
        row = self._connect().execute(
            "SELECT * FROM submissions WHERE client_id = ?", (client_id,)
        ).fetchone()
        return _row_to_submission(row) if row else None

    def settle(
        self,
        client_id: str,
        status: str,
        *,
        native_turn_id: str = "",
        mode: str = "",
        detail: str = "",
    ) -> Submission:
        if status not in (PENDING, *TERMINAL):
            raise JournalError(f"unknown submission status: {status}")
        with self._write_lock:
            self._connect().execute(
                "UPDATE submissions SET status = ?, native_turn_id = ?, mode = ?,"
                " detail = ?, updated_at = ? WHERE client_id = ?",
                (status, native_turn_id, mode, detail, time.time(), client_id),
            )
        submission = self.get(client_id)
        if submission is None:  # pragma: no cover - defensive
            raise JournalError("cannot settle an unknown submission")
        return submission

    def recent(self, room: str | None = None, limit: int = 50) -> list[Submission]:
        sql = "SELECT * FROM submissions"
        args: list = []
        if room:
            sql += " WHERE room = ?"
            args.append(room)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [_row_to_submission(r) for r in self._connect().execute(sql, args)]

    def unsettled(self) -> list[Submission]:
        return [
            _row_to_submission(r)
            for r in self._connect().execute(
                "SELECT * FROM submissions WHERE status IN (?, ?) ORDER BY created_at",
                (PENDING, UNCERTAIN),
            )
        ]

    # -- editable dispatch queue -----------------------------------------
    def enqueue(self, client_id: str, room: str, thread_id: str, body: str,
                attachments: list[dict], editable_until: float) -> tuple[QueuedMessage, bool]:
        """Durably reserve one exact input and queue it in the same transaction."""
        if not client_id or not room or not thread_id:
            raise JournalError("a queued message needs a client id, room and native target", "bad_submission")
        encoded = json.dumps(attachments, separators=(",", ":"))
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM queued_messages WHERE client_id = ?", (client_id,)).fetchone()
                if row:
                    existing = _row_to_queued(row)
                    if (existing.room != room or existing.thread_id != thread_id
                            or existing.body != body or existing.attachments != attachments):
                        raise DuplicateMismatch("that submission id was already used for different queued input",
                                                existing.as_json())
                    conn.execute("COMMIT")
                    return existing, False
                digest = body_hash(body)
                conn.execute("INSERT INTO submissions(client_id, room, thread_id, body, body_hash, created_at, updated_at, status, native_turn_id, mode, detail) VALUES(?,?,?,?,?,?,?,?,'','','')",
                             (client_id, room, thread_id, body, digest, now, now, PENDING))
                position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM queued_messages WHERE thread_id = ?", (thread_id,)).fetchone()[0]
                conn.execute("INSERT INTO queued_messages(client_id, room, thread_id, body, attachments, status, version, position, editable_until, queued_reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (client_id, room, thread_id, body, encoded, PENDING, 1, position, editable_until, "", now, now))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.queue_get(client_id), True  # type: ignore[return-value]

    def queue_get(self, client_id: str) -> QueuedMessage | None:
        row = self._connect().execute("SELECT * FROM queued_messages WHERE client_id = ?", (client_id,)).fetchone()
        return _row_to_queued(row) if row else None

    def queue_list(self, room: str) -> list[QueuedMessage]:
        return [_row_to_queued(row) for row in self._connect().execute(
            "SELECT * FROM queued_messages WHERE room = ? ORDER BY position, created_at", (room,))]

    def queue_due(self, now: float) -> list[QueuedMessage]:
        return [_row_to_queued(row) for row in self._connect().execute(
            "SELECT * FROM queued_messages WHERE status = ? AND editable_until <= ? ORDER BY thread_id, position, created_at",
            (PENDING, now))]

    def queue_update(self, client_id: str, version: int, *, body: str | None = None,
                     attachments: list[dict] | None = None, position: float | None = None,
                     editable_until: float | None = None, editing: bool | None = None) -> QueuedMessage:
        with self._write_lock:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM queued_messages WHERE client_id = ?", (client_id,)).fetchone()
                if not row: raise JournalError("that queued message is gone", "queue_unknown")
                current = _row_to_queued(row)
                if current.version != version: raise JournalError("that queued message changed elsewhere", "queue_conflict", current.as_json())
                if current.status not in (PENDING, EDITING): raise JournalError("that message is already dispatching and cannot be edited", "queue_locked", current.as_json())
                new_body = body if body is not None else current.body
                new_attachments = attachments if attachments is not None else current.attachments
                status = EDITING if editing is True else (PENDING if editing is False else current.status)
                reason = "editing" if editing is True else ""
                conn.execute("UPDATE queued_messages SET body=?, attachments=?, position=?, editable_until=?, status=?, queued_reason=?, version=version+1, updated_at=? WHERE client_id=?",
                             (new_body, json.dumps(new_attachments, separators=(",", ":")), position if position is not None else current.position,
                              editable_until if editable_until is not None else current.editable_until, status, reason, time.time(), client_id))
                conn.execute("UPDATE submissions SET body=?, body_hash=?, updated_at=? WHERE client_id=?",
                             (new_body, body_hash(new_body), time.time(), client_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK"); raise
        return self.queue_get(client_id)  # type: ignore[return-value]

    def queue_mark(self, client_id: str, status: str, reason: str = "") -> QueuedMessage:
        with self._write_lock:
            self._connect().execute("UPDATE queued_messages SET status=?, queued_reason=?, updated_at=? WHERE client_id=?",
                                    (status, reason, time.time(), client_id))
        return self.queue_get(client_id)  # type: ignore[return-value]

    def queue_cancel(self, client_id: str, version: int) -> QueuedMessage:
        with self._write_lock:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM queued_messages WHERE client_id = ?", (client_id,)).fetchone()
                if not row: raise JournalError("that queued message is gone", "queue_unknown")
                current = _row_to_queued(row)
                if current.version != version: raise JournalError("that queued message changed elsewhere", "queue_conflict", current.as_json())
                if current.status not in (PENDING, EDITING): raise JournalError("that message is already dispatching and cannot be cancelled", "queue_locked", current.as_json())
                conn.execute("UPDATE queued_messages SET status=?, version=version+1, updated_at=? WHERE client_id=?",
                             ("cancelled", time.time(), client_id))
                conn.execute("UPDATE submissions SET status=?, detail=?, updated_at=? WHERE client_id=?",
                             (FAILED, "cancelled before native dispatch", time.time(), client_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK"); raise
        return self.queue_get(client_id)  # type: ignore[return-value]

    # -- drafts ------------------------------------------------------------
    def draft(self, room: str) -> dict:
        row = self._connect().execute(
            "SELECT * FROM drafts WHERE room = ?", (room,)
        ).fetchone()
        if not row:
            return {"room": room, "body": "", "version": 0, "updated_at": 0.0, "device": ""}
        return {
            "room": row["room"],
            "body": row["body"],
            "version": int(row["version"]),
            "updated_at": float(row["updated_at"]),
            "device": row["device"],
        }

    def save_draft(self, room: str, body: str, base_version: int, device: str = "") -> dict:
        """Compare-and-set. A stale base version never overwrites newer text."""

        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT version, body, updated_at, device FROM drafts WHERE room = ?",
                    (room,),
                ).fetchone()
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
                    (room, body, version, now, device),
                )
                conn.execute("COMMIT")
            except DraftConflict:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"room": room, "body": body, "version": version, "updated_at": now, "device": device}

    # -- owned threads (survive a server restart, resumed only on request) --
    def remember_owned(self, thread_id: str, room: str = "", cwd: str = "") -> None:
        with self._write_lock:
            self._connect().execute(
                "INSERT INTO owned_threads(thread_id, room, cwd, claimed_at) VALUES(?,?,?,?)"
                " ON CONFLICT(thread_id) DO UPDATE SET room=excluded.room, cwd=excluded.cwd,"
                " claimed_at=excluded.claimed_at",
                (thread_id, room, cwd, time.time()),
            )

    def forget_owned(self, thread_id: str) -> None:
        with self._write_lock:
            self._connect().execute(
                "DELETE FROM owned_threads WHERE thread_id = ?", (thread_id,)
            )

    def remembered_owned(self) -> list[dict]:
        return [
            {
                "thread_id": r["thread_id"],
                "room": r["room"],
                "cwd": r["cwd"],
                "claimed_at": float(r["claimed_at"]),
            }
            for r in self._connect().execute(
                "SELECT * FROM owned_threads ORDER BY claimed_at DESC"
            )
        ]

    # -- how this person likes to see their own rooms ----------------------
    def room_prefs(self) -> dict[str, dict]:
        return {
            row["room"]: {
                "pinned": bool(row["pinned"]),
                "hidden": bool(row["hidden"]),
                "pinned_at": float(row["pinned_at"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in self._connect().execute("SELECT * FROM room_prefs")
        }

    def room_pref(self, room: str) -> dict:
        row = self._connect().execute(
            "SELECT * FROM room_prefs WHERE room = ?", (room,)
        ).fetchone()
        if not row:
            return {"room": room, "pinned": False, "hidden": False,
                    "pinned_at": 0.0, "updated_at": 0.0}
        return {
            "room": row["room"],
            "pinned": bool(row["pinned"]),
            "hidden": bool(row["hidden"]),
            "pinned_at": float(row["pinned_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def set_room_pref(self, room: str, *, pinned=None, hidden=None) -> dict:
        """One record's flags. See :meth:`set_group_pref` for a conversation."""

        if not room:
            raise JournalError("a preference needs a room", code="bad_pref")
        self.set_group_pref([room], selected=room, pinned=pinned, hidden=hidden)
        return self.room_pref(room)

    def set_group_pref(self, rooms, *, selected: str = "", pinned=None,
                       hidden=None) -> dict[str, dict]:
        """Settle one conversation's preference across all of its records.

        Every record of a conversation is written in a single transaction, so
        a hidden conversation cannot come back under a sibling record and two
        records cannot both claim to be a pinned row. Pinning names exactly one
        record as the face and clears the others; unpinning, hiding and
        restoring apply to all of them. Only the named flag is touched, so a
        phone pinning still never reverts what a laptop hid.
        """

        rooms = [str(room) for room in dict.fromkeys(rooms) if room]
        if not rooms:
            raise JournalError("a preference needs a room", code="bad_pref")
        if pinned is None and hidden is None:
            raise JournalError("nothing to change", code="bad_pref")
        if selected and selected not in rooms:
            raise JournalError("the chosen record is not part of that conversation",
                               code="bad_pref")
        face = selected or rooms[0]
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for room in rooms:
                    conn.execute(
                        "INSERT OR IGNORE INTO room_prefs(room, pinned, hidden, pinned_at,"
                        " updated_at) VALUES(?,0,0,0,?)",
                        (room, now),
                    )
                    if pinned is not None:
                        keep = bool(pinned) and room == face
                        conn.execute(
                            "UPDATE room_prefs SET pinned = ?, pinned_at = ?,"
                            " updated_at = ? WHERE room = ?",
                            (1 if keep else 0, now if keep else 0.0, now, room),
                        )
                    if hidden is not None:
                        conn.execute(
                            "UPDATE room_prefs SET hidden = ?, updated_at = ?"
                            " WHERE room = ?",
                            (1 if hidden else 0, now, room),
                        )
                    # A record with no preference left does not need a row.
                    conn.execute(
                        "DELETE FROM room_prefs WHERE room = ? AND pinned = 0 AND hidden = 0",
                        (room,),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {room: self.room_pref(room) for room in rooms}

    # -- project presentation (one owner, versioned across devices) -------
    def project_pref(self, project: str) -> dict:
        row = self._connect().execute(
            "SELECT * FROM project_prefs WHERE project = ?", (project,)
        ).fetchone()
        if not row:
            return {"project": project, "display_name": "", "appearance": "purple",
                    "icon_file_id": None, "version": 0, "updated_at": 0.0}
        return {"project": row["project"], "display_name": row["display_name"],
                "appearance": row["appearance"], "icon_file_id": row["icon_file_id"],
                "version": int(row["version"]), "updated_at": float(row["updated_at"])}

    def save_project_pref(self, project: str, base_version: int, *, display_name: str | None = None,
                          appearance: str | None = None, icon_file_id: str | None | object = ... ) -> dict:
        """Compare-and-set only the explicitly supplied presentation fields."""

        current = self.project_pref(project)
        if display_name is None:
            display_name = current["display_name"]
        if appearance is None:
            appearance = current["appearance"]
        if icon_file_id is ...:
            icon_file_id = current["icon_file_id"]
        now = time.time()
        with self._write_lock:
            conn = self._connect(); conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT version FROM project_prefs WHERE project = ?", (project,)).fetchone()
                version = int(row["version"]) if row else 0
                if int(base_version) != version:
                    conn.execute("ROLLBACK")
                    raise ProjectPrefConflict(self.project_pref(project))
                conn.execute(
                    "INSERT INTO project_prefs(project, display_name, appearance, icon_file_id, version, updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
                    "display_name=excluded.display_name, appearance=excluded.appearance, "
                    "icon_file_id=excluded.icon_file_id, version=excluded.version, updated_at=excluded.updated_at",
                    (project, display_name, appearance, icon_file_id, version + 1, now),
                )
                conn.execute("COMMIT")
            except ProjectPrefConflict:
                raise
            except Exception:
                conn.execute("ROLLBACK"); raise
        return self.project_pref(project)

    # -- local reading state ----------------------------------------------
    def mark_read(self, item_key: str, read: bool = True) -> None:
        with self._write_lock:
            if read:
                self._connect().execute(
                    "INSERT OR REPLACE INTO read_updates(item_key, read_at) VALUES(?,?)",
                    (item_key, time.time()),
                )
            else:
                self._connect().execute(
                    "DELETE FROM read_updates WHERE item_key = ?", (item_key,)
                )

    def read_keys(self) -> set[str]:
        return {r["item_key"] for r in self._connect().execute("SELECT item_key FROM read_updates")}


def _row_to_submission(row: sqlite3.Row) -> Submission:
    return Submission(
        client_id=row["client_id"],
        room=row["room"],
        thread_id=row["thread_id"],
        body=row["body"],
        status=row["status"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        native_turn_id=row["native_turn_id"] or "",
        mode=row["mode"] or "",
        detail=row["detail"] or "",
    )


def _row_to_queued(row: sqlite3.Row) -> QueuedMessage:
    try:
        attachments = json.loads(row["attachments"] or "[]")
    except json.JSONDecodeError:
        attachments = []
    return QueuedMessage(client_id=row["client_id"], room=row["room"], thread_id=row["thread_id"],
                         body=row["body"], attachments=attachments if isinstance(attachments, list) else [],
                         status=row["status"], version=int(row["version"]), position=float(row["position"]),
                         editable_until=float(row["editable_until"]), queued_reason=row["queued_reason"] or "",
                         created_at=float(row["created_at"]), updated_at=float(row["updated_at"]))


def summarize(submissions: list[Submission]) -> dict:
    counts: dict[str, int] = {}
    for submission in submissions:
        counts[submission.status] = counts.get(submission.status, 0) + 1
    return counts


__all__ = [
    "Journal",
    "JournalError",
    "DuplicateMismatch",
    "DraftConflict",
    "ProjectPrefConflict",
    "Submission",
    "QueuedMessage",
    "PENDING",
    "DISPATCHING",
    "ACCEPTED",
    "FAILED",
    "UNCERTAIN",
    "default_state_dir",
    "body_hash",
    "summarize",
]
