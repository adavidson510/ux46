#!/usr/bin/env python3
"""Operational stewardship of an Atlas project or Session Vault room.

This module implements the ownership half of
``architecture/COLLECTIVE-AGENT-BOOTSTRAP-1.md``: who currently writes a
project or a ``project/session`` room, and how that responsibility moves from
one principal to another without either side being able to take it unilaterally.

What stewardship is
-------------------

Stewardship is a coordination fact: the named principal is the one agent that
may write this record and speak for this room. That is all it is. It is **not**
legal or intellectual-property ownership, not employment or contractual
authority, not review, approval, or assignment acceptance, not deployment or
spend permission, and not evidence that anything outside Atlas changed. Git
still owns commits, branches, merges, and repository history.

How a transfer works
--------------------

Two parties, two acts. The current steward *offers* to a named target; only
that target may *accept*. Either side may *cancel* an open offer. Every act
increments a monotonically increasing epoch under compare-and-set, and writes
an immutable, epoch-versioned receipt that chains the previous state digest to
the new one. A stale ``--if-epoch`` refuses without touching anything.

Tell may carry a notice about an offer. A Tell can never be the acceptance —
the accept is a write by the target principal, or it did not happen.

Storage
-------

Project-owned plain JSON beside the sessions the project already owns::

    <project-root>/ownership/project.json
    <project-root>/ownership/sessions/<session>.json
    <project-root>/ownership/receipts/project/<epoch>-<event>.json
    <project-root>/ownership/receipts/sessions/<session>/<epoch>-<event>.json

It carries principals, nodes, timestamps, epochs, digests, and one short human
note. It never carries secrets, transcripts, prompts, paths to credentials, or
anything executable; the note is single-line, length-bounded, and screened by
the same conservative secret detector the central store uses.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # executed as a script; the siblings are here
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import central_store


STATE_SCHEMA = "atlas.ownership.state.v1"
RECEIPT_SCHEMA = "atlas.ownership.receipt.v1"
LAYOUT_VERSION = 1

PRINCIPAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SCOPE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
STATES = ("unowned", "held", "offered")
EVENTS = ("claim", "offer", "accept", "cancel")
MAX_NOTE_CHARS = 200
DEFAULT_OFFER_TTL = 7 * 24 * 3600
MIN_OFFER_TTL = 60
MAX_OFFER_TTL = 30 * 24 * 3600

AUTHORITY_NOTE = (
    "Operational stewardship only: who may write this record and coordinate "
    "this work. It is not legal or IP ownership, not external authority, not "
    "review or approval, and not deployment or spend permission."
)
TRANSFER_NOTE = (
    "A transfer requires two acts by two principals: the current steward "
    "offers, the named target accepts. A Tell notice is never the acceptance."
)


class OwnershipError(RuntimeError):
    """A user-facing stewardship error."""


# --- identity -----------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    kind: str            # "project" | "session"
    project: str
    session: str | None

    @property
    def identity(self) -> str:
        return self.project if self.session is None else f"{self.project}/{self.session}"

    @property
    def label(self) -> str:
        return f"{self.kind} {self.identity}"

    def state_path(self, project_root: Path) -> Path:
        base = Path(project_root) / "ownership"
        if self.session is None:
            return base / "project.json"
        return base / "sessions" / f"{self.session}.json"

    def receipt_dir(self, project_root: Path) -> Path:
        base = Path(project_root) / "ownership" / "receipts"
        if self.session is None:
            return base / "project"
        return base / "sessions" / self.session

    def receipt_relative(self, epoch: int, event: str) -> str:
        name = f"{epoch:06d}-{event}.json"
        if self.session is None:
            return f"ownership/receipts/project/{name}"
        return f"ownership/receipts/sessions/{self.session}/{name}"


def safe_name(value: str, label: str) -> str:
    raw = str(value)
    if raw != raw.strip():
        raise OwnershipError(f"{label} may not have surrounding whitespace: {raw!r}")
    if not raw:
        raise OwnershipError(f"{label} cannot be empty")
    if "/" in raw or "\\" in raw or ".." in raw or "\x00" in raw:
        raise OwnershipError(f"{label} may not contain a path separator or traversal: {raw!r}")
    if not SCOPE_NAME.fullmatch(raw):
        raise OwnershipError(
            f"Invalid {label}: {raw!r} "
            "(expected lowercase a-z, 0-9, '.', '-', '_', up to 64 characters)"
        )
    return raw


def principal(value: object, label: str = "principal") -> str:
    raw = str(value or "")
    if not PRINCIPAL_NAME.fullmatch(raw):
        raise OwnershipError(
            f"Invalid {label}: {value!r} (expected a stable lowercase principal "
            "such as 'local-workspace')"
        )
    return raw


def parse_scope(text: str) -> Scope:
    raw = str(text).strip()
    if raw.count("/") == 0:
        return Scope("project", safe_name(raw, "project id"), None)
    if raw.count("/") == 1:
        project, session = raw.split("/", 1)
        return Scope(
            "session", safe_name(project, "project id"), safe_name(session, "session name")
        )
    raise OwnershipError(
        f"Ownership scope must be 'project' or 'project/session': {text!r}"
    )


def note_text(value: str | None, label: str = "note") -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if any(character in raw for character in "\r\n\x00") or raw != "".join(
        character for character in raw if character.isprintable()
    ):
        raise OwnershipError(f"Ownership {label} must be one printable line")
    if len(raw) > MAX_NOTE_CHARS:
        raise OwnershipError(
            f"Ownership {label} exceeds {MAX_NOTE_CHARS} characters ({len(raw)}); "
            "the record carries coordination context, not documents"
        )
    try:
        central_store.scan_secrets(raw, f"ownership {label}")
    except central_store.StoreError as exc:
        raise OwnershipError(str(exc)) from exc
    return raw


# --- time ---------------------------------------------------------------------


def utc_now() -> str:
    return central_store.utc_now()


def parse_instant(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise OwnershipError(f"{label} must be an RFC3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise OwnershipError(f"{label} must carry a timezone offset or Z: {value!r}")
    return parsed


def offer_ttl(value: int | None) -> int:
    ttl = DEFAULT_OFFER_TTL if value is None else int(value)
    if not MIN_OFFER_TTL <= ttl <= MAX_OFFER_TTL:
        raise OwnershipError(
            f"Offer lifetime must be between {MIN_OFFER_TTL} and {MAX_OFFER_TTL} seconds: {ttl}"
        )
    return ttl


# --- state --------------------------------------------------------------------


@contextmanager
def scope_lock(directory: Path):
    """Serialize the read-decide-write window for one project's ownership tree."""

    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def unowned_state(scope: Scope) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "scope": scope.kind,
        "project": scope.project,
        "session": scope.session,
        "identity": scope.identity,
        "epoch": 0,
        "state": "unowned",
        "owner": None,
        "owner_node": None,
        "held_since": None,
        "offer": None,
        "updated_at": None,
        "last_event": None,
        "last_receipt": None,
        "authority": AUTHORITY_NOTE,
        "transfer": TRANSFER_NOTE,
    }


def validate_offer(offer: object, scope: Scope, path: Path) -> dict[str, Any]:
    if not isinstance(offer, dict):
        raise OwnershipError(f"Ownership offer must be an object: {path}")
    for field in ("offer_id", "from", "to", "offered_at", "expires_at"):
        if not offer.get(field):
            raise OwnershipError(f"Ownership offer is missing '{field}': {path}")
    principal(offer["from"], "offering principal")
    principal(offer["to"], "target principal")
    parse_instant(offer["offered_at"], "offer offered_at")
    parse_instant(offer["expires_at"], "offer expires_at")
    return offer


def validate_state(payload: object, scope: Scope, path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OwnershipError(f"Ownership state must be a JSON object: {path}")
    if payload.get("schema") != STATE_SCHEMA:
        raise OwnershipError(
            f"Unsupported ownership state schema at {path}: {payload.get('schema')!r}"
        )
    if (payload.get("project"), payload.get("session"), payload.get("scope")) != (
        scope.project, scope.session, scope.kind
    ):
        raise OwnershipError(f"Ownership state identity mismatch at {path}")
    epoch = payload.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise OwnershipError(f"Ownership epoch must be a non-negative integer: {path}")
    state = payload.get("state")
    if state not in STATES:
        raise OwnershipError(f"Unsupported ownership state {state!r} at {path}")
    if state == "unowned":
        if payload.get("owner") is not None:
            raise OwnershipError(f"Unowned ownership state names an owner: {path}")
    else:
        principal(payload.get("owner"), "recorded owner")
    if state == "offered":
        validate_offer(payload.get("offer"), scope, path)
    elif payload.get("offer") is not None:
        raise OwnershipError(f"Ownership state {state!r} may not carry an open offer: {path}")
    return payload


def read_state(project_root: Path, scope: Scope) -> dict[str, Any]:
    """Return the stewardship state, synthesising ``unowned`` when absent."""

    path = scope.state_path(project_root)
    if not path.is_file():
        return unowned_state(scope)
    if path.is_symlink():
        raise OwnershipError(f"Ownership state is a symlink; refusing to follow it: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"Invalid ownership state at {path}: {exc}") from exc
    return validate_state(payload, scope, path)


def state_digest(state: dict[str, Any]) -> str:
    return central_store.sha256_bytes(central_store.json_bytes(state))


def offer_expired(state: dict[str, Any], observed: str | None = None) -> bool:
    offer = state.get("offer")
    if not isinstance(offer, dict):
        return False
    now = parse_instant(observed or utc_now(), "observed instant")
    return parse_instant(offer["expires_at"], "offer expires_at") <= now


def projection(state: dict[str, Any], observed: str | None = None) -> dict[str, Any]:
    """A read-only view that answers 'who writes this, and is a handoff open?'."""

    at = observed or utc_now()
    view = dict(state)
    view["observed_at"] = at
    view["offer_open"] = state.get("state") == "offered" and not offer_expired(state, at)
    view["offer_expired"] = state.get("state") == "offered" and offer_expired(state, at)
    return view


# --- receipts -----------------------------------------------------------------


def write_receipt(project_root: Path, scope: Scope, receipt: dict[str, Any]) -> Path:
    path = scope.receipt_dir(project_root) / f"{receipt['epoch_after']:06d}-{receipt['event']}.json"
    try:
        central_store.write_once_json(
            path, receipt,
            ("event", "epoch_after", "actor", "to_owner", "previous_state_sha256"),
        )
    except central_store.StoreError as exc:
        raise OwnershipError(
            f"Refusing to rewrite the immutable ownership receipt {path}: {exc}"
        ) from exc
    return path


def load_receipt(project_root: Path, relative: str) -> dict[str, Any]:
    path = Path(project_root) / relative
    if not path.is_file():
        raise OwnershipError(f"Ownership receipt missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"Invalid ownership receipt at {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != RECEIPT_SCHEMA:
        raise OwnershipError(f"Unsupported ownership receipt schema at {path}")
    return payload


def verify_transfer_receipt(
    project_root: Path, scope: Scope, state: dict[str, Any]
) -> dict[str, Any]:
    """Confirm the current state was produced by an accepted transfer receipt.

    This is the only thing that ever authorises a takeover. A prompt, a Tell,
    a branch name, or a stale checkpoint is not a substitute.
    """

    relative = state.get("last_receipt")
    if not isinstance(relative, str) or not relative:
        raise OwnershipError(
            f"No ownership receipt is recorded for {scope.label}; there is no accepted transfer"
        )
    receipt = load_receipt(project_root, relative)
    if receipt.get("identity") != scope.identity:
        raise OwnershipError(f"Ownership receipt {relative} names a different scope")
    if receipt.get("event") != "accept":
        raise OwnershipError(
            f"The latest ownership receipt for {scope.label} is "
            f"{receipt.get('event')!r}, not an accepted transfer"
        )
    if receipt.get("epoch_after") != state.get("epoch"):
        raise OwnershipError(
            f"Ownership receipt {relative} is epoch {receipt.get('epoch_after')}, "
            f"but the state is epoch {state.get('epoch')}"
        )
    if receipt.get("to_owner") != state.get("owner"):
        raise OwnershipError(
            f"Ownership receipt {relative} transfers to {receipt.get('to_owner')!r}, "
            f"but the state names {state.get('owner')!r}"
        )
    if receipt.get("state_sha256") != state_digest(state):
        raise OwnershipError(
            f"Ownership receipt {relative} does not match the current state digest; "
            "the state file was edited outside the CLI"
        )
    return receipt


# --- transitions --------------------------------------------------------------


def check_epoch(state: dict[str, Any], expected: int | None, scope: Scope) -> None:
    if expected is None:
        return
    if state["epoch"] != expected:
        raise OwnershipError(
            f"Stale compare-and-set for {scope.label}: expected epoch {expected}, "
            f"found {state['epoch']}. Nothing was modified. Re-read with "
            f"'atlas own show {scope.identity}' and re-run with the observed epoch."
        )


def apply_event(
    state: dict[str, Any],
    scope: Scope,
    *,
    event: str,
    actor: str,
    node: str,
    at: str,
    target: str | None = None,
    ttl: int | None = None,
    note: str | None = None,
    expected_offer: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the next state and its receipt, or refuse. Pure, so it is testable."""

    current = state.get("state")
    epoch_before = int(state["epoch"])
    epoch_after = epoch_before + 1
    new = dict(state)
    new["epoch"] = epoch_after
    new["updated_at"] = at
    new["last_event"] = event
    new["authority"] = AUTHORITY_NOTE
    new["transfer"] = TRANSFER_NOTE
    extra: dict[str, Any] = {}

    if event == "claim":
        if current != "unowned":
            raise OwnershipError(
                f"{scope.label} is already stewarded by {state['owner']!r} at epoch "
                f"{epoch_before}. A claim never displaces a steward; ask for an "
                f"offer, or use a distinct child room."
            )
        actor_role = "first-steward"
        new.update({
            "state": "held", "owner": actor, "owner_node": node,
            "held_since": at, "offer": None,
        })
        from_owner, to_owner = None, actor
    elif event == "offer":
        if current == "unowned":
            raise OwnershipError(
                f"{scope.label} has no steward to offer it; run "
                f"'atlas own claim {scope.identity} --principal <principal>' first"
            )
        if current == "offered":
            open_offer = state["offer"]
            raise OwnershipError(
                f"{scope.label} already has an open offer to {open_offer['to']!r} "
                f"(offer {open_offer['offer_id']}). Cancel it before offering again."
            )
        if actor != state["owner"]:
            raise OwnershipError(
                f"Only the current steward may offer {scope.label}: the steward is "
                f"{state['owner']!r}, not {actor!r}"
            )
        if target == actor:
            raise OwnershipError("An ownership offer must name a different principal")
        offered = parse_instant(at, "offer instant")
        expires = (offered + timedelta(seconds=int(ttl or DEFAULT_OFFER_TTL)))
        offer_id = central_store.sha256_bytes(central_store.json_bytes({
            "identity": scope.identity, "from": actor, "to": target,
            "offered_at": at, "epoch": epoch_after,
        }))[:16]
        actor_role = "owner"
        new.update({
            "state": "offered",
            "offer": {
                "offer_id": offer_id,
                "from": actor,
                "to": target,
                "from_node": node,
                "offered_at": at,
                "expires_at": expires.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "ttl_seconds": int(ttl or DEFAULT_OFFER_TTL),
                "note": note,
            },
        })
        from_owner, to_owner = state["owner"], state["owner"]
        extra = {"offer_id": offer_id, "offered_to": target}
    elif event == "accept":
        if current != "offered":
            raise OwnershipError(
                f"There is no open ownership offer for {scope.label}; the current "
                f"steward must run 'atlas own offer {scope.identity} --to {actor}'"
            )
        offer = state["offer"]
        if actor != offer["to"]:
            raise OwnershipError(
                f"Only {offer['to']!r} may accept this offer for {scope.label}, not {actor!r}"
            )
        if expected_offer is not None and expected_offer != offer["offer_id"]:
            raise OwnershipError(
                f"Offer mismatch for {scope.label}: expected {expected_offer!r}, "
                f"found {offer['offer_id']!r}. Nothing was modified."
            )
        if parse_instant(offer["expires_at"], "offer expires_at") <= parse_instant(at, "now"):
            raise OwnershipError(
                f"The ownership offer for {scope.label} expired at {offer['expires_at']}; "
                f"ask {offer['from']!r} to offer it again"
            )
        actor_role = "target"
        from_owner, to_owner = state["owner"], actor
        extra = {"offer_id": offer["offer_id"], "offer_note": offer.get("note")}
        new.update({
            "state": "held", "owner": actor, "owner_node": node,
            "held_since": at, "offer": None,
        })
    elif event == "cancel":
        if current != "offered":
            raise OwnershipError(f"There is no open ownership offer to cancel for {scope.label}")
        offer = state["offer"]
        if actor == state["owner"]:
            actor_role, kind = "owner", "withdrawn"
        elif actor == offer["to"]:
            actor_role, kind = "target", "declined"
        else:
            raise OwnershipError(
                f"Only {state['owner']!r} or {offer['to']!r} may cancel this offer "
                f"for {scope.label}, not {actor!r}"
            )
        if expected_offer is not None and expected_offer != offer["offer_id"]:
            raise OwnershipError(
                f"Offer mismatch for {scope.label}: expected {expected_offer!r}, "
                f"found {offer['offer_id']!r}. Nothing was modified."
            )
        from_owner, to_owner = state["owner"], state["owner"]
        extra = {"offer_id": offer["offer_id"], "cancel_kind": kind, "offered_to": offer["to"]}
        new.update({"state": "held", "offer": None})
    else:  # pragma: no cover - guarded by argparse choices
        raise OwnershipError(f"Unsupported ownership event: {event}")

    new["last_receipt"] = scope.receipt_relative(epoch_after, event)
    digest = state_digest(new)
    core = {
        "schema": RECEIPT_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "scope": scope.kind,
        "project": scope.project,
        "session": scope.session,
        "identity": scope.identity,
        "event": event,
        "at": at,
        "node": node,
        "actor": actor,
        "actor_role": actor_role,
        "from_owner": from_owner,
        "to_owner": to_owner,
        "epoch_before": epoch_before,
        "epoch_after": epoch_after,
        "note": note,
        "previous_state_sha256": state_digest(state),
        "state_sha256": digest,
        "authority": AUTHORITY_NOTE,
        "transfer": TRANSFER_NOTE,
        **extra,
    }
    receipt = {**core, "receipt_id": central_store.sha256_bytes(
        central_store.json_bytes(core)
    )[:32]}
    return new, receipt


def commit_event(
    project_root: Path,
    scope: Scope,
    *,
    event: str,
    actor: str,
    node: str,
    expected_epoch: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply one stewardship event under a lock, receipt first, then state."""

    root = Path(project_root)
    with scope_lock(root / "ownership"):
        state = read_state(root, scope)
        check_epoch(state, expected_epoch, scope)
        at = utc_now()
        new, receipt = apply_event(
            state, scope, event=event, actor=actor, node=node, at=at, **kwargs
        )
        receipt_path = write_receipt(root, scope, receipt)
        state_path = scope.state_path(root)
        central_store.atomic_write_bytes(
            state_path, central_store.json_bytes(new), mode=0o644
        )
    return {
        "state": new,
        "receipt": receipt,
        "receipt_path": str(receipt_path),
        "state_path": str(state_path),
        "previous_state": state,
    }


# --- Tell notification (never authorisation) ----------------------------------


def notify_tell(
    *,
    tell_root: str | None,
    actor: str,
    peer: str,
    summary: str,
) -> dict[str, Any]:
    """Send one addressed Tell notice. Failure never undoes a committed act."""

    import session_vault

    try:
        root, client = session_vault.resolve_tell_client(tell_root)
        session_vault.run_tell(client, actor, [
            "send", "--to", peer, "--intent", "notice", "--summary", summary,
        ])
    except session_vault.VaultError as exc:
        return {
            "sent": False,
            "to": peer,
            "error": str(exc),
            "authority": "A Tell notice is delivery, never authorisation.",
        }
    return {
        "sent": True,
        "to": peer,
        "tell_root": str(root),
        "summary": summary,
        "authority": "A Tell notice is delivery, never authorisation.",
    }


# --- CLI ----------------------------------------------------------------------


def resolve_scope_root(
    scope: Scope,
    projects: list[Any],
    resolve_project: Callable[[list[Any], str], Any],
) -> Path:
    try:
        project = resolve_project(projects, scope.project)
    except RuntimeError as exc:  # AtlasError / VaultError from the calling CLI
        raise OwnershipError(str(exc)) from exc
    root = Path(project.root).expanduser()
    if not root.is_dir():
        raise OwnershipError(f"Project root missing for {scope.project}: {root}")
    if scope.session is not None:
        record = root / "sessions" / f"{scope.session}.md"
        if not record.is_file():
            raise OwnershipError(
                f"No Session Vault record for {scope.identity}: {record}. "
                f"Create the room first with 'session-vault start {scope.identity}'."
            )
    return root


def emit(payload: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for line in lines:
        print(line)


def summarize(view: dict[str, Any]) -> list[str]:
    lines = [
        f"{view['scope']} {view['identity']}: {view['state']} "
        f"(epoch {view['epoch']}, steward {view['owner'] or 'none'})"
    ]
    offer = view.get("offer")
    if isinstance(offer, dict):
        status = "open" if view.get("offer_open") else "EXPIRED"
        lines.append(
            f"  offer {offer['offer_id']} {status}: {offer['from']} -> {offer['to']} "
            f"(expires {offer['expires_at']})"
        )
    if view.get("last_receipt"):
        lines.append(f"  last receipt: {view['last_receipt']}")
    lines.append(f"  {AUTHORITY_NOTE}")
    return lines


def command_own(
    args: argparse.Namespace,
    projects: list[Any],
    node: str,
    resolve_project: Callable[[list[Any], str], Any],
) -> int:
    scope = parse_scope(args.scope)
    root = resolve_scope_root(scope, projects, resolve_project)
    action = args.own_action

    if action == "show":
        view = projection(read_state(root, scope))
        view["project_root"] = str(root)
        view["state_path"] = str(scope.state_path(root))
        emit(view, args.json, summarize(view))
        return 0

    actor = principal(args.principal, "acting principal")
    acting_node = safe_name(args.node or node, "node id")
    note = note_text(getattr(args, "note", None) or getattr(args, "reason", None))
    kwargs: dict[str, Any] = {"note": note}
    if action == "offer":
        kwargs["target"] = principal(args.to, "target principal")
        kwargs["ttl"] = offer_ttl(args.expires_in)
    if action in ("accept", "cancel") and getattr(args, "if_offer", None):
        kwargs["expected_offer"] = str(args.if_offer).strip()

    result = commit_event(
        root, scope,
        event=action, actor=actor, node=acting_node,
        expected_epoch=args.if_epoch, **kwargs,
    )
    view = projection(result["state"])
    payload = {
        "action": action,
        "identity": scope.identity,
        "scope": scope.kind,
        "project_root": str(root),
        "ownership": view,
        "receipt": result["receipt"],
        "receipt_path": result["receipt_path"],
        "state_path": result["state_path"],
        "previous_epoch": result["previous_state"]["epoch"],
        "previous_owner": result["previous_state"]["owner"],
        "authority": AUTHORITY_NOTE,
        "transfer": TRANSFER_NOTE,
    }

    if getattr(args, "notify_tell", False):
        receipt = result["receipt"]
        peer = {
            "offer": receipt.get("offered_to"),
            "accept": receipt.get("from_owner"),
            "cancel": receipt.get("offered_to") if actor == receipt.get("from_owner")
            else receipt.get("from_owner"),
            "claim": None,
        }.get(action)
        if peer and peer != actor:
            payload["notify"] = notify_tell(
                tell_root=getattr(args, "tell_root", None),
                actor=actor,
                peer=peer,
                summary=(
                    f"[atlas-ownership] {action} {scope.identity} epoch "
                    f"{receipt['epoch_after']} by {actor}; receipt "
                    f"{result['receipt_path']}. Notice only, never authorisation."
                ),
            )
        else:
            payload["notify"] = {"sent": False, "to": None, "error": "no distinct peer to notify"}

    lines = [f"{action}: {scope.identity} epoch {view['epoch']}"] + summarize(view)
    lines.append(f"  receipt: {result['receipt_path']}")
    emit(payload, args.json, lines)
    return 0


def add_own_parser(commands: argparse._SubParsersAction) -> None:
    own = commands.add_parser(
        "own",
        help="show and transfer operational stewardship of a project or room",
        description=(
            "Operational stewardship: which principal currently writes a project "
            "or a project/session room. A transfer takes two acts by two "
            "principals — the current steward offers, the named target accepts — "
            "under a monotonically increasing epoch with compare-and-set and an "
            "immutable receipt. This is not legal or IP ownership, not external "
            "authority, and not approval to act."
        ),
    )
    actions = own.add_subparsers(dest="own_action", required=True)

    show = actions.add_parser("show", help="report the current steward and any open offer")
    claim = actions.add_parser(
        "claim", help="become the first steward of an unowned project or room"
    )
    offer = actions.add_parser(
        "offer", help="offer stewardship to another principal (steward only)"
    )
    offer.add_argument("--to", required=True, help="target principal, e.g. cc-workspace")
    offer.add_argument(
        "--expires-in", type=int,
        help=f"offer lifetime in seconds (default {DEFAULT_OFFER_TTL})",
    )
    accept = actions.add_parser(
        "accept", help="accept an open offer (named target only)"
    )
    cancel = actions.add_parser(
        "cancel", help="withdraw or decline an open offer"
    )
    cancel.add_argument("--reason", help="one printable line recorded in the receipt")

    for command in (accept, cancel):
        command.add_argument("--if-offer", help="compare-and-set against the observed offer id")
    for command in (claim, offer, accept, cancel):
        command.add_argument(
            "--principal", required=True,
            help="the acting principal; never inferred from host, user, or model",
        )
        command.add_argument("--node", help="acting node id (default: the Atlas registry node)")
        command.add_argument(
            "--if-epoch", type=int,
            help="compare-and-set against the ownership epoch you observed",
        )
        command.add_argument(
            "--notify-tell", action="store_true",
            help="send the counterparty a Tell notice; a notice is never authorisation",
        )
        command.add_argument(
            "--tell-root", help="telld checkout holding src/tell/cli.py (default ~/telld)",
        )
    for command in (claim, offer, accept):
        command.add_argument("--note", help="one printable line recorded in the receipt")
    for command in (show, claim, offer, accept, cancel):
        command.add_argument("scope", help="'project' or 'project/session'")
        command.add_argument("--json", action="store_true")
