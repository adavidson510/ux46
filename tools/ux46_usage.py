"""Deterministic, local usage observations for UX46.

This module reads only a rollout file that its caller explicitly names.  It
does not discover ``~/.codex/sessions``, read conversation text, talk to an
agent, or turn native counters into a bill.  Native ``token_count`` messages
are runtime observations; cached input and reasoning remain subsets of input
and output respectively.

The store deliberately retains the raw source identity and coverage state so a
browser can say "still reading" or "this is an approximation" instead of
inventing a precise total.  SQLite makes normal refreshes append-only and lets
an initial multi-gigabyte rollout be drained in a daemon thread.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Iterator


_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)
_ID_KEYS = ("usage_id", "usageId", "response_id", "responseId")
_CHUNK = 256 * 1024
_ROLLOUT_RE = re.compile(r"^rollout-[0-9T:\-]+-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$", re.I)


def _number(value: Any) -> int:
    """A counter is useful only when it is a finite, non-negative integer."""
    if isinstance(value, bool):
        return 0
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _usage(value: Any) -> dict[str, int]:
    data = value if isinstance(value, dict) else {}
    return {key: _number(data.get(key)) for key in _USAGE_KEYS}


def _is_known_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _field_coverage(value: Any) -> dict[str, bool]:
    data = value if isinstance(value, dict) else {}
    return {key: _is_known_number(data.get(key)) for key in _USAGE_KEYS}


def _utc_day(stamp: Any) -> str:
    """Return a UTC ISO day, falling back to an explicit unknown bucket."""
    if isinstance(stamp, (int, float)):
        try:
            return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return "unknown"
    if not isinstance(stamp, str):
        return "unknown"
    try:
        value = stamp.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            # Native rollout timestamps are expected to be ISO UTC.  A naive
            # legacy value has no verified zone, so don't silently relabel it.
            return "unknown"
        return parsed.astimezone(dt.timezone.utc).date().isoformat()
    except ValueError:
        return "unknown"


def _stable_id(message: dict[str, Any], payload: dict[str, Any], info: dict[str, Any]) -> str:
    for mapping in (info, payload, message):
        for key in _ID_KEYS:
            value = mapping.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
    return ""


def _thread_from_path(path: Path) -> str:
    """Use an explicit rollout filename as a safe native thread identifier."""
    match = _ROLLOUT_RE.match(path.name)
    return match.group(1).lower() if match else ""


def _zero() -> dict[str, int]:
    return {key: 0 for key in _USAGE_KEYS}


def _add(target: dict[str, int], row: dict[str, Any]) -> None:
    for key in _USAGE_KEYS:
        target[key] += _number(row.get(key))


def _null_unknown(values: dict[str, int], known: dict[str, int | bool]) -> dict[str, int | None]:
    """Keep an absent native field distinct from a measured zero."""
    return {key: values[key] if _number(known.get(key)) else None for key in _USAGE_KEYS}


class UsageStore:
    """A local, incremental SQLite projection of explicit native rollouts.

    ``room`` is the UI-friendly non-blocking API.  The first call reads at most
    ``initial_read_bytes`` and continues in one background worker.  Call
    ``ingest`` in a command or test when a synchronous complete read is useful.
    All returned token values are reported processing volumes, never costs.
    """

    def __init__(
        self,
        state_dir: Path | str,
        *,
        tell_receipts_dir: Path | str | None = None,
        initial_read_bytes: int = 512 * 1024,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "usage.sqlite3"
        self.tell_receipts_dir = Path(tell_receipts_dir).expanduser() if tell_receipts_dir else None
        self.initial_read_bytes = max(_CHUNK, int(initial_read_bytes))
        self._lock = threading.RLock()
        self._pending: set[str] = set()
        # Browsing many rooms must not turn a backlog into one OS thread per
        # rollout. Two readers keep the selected room responsive while putting
        # a fixed ceiling on descriptors and SQLite connections.
        self._ingest_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ux46-usage-ingest")
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one transaction and always release its SQLite descriptor."""
        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    device INTEGER, inode INTEGER, offset INTEGER NOT NULL DEFAULT 0,
                    partial BLOB NOT NULL DEFAULT X'', last_snapshot TEXT NOT NULL DEFAULT '',
                    current_model TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'ready', rotations INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_key TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL, day TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'unknown', context_window INTEGER,
                    input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                    cache_write_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    reasoning_output_tokens INTEGER NOT NULL,
                    input_known INTEGER NOT NULL DEFAULT 0, cached_input_known INTEGER NOT NULL DEFAULT 0,
                    cache_write_input_known INTEGER NOT NULL DEFAULT 0, output_known INTEGER NOT NULL DEFAULT 0,
                    reasoning_output_known INTEGER NOT NULL DEFAULT 0, context_known INTEGER NOT NULL DEFAULT 0,
                    model_known INTEGER NOT NULL DEFAULT 0,
                    identity_kind TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_events_thread_day ON usage_events(thread_id, day);
                CREATE TABLE IF NOT EXISTS annotations (
                    annotation_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL, day TEXT NOT NULL,
                    kind TEXT NOT NULL, occurred_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS annotations_thread_day ON annotations(thread_id, day);
                CREATE TABLE IF NOT EXISTS room_threads (
                    room_id TEXT NOT NULL, agent TEXT NOT NULL, project TEXT NOT NULL,
                    thread_id TEXT NOT NULL, PRIMARY KEY(room_id, agent, project, thread_id)
                );
            """)
            # The module's first release had numeric columns only.  Additive
            # coverage columns preserve its cached observations while making
            # missing native fields honest on the next refresh.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
            for name in ("input_known", "cached_input_known", "cache_write_input_known",
                         "output_known", "reasoning_output_known", "context_known", "model_known"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            if "pending_record" not in source_columns:
                conn.execute("ALTER TABLE sources ADD COLUMN pending_record TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _path(value: Path | str) -> Path:
        return Path(value).expanduser().resolve()

    def _source(self, path: Path, *, create: bool = True) -> sqlite3.Row | None:
        thread_id = _thread_from_path(path)
        if not thread_id:
            raise ValueError("usage source must be an explicit rollout-<thread-id>.jsonl path")
        key = str(path)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE path=?", (key,)).fetchone()
            if row is None and create:
                conn.execute("INSERT INTO sources(path, thread_id, status) VALUES(?, ?, 'ready')", (key, thread_id))
                row = conn.execute("SELECT * FROM sources WHERE path=?", (key,)).fetchone()
            return row

    def _associate(self, thread_id: str, room_id: str, agent: str, project: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO room_threads(room_id,agent,project,thread_id) VALUES(?,?,?,?)",
                         (room_id, agent, project, thread_id))

    def room(
        self,
        path: Path | str,
        room_id: str,
        agent: str = "local",
        project: str | None = None,
        day: str | None = None,
    ) -> dict[str, Any]:
        """Return cached and incremental usage for one explicitly named rollout.

        ``day`` is an ISO UTC date.  The result's ``usage`` is a processing
        observation; ``cached_input_tokens`` is already inside ``input_tokens``
        and ``reasoning_output_tokens`` is already inside ``output_tokens``.
        """
        source_path = self._path(path)
        project_name = project or ""
        source = self._source(source_path)
        assert source is not None
        self._associate(source["thread_id"], room_id, agent, project_name)
        before = self._projection(source["thread_id"], day)
        incremental = self._read_once(source_path, self.initial_read_bytes)
        if incremental["pending"]:
            self.ingest_background(source_path)
        after = self._projection(source["thread_id"], day)
        return self._room_result(source_path, room_id, agent, project_name, day, before, after, incremental)

    def ingest(self, path: Path | str) -> dict[str, Any]:
        """Synchronously drain one known rollout; never searches for another."""
        source_path = self._path(path)
        result = {"bytes_read": 0, "events_ingested": 0, "annotations_ingested": 0, "pending": False}
        while True:
            one = self._read_once(source_path, None)
            for key in ("bytes_read", "events_ingested", "annotations_ingested"):
                result[key] += one[key]
            if not one["pending"]:
                return result

    def ingest_background(self, path: Path | str) -> bool:
        """Start (at most once) a bounded background initial/incremental read."""
        source_path = self._path(path)
        key = str(source_path)
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        self._ingest_workers.submit(self._background, source_path)
        return True

    def _background(self, source_path: Path) -> None:
        try:
            while self._read_once(source_path, self.initial_read_bytes)["pending"]:
                pass
        finally:
            with self._lock:
                self._pending.discard(str(source_path))

    def poll(self, path: Path | str) -> dict[str, Any]:
        source_path = self._path(path)
        source = self._source(source_path)
        with self._lock:
            pending = str(source_path) in self._pending
        return {"path": str(source_path), "pending": pending, "status": source["status"] if source else "missing"}

    def _read_once(self, path: Path, limit: int | None) -> dict[str, Any]:
        result = {"bytes_read": 0, "events_ingested": 0, "annotations_ingested": 0, "pending": False}
        source = self._source(path)
        assert source is not None
        try:
            stat = path.stat()
        except FileNotFoundError:
            with self._connect() as conn:
                conn.execute("UPDATE sources SET status='missing', updated_at=? WHERE path=?", (time.time(), str(path)))
            return result
        except OSError:
            with self._connect() as conn:
                conn.execute("UPDATE sources SET status='unreadable', updated_at=? WHERE path=?", (time.time(), str(path)))
            return result

        offset, partial = int(source["offset"]), bytes(source["partial"] or b"")
        last_snapshot = str(source["last_snapshot"] or "")
        current_model = str(source["current_model"] or "unknown")
        pending_record = str(source["pending_record"] or "")
        rotated = (source["device"] is not None and
                   (int(source["device"]) != stat.st_dev or int(source["inode"]) != stat.st_ino or stat.st_size < offset))
        if rotated:
            offset, partial, last_snapshot, current_model, pending_record = 0, b"", "", "unknown", ""
            with self._connect() as conn:
                conn.execute("UPDATE sources SET rotations=rotations+1, last_snapshot='' WHERE path=?", (str(path),))
            rotation_day = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).date().isoformat()
            self._annotation(source["thread_id"], rotation_day, "rotation", "", "rollout path changed or truncated")

        if offset >= stat.st_size:
            with self._connect() as conn:
                conn.execute("UPDATE sources SET device=?,inode=?,status='ready',updated_at=? WHERE path=?",
                             (stat.st_dev, stat.st_ino, time.time(), str(path)))
            return result
        read_size = stat.st_size - offset if limit is None else min(stat.st_size - offset, limit)
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                block = handle.read(read_size)
        except OSError:
            return result
        result["bytes_read"] = len(block)
        tail = partial + block
        lines = tail.split(b"\n")
        complete, partial = lines[:-1], lines[-1]
        # A final newline means no partial.  Keep at most one unparsed line;
        # pathological binary data cannot make the SQLite state unbounded.
        if len(partial) > 2 * 1024 * 1024:
            partial = b""
            self._annotation(source["thread_id"], "unknown", "malformed_line", "", "partial line exceeded 2 MiB")
            result["annotations_ingested"] += 1
        for raw in complete:
            current_model = self._turn_model(raw, current_model)
            event, annotation, last_snapshot, pending_record = self._parse(
                source["thread_id"], raw, last_snapshot, current_model, pending_record
            )
            if event is not None and self._upsert_event(event):
                result["events_ingested"] += 1
            if annotation is not None and self._annotation(**annotation):
                result["annotations_ingested"] += 1
        next_offset = offset + len(block)
        result["pending"] = next_offset < stat.st_size
        with self._connect() as conn:
            conn.execute("""UPDATE sources SET device=?,inode=?,offset=?,partial=?,last_snapshot=?,current_model=?,pending_record=?,status=?,updated_at=? WHERE path=?""",
                         (stat.st_dev, stat.st_ino, next_offset, partial, last_snapshot, current_model, pending_record,
                          "pending" if result["pending"] else "ready", time.time(), str(path)))
        return result

    @staticmethod
    def _turn_model(raw: bytes, current: str) -> str:
        """Keep model metadata from harmless turn-context headers only."""
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return current
        if not isinstance(message, dict) or message.get("type") != "turn_context":
            return current
        payload = message.get("payload")
        model = payload.get("model") if isinstance(payload, dict) else None
        return str(model) if isinstance(model, (str, int)) and str(model) else current

    def _parse(self, thread_id: str, raw: bytes, previous: str, current_model: str,
               pending_record: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, str]:
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None, {"thread_id": thread_id, "day": "unknown", "kind": "malformed_json", "occurred_at": "", "detail": ""}, previous, pending_record
        if not isinstance(message, dict):
            return None, None, previous, pending_record
        payload = message.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        stamp = message.get("timestamp") if isinstance(message.get("timestamp"), str) else ""
        day = _utc_day(stamp)
        if message.get("type") == "token_usage_record":
            counters, fields = _usage(payload.get("usage")), _field_coverage(payload.get("usage"))
            identity = _stable_id(message, payload, {})
            if not identity:
                # An unkeyed record could be the same request as a following
                # token_count, so leave it uncounted rather than guess twice.
                return None, None, previous, ""
            model = current_model or "unknown"
            event = self._event(
                "native:" + thread_id + ":" + identity, thread_id, day, stamp, model, None,
                "native_response_id", counters, fields, context_known=False,
            )
            paired = json.dumps({"event_key": event["event_key"], "usage": counters, "fields": fields}, sort_keys=True)
            return event, None, previous, paired
        if message.get("type") == "compacted":
            key = hashlib.sha256((thread_id + "|compacted|" + stamp + "|" + raw.decode("utf-8", "replace")).encode()).hexdigest()
            return None, {"thread_id": thread_id, "day": day, "kind": "compacted", "occurred_at": stamp, "detail": key}, previous, pending_record
        if payload.get("type") != "token_count" or not isinstance(payload.get("info"), dict):
            return None, None, previous, pending_record
        info = payload["info"]
        last = _usage(info.get("last_token_usage"))
        total = _usage(info.get("total_token_usage"))
        cumulative = total if any(total.values()) else last
        snapshot = json.dumps(cumulative, sort_keys=True, separators=(",", ":"))
        if snapshot == previous:
            return None, None, previous, ""
        reset = False
        try:
            prior_total = json.loads(previous) if previous else {}
            reset = isinstance(prior_total, dict) and any(
                _number(cumulative.get(key)) < _number(prior_total.get(key)) for key in _USAGE_KEYS
            )
        except ValueError:
            pass
        counters = last if any(last.values()) else total
        fields = _field_coverage(info.get("last_token_usage" if any(last.values()) else "total_token_usage"))
        paired = None
        try:
            paired = json.loads(pending_record) if pending_record else None
        except ValueError:
            pass
        matching_record = (isinstance(paired, dict) and isinstance(paired.get("usage"), dict)
                           and paired.get("usage") == counters and isinstance(paired.get("event_key"), str))
        if matching_record and isinstance(paired.get("fields"), dict):
            # The keyed record is authoritative about which zero-valued fields
            # were actually present; its companion snapshot can omit a field.
            fields = {key: bool(paired["fields"].get(key)) for key in _USAGE_KEYS}
        identity = _stable_id(message, payload, info)
        if matching_record:
            event_key, identity_kind = paired["event_key"], "native_response_id"
        elif identity:
            event_key, identity_kind = "native:" + thread_id + ":" + identity, "native_id"
        else:
            digest = hashlib.sha256((thread_id + "|" + stamp + "|" + snapshot + "|" + json.dumps(last, sort_keys=True)).encode()).hexdigest()
            event_key, identity_kind = "snapshot:" + digest, "legacy_snapshot_approximation"
        if not any(last.values()):
            identity_kind = "cumulative_fallback_approximation" if not matching_record else identity_kind
        model = info.get("model") or payload.get("model") or message.get("model") or current_model or "unknown"
        window_known = _is_known_number(info.get("model_context_window"))
        annotation = ({"thread_id": thread_id, "day": day, "kind": "counter_reset", "occurred_at": stamp,
                       "detail": "native cumulative counter decreased"} if reset else None)
        event = self._event(event_key, thread_id, day, stamp, str(model),
                            _number(info.get("model_context_window")) if window_known else None,
                            identity_kind, counters, fields, context_known=window_known)
        return event, annotation, snapshot, ""

    @staticmethod
    def _event(event_key: str, thread_id: str, day: str, occurred_at: str, model: str,
               context_window: int | None, identity_kind: str, counters: dict[str, int],
               fields: dict[str, bool], *, context_known: bool) -> dict[str, Any]:
        return {"event_key": event_key, "thread_id": thread_id, "day": day, "occurred_at": occurred_at,
                "model": model, "context_window": context_window, "identity_kind": identity_kind,
                **counters, "input_known": int(fields["input_tokens"]),
                "cached_input_known": int(fields["cached_input_tokens"]),
                "cache_write_input_known": int(fields["cache_write_input_tokens"]),
                "output_known": int(fields["output_tokens"]),
                "reasoning_output_known": int(fields["reasoning_output_tokens"]),
                "context_known": int(context_known), "model_known": int(model != "unknown")}

    def _upsert_event(self, event: dict[str, Any]) -> bool:
        with self._connect() as conn:
            prior = conn.execute("SELECT * FROM usage_events WHERE event_key=?", (event["event_key"],)).fetchone()
            conn.execute("""INSERT INTO usage_events(event_key,thread_id,day,occurred_at,model,context_window,input_tokens,cached_input_tokens,cache_write_input_tokens,output_tokens,reasoning_output_tokens,input_known,cached_input_known,cache_write_input_known,output_known,reasoning_output_known,context_known,model_known,identity_kind)
                            VALUES(:event_key,:thread_id,:day,:occurred_at,:model,:context_window,:input_tokens,:cached_input_tokens,:cache_write_input_tokens,:output_tokens,:reasoning_output_tokens,:input_known,:cached_input_known,:cache_write_input_known,:output_known,:reasoning_output_known,:context_known,:model_known,:identity_kind)
                            ON CONFLICT(event_key) DO UPDATE SET day=excluded.day,occurred_at=excluded.occurred_at,model=excluded.model,context_window=excluded.context_window,input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,cache_write_input_tokens=excluded.cache_write_input_tokens,output_tokens=excluded.output_tokens,reasoning_output_tokens=excluded.reasoning_output_tokens,input_known=excluded.input_known,cached_input_known=excluded.cached_input_known,cache_write_input_known=excluded.cache_write_input_known,output_known=excluded.output_known,reasoning_output_known=excluded.reasoning_output_known,context_known=excluded.context_known,model_known=excluded.model_known,identity_kind=excluded.identity_kind""", event)
        return prior is None

    def _annotation(self, thread_id: str, day: str, kind: str, occurred_at: str, detail: str) -> bool:
        key = hashlib.sha256((thread_id + "|" + day + "|" + kind + "|" + occurred_at + "|" + detail).encode()).hexdigest()
        with self._connect() as conn:
            before = conn.execute("SELECT 1 FROM annotations WHERE annotation_key=?", (key,)).fetchone()
            conn.execute("INSERT OR IGNORE INTO annotations(annotation_key,thread_id,day,kind,occurred_at,detail) VALUES(?,?,?,?,?,?)",
                         (key, thread_id, day, kind, occurred_at, detail))
        return before is None

    def _projection(self, thread_id: str, day: str | None) -> dict[str, Any]:
        where, params = "thread_id=?", [thread_id]
        if day:
            where += " AND day=?"
            params.append(day)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) steps, COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(cache_write_input_tokens),0) cache_write_input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(reasoning_output_tokens),0) reasoning_output_tokens, COALESCE(SUM(input_known),0) input_known, COALESCE(SUM(cached_input_known),0) cached_input_known, COALESCE(SUM(cache_write_input_known),0) cache_write_input_known, COALESCE(SUM(output_known),0) output_known, COALESCE(SUM(reasoning_output_known),0) reasoning_output_known FROM usage_events WHERE {where}", params).fetchone()
            recent_window = conn.execute(f"SELECT context_window FROM usage_events WHERE {where} AND context_known=1 ORDER BY occurred_at DESC, rowid DESC LIMIT 1", params).fetchone()
            newest = conn.execute(f"SELECT input_tokens,input_known,model,model_known FROM usage_events WHERE {where} ORDER BY occurred_at DESC, rowid DESC LIMIT 1", params).fetchone()
            models = conn.execute(f"SELECT model, COUNT(*) steps, SUM(input_tokens) input_tokens, SUM(cached_input_tokens) cached_input_tokens, SUM(cache_write_input_tokens) cache_write_input_tokens, SUM(output_tokens) output_tokens, SUM(reasoning_output_tokens) reasoning_output_tokens, SUM(input_known) input_known, SUM(cached_input_known) cached_input_known, SUM(cache_write_input_known) cache_write_input_known, SUM(output_known) output_known, SUM(reasoning_output_known) reasoning_output_known FROM usage_events WHERE {where} GROUP BY model ORDER BY model", params).fetchall()
            annotations = conn.execute(f"SELECT kind, COUNT(*) count FROM annotations WHERE {where} GROUP BY kind ORDER BY kind", params).fetchall()
            identities = conn.execute(f"SELECT identity_kind, COUNT(*) count FROM usage_events WHERE {where} GROUP BY identity_kind", params).fetchall()
        values = {key: _number(row[key]) for key in _USAGE_KEYS}
        known = {key: _number(row[key.replace("_tokens", "_known")]) for key in _USAGE_KEYS}
        usage = _null_unknown(values, known)
        usage["steps"] = _number(row["steps"])
        capacity = _number(recent_window["context_window"]) if recent_window else None
        usage["model_context_window_tokens"] = capacity
        # Kept as an additive compatibility name; it describes model capacity,
        # never the size of the latest request.
        usage["recent_context_window_tokens"] = capacity
        usage["latest_input_tokens"] = (_number(newest["input_tokens"])
                                        if newest and _number(newest["input_known"]) else None)
        usage["latest_model"] = (str(newest["model"])
                                 if newest and _number(newest["model_known"]) else None)
        by_model = []
        for model_row in models:
            raw = dict(model_row)
            model_values = {key: _number(raw[key]) for key in _USAGE_KEYS}
            model_known = {key: _number(raw[key.replace("_tokens", "_known")]) for key in _USAGE_KEYS}
            by_model.append({"model": raw["model"] if raw["model"] != "unknown" else None,
                             "steps": _number(raw["steps"]), **_null_unknown(model_values, model_known),
                             "field_coverage": {key: bool(model_known[key]) for key in _USAGE_KEYS}})
        return {"usage": usage, "by_model": by_model,
                "annotations": [dict(r) for r in annotations], "identity_kinds": [dict(r) for r in identities]}

    def _room_result(self, path: Path, room_id: str, agent: str, project: str, day: str | None,
                     before: dict[str, Any], after: dict[str, Any], incremental: dict[str, Any]) -> dict[str, Any]:
        source = self._source(path)
        assert source is not None
        aliases = self._aliases(source["thread_id"])
        usage = after["usage"]
        return {
            "room_id": room_id, "agent": agent, "project": project or None,
            "day": day or "all_recorded_utc_days", "usage": usage,
            "cached": before["usage"], "incremental": incremental,
            "by_model": after["by_model"],
            "source": {"path": str(path), "thread_id": source["thread_id"], "status": source["status"],
                       "rotations": source["rotations"], "explicit_path": True},
            "coverage": {"native": "partial" if incremental["pending"] else "observed", "utc": True,
                         "identity": after["identity_kinds"],
                         "field_coverage": {key: usage[key] is not None for key in _USAGE_KEYS},
                         "model_context_window_tokens": usage["model_context_window_tokens"] is not None,
                         "latest_input_tokens": usage["latest_input_tokens"] is not None,
                         "latest_model": usage["latest_model"] is not None,
                         "notes": ["input_tokens includes cached_input_tokens", "reasoning_output_tokens is included in output_tokens", "model_context_window_tokens is model capacity, not prompt size", "native usage is not a billing total"]},
            "annotations": after["annotations"],
            "alias": {"same_native_thread_rooms": aliases, "deduplication_key": "native:" + source["thread_id"]},
        }

    def _aliases(self, thread_id: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT room_id,agent,project FROM room_threads WHERE thread_id=? ORDER BY agent,project,room_id", (thread_id,)).fetchall()
        return [dict(row) for row in rows]

    def summary(self, *, agent: str | None = None, project: str | None = None, day: str | None = None) -> dict[str, Any]:
        """Aggregate one time per native thread, even when several rooms alias it."""
        clauses, params = [], []
        if agent is not None:
            clauses.append("agent=?"); params.append(agent)
        if project is not None:
            clauses.append("project=?"); params.append(project)
        room_where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            threads = conn.execute(f"SELECT DISTINCT thread_id FROM room_threads {room_where}", params).fetchall()
        totals = _zero(); known = {key: 0 for key in _USAGE_KEYS}; steps = 0; annotations: dict[str, int] = {}
        by_thread = []
        for item in threads:
            projection = self._projection(item["thread_id"], day)
            _add(totals, projection["usage"]); steps += projection["usage"]["steps"]
            for key in _USAGE_KEYS:
                known[key] += int(projection["usage"][key] is not None)
            for annotation in projection["annotations"]:
                annotations[annotation["kind"]] = annotations.get(annotation["kind"], 0) + annotation["count"]
            by_thread.append({"thread_id": item["thread_id"], "usage": projection["usage"], "aliases": self._aliases(item["thread_id"])})
        return {"day": day or "all_recorded_utc_days", "utc": True,
                "usage": {**_null_unknown(totals, known), "steps": steps}, "native_threads": by_thread,
                "annotations": [{"kind": kind, "count": count} for kind, count in sorted(annotations.items())],
                "coverage": {"deduplicated_by": "native_thread_id", "field_coverage": {key: bool(known[key]) for key in _USAGE_KEYS}, "billing": "unknown; no rates or dollars configured"}}

    def tell_receipts(self, path: Path | str | None = None, *, day: str | None = None) -> dict[str, Any]:
        """Read explicit Tell review receipts separately; missing usage remains unknown."""
        root = self._path(path) if path else self.tell_receipts_dir
        result = {"source": "tell_review_receipts", "path": str(root) if root else None,
                  "usage": {**_zero(), "receipts": 0, "recorded_usage_receipts": 0},
                  "unknown_cost_receipts": 0, "coverage": "unavailable", "by_model": {}}
        known = {key: 0 for key in _USAGE_KEYS}
        if root is None or not root.is_dir():
            result["usage"] = {**{key: None for key in _USAGE_KEYS}, "receipts": 0, "recorded_usage_receipts": 0}
            result["field_coverage"] = {key: False for key in _USAGE_KEYS}
            return result
        result["coverage"] = "observed_explicit_directory"
        # Deliberately non-recursive: this is one named Tell receipt directory.
        for receipt in sorted(root.glob("*.json")):
            try:
                value = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict) or (day and value.get("date") != day):
                continue
            result["usage"]["receipts"] += 1
            usage = value.get("usage")
            if not isinstance(usage, dict):
                result["unknown_cost_receipts"] += 1
                continue
            observed = _usage(usage)
            fields = _field_coverage(usage)
            if not any(fields.values()):
                result["unknown_cost_receipts"] += 1
                continue
            _add(result["usage"], observed); result["usage"]["recorded_usage_receipts"] += 1
            for key in _USAGE_KEYS:
                known[key] += int(fields[key])
            model = str(usage.get("model") or "unknown")
            bucket = result["by_model"].setdefault(model, {"values": _zero(), "known": {key: 0 for key in _USAGE_KEYS}})
            _add(bucket["values"], observed)
            for key in _USAGE_KEYS:
                bucket["known"][key] += int(fields[key])
        result["usage"] = {**_null_unknown({key: result["usage"][key] for key in _USAGE_KEYS}, known),
                           "receipts": result["usage"]["receipts"],
                           "recorded_usage_receipts": result["usage"]["recorded_usage_receipts"]}
        result["field_coverage"] = {key: bool(known[key]) for key in _USAGE_KEYS}
        result["by_model"] = {
            (model if model != "unknown" else "unknown"): {**_null_unknown(bucket["values"], bucket["known"]),
                                                               "field_coverage": {key: bool(bucket["known"][key]) for key in _USAGE_KEYS}}
            for model, bucket in result["by_model"].items()
        }
        return result
