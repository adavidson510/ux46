#!/usr/bin/env python3
"""Project Atlas Session Vault: search and validate project-local records."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

if __package__ in (None, ""):  # executed as a script; the sibling module is here
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import central_store


STOP_WORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "did", "do",
    "find", "for", "from", "had", "in", "is", "it", "me", "my", "of",
    "on", "or", "our", "project", "projects", "session", "sessions", "that",
    "the", "this", "to", "vault", "was", "we", "what", "when", "where",
    "which", "with", "window", "you",
}
REQUIRED_FIELDS = (
    "project", "session", "title", "date", "updated", "status", "keywords"
)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ORIGINS_SCHEMA_VERSION = 1
SUPPORTED_RESUME_RUNTIMES = {"codex"}
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_STATES = {
    "working", "waiting", "needs-human", "blocked", "quiet", "complete",
}
CHECKPOINT_NEEDS = {
    "none", "answer", "review", "decision", "approval", "action",
    "credential", "ui", "external",
}
HUMAN_CHECKPOINT_NEEDS = {
    "answer", "review", "decision", "approval", "action", "credential", "ui",
}
CHECKPOINT_STATE_NEEDS = {
    "working": {"none"},
    "waiting": {"none", "external"},
    "needs-human": HUMAN_CHECKPOINT_NEEDS,
    "blocked": HUMAN_CHECKPOINT_NEEDS | {"external"},
    "quiet": {"none"},
    "complete": {"none"},
}
CHECKPOINT_FIELDS = (
    "checkpoint_schema",
    "checkpoint_at",
    "checkpoint_node",
    "checkpoint_reporter",
    "checkpoint_state",
    "checkpoint_need",
    "checkpoint_next",
)
SESSION_STATUSES = {"active", "parked", "validation", "decided", "complete"}

# --- Derived attention -------------------------------------------------------
# `status` is the long-lived lifecycle of the room; `checkpoint_state` is what
# the last reporter asserted was true. They answer different questions, so
# neither is rewritten into the other and both stay in the payload verbatim.
# Attention is a read-time projection only: it reads the current checkpoint
# first, because a `parked` room whose last checkpoint asks for a decision
# still needs that decision, and an `active` room whose reporter checkpointed
# `complete` does not. Nothing here is ever written back into a record.
ATTENTION_SCHEMA_VERSION = 1
ATTENTION_STATES = ("none", "review_ready", "needs_soon", "needs_now")
ATTENTION_RANK = {state: rank for rank, state in enumerate(ATTENTION_STATES)}
STATUS_ATTENTION = {"validation": "review_ready"}
CHECKPOINT_HISTORY_NOTICE_COUNT = 20
CHECKPOINT_HISTORY_TITLE = "Checkpoint history"
CHECKPOINT_HISTORY_PATTERN = re.compile(
    r"(?ms)^## Checkpoint history\s*\n.*?(?=^##\s|\Z)"
)
CHECKPOINT_HISTORY_ENTRY = re.compile(r"(?m)^### ")
# Archived history is moved, never deleted, and never overwritten: it lands in
# a nested directory the record loader does not scan, under an incrementing
# version, so the top-level record keeps exactly one current checkpoint.
HISTORY_ARCHIVE_DIRNAME = "history"
HISTORY_ARCHIVE_SCHEMA_VERSION = 1
HISTORY_ARCHIVE_KIND = "checkpoint-history-archive"
HISTORY_ARCHIVE_MAX_VERSION = 999
HISTORY_ARCHIVE_NAME = re.compile(
    r"^(?P<session>.+)\.checkpoint-history\.(?P<version>\d{3,})\.md$"
)
HISTORY_ARCHIVE_AUTHORITY_NOTE = (
    "An archived checkpoint history is what reporters asserted at the time. "
    "It is not current state, approval, or proof of an external change."
)
RECORD_SIZE_NOTICE_BYTES = 256 * 1024
SCAFFOLD_TEXT = (
    "Session initialized; durable re-entry context has not yet been captured."
)

# --- Tell live route ---------------------------------------------------------
# Session Vault owns durable project/session memory. Tell owns addressed
# delivery and the ephemeral, receiver-owned live route. A Tell route is
# transport state: it is never authority, never approval, and never evidence
# that an external system changed. Nothing below is written into the Markdown
# record or the origins sidecar.
TELL_SESSION_ID = re.compile(r"^s_[A-Za-z0-9_-]{1,96}$")
TELL_DEFAULT_COLLECTIVE = "workspace"
TELL_DEFAULT_TTL = 900
TELL_MAX_TTL = 86_400
TELL_TIMEOUT_SECONDS = 20
TELL_LABEL_MAX_CHARS = 120
TELL_CLIENT_CANDIDATES = (
    ("src", "tell", "cli.py"),
    ("tell", "cli.py"),
    ("cli.py",),
)
ROUTE_STATE_SCHEMA_VERSION = 1
ROUTE_STATE_KIND = "tell-route-runtime"
ROUTE_AUTHORITY_NOTE = (
    "A Tell route is ephemeral delivery routing owned by the receiver. "
    "It is not authority, approval, or proof of an external change."
)


class VaultError(RuntimeError):
    """A user-facing registry or lookup error."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: Path
    aliases: tuple[str, ...]

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def lookup_names(self) -> tuple[str, ...]:
        return (self.id, self.name, *self.aliases)


@dataclass(frozen=True)
class Record:
    project: Project
    path: Path
    metadata: dict[str, str]
    body: str

    @property
    def session(self) -> str:
        return self.metadata.get("session", self.path.stem)

    @property
    def identity(self) -> str:
        return f"{self.project.id}/{self.session}"

    @property
    def title(self) -> str:
        return self.metadata.get("title", "Untitled")

    @property
    def aliases(self) -> tuple[str, ...]:
        values = []
        for key in ("aliases", "legacy_ids"):
            values.extend(split_csv(self.metadata.get(key, "")))
        return tuple(values)

    @property
    def origins_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.origins.json")


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def default_registry() -> Path:
    configured = os.environ.get("ATLAS_REGISTRY")
    if configured:
        return Path(configured).expanduser()
    return codex_home() / "projects" / "registry.json"


def load_registry(registry_path: Path) -> dict:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VaultError(f"Atlas registry not found: {registry_path}") from exc
    except json.JSONDecodeError as exc:
        raise VaultError(f"Invalid Atlas registry JSON at {registry_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VaultError(f"Atlas registry must be an object: {registry_path}")
    return payload


def registry_node(registry: dict) -> str:
    node = str(registry.get("node_id", "local")).strip()
    if not node or not SAFE_COMPONENT.fullmatch(node):
        raise VaultError(f"Invalid Atlas node id: {node!r}")
    return node


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_frontmatter_value(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped.strip('"')
        if isinstance(decoded, (str, int, float, bool)):
            return str(decoded)
    return stripped.strip('"')


def frontmatter_value(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def parse_record(project: Project, path: Path) -> Record:
    text = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip().lower()] = parse_frontmatter_value(value)
            body = parts[2].lstrip()
    return Record(project=project, path=path, metadata=metadata, body=body)


def load_projects(payload: dict, registry_path: Path) -> list[Project]:
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise VaultError(f"Atlas registry has no projects list: {registry_path}")

    result: list[Project] = []
    for raw in raw_projects:
        if not isinstance(raw, dict):
            raise VaultError("Atlas registry project entries must be objects")
        project_id = str(raw.get("id", "")).strip()
        name = str(raw.get("name", project_id)).strip()
        root_text = str(raw.get("root", "")).strip()
        aliases = raw.get("aliases", [])
        if not project_id or not root_text:
            raise VaultError("Atlas registry projects require id and root")
        if not isinstance(aliases, list):
            raise VaultError(f"Project aliases must be a list: {project_id}")
        result.append(Project(
            id=project_id,
            name=name,
            root=Path(root_text).expanduser(),
            aliases=tuple(str(alias) for alias in aliases),
        ))
    return result


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@contextmanager
def record_lock(path: Path):
    """Serialize Vault record writes without adding lock files to projects."""
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def replace_markdown_section(body: str, title: str, content: str) -> str:
    marker = f"## {title}"
    replacement = f"{marker}\n\n{content.strip()}\n\n"
    pattern = re.compile(
        rf"(?ms)^{re.escape(marker)}\s*\n.*?(?=^##\s|\Z)"
    )
    if pattern.search(body):
        return pattern.sub(replacement, body, count=1)
    open_loops = re.search(r"(?m)^## Open loops\s*$", body)
    if open_loops:
        return body[:open_loops.start()] + replacement + body[open_loops.start():]
    return body.rstrip() + "\n\n" + replacement


def archive_current_checkpoint(
    body: str,
    checkpoint: dict[str, object] | None,
) -> str:
    if checkpoint is None:
        return body
    current_pattern = re.compile(
        r"(?ms)^## Current checkpoint\s*\n(.*?)(?=^##\s|\Z)"
    )
    current = current_pattern.search(body)
    if current is None:
        return body
    entry_title = (
        f"### {checkpoint.get('at', '')} — {checkpoint.get('reporter', '')} — "
        f"{checkpoint.get('state', '')}/{checkpoint.get('need', '')}"
    )
    entry = f"{entry_title}\n\n{current.group(1).strip()}\n\n"
    history = CHECKPOINT_HISTORY_PATTERN.search(body)
    if history is not None:
        replacement = history.group(0).rstrip() + "\n\n" + entry
        return body[:history.start()] + replacement + body[history.end():]
    marker = re.search(r"(?m)^## Open loops\s*$", body)
    history_block = f"## Checkpoint history\n\n{entry}"
    if marker is not None:
        return body[:marker.start()] + history_block + body[marker.start():]
    return body.rstrip() + "\n\n" + history_block


def update_record(
    path: Path,
    fields: dict[str, object],
    *,
    section_title: str | None = None,
    section_content: str | None = None,
    checkpoint_history: dict[str, object] | None = None,
) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise VaultError(f"Session record has no frontmatter: {path}")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise VaultError(f"Session record has malformed frontmatter: {path}")
    lines = parts[1].splitlines()
    remaining = {key.casefold(): (key, value) for key, value in fields.items()}
    for index, line in enumerate(lines):
        folded = line.split(":", 1)[0].strip().casefold()
        if folded in remaining:
            key, value = remaining.pop(folded)
            lines[index] = f"{key}: {frontmatter_value(value)}"
    for key, value in remaining.values():
        lines.append(f"{key}: {frontmatter_value(value)}")
    body = parts[2]
    if checkpoint_history is not None:
        body = archive_current_checkpoint(body, checkpoint_history)
    if section_title is not None and section_content is not None:
        body = replace_markdown_section(body, section_title, section_content)
    atomic_write(path, "---\n" + "\n".join(lines) + "\n---\n" + body)


def set_frontmatter_field(path: Path, key: str, value: str) -> None:
    with record_lock(path):
        update_record(path, {key: value})


def checkpoint_payload(record: Record) -> dict[str, object] | None:
    if not any(record.metadata.get(field) for field in CHECKPOINT_FIELDS):
        return None
    schema = record.metadata.get("checkpoint_schema", "")
    return {
        "schema_version": int(schema) if schema.isdigit() else schema,
        "at": record.metadata.get("checkpoint_at", ""),
        "node": record.metadata.get("checkpoint_node", ""),
        "reporter": record.metadata.get("checkpoint_reporter", ""),
        "state": record.metadata.get("checkpoint_state", ""),
        "need": record.metadata.get("checkpoint_need", ""),
        "next": record.metadata.get("checkpoint_next", ""),
    }


def checkpoint_attention(state: str, need: str) -> str:
    """Map one reported checkpoint state/need pair to an attention state."""
    if state in ("needs-human", "blocked"):
        if need == "review":
            return "review_ready"
        if need in HUMAN_CHECKPOINT_NEEDS:
            return "needs_now"
        if need == "external":
            return "needs_soon"
    return "none"


def attention_projection(record: Record) -> dict[str, object]:
    """Derive attention from the current checkpoint, falling back to lifecycle.

    The projection is additive: `status` and `checkpoint` remain in the payload
    exactly as recorded, and no record is mutated to produce it.
    """

    status = record.metadata.get("status", "")
    checkpoint = checkpoint_payload(record)
    malformed = bool(checkpoint) and bool(checkpoint_errors(record.metadata))
    if checkpoint is not None and not malformed:
        state = str(checkpoint.get("state", ""))
        need = str(checkpoint.get("need", ""))
        derived = checkpoint_attention(state, need)
        return {
            "schema_version": ATTENTION_SCHEMA_VERSION,
            "state": derived,
            "rank": ATTENTION_RANK[derived],
            "source": "checkpoint",
            "basis": f"checkpoint {state}/{need}",
            "checkpoint_at": str(checkpoint.get("at", "")),
            "lifecycle_status": status,
        }
    derived = STATUS_ATTENTION.get(status, "none")
    basis = (
        f"malformed checkpoint; fell back to status {status or 'unknown'}"
        if malformed
        else f"no checkpoint; status {status or 'unknown'}"
    )
    return {
        "schema_version": ATTENTION_SCHEMA_VERSION,
        "state": derived,
        "rank": ATTENTION_RANK[derived],
        "source": "status",
        "basis": basis,
        "checkpoint_at": "",
        "lifecycle_status": status,
    }


def parse_checkpoint_at(value: str) -> datetime:
    if not value or "T" not in value:
        raise ValueError("checkpoint_at must be an RFC3339 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("checkpoint_at must include an offset or Z")
    return parsed


def checkpoint_errors(metadata: dict[str, str]) -> list[str]:
    if not any(metadata.get(field) for field in CHECKPOINT_FIELDS):
        return []
    errors: list[str] = []
    for field in CHECKPOINT_FIELDS:
        if not metadata.get(field):
            errors.append(f"missing frontmatter field '{field}'")
    if metadata.get("checkpoint_schema") != str(CHECKPOINT_SCHEMA_VERSION):
        errors.append(
            f"checkpoint_schema must be {CHECKPOINT_SCHEMA_VERSION}"
        )
    try:
        parse_checkpoint_at(metadata.get("checkpoint_at", ""))
    except (TypeError, ValueError):
        errors.append("checkpoint_at must be an RFC3339 timestamp with timezone")
    for field in ("checkpoint_node", "checkpoint_reporter"):
        value = metadata.get(field, "")
        if value and not SAFE_COMPONENT.fullmatch(value):
            errors.append(f"{field} is unsafe")
    state = metadata.get("checkpoint_state", "")
    need = metadata.get("checkpoint_need", "")
    if state and state not in CHECKPOINT_STATES:
        errors.append(f"checkpoint_state is unsupported: {state}")
    if need and need not in CHECKPOINT_NEEDS:
        errors.append(f"checkpoint_need is unsupported: {need}")
    allowed_needs = CHECKPOINT_STATE_NEEDS.get(state)
    if allowed_needs is not None and need and need not in allowed_needs:
        allowed = ", ".join(sorted(allowed_needs))
        errors.append(
            f"checkpoint_state {state} allows checkpoint_need: {allowed}"
        )
    next_action = metadata.get("checkpoint_next", "")
    if len(next_action) > 500:
        errors.append("checkpoint_next exceeds 500 characters")
    return errors


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def empty_origins(record: Record) -> dict:
    return {
        "schema_version": ORIGINS_SCHEMA_VERSION,
        "project": record.project.id,
        "session": record.session,
        "origins": [],
    }


def load_origins(record: Record) -> dict:
    if not record.origins_path.is_file():
        return empty_origins(record)
    try:
        payload = json.loads(record.origins_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultError(f"Invalid origins JSON at {record.origins_path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("origins"), list):
        raise VaultError(f"Origins file must contain an origins list: {record.origins_path}")
    if payload.get("schema_version") != ORIGINS_SCHEMA_VERSION:
        raise VaultError(
            f"Origins schema_version must be {ORIGINS_SCHEMA_VERSION}: {record.origins_path}"
        )
    if payload.get("project") != record.project.id or payload.get("session") != record.session:
        raise VaultError(f"Origins identity mismatch: {record.origins_path}")
    return payload


def origin_projection(record: Record) -> dict[str, object]:
    try:
        count = len(load_origins(record)["origins"])
    except VaultError:
        return {"native_origins": None, "origins_status": "invalid"}
    return {"native_origins": count, "origins_status": "valid"}


def save_origins(record: Record, payload: dict) -> None:
    atomic_write(record.origins_path, json.dumps(payload, indent=2) + "\n")
    set_frontmatter_field(record.path, "origins", record.origins_path.name)


def validate_uuid(value: str, label: str = "session id") -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise VaultError(f"Invalid Codex {label}: {value}") from exc


def origin_from_args(args: argparse.Namespace, default_node: str) -> dict | None:
    if getattr(args, "no_link_current", False):
        return None
    runtime = inline(getattr(args, "runtime", None) or "codex").casefold()
    node = inline(getattr(args, "node", None) or default_node)
    session_id = inline(getattr(args, "session_id", None) or "")
    session_name = inline(getattr(args, "session_name", None) or "")
    if not session_id and not session_name and runtime == "codex":
        session_id = inline(os.environ.get("CODEX_THREAD_ID", ""))
    if not session_id and not session_name:
        return None
    if not SAFE_COMPONENT.fullmatch(runtime):
        raise VaultError(f"Invalid runtime id: {runtime}")
    if not SAFE_COMPONENT.fullmatch(node):
        raise VaultError(f"Invalid node id: {node}")
    if runtime == "codex" and session_id:
        session_id = validate_uuid(session_id)
    if runtime == "codex" and session_name.startswith("-"):
        raise VaultError("Codex session names cannot begin with '-'")
    cwd = inline(getattr(args, "cwd", None) or os.getcwd())
    return {
        "runtime": runtime,
        "node": node,
        "session_id": session_id,
        "session_name": session_name,
        "cwd": cwd,
        "captured": date.today().isoformat(),
        "last_seen": date.today().isoformat(),
        "primary": bool(getattr(args, "primary", False)),
    }


def link_origin(record: Record, origin: dict, make_primary: bool = False) -> dict:
    payload = load_origins(record)
    origins = payload["origins"]
    key = native_origin_key(origin)
    if make_primary or not origins:
        for existing in origins:
            if isinstance(existing, dict):
                existing["primary"] = False
        origin["primary"] = True
    matched = None
    for existing in origins:
        if not isinstance(existing, dict):
            continue
        existing_key = native_origin_key(existing)
        if existing_key == key:
            matched = existing
            break
    if matched is None:
        origins.append(origin)
        matched = origin
    else:
        captured = matched.get("captured", origin["captured"])
        primary = matched.get("primary", False) or origin.get("primary", False)
        matched.update(origin)
        matched["captured"] = captured
        matched["primary"] = primary
    save_origins(record, payload)
    return matched


def native_origin_key(origin: dict) -> tuple[str, str, str, str]:
    """A name is a fallback identity, not another identity for the same ID."""
    if not isinstance(origin, dict):
        raise VaultError("Cannot verify ownership of a malformed native origin")
    session_id = str(origin.get("session_id") or "")
    return (
        str(origin.get("runtime") or ""), str(origin.get("node") or ""),
        "id" if session_id else "name",
        session_id or str(origin.get("session_name") or ""),
    )


def require_available_origin(identity: str, origin: dict, projects: list[Project]) -> None:
    """Reject a new cross-record link; existing links remain resumable.

    The normal CLI holds the registry write lock throughout this scan and the
    subsequent write. Direct native-creation adapters use link_origin only
    after creating a fresh native thread; this is not their mutation path.
    """
    all_records, errors = records(projects)
    if errors:
        raise VaultError("Cannot verify native origin ownership: " + "; ".join(errors))
    key = native_origin_key(origin)
    target = next((record for record in all_records if record.identity == identity), None)
    if target and any(native_origin_key(item) == key for item in load_origins(target)["origins"]):
        return
    owners = [
        record.identity for record in all_records
        if record.identity != identity
        and any(native_origin_key(item) == key for item in load_origins(record)["origins"])
    ]
    if owners:
        raise VaultError(
            f"Native origin {key[0]}@{key[1]} {key[2]}:{key[3]} is already linked to "
            f"{', '.join(sorted(owners))}; refusing a new resume link for {identity}. "
            "Use start --no-link-current for a portable capture, or review the "
            "existing ownership and use the explicit origins rehome migration."
        )


def codex_session_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    sessions_root = codex_home() / "sessions"
    if not sessions_root.is_dir():
        return None
    matches = sorted(sessions_root.rglob(f"*{session_id}.jsonl"))
    return matches[-1] if matches else None


def local_resume_origin(record: Record, node: str, runtime: str | None = None) -> dict:
    payload = load_origins(record)
    origins = [origin for origin in payload["origins"] if isinstance(origin, dict)]
    if runtime:
        origins = [origin for origin in origins if origin.get("runtime") == runtime]
    local = [origin for origin in origins if origin.get("node") == node]
    supported = [
        origin for origin in local
        if origin.get("runtime") in SUPPORTED_RESUME_RUNTIMES
        and (origin.get("session_id") or origin.get("session_name"))
    ]
    if not supported:
        if origins:
            locations = ", ".join(
                sorted({f"{item.get('runtime', '?')}@{item.get('node', '?')}" for item in origins})
            )
            raise VaultError(
                f"No locally resumable origin for {record.identity}; known origins: {locations}. "
                "Use the portable Session Vault record on this node."
            )
        raise VaultError(
            f"No native origin is linked to {record.identity}; use the portable Session Vault record."
        )
    supported.sort(
        key=lambda item: (
            bool(item.get("primary")),
            str(item.get("last_seen", item.get("captured", ""))),
        ),
        reverse=True,
    )
    return supported[0]


def records(projects: list[Project]) -> tuple[list[Record], list[str]]:
    result: list[Record] = []
    errors: list[str] = []
    for project in projects:
        if not project.sessions_dir.is_dir():
            continue
        paths = sorted(
            path
            for path in project.sessions_dir.glob("*.md")
            if not path.name.startswith("_")
        )
        for path in paths:
            try:
                result.append(parse_record(project, path))
            except (OSError, UnicodeError) as exc:
                errors.append(f"cannot read session record {path}: {exc}")
    return result, errors


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def meaningful_tokens(text: str) -> list[str]:
    raw = tokens(text)
    filtered = [
        token
        for token in raw
        if token not in STOP_WORDS
        and not (
            len(token) >= 5
            and any(
                SequenceMatcher(None, token, stop).ratio() >= 0.86
                for stop in STOP_WORDS
            )
        )
    ]
    return filtered or raw


def field_tokens(text: str) -> set[str]:
    return set(tokens(text))


def fuzzy_contains(term: str, candidates: set[str]) -> bool:
    if len(term) < 5:
        return False
    return any(
        abs(len(term) - len(candidate)) <= 2
        and SequenceMatcher(None, term, candidate).ratio() >= 0.84
        for candidate in candidates
    )


def score_record(record: Record, query: str) -> tuple[float, int]:
    terms = meaningful_tokens(query)
    normalized_query = " ".join(tokens(query))
    project_text = " ".join(record.project.lookup_names).casefold()
    fields = {
        "identity": record.identity.casefold(),
        "project": project_text,
        "session": record.session.casefold(),
        "title": record.title.casefold(),
        "memory": " ".join(
            record.metadata.get(key, "").casefold()
            for key in (
                "people", "aliases", "legacy_ids", "keywords", "sources",
                "related_projects",
            )
        ),
        "body": record.body.casefold(),
    }
    sets = {name: field_tokens(value) for name, value in fields.items()}
    normalized = {name: " ".join(tokens(value)) for name, value in fields.items()}
    score = 0.0
    matched = 0

    if normalized_query:
        if normalized_query == normalized["identity"]:
            score += 60
        elif normalized_query == normalized["session"]:
            score += 42
        elif normalized_query in normalized["title"]:
            score += 28
        elif normalized_query in normalized["memory"]:
            score += 20
        elif normalized_query in normalized["body"]:
            score += 10

    for term in terms:
        term_score = 0.0
        if term in sets["identity"] or term in normalized["identity"]:
            term_score = max(term_score, 20)
        if term in sets["project"]:
            term_score = max(term_score, 16)
        if term in sets["session"]:
            term_score = max(term_score, 16)
        if term in sets["title"]:
            term_score = max(term_score, 12)
        if term in sets["memory"]:
            term_score = max(term_score, 9)
        if term in sets["body"]:
            term_score = max(term_score, 4)
        if not term_score:
            for name, weight in (
                ("project", 7), ("session", 7), ("title", 5),
                ("memory", 4), ("body", 2),
            ):
                if fuzzy_contains(term, sets[name]):
                    term_score = weight
                    break
        if term_score:
            matched += 1
            score += term_score

    if not matched:
        return 0.0, 0
    coverage = matched / max(len(terms), 1)
    if len(terms) >= 3 and coverage < 0.60:
        return 0.0, 0
    score += 10 * coverage
    return score, matched


def summary(record: Record) -> str:
    match = re.search(r"(?ims)^## Summary\s*\n+(.*?)(?=\n## |\Z)", record.body)
    source = match.group(1) if match else record.body
    compact = re.sub(r"\s+", " ", source).strip()
    return compact[:260] + ("…" if len(compact) > 260 else "")


def project_matches(project: Project, requested: str) -> bool:
    wanted = requested.casefold()
    return any(name.casefold() == wanted for name in project.lookup_names)


def resolve_project(projects: list[Project], requested: str) -> Project:
    matches = [project for project in projects if project_matches(project, requested)]
    if not matches:
        raise VaultError(f"Project not found: {requested}")
    if len(matches) > 1:
        names = ", ".join(project.id for project in matches)
        raise VaultError(f"Ambiguous project '{requested}': {names}")
    return matches[0]


def identity_matches(record: Record, requested: str) -> bool:
    if "/" in requested:
        project_name, session_name = requested.split("/", 1)
        return (
            project_matches(record.project, project_name)
            and record.session.casefold() == session_name.casefold()
        )
    wanted = requested.casefold()
    return (
        record.session.casefold() == wanted
        or any(alias.casefold() == wanted for alias in record.aliases)
    )


def find_exact(all_records: list[Record], requested: str) -> Record:
    matches = [record for record in all_records if identity_matches(record, requested)]
    if not matches:
        raise VaultError(f"Session not found: {requested}")
    if len(matches) > 1:
        candidates = ", ".join(sorted(record.identity for record in matches))
        raise VaultError(
            f"Ambiguous session '{requested}'. Use project/session: {candidates}"
        )
    return matches[0]


def command_search(
    args: argparse.Namespace,
    projects: list[Project],
    all_records: list[Record],
) -> int:
    query = " ".join(args.query).strip()
    selected = all_records
    if args.project:
        project = resolve_project(projects, args.project)
        selected = [record for record in all_records if record.project.id == project.id]

    ranked = []
    for record in selected:
        score, matched = score_record(record, query)
        if score > 0:
            ranked.append((score, matched, record))
    ranked.sort(key=lambda item: (-item[0], item[2].identity.casefold()))
    ranked = ranked[: args.limit]

    if args.json:
        payload = [
            {
                "identity": record.identity,
                "project": record.project.id,
                "project_name": record.project.name,
                "session": record.session,
                "title": record.title,
                "date": record.metadata.get("date", ""),
                "updated": record.metadata.get("updated", ""),
                "status": record.metadata.get("status", ""),
                "checkpoint": checkpoint_payload(record),
                "attention": attention_projection(record),
                "score": round(score, 2),
                "matched_terms": matched,
                "summary": summary(record),
                **origin_projection(record),
                "path": str(record.path.resolve()),
            }
            for score, matched, record in ranked
        ]
        print(json.dumps(payload, indent=2))
    elif ranked:
        for score, _, record in ranked:
            date = record.metadata.get("updated") or record.metadata.get(
                "date", "unknown date"
            )
            status = record.metadata.get("status", "unknown status")
            print(f"{record.identity} — {record.title}  [score {score:.1f}]")
            print(f"  {record.project.name} · {date} · {status}")
            if summary(record):
                print(f"  {summary(record)}")
            print(f"  {record.path.resolve()}")
    else:
        print(f"No vault sessions matched: {query}")
        return 1
    return 0


def command_show(args: argparse.Namespace, all_records: list[Record]) -> int:
    record = find_exact(all_records, args.identity)
    print(record.path.read_text(encoding="utf-8"), end="")
    return 0


def command_list(args: argparse.Namespace, projects: list[Project], all_records: list[Record]) -> int:
    selected = all_records
    if args.project:
        project = resolve_project(projects, args.project)
        selected = [record for record in all_records if record.project.id == project.id]
    ordered = sorted(
        selected,
        key=lambda record: (
            record.metadata.get("updated", record.metadata.get("date", "")),
            record.identity.casefold(),
        ),
        reverse=True,
    )
    if args.json:
        print(json.dumps([
            {
                "identity": record.identity,
                "project": record.project.id,
                "session": record.session,
                "title": record.title,
                "date": record.metadata.get("date", ""),
                "updated": record.metadata.get("updated", ""),
                "status": record.metadata.get("status", ""),
                "checkpoint": checkpoint_payload(record),
                "attention": attention_projection(record),
                **origin_projection(record),
                "path": str(record.path.resolve()),
            }
            for record in ordered
        ], indent=2))
    elif not ordered:
        print("The Session Vault is empty.")
    else:
        for record in ordered:
            stamp = record.metadata.get("updated") or record.metadata.get(
                "date", "unknown"
            )
            print(
                f"{record.identity}\t{stamp}\t"
                f"{record.metadata.get('status', '')}\t{record.title}"
            )
    return 0


def command_doctor(
    registry_path: Path,
    node: str,
    projects: list[Project],
    all_records: list[Record],
    record_load_errors: list[str],
) -> int:
    errors: list[str] = list(record_load_errors)
    warnings: list[str] = []
    notices: list[str] = []
    seen_projects: dict[str, Path] = {}
    seen_roots: dict[str, str] = {}
    native_origin_records: dict[tuple[str, str], set[str]] = {}
    storage_bytes = 0
    storage_files = 0
    largest_record_bytes = 0
    largest_record_identity = ""

    for project in projects:
        if not SAFE_COMPONENT.fullmatch(project.id):
            errors.append(f"unsafe project id '{project.id}'")
        folded = project.id.casefold()
        if folded in seen_projects:
            errors.append(f"duplicate project id '{project.id}'")
        seen_projects[folded] = project.root
        root_key = str(project.root.resolve()) if project.root.exists() else str(project.root)
        if root_key in seen_roots:
            errors.append(
                f"projects '{seen_roots[root_key]}' and '{project.id}' share root {project.root}"
            )
        seen_roots[root_key] = project.id
        if not project.root.is_dir():
            errors.append(f"project root missing: {project.id} -> {project.root}")
            continue
        manifest_path = project.root / "project.json"
        if not manifest_path.is_file():
            errors.append(f"project manifest missing: {manifest_path}")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("id") != project.id:
                    errors.append(
                        f"manifest id mismatch: registry={project.id}, "
                        f"manifest={manifest.get('id')} at {manifest_path}"
                    )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid project manifest {manifest_path}: {exc}")
        if not project.sessions_dir.is_dir():
            errors.append(f"sessions directory missing: {project.sessions_dir}")
            continue
        for origins_path in sorted(project.sessions_dir.glob("*.origins.json")):
            record_name = origins_path.name[:-len(".origins.json")] + ".md"
            if not origins_path.with_name(record_name).is_file():
                errors.append(f"orphan origins file has no session record: {origins_path}")

    seen_records: dict[str, Path] = {}
    portable_only = 0
    for record in all_records:
        try:
            record_bytes = record.path.stat().st_size
        except OSError as exc:
            errors.append(f"cannot stat session record {record.path}: {exc}")
            record_bytes = 0
        storage_bytes += record_bytes
        storage_files += 1
        if record_bytes > largest_record_bytes:
            largest_record_bytes = record_bytes
            largest_record_identity = record.identity
        if record_bytes >= RECORD_SIZE_NOTICE_BYTES:
            notices.append(
                f"large record {record.identity}: {record_bytes} bytes; distill before it grows further"
            )
        history_count = len(re.findall(
            r"(?m)^### .* — .* — .*/.*$", record.body
        ))
        if history_count >= CHECKPOINT_HISTORY_NOTICE_COUNT:
            notices.append(
                f"{record.identity} has {history_count} archived checkpoints; "
                "consider distilling before the history becomes noisy"
            )
        if SCAFFOLD_TEXT in record.body:
            notices.append(
                f"scaffold context has not been distilled: {record.identity}"
            )
        for field in REQUIRED_FIELDS:
            if not record.metadata.get(field):
                errors.append(f"{record.path}: missing frontmatter field '{field}'")
        status = record.metadata.get("status", "")
        if status and status not in SESSION_STATUSES:
            errors.append(f"{record.path}: unsupported status '{status}'")
        for field in ("date", "updated"):
            value = record.metadata.get(field, "")
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    errors.append(f"{record.path}: {field} must be an ISO date")
        if record.metadata.get("project") != record.project.id:
            errors.append(
                f"{record.path}: project must be '{record.project.id}'"
            )
        if not SAFE_COMPONENT.fullmatch(record.session):
            errors.append(f"{record.path}: unsafe session '{record.session}'")
        if record.path.stem != record.session:
            errors.append(f"{record.path}: filename must be {record.session}.md")
        folded = record.identity.casefold()
        if folded in seen_records:
            errors.append(
                f"duplicate identity '{record.identity}': "
                f"{seen_records[folded]} and {record.path}"
            )
        seen_records[folded] = record.path
        for error in checkpoint_errors(record.metadata):
            errors.append(f"{record.path}: {error}")
        if checkpoint_payload(record) is not None and not re.search(
            r"(?m)^## Current checkpoint\s*$", record.body
        ):
            errors.append(
                f"{record.path}: checkpoint fields require a Current checkpoint section"
            )
        declared_origins = record.metadata.get("origins", "")
        if declared_origins and declared_origins != record.origins_path.name:
            errors.append(
                f"{record.path}: origins must be sibling {record.origins_path.name}"
            )
        if declared_origins and not record.origins_path.is_file():
            errors.append(f"declared origins file missing: {record.origins_path}")
        if record.origins_path.is_file() and not declared_origins:
            errors.append(f"record does not declare origins file: {record.path}")
        if record.origins_path.is_file():
            try:
                storage_bytes += record.origins_path.stat().st_size
                storage_files += 1
            except OSError as exc:
                errors.append(f"cannot stat origins file {record.origins_path}: {exc}")
        try:
            origin_payload = load_origins(record)
        except VaultError as exc:
            errors.append(str(exc))
            continue
        origins = origin_payload["origins"]
        if origin_payload.get("schema_version") != ORIGINS_SCHEMA_VERSION:
            errors.append(
                f"{record.origins_path}: schema_version must be {ORIGINS_SCHEMA_VERSION}"
            )
        if not origins:
            portable_only += 1
        primary_count = 0
        seen_origin_keys: set[tuple[str, str]] = set()
        for origin in origins:
            if not isinstance(origin, dict):
                errors.append(f"{record.origins_path}: origins must be objects")
                continue
            runtime = str(origin.get("runtime", ""))
            origin_node = str(origin.get("node", ""))
            session_id = str(origin.get("session_id", ""))
            session_name = str(origin.get("session_name", ""))
            origin_key = (
                runtime,
                f"id:{session_id}" if session_id
                else f"name:{origin_node}:{session_name}",
            )
            if origin_key in seen_origin_keys:
                errors.append(
                    f"{record.origins_path}: duplicate native origin "
                    f"{runtime}@{origin_node} {session_id or session_name}"
                )
            seen_origin_keys.add(origin_key)
            if session_id:
                shared_key = (runtime, f"id:{session_id}")
                native_origin_records.setdefault(shared_key, set()).add(record.identity)
            elif session_name:
                shared_key = (runtime, f"name:{origin_node}:{session_name}")
                native_origin_records.setdefault(shared_key, set()).add(record.identity)
            if not runtime or not SAFE_COMPONENT.fullmatch(runtime):
                errors.append(f"{record.origins_path}: invalid runtime '{runtime}'")
            if not origin_node or not SAFE_COMPONENT.fullmatch(origin_node):
                errors.append(f"{record.origins_path}: invalid node '{origin_node}'")
            if not session_id and not session_name:
                errors.append(f"{record.origins_path}: origin requires session_id or session_name")
            if runtime == "codex" and session_id:
                try:
                    validate_uuid(session_id)
                except VaultError as exc:
                    errors.append(f"{record.origins_path}: {exc}")
            if runtime == "codex" and session_name and (
                session_name.startswith("-")
                or any(character in session_name for character in "\r\n\0")
            ):
                errors.append(f"{record.origins_path}: unsafe Codex session name")
            if origin.get("primary") is True:
                primary_count += 1
            if runtime == "codex" and origin_node == node and session_id:
                if codex_session_path(session_id) is None:
                    warnings.append(
                        f"native Codex session not found locally: {record.identity} -> {session_id}"
                    )
        if primary_count > 1:
            errors.append(f"{record.origins_path}: more than one primary origin")

    for (runtime, native_key), identities in sorted(native_origin_records.items()):
        if len(identities) < 2:
            continue
        label = native_key.removeprefix("id:").removeprefix("name:")
        notices.append(
            f"native origin reused by {len(identities)} record(s): "
            f"{runtime} {label} -> {', '.join(sorted(identities))}"
        )

    legacy_dir = codex_home() / "session-vault" / "sessions"
    if legacy_dir.is_dir() and list(legacy_dir.glob("*.md")):
        warnings.append(
            f"legacy prototype preserved but not indexed: {legacy_dir.resolve()}"
        )

    if errors:
        print("Session Vault validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        for notice in notices:
            print(f"Notice: {notice}", file=sys.stderr)
        return 1
    print(
        f"Session Vault OK: {len(all_records)} record(s), "
        f"{len(projects)} project(s), registry {registry_path.resolve()}"
    )
    print(
        f"Storage: {storage_bytes} byte(s) in {storage_files} file(s); "
        f"largest record {largest_record_bytes} byte(s): {largest_record_identity or 'none'}"
    )
    for warning in warnings:
        print(f"Warning: {warning}")
    for notice in notices:
        print(f"Notice: {notice}")
    if portable_only:
        print(f"Notice: {portable_only} record(s) are portable-only with no native origin")
    return 0


def command_path(args: argparse.Namespace, all_records: list[Record]) -> int:
    record = find_exact(all_records, args.identity)
    print(record.path.resolve())
    return 0


def command_projects(args: argparse.Namespace, projects: list[Project]) -> int:
    if args.json:
        print(json.dumps([
            {
                "id": project.id,
                "name": project.name,
                "root": str(project.root.resolve()),
                "aliases": list(project.aliases),
            }
            for project in projects
        ], indent=2))
    else:
        for project in projects:
            print(f"{project.id}\t{project.name}\t{project.root.resolve()}")
    return 0


def inline(value: str) -> str:
    """Return one frontmatter-safe line without inventing YAML semantics."""
    return re.sub(r"\s+", " ", value).strip()


def command_start(args: argparse.Namespace, projects: list[Project], node: str) -> int:
    if args.identity.count("/") != 1:
        raise VaultError("Start requires composite identity project/session")
    requested_project, session = args.identity.split("/", 1)
    project = resolve_project(projects, requested_project)
    if not SAFE_COMPONENT.fullmatch(session):
        raise VaultError(
            "Session names must use only letters, digits, dots, underscores, or hyphens"
        )
    if not project.sessions_dir.is_dir():
        raise VaultError(f"Sessions directory missing: {project.sessions_dir}")

    path = project.sessions_dir / f"{session}.md"
    identity = f"{project.id}/{session}"
    origin = origin_from_args(args, node)
    if origin:
        require_available_origin(identity, origin, projects)
    if path.exists():
        record = parse_record(project, path)
        linked = link_origin(record, origin, args.primary) if origin else None
        payload = {
            "identity": identity,
            "created": False,
            "linked_origin": linked,
            "path": str(path.resolve()),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Existing session: {identity}")
            print(path.resolve())
        return 0

    title = inline(args.title or session.replace("-", " ").replace("_", " ").title())
    summary_text = inline(
        args.summary
        or "Session initialized; durable re-entry context has not yet been captured."
    )
    keywords = inline(args.keywords or f"{project.id}, {session}, {title}")
    if not title:
        raise VaultError("Session title cannot be empty")
    if not keywords:
        raise VaultError("Session keywords cannot be empty")
    stamp = date.today().isoformat()
    content = (
        "---\n"
        f"project: {project.id}\n"
        f"session: {session}\n"
        f"title: {title}\n"
        f"date: {stamp}\n"
        f"updated: {stamp}\n"
        f"status: {args.status}\n"
        f"keywords: {keywords}\n"
        "---\n\n"
        f"# {identity} — {title}\n\n"
        "## Summary\n\n"
        f"{summary_text}\n\n"
        "## Open loops\n\n"
        "- Capture decisions, references, and the next useful re-entry point.\n"
    )
    atomic_write(path, content)
    record = parse_record(project, path)
    linked = link_origin(record, origin, args.primary) if origin else None
    payload = {
        "identity": identity,
        "created": True,
        "linked_origin": linked,
        "path": str(path.resolve()),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Started session: {identity}")
        print(path.resolve())
    return 0


def command_checkpoint(
    args: argparse.Namespace,
    node: str,
    all_records: list[Record],
) -> int:
    if args.identity.count("/") != 1:
        raise VaultError("Checkpoint requires composite identity project/session")
    record = find_exact(all_records, args.identity)
    reporter = inline(args.reporter)
    next_action = inline(args.next_action)
    if not reporter or not SAFE_COMPONENT.fullmatch(reporter):
        raise VaultError("Checkpoint reporter must be a stable safe principal")
    if not next_action:
        raise VaultError("Checkpoint next action cannot be empty")
    with record_lock(record.path):
        fresh_record = parse_record(record.project, record.path)
        current = checkpoint_payload(fresh_record)
        current_at = str(current.get("at", "")) if current else ""
        if args.if_checkpoint_at is not None and args.if_checkpoint_at != current_at:
            raise VaultError(
                f"Checkpoint changed for {record.identity}; expected "
                f"{args.if_checkpoint_at!r}, found {current_at!r}"
            )
        try:
            parsed_at = parse_checkpoint_at(args.checkpoint_at or utc_now())
            current_datetime = parse_checkpoint_at(current_at) if current_at else None
        except (TypeError, ValueError) as exc:
            raise VaultError(str(exc)) from exc
        if current_datetime is not None and parsed_at <= current_datetime:
            if args.checkpoint_at:
                raise VaultError(
                    f"checkpoint_at must be newer than current checkpoint {current_at}"
                )
            parsed_at = current_datetime + timedelta(microseconds=1)
        checkpoint_at = (
            parsed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        fields = {
            "updated": date.today().isoformat(),
            "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_at": checkpoint_at,
            "checkpoint_node": node,
            "checkpoint_reporter": reporter,
            "checkpoint_state": args.state,
            "checkpoint_need": args.need,
            "checkpoint_next": next_action,
        }
        merged = dict(fresh_record.metadata)
        merged.update({key: str(value) for key, value in fields.items()})
        errors = checkpoint_errors(merged)
        if errors:
            raise VaultError("; ".join(errors))

        lines = [
            f"- At: `{checkpoint_at}`",
            f"- Reporter: `{reporter}` on `{node}`",
            f"- State: `{args.state}`",
            f"- Current need: `{args.need}`",
            f"- Next action: {next_action}",
        ]
        note = inline(args.note or "")
        if note:
            lines.extend(("", f"Note: {note}"))
        evidence = [inline(value) for value in args.evidence if inline(value)]
        if evidence:
            lines.extend(("", "Evidence:"))
            lines.extend(f"- {value}" for value in evidence)
        update_record(
            record.path,
            fields,
            section_title="Current checkpoint",
            section_content="\n".join(lines),
            checkpoint_history=current,
        )
    result = {
        "identity": record.identity,
        "checkpoint": {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "at": checkpoint_at,
            "node": node,
            "reporter": reporter,
            "state": args.state,
            "need": args.need,
            "next": next_action,
        },
        "record_path": str(record.path.resolve()),
    }
    # An optional owner-configured source packet, inside this existing turn.
    # Unchanged evidence is suppressed; a failed reader cannot block a checkpoint.
    try:
        from ux46_work_checkpoint import packet
        pending = packet(record.identity, reporter)
    except Exception:
        pending = None
    if pending:
        result['room_review'] = pending
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Checkpointed {record.identity}: {args.state} / {args.need}")
        print(record.path.resolve())
        if pending:
            print("Changed suggestions for this room: " + json.dumps(pending, ensure_ascii=False))
    return 0


def split_record_text(path: Path) -> tuple[str, str]:
    """Return the verbatim frontmatter block and body, or refuse the record."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise VaultError(f"Session record has no frontmatter: {path}")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise VaultError(f"Session record has malformed frontmatter: {path}")
    return "---\n" + parts[1] + "---\n", parts[2]


def next_archive_version(directory: Path, session: str) -> int:
    """The next free version, derived only from what is already on disk."""
    highest = 0
    if directory.is_dir():
        for path in directory.glob(f"{session}.checkpoint-history.*.md"):
            match = HISTORY_ARCHIVE_NAME.fullmatch(path.name)
            if match and match.group("session") == session:
                highest = max(highest, int(match.group("version")))
    return highest + 1


def history_archive_text(
    record: Record,
    node: str,
    version: int,
    entries: int,
    source_digest: str,
    section: str,
) -> str:
    """Wrap the verbatim history section in provenance frontmatter."""
    return (
        "---\n"
        f"kind: {HISTORY_ARCHIVE_KIND}\n"
        f"archive_schema: {HISTORY_ARCHIVE_SCHEMA_VERSION}\n"
        f"archive_version: {version}\n"
        f"archive_of: {record.identity}\n"
        f"archive_project: {record.project.id}\n"
        f"archive_session: {record.session}\n"
        f"archived_at: {utc_now()}\n"
        f"archived_by_node: {node}\n"
        f"archived_from: {record.path.name}\n"
        f"source_sha256: {source_digest}\n"
        f"entries: {entries}\n"
        "---\n\n"
        f"# {record.identity} — archived checkpoint history {version}\n\n"
        f"Moved verbatim from `{record.path.name}`. {HISTORY_ARCHIVE_AUTHORITY_NOTE}\n\n"
        f"{section.rstrip()}\n"
    )


def command_archive_history(
    args: argparse.Namespace,
    node: str,
    all_records: list[Record],
) -> int:
    """Move the archived Checkpoint history out of one top-level record.

    Only that section moves. The current checkpoint, the frontmatter, and every
    other section stay byte-identical, the archive is written before the record
    is rewritten so history is never briefly absent, and an existing archive is
    never overwritten.
    """

    if args.identity.count("/") != 1:
        raise VaultError(
            "Archive requires composite identity project/session"
        )
    record = find_exact(all_records, args.identity)
    sessions_dir = record.project.sessions_dir
    if record.path.parent.resolve() != sessions_dir.resolve():
        raise VaultError(
            f"Only a top-level session record can be archived: {record.path}"
        )
    if not SAFE_COMPONENT.fullmatch(record.session):
        raise VaultError(f"Unsafe session name: {record.session!r}")

    with record_lock(record.path):
        fresh = parse_record(record.project, record.path)
        head, body = split_record_text(record.path)
        errors = checkpoint_errors(fresh.metadata)
        if errors:
            raise VaultError(
                "Refusing to archive a record with a malformed checkpoint: "
                + "; ".join(errors)
            )
        if checkpoint_payload(fresh) is not None and not re.search(
            r"(?m)^## Current checkpoint\s*$", body
        ):
            raise VaultError(
                f"Refusing to archive: checkpoint fields have no Current "
                f"checkpoint section: {record.path}"
            )
        matches = list(CHECKPOINT_HISTORY_PATTERN.finditer(body))
        if not matches:
            raise VaultError(
                f"No '## {CHECKPOINT_HISTORY_TITLE}' section to archive: "
                f"{record.identity}"
            )
        if len(matches) > 1:
            raise VaultError(
                f"Refusing to archive: {len(matches)} '## "
                f"{CHECKPOINT_HISTORY_TITLE}' sections in {record.path}"
            )
        match = matches[0]
        section = match.group(0)
        entries = len(CHECKPOINT_HISTORY_ENTRY.findall(section))
        if entries == 0:
            raise VaultError(
                f"Refusing to archive an empty checkpoint history: "
                f"{record.identity}"
            )

        archive_dir = sessions_dir / HISTORY_ARCHIVE_DIRNAME
        if archive_dir.exists() and not archive_dir.is_dir():
            raise VaultError(f"History archive path is not a directory: {archive_dir}")
        version = next_archive_version(archive_dir, record.session)
        if version > HISTORY_ARCHIVE_MAX_VERSION:
            raise VaultError(
                f"Checkpoint history archive versions are exhausted for "
                f"{record.identity}; distil the existing archives first"
            )
        archive_path = archive_dir / (
            f"{record.session}.checkpoint-history.{version:03d}.md"
        )
        if archive_path.exists():
            raise VaultError(f"Refusing to overwrite an archive: {archive_path}")

        source_digest = hashlib.sha256(
            record.path.read_bytes()
        ).hexdigest()
        remainder = body[: match.start()] + body[match.end():]
        if match.end() >= len(body):
            remainder = remainder.rstrip() + "\n"
        archive_text = history_archive_text(
            fresh, node, version, entries, source_digest, section
        )

        payload = {
            "identity": record.identity,
            "action": "archive-history",
            "dry_run": bool(args.dry_run),
            "record_path": str(record.path.resolve()),
            "archive_path": str(archive_path),
            "archive_version": version,
            "entries": entries,
            "archived_bytes": len(section.encode("utf-8")),
            "source_sha256": source_digest,
            "authority": HISTORY_ARCHIVE_AUTHORITY_NOTE,
        }
        if args.dry_run:
            payload["archived"] = False
            payload["current_checkpoint_preserved"] = checkpoint_payload(fresh) is not None
        else:
            # Archive first: a crash between the two writes duplicates the
            # history, it never loses it.
            atomic_write(archive_path, archive_text)
            atomic_write(record.path, head + remainder)
            after = parse_record(record.project, record.path)
            payload["archived"] = True
            payload["archive_path"] = str(archive_path.resolve())
            payload["archive_sha256"] = hashlib.sha256(
                archive_path.read_bytes()
            ).hexdigest()
            payload["current_checkpoint_preserved"] = (
                checkpoint_payload(after) == checkpoint_payload(fresh)
            )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        verb = "Would archive" if args.dry_run else "Archived"
        print(
            f"{verb} {entries} checkpoint entry(ies) from {record.identity} "
            f"as version {version}"
        )
        print(payload["archive_path"])
    return 0


def command_link(
    args: argparse.Namespace,
    node: str,
    all_records: list[Record],
    projects: list[Project],
) -> int:
    record = find_exact(all_records, args.identity)
    origin = origin_from_args(args, node)
    if origin is None:
        raise VaultError(
            "No native session supplied. Pass --session-id/--session-name or run inside Codex."
        )
    require_available_origin(record.identity, origin, projects)
    linked = link_origin(record, origin, args.primary)
    payload = {
        "identity": record.identity,
        "origin": linked,
        "origins_path": str(record.origins_path.resolve()),
        "record_path": str(record.path.resolve()),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        target = linked.get("session_id") or linked.get("session_name")
        print(
            f"Linked {record.identity} to "
            f"{linked.get('runtime')}@{linked.get('node')}:{target}"
        )
        print(record.origins_path.resolve())
    return 0


def command_resume(
    args: argparse.Namespace,
    node: str,
    all_records: list[Record],
) -> int:
    record = find_exact(all_records, args.identity)
    runtime = args.runtime.casefold() if args.runtime else None
    origin = local_resume_origin(record, node, runtime)
    target = str(origin.get("session_id") or origin.get("session_name"))
    if origin.get("runtime") != "codex":
        raise VaultError(f"No resume adapter for runtime: {origin.get('runtime')}")
    if origin.get("session_id"):
        target = validate_uuid(target)
    elif target.startswith("-") or any(character in target for character in "\r\n\0"):
        raise VaultError("Unsafe Codex session name in origins record")
    executable = shutil.which("codex")
    if executable is None:
        raise VaultError("Codex executable not found on PATH")
    argv = [executable, "resume", target]
    transcript = codex_session_path(str(origin.get("session_id", "")))
    payload = {
        "identity": record.identity,
        "mode": "exact-native-session",
        "runtime": origin.get("runtime"),
        "node": origin.get("node"),
        "session_id": origin.get("session_id", ""),
        "session_name": origin.get("session_name", ""),
        "saved_cwd": origin.get("cwd", ""),
        "transcript_managed_by": "codex",
        "transcript_available": transcript is not None,
        "command": argv,
        "record_path": str(record.path.resolve()),
        "origins_path": str(record.origins_path.resolve()),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if args.print_command:
        print(shlex.join(argv))
        return 0
    try:
        os.execv(executable, argv)
    except OSError as exc:
        raise VaultError(f"Failed to launch native Codex session: {exc}") from exc
    return 0


def environment_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def safe_token(value: str, label: str) -> str:
    token = inline(value)
    if not SAFE_COMPONENT.fullmatch(token):
        raise VaultError(f"Invalid {label}: {value!r}")
    return token


def tell_root_path(explicit: str | Path | None) -> Path:
    configured = str(explicit) if explicit else environment_value(
        "SESSION_VAULT_TELL_ROOT", "TELL_ROOT"
    )
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "telld"


def resolve_tell_client(explicit: str | Path | None) -> tuple[Path, Path]:
    """Locate the local Tell client without trusting PATH or a shell.

    The installed wrapper is not guaranteed on every node, so the repository
    checkout is the contract: a Tell root is a directory holding
    ``src/tell/cli.py``. An explicit file path is also accepted so a node with
    an unusual layout can point straight at the client.
    """

    root = tell_root_path(explicit)
    if root.is_file():
        return root.parent, root
    if not root.is_dir():
        raise VaultError(
            f"Tell root not found: {root}. Pass --tell-root or set "
            "SESSION_VAULT_TELL_ROOT to a telld checkout."
        )
    for parts in TELL_CLIENT_CANDIDATES:
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return root, candidate
    raise VaultError(f"Tell client not found under {root} (expected src/tell/cli.py)")


def tell_principal(explicit: str | None) -> str | None:
    value = explicit or environment_value(
        "SESSION_VAULT_TELL_PRINCIPAL", "TELL_PRINCIPAL"
    )
    if not value:
        return None
    return safe_token(value, "Tell principal")


def tell_collective(explicit: str | None) -> str:
    value = (
        explicit
        or environment_value("SESSION_VAULT_TELL_COLLECTIVE")
        or TELL_DEFAULT_COLLECTIVE
    )
    return safe_token(value, "Tell collective")


def tell_session_id(value: str) -> str:
    token = inline(value)
    if not TELL_SESSION_ID.fullmatch(token):
        raise VaultError(f"Invalid Tell session id: {value!r}")
    return token


def tell_ttl(value: int | None) -> int:
    ttl = TELL_DEFAULT_TTL if value is None else int(value)
    if not 1 <= ttl <= TELL_MAX_TTL:
        raise VaultError(f"Tell TTL must be between 1 and {TELL_MAX_TTL} seconds: {ttl}")
    return ttl


def tell_label(value: str | None, fallback: str) -> str:
    label = inline(value or fallback)
    if not label or label.startswith("-"):
        raise VaultError(f"Invalid Tell label: {value!r}")
    if len(label) > TELL_LABEL_MAX_CHARS:
        label = label[:TELL_LABEL_MAX_CHARS].rstrip()
    return label


def tell_wake(raw: str | None, wake_file: Path | None) -> tuple[str | None, str]:
    """Normalise a receiver-owned wake spec into compact JSON plus its kind.

    Only the kind is ever reported back. The spec itself may name local paths
    or argv and has no business being echoed into a transcript.
    """

    if wake_file is not None:
        if raw:
            raise VaultError("Use either --wake or --wake-file, not both")
        try:
            raw = wake_file.expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise VaultError(f"Cannot read wake spec {wake_file}: {exc}") from exc
    if not raw or not raw.strip():
        return None, "none"
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VaultError(f"Wake spec must be JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise VaultError("Wake spec must be a JSON object")
    kind = spec.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise VaultError("Wake spec must carry a non-empty string 'kind'")
    return json.dumps(spec, separators=(",", ":"), sort_keys=True), kind.strip()


def run_tell(
    client: Path,
    principal: str | None,
    arguments: list[str],
    *,
    tolerated_returncodes: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Invoke the Tell client as a plain argv vector. No shell, ever."""

    argv = [sys.executable, str(client)]
    if principal:
        argv += ["--as", principal]
    argv += arguments
    for item in argv:
        if any(character in item for character in "\r\n\0"):
            raise VaultError("Refusing to pass a control character to the Tell client")
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=TELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VaultError(
            f"Tell client timed out after {TELL_TIMEOUT_SECONDS}s: {' '.join(arguments)}"
        ) from exc
    except OSError as exc:
        raise VaultError(f"Cannot run the Tell client {client}: {exc}") from exc
    if completed.returncode != 0 and completed.returncode not in tolerated_returncodes:
        detail = inline(completed.stderr or completed.stdout)[:400] or "no output"
        raise VaultError(
            f"tell {' '.join(arguments)} failed (exit {completed.returncode}): {detail}"
        )
    return completed


def tell_receipt(completed: subprocess.CompletedProcess[str], context: str) -> dict:
    text = (completed.stdout or "").strip()
    if not text:
        raise VaultError(f"Tell returned no {context} receipt")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VaultError(
            f"Tell {context} receipt was not JSON: {inline(text)[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        raise VaultError(f"Tell {context} receipt must be a JSON object")
    return payload


def route_state_dir() -> Path:
    configured = environment_value("SESSION_VAULT_ROUTE_STATE")
    if configured:
        return Path(configured).expanduser()
    base = environment_value("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "session-vault" / "routes"


def route_state_path(identity: str, principal: str | None) -> Path:
    project, _, session = identity.partition("/")
    owner = principal or "default"
    return route_state_dir() / f"{project}__{session}__{owner}.json"


def read_route_cache_file(path: Path) -> dict | None:
    """Parse one local route cache file, or None when it is not one."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != ROUTE_STATE_KIND:
        return None
    return payload


def load_route_state(identity: str, principal: str | None) -> dict:
    path = route_state_path(identity, principal)
    if not path.is_file():
        return {}
    payload = read_route_cache_file(path)
    if payload is None or payload.get("identity") != identity:
        return {}
    return payload


def save_route_state(identity: str, principal: str | None, payload: dict) -> Path:
    path = route_state_path(identity, principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def clear_route_state(identity: str, principal: str | None) -> None:
    try:
        route_state_path(identity, principal).unlink()
    except OSError:
        pass


def adopt_route_from_registry(
    client: Path, principal: str | None, identity: str, collective: str | None
) -> str | None:
    """Rebuild a lost session id from the Tell registry, which is the truth.

    The local runtime file is a cache. When it is missing the registry listing
    is asked instead, so a cleared cache never becomes a second, competing
    source of routing state.
    """

    completed = run_tell(client, principal, ["session", "list"], tolerated_returncodes=(2,))
    text = (completed.stdout or "").strip()
    if not text:
        return None
    try:
        sessions = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(sessions, list):
        return None
    matches = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("vault", "")) != identity:
            continue
        listed = entry.get("collective")
        if collective and listed and str(listed) != collective:
            continue
        session_id = entry.get("session_id")
        if isinstance(session_id, str) and TELL_SESSION_ID.fullmatch(session_id):
            matches.append(entry)
    if len(matches) != 1:
        return None
    return str(matches[0]["session_id"])


def tell_registry_sessions(client: Path, principal: str | None) -> list[dict] | None:
    """Read the Tell session registry. Read-only: it never mutates Tell state."""
    completed = run_tell(
        client, principal, ["session", "list"], tolerated_returncodes=(2,)
    )
    text = (completed.stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [entry for entry in payload if isinstance(entry, dict)]


def resolve_route_session(
    client: Path,
    principal: str | None,
    identity: str,
    explicit: str | None,
    collective: str | None,
    *,
    adopt: bool,
) -> tuple[str | None, str]:
    if explicit:
        return tell_session_id(explicit), "argument"
    from_environment = environment_value("TELL_SESSION_ID")
    if from_environment:
        return tell_session_id(from_environment), "environment"
    cached = load_route_state(identity, principal).get("session_id")
    if isinstance(cached, str) and TELL_SESSION_ID.fullmatch(cached):
        return cached, "runtime-state"
    if adopt:
        adopted = adopt_route_from_registry(client, principal, identity, collective)
        if adopted:
            return adopted, "tell-registry"
    return None, "unresolved"


def emit_route(payload: dict, as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(summary)


def command_route_prune(args: argparse.Namespace) -> int:
    """Drop local route cache entries the Tell registry proves are gone.

    The cache is rebuildable; the Tell session registry is the truth. So the
    registry is read once, read-only, and nothing is removed unless that
    reading proves the entry absent or stale. If the registry cannot be read,
    nothing is removed at all: an unreadable truth is not an absence.
    """

    root, client = resolve_tell_client(getattr(args, "tell_root", None))
    principal = tell_principal(getattr(args, "principal", None))
    owner = principal or ""
    directory = route_state_dir()
    base = {
        "action": "prune",
        "dry_run": bool(args.dry_run),
        "principal": owner,
        "state_dir": str(directory),
        "tell_root": str(root),
        "tell_client": str(client),
        "tell_mutated": False,
        "authority": ROUTE_AUTHORITY_NOTE,
    }
    sessions = tell_registry_sessions(client, principal)
    if sessions is None:
        payload = {
            **base,
            "state": "unknown",
            "reason": "tell-registry-unreadable",
            "registry_sessions": 0,
            "examined": 0,
            "pruned": [],
            "kept": [],
            "skipped": [],
        }
        emit_route(
            payload, args.json,
            "Tell registry truth is unavailable; removed nothing",
        )
        return 2

    live: dict[str, dict] = {}
    for entry in sessions:
        session_id = entry.get("session_id")
        if isinstance(session_id, str) and TELL_SESSION_ID.fullmatch(session_id):
            live[session_id] = entry

    pruned: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    for path in paths:
        payload = read_route_cache_file(path)
        if payload is None:
            skipped.append({"path": str(path), "reason": "not-a-route-cache-file"})
            continue
        identity = str(payload.get("identity", ""))
        session_id = str(payload.get("session_id", ""))
        item = {"path": str(path), "identity": identity, "session_id": session_id}
        if payload.get("rebuildable") is not True:
            skipped.append({**item, "reason": "not-declared-rebuildable"})
            continue
        if str(payload.get("principal", "")) != owner:
            skipped.append({**item, "reason": "owned-by-another-principal"})
            continue
        if not TELL_SESSION_ID.fullmatch(session_id):
            skipped.append({**item, "reason": "no-usable-session-id"})
            continue
        entry = live.get(session_id)
        if entry is None:
            reason = "absent-from-tell-registry"
        elif identity and str(entry.get("vault", identity)) != identity:
            reason = "stale-identity-mismatch"
        elif isinstance(entry.get("state"), str) and entry["state"] != "live":
            reason = f"stale-registry-state-{entry['state']}"
        else:
            kept.append({**item, "reason": "present-in-tell-registry"})
            continue
        removal = {**item, "reason": reason, "removed": False}
        if not args.dry_run:
            try:
                path.unlink()
                removal["removed"] = True
            except OSError as exc:
                removal["error"] = str(exc)
        pruned.append(removal)

    payload = {
        **base,
        "state": "pruned",
        "registry_sessions": len(live),
        "examined": len(paths),
        "pruned": pruned,
        "kept": kept,
        "skipped": skipped,
    }
    verb = "would remove" if args.dry_run else "removed"
    emit_route(
        payload, args.json,
        f"route cache: {verb} {len(pruned)}, kept {len(kept)}, "
        f"skipped {len(skipped)} of {len(paths)} entry(ies)",
    )
    return 0


def command_route(args: argparse.Namespace, all_records: list[Record]) -> int:
    if args.route_action == "prune":
        # Prune is about the local cache directory, not about one record.
        return command_route_prune(args)
    record = find_exact(all_records, args.identity)
    identity = record.identity
    root, client = resolve_tell_client(getattr(args, "tell_root", None))
    principal = tell_principal(getattr(args, "principal", None))
    action = args.route_action
    base = {
        "identity": identity,
        "record_path": str(record.path.resolve()),
        "tell_root": str(root),
        "tell_client": str(client),
        "principal": principal or "",
        "authority": ROUTE_AUTHORITY_NOTE,
    }

    if action == "bind":
        collective = tell_collective(args.collective)
        ttl = tell_ttl(args.ttl)
        wake, wake_kind = tell_wake(args.wake, args.wake_file)
        label = tell_label(args.label, identity)
        arguments = [
            "session", "register",
            "--vault", identity,
            "--collective", collective,
            "--ttl", str(ttl),
            "--label", label,
        ]
        if args.session_id:
            arguments += ["--session-id", tell_session_id(args.session_id)]
        if wake:
            arguments += ["--wake", wake]
        receipt = tell_receipt(run_tell(client, principal, arguments), "register")
        session_id = receipt.get("session_id")
        if not isinstance(session_id, str) or not TELL_SESSION_ID.fullmatch(session_id):
            raise VaultError(f"Tell register receipt has no usable session id: {receipt}")
        payload = {
            **base,
            "action": "bind",
            "state": "bound",
            "collective": collective,
            "session_id": session_id,
            "label": str(receipt.get("label", label)),
            "ttl_seconds": ttl,
            "wake_kind": wake_kind,
            "bound_at": utc_now(),
        }
        state_path = save_route_state(identity, principal, {
            "schema_version": ROUTE_STATE_SCHEMA_VERSION,
            "kind": ROUTE_STATE_KIND,
            "rebuildable": True,
            "source_of_truth": "tell-session-registry",
            "identity": identity,
            "principal": principal or "",
            "collective": collective,
            "session_id": session_id,
            "tell_root": str(root),
            "bound_at": payload["bound_at"],
        })
        payload["runtime_state_path"] = str(state_path)
        emit_route(
            payload, args.json,
            f"bound {identity} -> {session_id} (collective {collective}, ttl {ttl}s)",
        )
        return 0

    session_id, source = resolve_route_session(
        client, principal, identity, args.session_id,
        getattr(args, "collective", None), adopt=action != "beat",
    )
    base["session_id"] = session_id or ""
    base["session_id_source"] = source

    if session_id is None:
        payload = {**base, "action": action, "state": "unbound"}
        emit_route(
            payload, args.json,
            f"{identity} has no known live Tell route; run 'route bind' first",
        )
        return 2

    if action == "beat":
        completed = run_tell(
            client, principal, ["session", "heartbeat", "--session-id", session_id],
            tolerated_returncodes=(2,),
        )
        live = completed.returncode == 0
        payload = {
            **base,
            "action": "beat",
            "state": "live" if live else "stale",
            "beat_at": utc_now(),
        }
        emit_route(
            payload, args.json,
            f"heartbeat {identity} -> {session_id}: {payload['state']}"
            + ("" if live else "; re-bind before relying on delivery"),
        )
        return 0 if live else 2

    if action == "check":
        # Answered from the Tell registry, never from the local cache. A cache
        # that says "bound" while the registry has no record is exactly the
        # failure this command exists to catch.
        completed = run_tell(
            client, principal, ["session", "check", "--session-id", session_id],
            tolerated_returncodes=(2,),
        )
        receipt = tell_receipt(completed, "check")
        state = str(receipt.get("state", "unknown"))
        payload = {**base, "action": "check", "state": state}
        if receipt.get("last_seen") is not None:
            payload["last_seen"] = receipt["last_seen"]
        if state == "live" and source == "tell-registry":
            save_route_state(identity, principal, {
                "schema_version": ROUTE_STATE_SCHEMA_VERSION,
                "kind": ROUTE_STATE_KIND,
                "rebuildable": True,
                "source_of_truth": "tell-session-registry",
                "identity": identity,
                "principal": principal or "",
                "collective": tell_collective(args.collective) if args.collective else "",
                "session_id": session_id,
                "tell_root": str(root),
                "bound_at": utc_now(),
            })
        emit_route(payload, args.json, f"{identity} -> {session_id}: {state}")
        return 0 if state == "live" else 2

    completed = run_tell(
        client, principal, ["session", "end", "--session-id", session_id],
        tolerated_returncodes=(2,),
    )
    clear_route_state(identity, principal)
    ended = completed.returncode == 0
    payload = {
        **base,
        "action": "end",
        "state": "ended" if ended else "already-absent",
        "ended_at": utc_now(),
    }
    emit_route(payload, args.json, f"ended Tell route for {identity} ({payload['state']})")
    return 0


def add_origin_arguments(command: argparse.ArgumentParser, *, allow_skip: bool) -> None:
    command.add_argument("--runtime", default="codex")
    command.add_argument("--session-id")
    command.add_argument("--session-name")
    command.add_argument("--node")
    command.add_argument("--cwd")
    command.add_argument("--primary", action="store_true")
    if allow_skip:
        command.add_argument("--no-link-current", action="store_true")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--registry", type=Path, default=default_registry())
    commands = result.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="search records using natural-language terms")
    search.add_argument("query", nargs="+")
    search.add_argument("--project")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")

    show = commands.add_parser("show", help="print a record by project/session")
    show.add_argument("identity")

    listing = commands.add_parser("list", help="list records newest first")
    listing.add_argument("--project")
    listing.add_argument("--json", action="store_true")

    commands.add_parser("doctor", help="validate the registry and every record")
    path = commands.add_parser("path", help="print a record path by project/session")
    path.add_argument("identity")
    projects = commands.add_parser("projects", help="list registered projects")
    projects.add_argument("--json", action="store_true")
    start = commands.add_parser(
        "start", help="create an idempotent project-local session scaffold"
    )
    start.add_argument("identity", help="composite identity project/session")
    start.add_argument("--title")
    start.add_argument("--summary")
    start.add_argument("--keywords")
    start.add_argument(
        "--status",
        choices=("active", "parked", "validation", "decided", "complete"),
        default="active",
    )
    start.add_argument("--json", action="store_true")
    add_origin_arguments(start, allow_skip=True)
    checkpoint = commands.add_parser(
        "checkpoint",
        help="write a machine-readable cross-agent project checkpoint",
    )
    checkpoint.add_argument("identity", help="composite identity project/session")
    checkpoint.add_argument("--reporter", required=True)
    checkpoint.add_argument("--state", choices=tuple(sorted(CHECKPOINT_STATES)), required=True)
    checkpoint.add_argument("--need", choices=tuple(sorted(CHECKPOINT_NEEDS)), default="none")
    checkpoint.add_argument("--next", dest="next_action", required=True)
    checkpoint.add_argument("--at", dest="checkpoint_at")
    checkpoint.add_argument("--note")
    checkpoint.add_argument("--evidence", action="append", default=[])
    checkpoint.add_argument("--if-checkpoint-at")
    checkpoint.add_argument("--json", action="store_true")
    archive = commands.add_parser(
        "archive-history",
        help="move archived checkpoint history into a versioned archive file",
        description=(
            "Move only the '## Checkpoint history' section of one top-level "
            "session record into sessions/history/<session>."
            "checkpoint-history.<NNN>.md. The current checkpoint and every "
            "other section stay byte-identical, history is never deleted, and "
            "an existing archive is never overwritten."
        ),
    )
    archive.add_argument("identity", help="composite identity project/session")
    archive.add_argument(
        "--dry-run", action="store_true", help="report the plan and write nothing"
    )
    archive.add_argument("--json", action="store_true")
    link = commands.add_parser(
        "link", help="link a vault record to a native runtime session"
    )
    link.add_argument("identity")
    link.add_argument("--json", action="store_true")
    add_origin_arguments(link, allow_skip=False)
    route = commands.add_parser(
        "route",
        help="bind, heartbeat, check, and end the live Tell route for a session",
        description=(
            "Manage the ephemeral Tell delivery route for an existing "
            "project/session record. The route is transport state owned by the "
            "Tell registry; it is never written into the Markdown record, the "
            "origins sidecar, or a checkpoint, and it is never authority."
        ),
    )
    route_actions = route.add_subparsers(dest="route_action", required=True)
    route_bind = route_actions.add_parser(
        "bind", help="register this runtime as the live Tell route for the session"
    )
    route_bind.add_argument(
        "--collective", help=f"Tell collective (default {TELL_DEFAULT_COLLECTIVE})"
    )
    route_bind.add_argument("--ttl", type=int, help=f"lease seconds (default {TELL_DEFAULT_TTL})")
    route_bind.add_argument("--wake", help="receiver-owned wake spec as JSON")
    route_bind.add_argument(
        "--wake-file", type=Path, help="read the wake spec from a file instead of argv"
    )
    route_bind.add_argument("--label", help="human-readable route label")
    route_beat = route_actions.add_parser(
        "beat", help="refresh the live Tell lease at a turn boundary"
    )
    route_check = route_actions.add_parser(
        "check", help="ask the Tell registry whether the route is live"
    )
    route_check.add_argument("--collective", help="restrict registry adoption to this collective")
    route_end = route_actions.add_parser(
        "end", help="end the live Tell route when the runtime truly ends"
    )
    route_prune = route_actions.add_parser(
        "prune",
        help="remove local route cache entries the Tell registry proves are gone",
        description=(
            "Compare the rebuildable local route cache against the Tell "
            "session registry and remove only the entries that registry "
            "proves absent or stale. Tell registry and authority state are "
            "never modified, and an unreadable registry removes nothing."
        ),
    )
    route_prune.add_argument("--principal", help="Tell principal to act as")
    route_prune.add_argument(
        "--tell-root",
        help="telld checkout holding src/tell/cli.py (default ~/telld)",
    )
    route_prune.add_argument(
        "--dry-run", action="store_true", help="report the plan and remove nothing"
    )
    route_prune.add_argument("--json", action="store_true")
    for command in (route_bind, route_beat, route_check, route_end):
        command.add_argument("identity", help="composite identity project/session")
        command.add_argument("--principal", help="Tell principal to act as")
        command.add_argument("--session-id", help="explicit Tell session id (s_*)")
        command.add_argument(
            "--tell-root",
            help="telld checkout holding src/tell/cli.py (default ~/telld)",
        )
        command.add_argument("--json", action="store_true")
    central_store.add_store_parser(commands)
    resume = commands.add_parser(
        "resume", help="resume the exact linked native session"
    )
    resume.add_argument("identity")
    resume.add_argument("--runtime")
    resume.add_argument(
        "--print", dest="print_command", action="store_true",
        help="print the resolved command instead of executing it",
    )
    resume.add_argument("--json", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    registry_path = args.registry.expanduser()
    try:
        if args.command in ("start", "link"):
            # A stable directory lock survives atomic registry replacement.
            # Read the catalog only after acquiring it, so concurrent starts
            # cannot both claim the same previously unowned native origin.
            with record_lock(registry_path.resolve()):
                registry = load_registry(registry_path)
                node = registry_node(registry)
                projects = load_projects(registry, registry_path)
                if args.command == "start":
                    return command_start(args, projects, node)
                all_records, _errors = records(projects)
                return command_link(args, node, all_records, projects)
        registry = load_registry(registry_path)
        node = registry_node(registry)
        projects = load_projects(registry, registry_path)
        all_records, record_load_errors = records(projects)
        if record_load_errors and args.command != "doctor":
            for error in record_load_errors:
                print(f"Warning: {error}", file=sys.stderr)
        if args.command == "search":
            return command_search(args, projects, all_records)
        if args.command == "show":
            return command_show(args, all_records)
        if args.command == "list":
            return command_list(args, projects, all_records)
        if args.command == "doctor":
            return command_doctor(
                registry_path, node, projects, all_records, record_load_errors
            )
        if args.command == "path":
            return command_path(args, all_records)
        if args.command == "projects":
            return command_projects(args, projects)
        if args.command == "checkpoint":
            return command_checkpoint(args, node, all_records)
        if args.command == "archive-history":
            return command_archive_history(args, node, all_records)
        if args.command == "resume":
            return command_resume(args, node, all_records)
        if args.command == "route":
            return command_route(args, all_records)
        if args.command == "store":
            return central_store.command_store(
                args, node, projects, all_records, find_exact
            )
    except (VaultError, central_store.StoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
