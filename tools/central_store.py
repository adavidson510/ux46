#!/usr/bin/env python3
"""Session Vault publication into the Collective central store.

This module implements the first bounded slice of
``architecture/COLLECTIVE-CENTRAL-STORE-ARCHITECTURE-1.md``: attributed
one-way publication of a single Session Vault room, verified read-cache pull,
and a machine-readable catalog of current rooms.

Three boundaries hold everywhere below.

*Scope.* The publish allow-list is exact — one Markdown record and, when it
exists, its ``.origins.json`` sidecar. Nothing walks a project, follows a
dependency, or replicates arbitrary files. Origins are copied as pointers;
transcripts, prompts and terminal buffers never enter the store.

*Mutation.* Objects and version manifests are immutable and written once. The
only mutable object is the small ``current.json`` pointer, and updating an
existing one requires a compare-and-set against the digest the publisher
observed. A stale, missing or mismatched comparison refuses without touching
it. Nothing is ever deleted, and no digest mismatch is ever overwritten.

*Authority.* A successful byte copy proves byte identity and attributed
origin. It is not review, approval, promotion by anyone else, or permission to
act. Promotion of the current pointer is an explicit act, reported as such in
the publish receipt.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # executed as a script; the siblings are here
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import domains


STORE_LAYOUT_VERSION = 1
MANIFEST_SCHEMA = "atlas.central-store.manifest.v1"
POINTER_SCHEMA = "atlas.central-store.current.v1"
RECEIPT_SCHEMA = "atlas.central-store.receipt.v1"
PULL_SCHEMA = "atlas.central-store.pull.v1"
CATALOG_SCHEMA = "atlas.central-store.catalog.v1"

DEFAULT_COLLECTIVE = "workspace"
CLASSIFICATIONS = ("public", "internal", "restricted")
DEFAULT_CLASSIFICATION = "internal"

STORE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
# Architecture section 11: publish names are lowercase and central paths stay
# under 240 characters so a Windows SMB3 client can read what a Mac published.
MAX_CENTRAL_PATH_CHARS = 240
MAX_RECORD_BYTES = 1024 * 1024
MAX_ORIGINS_BYTES = 256 * 1024
MAX_ORIGIN_FIELD_CHARS = 512
FORBIDDEN_ORIGIN_KEYS = {
    "body", "buffer", "content", "message", "messages", "output",
    "prompt", "prompts", "text", "transcript", "transcripts",
}

# Conservative, high-signal shapes only. This detector fails the publish
# closed; it never prints the matched bytes, only the rule and the line.
SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem-private-key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws-secret-assignment", re.compile(r"\bAWS_SECRET_ACCESS_KEY\b\s*[:=]\s*\S{16,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[abporsu]-[A-Za-z0-9-]{12,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("model-provider-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}\b")),
    ("onepassword-service-token", re.compile(r"\bops_[A-Za-z0-9]{40,}\b")),
    ("json-web-token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("secret-assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|"
        r"auth[_-]?token|bearer[_-]?token|password|passwd|private[_-]?key)\b"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{20,}"
    )),
)

AUTHORITY_NOTE = (
    "Publishing proves byte identity and attributed origin. It is not review, "
    "approval, assignment acceptance, or permission to act."
)
REPORTED_CONTEXT_NOTE = (
    "A published record is reported context asserted by its reporter at its "
    "checkpoint time, not authority and not proof of an external change."
)
REENTRY_NOTE = (
    "Semantic re-entry from a verified read-cache copy. Native transcript "
    "resumption does not exist across nodes; origins are pointers only."
)
CACHE_NOTE = (
    "Read cache only. This copy is never the writer copy and is never written "
    "back into a project, a repository, or a runtime path."
)


class StoreError(RuntimeError):
    """A user-facing central-store error."""


# --- naming, identity, and path safety ---------------------------------------


def store_name(value: str, label: str) -> str:
    raw = str(value)
    if raw != raw.strip():
        raise StoreError(
            f"Central store {label} may not have leading or trailing whitespace: {raw!r}"
        )
    if not raw:
        raise StoreError(f"Central store {label} cannot be empty")
    if ".." in raw or "/" in raw or "\\" in raw or "\x00" in raw:
        raise StoreError(
            f"Central store {label} may not contain a path separator or traversal: {raw!r}"
        )
    if raw != raw.casefold():
        raise StoreError(
            f"Central store {label} must be lowercase so case-insensitive clients "
            f"cannot collide: {raw!r}"
        )
    if raw.endswith("."):
        raise StoreError(f"Central store {label} may not end with a dot: {raw!r}")
    if not STORE_NAME.fullmatch(raw):
        raise StoreError(
            f"Invalid central store {label}: {raw!r} "
            "(expected lowercase a-z, 0-9, '.', '-', '_', up to 64 characters)"
        )
    if raw.split(".")[0] in WINDOWS_RESERVED:
        raise StoreError(f"Central store {label} is a Windows reserved name: {raw!r}")
    return raw


def parse_identity(identity: str) -> tuple[str, str]:
    text = str(identity)
    if text.count("/") != 1:
        raise StoreError("Central store requires a composite identity project/session")
    project, session = text.split("/", 1)
    return store_name(project, "project"), store_name(session, "session")


def check_central_path(root: Path, path: Path) -> Path:
    relative = os.path.relpath(path, root)
    if relative.startswith(".."):
        raise StoreError(f"Central path escapes the store root: {path}")
    if len(relative) > MAX_CENTRAL_PATH_CHARS:
        raise StoreError(
            f"Central path exceeds {MAX_CENTRAL_PATH_CHARS} characters "
            f"({len(relative)}): {relative}"
        )
    return path


def child(parent: Path, name: str) -> Path:
    """Resolve ``parent/name``, refusing a case-colliding or symlinked sibling.

    macOS and Windows are case-insensitive while the Server store is ext4. Two
    names differing only by case collapse on the client, so the store refuses
    them rather than silently merging two rooms.
    """

    candidate = parent / name
    if candidate.is_symlink():
        raise StoreError(f"Central store path is a symlink; refusing to follow it: {candidate}")
    if parent.is_dir():
        try:
            siblings = list(parent.iterdir())
        except OSError as exc:
            raise StoreError(f"Cannot read central store directory {parent}: {exc}") from exc
        for existing in siblings:
            if existing.name != name and existing.name.casefold() == name.casefold():
                raise StoreError(
                    f"Case collision in the central store: {existing.name!r} already exists "
                    f"beside {name!r} under {parent}"
                )
    return candidate


def descend(root: Path, base: Path, *names: str) -> Path:
    path = base
    for name in names:
        path = check_central_path(root, child(path, name))
    return path


def store_root_path(value: str | os.PathLike[str]) -> Path:
    root = Path(value).expanduser()
    if not root.exists():
        raise StoreError(
            f"Central store root does not exist: {root}. This slice never mounts or "
            "creates a share; point --store-root at an existing local directory."
        )
    if not root.is_dir():
        raise StoreError(f"Central store root is not a directory: {root}")
    return root.resolve()


def source_file(path: Path, allowed_root: Path, label: str) -> Path:
    if path.is_symlink():
        raise StoreError(f"Refusing to publish a symlinked {label}: {path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise StoreError(
            f"Refusing to publish a {label} that resolves outside its project root: "
            f"{resolved} is not under {allowed_root}"
        ) from exc
    info = resolved.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise StoreError(f"Refusing to publish a non-regular {label}: {resolved}")
    return resolved


# --- primitives ---------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def iso_from_ns(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=1024 * 1024) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o664) -> None:
    """Temp-write beside the target, fsync, then rename on the same filesystem.

    A mount that vanishes mid-publish leaves a dot-prefixed temp file, never a
    truncated file at a real path. Central files land group-readable so a peer
    node in the ``agents`` group can read what this node published; the local
    read cache is written private instead.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextmanager
def room_lock(room: Path):
    """Serialize the read-compare-write window on one room directory.

    The lock is a courtesy for concurrent local publishers. Correctness comes
    from the compare-and-set below, because SMB advisory locking cannot be
    trusted (architecture section 11).
    """

    room.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(room, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# --- content screening --------------------------------------------------------


def scan_secrets(text: str, label: str) -> None:
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in SECRET_RULES:
            if pattern.search(line):
                raise StoreError(
                    f"Refusing to publish {label}: secret-shaped content matched rule "
                    f"{rule!r} at line {number}. The store never carries credentials; "
                    "remove or reference the material and re-run. "
                    "(The matched text is deliberately not printed.)"
                )


def check_origins_pointers(payload: object, label: str) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("origins"), list):
        raise StoreError(
            f"Refusing to publish {label}: origins must be an object with an origins list"
        )
    origins = payload["origins"]
    for index, origin in enumerate(origins):
        if not isinstance(origin, dict):
            raise StoreError(f"Refusing to publish {label}: origins entry {index} is not an object")
        for key, value in origin.items():
            if str(key).casefold() in FORBIDDEN_ORIGIN_KEYS:
                raise StoreError(
                    f"Refusing to publish {label}: origins entry {index} carries a body-shaped "
                    f"field {key!r}. Origins are runtime pointers only."
                )
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise StoreError(
                    f"Refusing to publish {label}: origins entry {index} field "
                    f"{key!r} is not a scalar pointer"
                )
            if isinstance(value, str) and len(value) > MAX_ORIGIN_FIELD_CHARS:
                raise StoreError(
                    f"Refusing to publish {label}: origins entry {index} field {key!r} exceeds "
                    f"{MAX_ORIGIN_FIELD_CHARS} characters. Origins are pointers, not bodies."
                )
    return len(origins)


# --- git provenance -----------------------------------------------------------


def sanitize_remote(url: str | None) -> str | None:
    if not url:
        return None
    # A remote may embed a token as URL userinfo; that never reaches a manifest.
    return re.sub(r"://[^/@\s]*@", "://", url)


def git_metadata(root: Path) -> dict[str, Any]:
    """Best-effort Git provenance. Unavailable is recorded, never guessed."""

    def run(*argv: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(root), *argv],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    toplevel = run("rev-parse", "--show-toplevel")
    if not toplevel:
        return {"available": False, "repo": None, "ref": None, "commit": None, "dirty": None}
    status = run("status", "--porcelain")
    return {
        "available": True,
        "root": toplevel,
        "repo": sanitize_remote(run("config", "--get", "remote.origin.url")),
        "ref": run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": run("rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status),
    }


# --- namespace ----------------------------------------------------------------


class Namespace:
    """Paths under ``collectives/<collective>/`` as defined by the architecture."""

    def __init__(self, root: Path, collective: str) -> None:
        self.root = root
        self.collective = collective
        self.base = descend(root, root, "collectives", collective)

    @property
    def objects(self) -> Path:
        return descend(self.root, self.base, "objects", "sha256")

    def object_path(self, digest: str) -> Path:
        if not SHA256_HEX.fullmatch(digest):
            raise StoreError(f"Invalid sha256 digest: {digest!r}")
        return descend(self.root, self.objects, digest[:2], digest[2:4], digest)

    def project(self, project: str) -> Path:
        return descend(self.root, self.base, "projects", project)

    def room(self, project: str, session: str) -> Path:
        return descend(self.root, self.project(project), "rooms", session)

    def current(self, project: str, session: str) -> Path:
        return descend(self.root, self.room(project, session), "current.json")

    def version(self, project: str, session: str, digest: str) -> Path:
        return descend(self.root, self.room(project, session), "versions", f"{digest}.json")

    def manifest(self, project: str, publish_id: str) -> Path:
        return descend(self.root, self.project(project), "manifests", f"{publish_id}.json")

    def incoming(
        self, project: str, node: str, principal: str, session: str, publish_id: str
    ) -> Path:
        return descend(
            self.root, self.project(project),
            "incoming", node, principal, "rooms", session, f"{publish_id}.json",
        )

    def relative(self, path: Path) -> str:
        return os.path.relpath(path, self.root)


def write_object(namespace: Namespace, digest: str, data: bytes) -> tuple[Path, str]:
    path = namespace.object_path(digest)
    if path.exists():
        existing = sha256_file(path)
        if existing != digest:
            raise StoreError(
                f"Central object at {path} hashes to {existing}, not {digest}. "
                "Refusing to overwrite a digest mismatch; this object needs quarantine review."
            )
        return path, "existing"
    atomic_write_bytes(path, data)
    return path, "created"


def write_once_json(path: Path, payload: dict[str, Any], identity_keys: tuple[str, ...]) -> str:
    """Write an immutable JSON file, or confirm the existing one is the same thing."""

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"Cannot read existing central file {path}: {exc}") from exc
        for key in identity_keys:
            if existing.get(key) != payload.get(key):
                raise StoreError(
                    f"Central store already holds a different {key} at {path}; "
                    "refusing to overwrite an immutable file."
                )
        return "existing"
    atomic_write_bytes(path, json_bytes(payload))
    return "created"


def load_current(path: Path, project: str, session: str, collective: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"Invalid central current pointer at {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != POINTER_SCHEMA:
        raise StoreError(f"Unsupported central current pointer schema at {path}")
    if (payload.get("project"), payload.get("session"), payload.get("collective")) != (
        project, session, collective
    ):
        raise StoreError(f"Central current pointer identity mismatch at {path}")
    if not SHA256_HEX.fullmatch(str(payload.get("record_sha256", ""))):
        raise StoreError(f"Central current pointer has no usable record digest at {path}")
    payload["pointer_sha256"] = sha256_bytes(path.read_bytes())
    return payload


# --- publish ------------------------------------------------------------------


def decide_current(
    observed: dict[str, Any] | None,
    record_digest: str,
    origins_digest: str | None,
    compare: str | None,
    identity: str,
) -> str:
    """Return the intended current-pointer transition, or refuse.

    Refusal happens before anything is written, so a stale compare-and-set
    never mutates the pointer and never leaves half-published state.
    """

    if compare is not None and not SHA256_HEX.fullmatch(compare):
        raise StoreError(f"--if-central-sha256 must be a sha256 hex digest: {compare!r}")
    if observed is None:
        if compare is not None:
            raise StoreError(
                f"No central current exists for {identity}, so there is nothing to "
                "compare against. "
                "Re-run without --if-central-sha256 to initialize it."
            )
        return "initialize"
    current_digest = str(observed["record_sha256"])
    if compare is not None and compare != current_digest:
        raise StoreError(
            f"Stale compare-and-set for {identity}: expected central record {compare}, "
            f"found {current_digest}. The central current pointer was not modified. "
            "Re-read the central current and re-run with the observed digest."
        )
    if current_digest == record_digest and observed.get("origins_sha256") == origins_digest:
        return "unchanged"
    if compare is None:
        raise StoreError(
            f"Central current for {identity} already points at record {current_digest}. "
            f"Updating it requires --if-central-sha256 {current_digest}, or use "
            "--no-promote to store the attributed replica without promoting it."
        )
    return "update"


def command_publish(args: argparse.Namespace, node: str, record: Any) -> int:
    project, session = parse_identity(record.identity)
    collective = store_name(args.collective or DEFAULT_COLLECTIVE, "collective")
    node_id = store_name(args.node or node, "node")
    principal = store_name(args.principal, "principal")
    reporter = store_name(args.reporter or args.principal, "reporter")
    classification = args.classification
    identity = f"{project}/{session}"

    project_root = record.project.root.expanduser().resolve()
    # The domain is resolved from the owning project manifest and nowhere else.
    # There is deliberately no --domain flag: keywords, aliases and CLI text
    # never assign an ownership boundary.
    try:
        domain = domains.resolve_domain(project_root)
    except domains.DomainError as exc:
        raise StoreError(str(exc)) from exc
    record_path = source_file(record.path, project_root, "session record")
    if record_path.name != f"{session}.md":
        raise StoreError(
            f"Publish allow-list expects {session}.md for {identity}, found {record_path.name}"
        )

    record_info = record_path.lstat()
    if record_info.st_size > MAX_RECORD_BYTES:
        raise StoreError(
            f"Session record exceeds {MAX_RECORD_BYTES} bytes ({record_info.st_size}); "
            "the store carries distilled context, not transcripts."
        )
    record_data = record_path.read_bytes()
    try:
        record_text = record_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StoreError(f"Session record is not valid UTF-8: {record_path} ({exc})") from exc
    scan_secrets(record_text, f"{identity} record")
    record_digest = sha256_bytes(record_data)

    # The allow-list is exactly these two paths. Nothing else is ever considered.
    origins_path = record.origins_path
    origins_digest: str | None = None
    origins_data: bytes | None = None
    origins_count: int | None = None
    if origins_path.exists():
        origins_path = source_file(origins_path, project_root, "origins sidecar")
        if origins_path.name != f"{session}.origins.json":
            raise StoreError(
                f"Publish allow-list expects {session}.origins.json for {identity}, "
                f"found {origins_path.name}"
            )
        if origins_path.lstat().st_size > MAX_ORIGINS_BYTES:
            raise StoreError(f"Origins sidecar exceeds {MAX_ORIGINS_BYTES} bytes: {origins_path}")
        origins_data = origins_path.read_bytes()
        try:
            origins_text = origins_data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoreError(f"Origins sidecar is not valid UTF-8: {origins_path} ({exc})") from exc
        scan_secrets(origins_text, f"{identity} origins")
        try:
            origins_payload = json.loads(origins_text)
        except json.JSONDecodeError as exc:
            raise StoreError(f"Origins sidecar is not valid JSON: {origins_path} ({exc})") from exc
        origins_count = check_origins_pointers(origins_payload, f"{identity} origins")
        origins_digest = sha256_bytes(origins_data)

    root = store_root_path(args.store_root)
    namespace = Namespace(root, collective)
    room = namespace.room(project, session)
    current_path = namespace.current(project, session)
    checkpoint_at = str(record.metadata.get("checkpoint_at", "")) or None

    # Pre-flight: refuse a stale or unguarded promotion before writing bytes.
    observed = load_current(current_path, project, session, collective)
    transition = (
        "untouched" if args.no_promote
        else decide_current(
            observed, record_digest, origins_digest, args.if_central_sha256, identity
        )
    )

    items = [{
        "role": "record",
        "source_path": str(record_path),
        "filename": record_path.name,
        "sha256": record_digest,
        "size": record_info.st_size,
        "captured_at": iso_from_ns(record_info.st_mtime_ns),
        "central_object": namespace.relative(namespace.object_path(record_digest)),
    }]
    if origins_digest is not None:
        origins_info = origins_path.lstat()
        items.append({
            "role": "origins",
            "source_path": str(origins_path),
            "filename": origins_path.name,
            "sha256": origins_digest,
            "size": origins_info.st_size,
            "captured_at": iso_from_ns(origins_info.st_mtime_ns),
            "central_object": namespace.relative(namespace.object_path(origins_digest)),
            "pointer_count": origins_count,
            "note": "native runtime pointers only; no transcript is published",
        })

    core = {
        "collective": collective,
        "project": project,
        "session": session,
        "node": node_id,
        "principal": principal,
        "reporter": reporter,
        "domain": domain,
        "classification": classification,
        "record_sha256": record_digest,
        "origins_sha256": origins_digest,
        "checkpoint_at": checkpoint_at,
        "source_record": str(record_path),
        "source_origins": str(origins_path) if origins_digest else None,
        "git": git_metadata(project_root),
    }
    publish_id = sha256_bytes(json_bytes(core))[:32]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "layout_version": STORE_LAYOUT_VERSION,
        "publish_id": publish_id,
        "identity": identity,
        "published_at": utc_now(),
        "project_root": str(project_root),
        "items": items,
        "authority": AUTHORITY_NOTE,
        "reported_context": REPORTED_CONTEXT_NOTE,
        **core,
    }

    published_at = manifest["published_at"]
    with room_lock(room):
        # Authoritative re-check inside the lock; a racing publisher between
        # pre-flight and here is refused here rather than silently winning.
        observed = load_current(current_path, project, session, collective)
        transition = (
            "untouched" if args.no_promote
            else decide_current(
                observed, record_digest, origins_digest, args.if_central_sha256, identity
            )
        )

        created = existing = 0
        for digest, data in (
            (record_digest, record_data),
            *(((origins_digest, origins_data),) if origins_digest else ()),
        ):
            _, state = write_object(namespace, digest, data)
            created += state == "created"
            existing += state == "existing"

        version_path = namespace.version(project, session, record_digest)
        version_state = write_once_json(
            version_path, manifest,
            ("collective", "project", "session", "record_sha256"),
        )
        manifest_path = namespace.manifest(project, publish_id)
        manifest_state = write_once_json(
            manifest_path, manifest, ("publish_id", "record_sha256", "node", "principal"),
        )
        incoming_path = namespace.incoming(project, node_id, principal, session, publish_id)
        incoming_state = write_once_json(
            incoming_path, manifest, ("publish_id", "record_sha256", "node", "principal"),
        )

        pointer_sha = observed.get("pointer_sha256") if observed else None
        if transition in ("initialize", "update"):
            pointer = {
                "schema": POINTER_SCHEMA,
                "layout_version": STORE_LAYOUT_VERSION,
                "collective": collective,
                "project": project,
                "session": session,
                "identity": identity,
                "record_sha256": record_digest,
                "record_bytes": record_info.st_size,
                "origins_sha256": origins_digest,
                "version_path": namespace.relative(version_path),
                "manifest_path": namespace.relative(manifest_path),
                "incoming_path": namespace.relative(incoming_path),
                "publish_id": publish_id,
                "node": node_id,
                "principal": principal,
                "reporter": reporter,
                "domain": domain,
                "classification": classification,
                "checkpoint_at": checkpoint_at,
                "promoted_at": published_at,
                "promotion": "explicit publish by the named principal on the named node",
                "previous_record_sha256": str(observed["record_sha256"]) if observed else None,
                "authority": AUTHORITY_NOTE,
            }
            encoded = json_bytes(pointer)
            atomic_write_bytes(current_path, encoded)
            pointer_sha = sha256_bytes(encoded)

    promoted = transition in ("initialize", "update")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "action": "publish",
        "identity": identity,
        "collective": collective,
        "store_root": str(root),
        "publish_id": publish_id,
        "published_at": published_at,
        "node": node_id,
        "principal": principal,
        "reporter": reporter,
        "domain": domain,
        "domain_source": "project manifest" if domain else "unscoped (no manifest domain)",
        "classification": classification,
        "checkpoint_at": checkpoint_at,
        "git": core["git"],
        "items": items,
        "objects": {"created": created, "existing": existing},
        "version": {"path": namespace.relative(version_path), "state": version_state},
        "manifest": {"path": namespace.relative(manifest_path), "state": manifest_state},
        "incoming": {
            "path": namespace.relative(incoming_path),
            "state": incoming_state,
            "attribution": f"{node_id}/{principal}",
        },
        "current": {
            "state": {
                "initialize": "initialized", "update": "updated",
                "unchanged": "unchanged", "untouched": "untouched",
            }[transition],
            "record_sha256": record_digest if promoted else (
                str(observed["record_sha256"]) if observed else None
            ),
            "previous_record_sha256": str(observed["record_sha256"]) if observed else None,
            "pointer_sha256": pointer_sha,
            "path": namespace.relative(current_path),
        },
        "promoted": promoted,
        "promotion": (
            "explicit — this publish updated the central current pointer"
            if promoted else
            "none — bytes are stored under the attributed incoming path only"
        ),
        "authority": AUTHORITY_NOTE,
        "reported_context": REPORTED_CONTEXT_NOTE,
        "transcripts": (
            "no transcript, prompt, or terminal body is published; origins are pointers only"
        ),
    }

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(
            f"published {identity} -> {collective} (node {node_id}, principal {principal}, "
            f"domain {domains.domain_label(domain)})"
        )
        print(f"record sha256 {record_digest} ({record_info.st_size} bytes)")
        if origins_digest:
            print(f"origins sha256 {origins_digest} (pointers only)")
        print(
            f"objects: {created} created, {existing} existing; "
            f"version {version_state}; incoming {incoming_state}"
        )
        print(f"current: {receipt['current']['state']} — {receipt['promotion']}")
        print(AUTHORITY_NOTE)
        print(namespace.relative(current_path))
    return 0


# --- pull ---------------------------------------------------------------------


def default_cache_root() -> Path:
    configured = os.environ.get("ATLAS_STORE_CACHE")
    if configured and configured.strip():
        return Path(configured.strip()).expanduser()
    return Path.home() / ".cache" / "atlas" / "central-store"


def check_cache_root(cache: Path, projects: list[Any]) -> Path:
    """A read cache must never sit inside — or swallow — a project working copy."""

    resolved = cache.expanduser()
    if resolved.exists():
        resolved = resolved.resolve()
    else:
        resolved = resolved.parent.resolve() / resolved.name
    for project in projects:
        root = project.root.expanduser()
        root = root.resolve() if root.exists() else root
        if resolved == root or root in resolved.parents or resolved in root.parents:
            raise StoreError(
                f"Refusing a read cache at {resolved}: it overlaps the registered project "
                f"root {root}. A pull is never allowed to touch a writer copy."
            )
    return resolved


def verified_object(namespace: Namespace, digest: str, label: str) -> bytes:
    path = namespace.object_path(digest)
    if not path.is_file():
        raise StoreError(f"Central object missing for {label}: {namespace.relative(path)}")
    data = path.read_bytes()
    actual = sha256_bytes(data)
    if actual != digest:
        raise StoreError(
            f"Central object for {label} failed verification: expected {digest}, "
            f"computed {actual} at {namespace.relative(path)}. Nothing was cached; "
            "this object needs quarantine review before use."
        )
    return data


def pull_into_cache(
    identity_text: str,
    store_root: str | os.PathLike[str],
    collective_id: str | None,
    cache_dir: str | os.PathLike[str] | None,
    projects: list[Any],
) -> dict[str, Any]:
    """Verify the central current record and cache it. Returns the pull receipt.

    Separated from the CLI so a cold-agent bootstrap can fall back to the
    central store for a remote-only record and keep the same verification,
    the same read-cache boundary, and the same honest semantic-re-entry label.
    """

    project, session = parse_identity(identity_text)
    collective = store_name(collective_id or DEFAULT_COLLECTIVE, "collective")
    identity = f"{project}/{session}"
    root = store_root_path(store_root)
    namespace = Namespace(root, collective)

    current_path = namespace.current(project, session)
    pointer = load_current(current_path, project, session, collective)
    if pointer is None:
        raise StoreError(f"No central current pointer for {identity} under {root}")

    version_path = namespace.version(project, session, str(pointer["record_sha256"]))
    if not version_path.is_file():
        raise StoreError(f"Central version manifest missing: {namespace.relative(version_path)}")
    try:
        version = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"Invalid central version manifest {version_path}: {exc}") from exc
    if version.get("record_sha256") != pointer["record_sha256"]:
        raise StoreError(
            f"Central version manifest disagrees with the current pointer for {identity}; "
            "refusing to cache a contradictory version."
        )

    verified: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for item in version.get("items", []):
        digest = str(item.get("sha256", ""))
        payloads[item["role"]] = verified_object(namespace, digest, f"{identity} {item['role']}")
        if len(payloads[item["role"]]) != item.get("size"):
            raise StoreError(
                f"Central object size mismatch for {identity} {item['role']}; nothing was cached."
            )
        verified.append({
            "role": item["role"],
            "filename": item["filename"],
            "sha256": digest,
            "size": item["size"],
            "central_object": item["central_object"],
            "verified": True,
        })
    if "record" not in payloads:
        raise StoreError(f"Central version manifest for {identity} lists no record item")
    if str(pointer.get("origins_sha256") or "") and "origins" not in payloads:
        raise StoreError(f"Central current pointer for {identity} names origins the manifest omits")

    cache_root = check_cache_root(
        Path(cache_dir) if cache_dir else default_cache_root(), projects
    )
    cache_directory = cache_root / collective / project / session
    written: list[str] = []
    for role, data in payloads.items():
        name = next(entry["filename"] for entry in verified if entry["role"] == role)
        target = cache_directory / name
        if target.is_symlink():
            raise StoreError(
                f"Read cache path is a symlink; refusing to write through it: {target}"
            )
        atomic_write_bytes(target, data, mode=0o600)
        written.append(str(target))

    receipt = {
        "schema": PULL_SCHEMA,
        "action": "pull",
        "identity": identity,
        "collective": collective,
        "store_root": str(root),
        "pulled_at": utc_now(),
        "node": pointer.get("node"),
        "principal": pointer.get("principal"),
        "reporter": pointer.get("reporter"),
        "domain": pointer.get("domain"),
        "classification": pointer.get("classification"),
        "checkpoint_at": pointer.get("checkpoint_at"),
        "record_sha256": pointer["record_sha256"],
        "origins_sha256": pointer.get("origins_sha256"),
        "pointer_sha256": pointer.get("pointer_sha256"),
        "central_current": namespace.relative(current_path),
        "central_version": namespace.relative(version_path),
        "items": verified,
        "cache_dir": str(cache_directory),
        "cache_files": sorted(written),
        "cache": CACHE_NOTE,
        "reentry": "semantic-reentry",
        "native_transcript_resumption": False,
        "reentry_note": REENTRY_NOTE,
        "reported_context": REPORTED_CONTEXT_NOTE,
        "authority": AUTHORITY_NOTE,
    }
    atomic_write_bytes(cache_directory / "pull-receipt.json", json_bytes(receipt), mode=0o600)
    return receipt


def command_pull(args: argparse.Namespace, projects: list[Any]) -> int:
    receipt = pull_into_cache(
        args.identity, args.store_root, args.collective, args.cache_dir, projects
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(
            f"pulled {receipt['identity']} from {receipt['collective']} "
            f"(published by {receipt.get('principal')} on {receipt.get('node')}, "
            f"domain {domains.domain_label(receipt.get('domain'))})"
        )
        print(
            f"verified {len(receipt['items'])} object(s); "
            f"record sha256 {receipt['record_sha256']}"
        )
        print(f"read cache: {receipt['cache_dir']}")
        print(CACHE_NOTE)
        print(REENTRY_NOTE)
    return 0


# --- catalog ------------------------------------------------------------------


def checkpoint_age_seconds(value: str | None, observed: str) -> float | None:
    if not value:
        return None
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return round((now - then).total_seconds(), 3)


def catalog_entry(
    namespace: Namespace, project: str, session: str, observed: str
) -> dict[str, Any] | None:
    """Build one catalog entry from the central current pointer, or ``None``.

    Exact identity in, one recorded-context entry out. Shared with the
    cold-agent bootstrap so a remote-only resume reads the same fields the
    catalog reports rather than a second, drifting projection.
    """

    root = namespace.root
    collective = namespace.collective
    pointer_path = namespace.current(project, session)
    pointer = load_current(pointer_path, project, session, collective)
    if pointer is None:
        return None
    version_path = namespace.version(project, session, str(pointer["record_sha256"]))
    record_object = namespace.object_path(str(pointer["record_sha256"]))
    return {
        "schema": CATALOG_SCHEMA,
        "store_root": str(root),
        "collective": collective,
        "identity": f"{project}/{session}",
        "project": project,
        "session": session,
        "state": "current",
        "node": pointer.get("node"),
        "principal": pointer.get("principal"),
        "reporter": pointer.get("reporter"),
        "domain": pointer.get("domain"),
        "classification": pointer.get("classification"),
        "record_sha256": pointer["record_sha256"],
        "record_bytes": pointer.get("record_bytes"),
        "origins_sha256": pointer.get("origins_sha256"),
        "pointer_sha256": pointer.get("pointer_sha256"),
        "publish_id": pointer.get("publish_id"),
        "checkpoint_at": pointer.get("checkpoint_at"),
        "checkpoint_age_seconds": checkpoint_age_seconds(pointer.get("checkpoint_at"), observed),
        "promoted_at": pointer.get("promoted_at"),
        "promoted_age_seconds": checkpoint_age_seconds(pointer.get("promoted_at"), observed),
        "observed_at": observed,
        "central_current": namespace.relative(pointer_path),
        "central_version": namespace.relative(version_path),
        "central_record_object": namespace.relative(record_object),
        "central_incoming": pointer.get("incoming_path"),
        "central_manifest": pointer.get("manifest_path"),
        "version_present": version_path.is_file(),
        "record_object_present": record_object.is_file(),
        "verified": False,
        "verification": "catalog reports recorded digests; 'store pull' verifies bytes",
        "native_transcript_resumption": False,
        "reentry": "semantic-reentry",
        "reported_context": REPORTED_CONTEXT_NOTE,
    }


def command_catalog(args: argparse.Namespace) -> int:
    collective = store_name(args.collective or DEFAULT_COLLECTIVE, "collective")
    root = store_root_path(args.store_root)
    namespace = Namespace(root, collective)
    wanted = store_name(args.project, "project") if args.project else None
    try:
        wanted_domain = domains.domain_filter(args.domain) if args.domain else None
    except domains.DomainError as exc:
        raise StoreError(str(exc)) from exc
    observed = utc_now()

    projects_dir = descend(root, namespace.base, "projects")
    entries: list[dict[str, Any]] = []
    for project_dir in sorted(p for p in projects_dir.glob("*") if p.is_dir()):
        project = project_dir.name
        if wanted and project != wanted:
            continue
        rooms = project_dir / "rooms"
        for room in sorted(p for p in rooms.glob("*") if p.is_dir()):
            session = room.name
            try:
                entry = catalog_entry(namespace, project, session, observed)
            except StoreError as exc:
                entries.append({
                    "schema": CATALOG_SCHEMA, "store_root": str(root), "collective": collective,
                    "identity": f"{project}/{session}", "state": "unreadable",
                    "observed_at": observed, "error": str(exc),
                })
                continue
            if entry is None:
                continue
            if not domains.matches_domain(entry.get("domain"), wanted_domain):
                continue
            entries.append(entry)

    if args.json:
        print(json.dumps({
            "schema": CATALOG_SCHEMA,
            "store_root": str(root),
            "collective": collective,
            "observed_at": observed,
            "count": len(entries),
            "entries": entries,
            "reported_context": REPORTED_CONTEXT_NOTE,
            "authority": AUTHORITY_NOTE,
        }, indent=2, sort_keys=True))
    else:
        for entry in entries:
            print(json.dumps(entry, sort_keys=True))
    return 0


# --- CLI ----------------------------------------------------------------------


def add_store_parser(commands: argparse._SubParsersAction) -> None:
    store = commands.add_parser(
        "store",
        help="publish, pull, and catalog Session Vault rooms in the Collective central store",
        description=(
            "Attributed one-way publication of a single Session Vault room into an "
            "explicit local central store, plus verified read-cache pull and a "
            "machine-readable catalog. The allow-list is exactly the Markdown record "
            "and its origins sidecar; no project tree is walked and no share is "
            "mounted. Publishing proves byte identity and attributed origin — it is "
            "never review, approval, or permission to act."
        ),
    )
    actions = store.add_subparsers(dest="store_action", required=True)

    publish = actions.add_parser(
        "publish",
        help="publish one project/session record and its origins sidecar",
        description=(
            "Hash and copy exactly one Markdown record and, when present, its "
            "origins sidecar into content-addressed immutable objects, write an "
            "immutable version manifest plus an attributed incoming manifest, and "
            "explicitly promote the small current pointer. Updating an existing "
            "current pointer requires --if-central-sha256; a stale or missing "
            "comparison refuses without modifying it."
        ),
    )
    publish.add_argument("identity", help="composite identity project/session")
    publish.add_argument("--principal", required=True, help="publishing principal, e.g. cp-workspace")
    publish.add_argument("--reporter", help="reporter asserting the record (default: --principal)")
    publish.add_argument("--node", help="publishing node id (default: the Atlas registry node)")
    publish.add_argument(
        "--classification", choices=CLASSIFICATIONS, default=DEFAULT_CLASSIFICATION,
        help=f"central classification (default {DEFAULT_CLASSIFICATION})",
    )
    publish.add_argument(
        "--if-central-sha256", dest="if_central_sha256",
        help="compare-and-set: the central current record digest observed before this publish",
    )
    publish.add_argument(
        "--no-promote", action="store_true",
        help="store the attributed replica without touching the central current pointer",
    )

    pull = actions.add_parser(
        "pull",
        help="verify and cache the central current record for a project/session",
        description=(
            "Read the central current pointer, verify every digest, and write the "
            "verified bytes into a local read cache only. A pull never writes into a "
            "project, repository, or runtime path, and it is semantic re-entry — "
            "native transcript resumption does not cross nodes."
        ),
    )
    pull.add_argument("identity", help="composite identity project/session")
    pull.add_argument(
        "--cache-dir",
        help="read-cache root (default: $ATLAS_STORE_CACHE or ~/.cache/atlas/central-store)",
    )

    catalog = actions.add_parser(
        "catalog",
        help="emit machine-readable current-room entries from the central store",
        description=(
            "Walk central current pointers and emit one JSON object per current room "
            "with node, principal, resolved domain, digests, checkpoint freshness, "
            "classification, and central paths. Newline-delimited JSON by default; "
            "--json emits one document. Entries are reported context, not authority."
        ),
    )
    catalog.add_argument("--project", help="restrict the catalog to one project")
    catalog.add_argument(
        "--domain",
        help=(
            "restrict the catalog to one resolved project domain, or 'unscoped' "
            "for rooms whose project declares none"
        ),
    )

    for command in (publish, pull, catalog):
        command.add_argument(
            "--store-root", required=True,
            help="explicit local filesystem root of the central store",
        )
        command.add_argument(
            "--collective", help=f"collective id (default {DEFAULT_COLLECTIVE})",
        )
        command.add_argument("--json", action="store_true")


def command_store(
    args: argparse.Namespace,
    node: str,
    projects: list[Any],
    all_records: list[Any],
    resolve_record: Callable[[list[Any], str], Any],
) -> int:
    action = args.store_action
    if action == "catalog":
        return command_catalog(args)
    # Identity is validated before any lookup so traversal is rejected as
    # traversal rather than reported as a missing record.
    parse_identity(args.identity)
    if action == "pull":
        return command_pull(args, projects)
    return command_publish(args, node, resolve_record(all_records, args.identity))
