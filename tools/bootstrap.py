#!/usr/bin/env python3
"""Deterministic cold-agent bootstrap: ``atlas resume project/session``.

This is the first vertical slice of
``architecture/COLLECTIVE-AGENT-BOOTSTRAP-1.md``. A cold agent that knows only
a ``project/session`` runs one installed command and gets back one structured
receipt: who it is, which node and Collective it is on, where the memory came
from, whether re-entry is exact-native or semantic, who currently writes the
room, whether it was admitted, and whether live routing is degraded.

Three rules make it deterministic.

*No search.* Configuration comes from ``ATLAS_BOOTSTRAP_CONFIG`` or one fixed
system path. Nothing lists, globs, or walks ``$HOME``, and nothing infers the
principal from a hostname, a Unix user, or a model name. A missing field fails
closed naming the exact field and file.

*Honest re-entry.* A locally linked, locally supported native origin is
labelled ``exact-native-session``. Everything else — including every record
retrieved from the central store — is ``semantic-re-entry`` with
``native_transcript_resumption: false``. Cross-node transcript resumption does
not exist and is never implied.

*No silent second writer.* Bootstrap reports the existing steward. It admits a
writer only when the steward is this principal, when the scope is unowned and
``--claim`` was passed explicitly, or when an accepted transfer receipt
verifies. Otherwise it refuses and says who holds the room. Observers are
read-only; a builder must name its own distinct child room.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # executed as a script; the siblings are here
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import central_store
import domains
import ownership
import session_vault


RECEIPT_SCHEMA = "atlas.bootstrap.receipt.v1"
CONFIG_SCHEMA = "atlas.bootstrap.v1"
LAYOUT_VERSION = 1

CONFIG_ENVIRONMENT = "ATLAS_BOOTSTRAP_CONFIG"
PRINCIPAL_ENVIRONMENT = "ATLAS_PRINCIPAL"
SYSTEM_CONFIG_PATH = Path("/etc/demo-platform/atlas-bootstrap.json")

ROLES = ("observer", "coordinator", "builder", "takeover")
WRITER_ROLES = ("coordinator", "builder", "takeover")

REQUIRED_CONFIG_FIELDS = ("node", "registry", "work_root")
KNOWN_CONFIG_FIELDS = frozenset({
    "schema", "node", "collective", "registry", "work_root", "store_root",
    "cache_root", "tell_root", "bind_tell", "tell_ttl", "route_state_dir",
    "principal", "principal_file", "atlas_executable",
    "session_vault_executable", "description",
})

EXACT_NATIVE = "exact-native-session"
SEMANTIC = "semantic-re-entry"

AUTHORITY_NOTE = (
    "A bootstrap receipt reports identity, retrieved memory, and a coordination "
    "admission. It is not approval, assignment acceptance, deployment or spend "
    "permission, or proof that anything outside Atlas changed."
)
SEMANTIC_NOTE = (
    "Semantic re-entry from a portable record. Native transcript resumption "
    "does not cross nodes or runtimes; origins are pointers only."
)
EXACT_NATIVE_NOTE = (
    "A supported native origin is linked on this node. The runtime verifies and "
    "opens the transcript at resume time; bootstrap does not read runtime state."
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3


class BootstrapError(RuntimeError):
    """A user-facing bootstrap error."""


# --- configuration ------------------------------------------------------------


@dataclass(frozen=True)
class NodeConfig:
    path: Path
    node: str
    collective: str
    registry: Path
    work_root: Path
    store_root: Path | None
    cache_root: Path | None
    tell_root: Path | None
    bind_tell: bool
    tell_ttl: int
    route_state_dir: Path | None
    principal: str | None
    principal_file: Path | None
    atlas_executable: Path | None
    session_vault_executable: Path | None
    notices: tuple[str, ...] = field(default=())


def config_path(explicit: str | None) -> Path:
    """Resolve the trusted bootstrap configuration. Two places, then failure."""

    if explicit:
        candidate = Path(explicit)
    else:
        configured = os.environ.get(CONFIG_ENVIRONMENT, "").strip()
        candidate = Path(configured) if configured else SYSTEM_CONFIG_PATH
    if not candidate.is_absolute():
        raise BootstrapError(
            f"Bootstrap configuration path must be absolute: {candidate}"
        )
    if not candidate.is_file():
        raise BootstrapError(
            f"Bootstrap configuration not found: {candidate}. Set "
            f"{CONFIG_ENVIRONMENT} or install {SYSTEM_CONFIG_PATH}. "
            "Bootstrap never searches the home directory for one."
        )
    return candidate


def config_string(payload: dict, key: str, path: Path, required: bool) -> str | None:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BootstrapError(f"Bootstrap configuration {path} is missing '{key}'")
        return None
    if not isinstance(value, str):
        raise BootstrapError(f"Bootstrap configuration '{key}' must be a string: {path}")
    return value.strip()


def config_path_field(payload: dict, key: str, path: Path, required: bool) -> Path | None:
    raw = config_string(payload, key, path, required)
    if raw is None:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise BootstrapError(
            f"Bootstrap configuration '{key}' must be an absolute path: {raw} ({path})"
        )
    return candidate


def load_config(path: Path) -> NodeConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Invalid bootstrap configuration {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"Bootstrap configuration must be a JSON object: {path}")
    if payload.get("schema") not in (None, CONFIG_SCHEMA):
        raise BootstrapError(
            f"Unsupported bootstrap configuration schema at {path}: {payload.get('schema')!r}"
        )
    for key in REQUIRED_CONFIG_FIELDS:
        config_string(payload, key, path, required=True)

    notices = [
        f"unknown bootstrap configuration key ignored: {key}"
        for key in sorted(set(payload) - KNOWN_CONFIG_FIELDS)
    ]
    bind_tell = payload.get("bind_tell", False)
    if not isinstance(bind_tell, bool):
        raise BootstrapError(f"Bootstrap configuration 'bind_tell' must be a boolean: {path}")
    ttl = payload.get("tell_ttl", session_vault.TELL_DEFAULT_TTL)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= session_vault.TELL_MAX_TTL:
        raise BootstrapError(
            f"Bootstrap configuration 'tell_ttl' must be 1-{session_vault.TELL_MAX_TTL}: {path}"
        )

    registry = config_path_field(payload, "registry", path, required=True)
    work_root = config_path_field(payload, "work_root", path, required=True)
    assert registry is not None and work_root is not None  # required above
    if not registry.is_file():
        raise BootstrapError(
            f"Bootstrap configuration names a registry that does not exist: {registry} ({path})"
        )
    if not work_root.is_dir():
        raise BootstrapError(
            f"Bootstrap configuration names a work root that does not exist: "
            f"{work_root} ({path})"
        )

    principal_value = config_string(payload, "principal", path, required=False)
    return NodeConfig(
        path=path,
        node=ownership.safe_name(config_string(payload, "node", path, True), "node id"),
        collective=ownership.safe_name(
            config_string(payload, "collective", path, False)
            or central_store.DEFAULT_COLLECTIVE,
            "collective id",
        ),
        registry=registry,
        work_root=work_root,
        store_root=config_path_field(payload, "store_root", path, required=False),
        cache_root=config_path_field(payload, "cache_root", path, required=False),
        tell_root=config_path_field(payload, "tell_root", path, required=False),
        bind_tell=bind_tell,
        tell_ttl=ttl,
        route_state_dir=config_path_field(payload, "route_state_dir", path, required=False),
        principal=ownership.principal(principal_value) if principal_value else None,
        principal_file=config_path_field(payload, "principal_file", path, required=False),
        atlas_executable=config_path_field(payload, "atlas_executable", path, required=False),
        session_vault_executable=config_path_field(
            payload, "session_vault_executable", path, required=False
        ),
        notices=tuple(notices),
    )


def resolve_principal(explicit: str | None, config: NodeConfig) -> tuple[str, str]:
    """Launcher, then environment, then agent-account configuration. Never guessed."""

    if explicit:
        return ownership.principal(explicit, "principal"), "argument"
    from_environment = os.environ.get(PRINCIPAL_ENVIRONMENT, "").strip()
    if from_environment:
        return ownership.principal(from_environment, "principal"), "environment"
    if config.principal:
        return config.principal, "node-config"
    if config.principal_file is not None:
        if not config.principal_file.is_file():
            raise BootstrapError(
                f"Bootstrap configuration names a principal file that does not exist: "
                f"{config.principal_file} ({config.path})"
            )
        try:
            payload = json.loads(config.principal_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"Invalid principal file {config.principal_file}: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("principal"):
            raise BootstrapError(
                f"Principal file must be an object carrying 'principal': {config.principal_file}"
            )
        return ownership.principal(payload["principal"], "principal"), "principal-file"
    raise BootstrapError(
        "No enrolled principal. Pass --principal, export "
        f"{PRINCIPAL_ENVIRONMENT}, or add 'principal'/'principal_file' to "
        f"{config.path}. A principal is never inferred from hostname, Unix "
        "user, or model name."
    )


# --- helpers ------------------------------------------------------------------


def utc_now() -> str:
    return central_store.utc_now()


def age_seconds(value: object, observed: str) -> float | None:
    return central_store.checkpoint_age_seconds(
        value if isinstance(value, str) else None, observed
    )


def bounded_text(value: str | None, label: str, limit: int = 120) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if not raw.isprintable():
        raise BootstrapError(f"{label} must be one printable line")
    if len(raw) > limit:
        raise BootstrapError(f"{label} exceeds {limit} characters ({len(raw)})")
    return raw


def record_projection(record: Any, source: str, observed: str) -> dict[str, Any]:
    return {
        "identity": record.identity,
        "title": record.title,
        "status": record.metadata.get("status", ""),
        "updated": record.metadata.get("updated", ""),
        "path": str(record.path.resolve()),
        "source": source,
        "sha256": central_store.sha256_file(record.path),
        "bytes": record.path.stat().st_size,
        "checkpoint": session_vault.checkpoint_payload(record),
        "checkpoint_age_seconds": age_seconds(
            record.metadata.get("checkpoint_at"), observed
        ),
        "summary": session_vault.summary(record),
        "reported_context": central_store.REPORTED_CONTEXT_NOTE,
    }


# --- Tell route ---------------------------------------------------------------


def bind_route(
    config: NodeConfig, principal_id: str, identity: str, collective: str
) -> dict[str, Any]:
    """Bind the receiver-owned live route. Failure degrades; it never loses memory."""

    unbound = {
        "state": "unbound",
        "identity": identity,
        "expected": bool(config.bind_tell),
        "authority": session_vault.ROUTE_AUTHORITY_NOTE,
        "memory_retained": True,
    }
    if not config.bind_tell:
        return {**unbound, "reason": "tell binding is not enabled in the node configuration"}
    if config.tell_root is None:
        return {
            **unbound,
            "reason": (
                f"'tell_root' is not set in {config.path}; bootstrap never searches "
                "for a Tell client"
            ),
        }
    if config.route_state_dir is not None:
        os.environ["SESSION_VAULT_ROUTE_STATE"] = str(config.route_state_dir)
    try:
        root, client = session_vault.resolve_tell_client(config.tell_root)
        receipt = session_vault.tell_receipt(
            session_vault.run_tell(client, principal_id, [
                "session", "register",
                "--vault", identity,
                "--collective", collective,
                "--ttl", str(config.tell_ttl),
                "--label", identity,
            ]),
            "register",
        )
        session_id = receipt.get("session_id")
        if not isinstance(session_id, str) or not session_vault.TELL_SESSION_ID.fullmatch(
            session_id
        ):
            raise session_vault.VaultError(
                f"Tell register receipt has no usable session id: {receipt}"
            )
        state_path = session_vault.save_route_state(identity, principal_id, {
            "schema_version": session_vault.ROUTE_STATE_SCHEMA_VERSION,
            "kind": session_vault.ROUTE_STATE_KIND,
            "rebuildable": True,
            "source_of_truth": "tell-session-registry",
            "identity": identity,
            "principal": principal_id,
            "collective": collective,
            "session_id": session_id,
            "tell_root": str(root),
            "bound_at": utc_now(),
        })
    except session_vault.VaultError as exc:
        return {**unbound, "reason": str(exc)}
    return {
        "state": "bound",
        "identity": identity,
        "collective": collective,
        "session_id": session_id,
        "ttl_seconds": config.tell_ttl,
        "tell_root": str(root),
        "runtime_state_path": str(state_path),
        "authority": session_vault.ROUTE_AUTHORITY_NOTE,
        "memory_retained": True,
    }


# --- admission ----------------------------------------------------------------


def refusal(reason: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": "refused",
        "writer": False,
        "code": code,
        "reason": reason,
        **extra,
    }


def steward_projection(state: dict[str, Any], observed: str) -> dict[str, Any]:
    view = ownership.projection(state, observed)
    view["held_age_seconds"] = age_seconds(state.get("held_since"), observed)
    return view


def decide_admission(
    *,
    role: str,
    principal_id: str,
    node: str,
    source: str,
    identity: str,
    project: str,
    project_root: Path | None,
    child_session: str | None,
    slice_label: str | None,
    claim: bool,
    observed: str,
) -> dict[str, Any]:
    """Resolve the requested role against recorded stewardship, or refuse."""

    if role == "observer":
        # An observer still gets told who writes the room it is reading, so it
        # can never mistake retrieved memory for an invitation to write.
        steward = None
        if project_root is not None:
            steward = steward_projection(
                ownership.read_state(project_root, ownership.parse_scope(identity)), observed
            )
        return {
            "state": "admitted",
            "role": "observer",
            "writer": False,
            "room": identity,
            "parent_room": None,
            "slice": slice_label,
            "ownership": steward,
            "reason": (
                "observers read shared memory; they own no record and write nothing"
                + (f"; {identity} is written by {steward['owner']!r}"
                   if steward and steward.get("owner") else "")
            ),
        }

    if source != "local-session-vault" or project_root is None:
        return refusal(
            "This record was retrieved from the Collective central store into a "
            "verified read cache. A read cache is never a writer copy, so only "
            "'--role observer' is available here. Work the room on the node that "
            "owns the project, or open a distinct local builder room.",
            "remote-only-record",
            role=role, room=identity,
        )

    if role == "builder":
        if not child_session:
            return refusal(
                "A builder owns a distinct child room, never the parent "
                f"coordination record. Re-run with '--child-session <session>' "
                f"(for example '--child-session {project}-builder') so the parent "
                f"room {identity} keeps its single writer.",
                "builder-needs-child-room",
                role=role, room=None, parent_room=identity,
            )
        if child_session == identity.split("/", 1)[1]:
            return refusal(
                f"The builder child room must differ from the parent room {identity}",
                "builder-child-collides-with-parent",
                role=role, room=None, parent_room=identity,
            )
        room = f"{project}/{child_session}"
        parent_room: str | None = identity
    else:
        room = identity
        parent_room = None

    scope = ownership.parse_scope(room)
    if scope.session is not None:
        record_path = project_root / "sessions" / f"{scope.session}.md"
        if not record_path.is_file():
            return refusal(
                f"No Session Vault record for {room}: {record_path}. Create the "
                f"room first with 'session-vault start {room}', then re-run this "
                "bootstrap. Bootstrap never invents a room on someone's behalf.",
                "room-record-missing",
                role=role, room=room, parent_room=parent_room,
            )

    state = ownership.read_state(project_root, scope)
    steward = steward_projection(state, observed)
    common = {
        "role": role, "room": room, "parent_room": parent_room,
        "slice": slice_label, "ownership": steward,
    }

    if state["state"] == "unowned":
        if role == "takeover":
            return refusal(
                f"{room} has no steward and therefore no accepted transfer "
                "receipt. A takeover is only ever granted by an accepted "
                f"receipt. Claim it instead: 'atlas own claim {room} "
                f"--principal {principal_id}'.",
                "takeover-without-receipt", **common,
            )
        if not claim:
            return refusal(
                f"{room} has no recorded steward. Bootstrap does not silently "
                "install one. Re-run with '--claim' to become the first steward, "
                f"or run 'atlas own claim {room} --principal {principal_id}'.",
                "unowned-room", **common,
            )
        note = ownership.note_text(
            f"bootstrap claim: role={role}"
            + (f" slice={slice_label}" if slice_label else "")
        )
        result = ownership.commit_event(
            project_root, scope, event="claim",
            actor=principal_id, node=node, expected_epoch=0, note=note,
        )
        return {
            "state": "admitted",
            "writer": True,
            "reason": f"{principal_id} claimed the unowned room {room} at epoch "
                      f"{result['state']['epoch']}",
            "claimed": True,
            "receipt_path": result["receipt_path"],
            **{**common, "ownership": steward_projection(result["state"], observed)},
        }

    owner = state["owner"]
    if owner != principal_id:
        return refusal(
            f"{room} is stewarded by {owner!r} at epoch {state['epoch']}. "
            "Atlas admits one writer per record. Either open a distinct builder "
            f"child room ('--role builder --child-session <session>'), or ask "
            f"{owner!r} to run 'atlas own offer {room} --principal {owner} --to "
            f"{principal_id}' and then accept it with 'atlas own accept {room} "
            f"--principal {principal_id}'.",
            "writer-conflict", **common,
        )

    if role == "takeover":
        try:
            receipt = ownership.verify_transfer_receipt(project_root, scope, state)
        except ownership.OwnershipError as exc:
            return refusal(
                f"Takeover refused for {room}: {exc}", "takeover-receipt-invalid", **common,
            )
        return {
            "state": "admitted",
            "writer": True,
            "reason": (
                f"{principal_id} holds {room} through accepted transfer receipt "
                f"{receipt['receipt_id']} at epoch {receipt['epoch_after']}"
            ),
            "takeover_receipt": state["last_receipt"],
            "takeover_receipt_id": receipt["receipt_id"],
            **common,
        }

    reason = f"{principal_id} is the recorded steward of {room} at epoch {state['epoch']}"
    if state["state"] == "offered":
        reason += (
            f"; an offer to {state['offer']['to']!r} is open — cancel it if you "
            "intend to keep writing"
        )
    return {"state": "admitted", "writer": True, "reason": reason, **common}


# --- command ------------------------------------------------------------------


def command_resume(args: argparse.Namespace) -> int:
    """Run the bootstrap, reporting every underlying refusal as a bootstrap error.

    A caller of this command should never have to reason about which layer
    raised: an unreadable registry, an unsafe identity, an invalid manifest
    domain, and a refused stewardship write all surface as one exact,
    actionable bootstrap error.
    """

    try:
        return resume(args)
    except (
        session_vault.VaultError,
        central_store.StoreError,
        ownership.OwnershipError,
        domains.DomainError,
    ) as exc:
        raise BootstrapError(str(exc)) from exc


def resume(args: argparse.Namespace) -> int:
    observed = utc_now()
    path = config_path(args.config)
    config = load_config(path)
    principal_id, principal_source = resolve_principal(args.principal, config)
    collective = ownership.safe_name(args.collective or config.collective, "collective id")
    role = args.role
    slice_label = bounded_text(args.slice_label, "--slice")
    runtime = ownership.safe_name(args.runtime, "runtime id") if args.runtime else None
    child_session = (
        ownership.safe_name(args.child_session, "child session name")
        if args.child_session else None
    )

    try:
        project, session = central_store.parse_identity(args.identity)
    except central_store.StoreError as exc:
        raise BootstrapError(str(exc)) from exc
    identity = f"{project}/{session}"

    degraded: list[str] = list(config.notices)

    registry_payload = session_vault.load_registry(config.registry)
    registry_node = session_vault.registry_node(registry_payload)
    if registry_node != config.node:
        degraded.append(
            f"node-mismatch: bootstrap configuration says {config.node!r} while "
            f"{config.registry} says {registry_node!r}; using the configured node"
        )
    projects = session_vault.load_projects(registry_payload, config.registry)
    all_records, load_errors = session_vault.records(projects)
    degraded.extend(f"record-unreadable: {error}" for error in load_errors)

    matches = [
        item for item in all_records
        if item.project.id == project and item.session == session
    ]
    if len(matches) > 1:
        raise BootstrapError(
            f"Ambiguous local record for {identity}: "
            + ", ".join(str(item.path) for item in matches)
        )

    central: dict[str, Any] | None = None
    if matches:
        record = matches[0]
        project_root = record.project.root.expanduser().resolve()
        source = "local-session-vault"
        try:
            domain = domains.resolve_domain(project_root)
        except domains.DomainError as exc:
            raise BootstrapError(str(exc)) from exc
        try:
            origin = session_vault.local_resume_origin(record, config.node)
        except session_vault.VaultError as exc:
            origin, origin_note = None, str(exc)
        else:
            origin_note = EXACT_NATIVE_NOTE
        memory = record_projection(record, source, observed)
    else:
        record, project_root, origin = None, None, None
        source = "central-store"
        origin_note = SEMANTIC_NOTE
        if config.store_root is None:
            raise BootstrapError(
                f"{identity} is not registered locally and {config.path} has no "
                "'store_root', so there is nowhere to look it up. Register the "
                "project on this node or configure the Collective store root."
            )
        if config.cache_root is None:
            raise BootstrapError(
                f"{identity} is only available centrally and {config.path} has no "
                "'cache_root'. A verified read cache path must be configured; "
                "bootstrap never guesses one under the home directory."
            )
        try:
            store_root = central_store.store_root_path(config.store_root)
            namespace = central_store.Namespace(store_root, collective)
            entry = central_store.catalog_entry(namespace, project, session, observed)
            if entry is None:
                raise BootstrapError(
                    f"{identity} is neither a registered local record nor a current "
                    f"room in the {collective} central store at {store_root}. "
                    "Check the identity, or ask its owner to publish it."
                )
            pull = central_store.pull_into_cache(
                identity, config.store_root, collective, config.cache_root, projects
            )
        except central_store.StoreError as exc:
            raise BootstrapError(str(exc)) from exc
        central = {"catalog": entry, "pull": pull}
        domain = entry.get("domain")
        cached = Path(pull["cache_dir"]) / f"{session}.md"
        cached_record = session_vault.parse_record(
            session_vault.Project(
                id=project, name=project, root=Path(pull["cache_dir"]), aliases=()
            ),
            cached,
        )
        memory = record_projection(cached_record, source, observed)
        memory["identity"] = identity
        memory["central_record_sha256"] = pull["record_sha256"]
        memory["verified"] = memory["sha256"] == pull["record_sha256"]
        if not memory["verified"]:
            raise BootstrapError(
                f"Cached record for {identity} does not match the verified central "
                "digest; refusing to report it as memory."
            )

    exact_native = origin is not None
    admission = decide_admission(
        role=role,
        principal_id=principal_id,
        node=config.node,
        source=source,
        identity=identity,
        project=project,
        project_root=project_root,
        child_session=child_session,
        slice_label=slice_label,
        claim=bool(args.claim),
        observed=observed,
    )

    project_ownership: dict[str, Any] | None = None
    if project_root is not None:
        project_ownership = steward_projection(
            ownership.read_state(project_root, ownership.parse_scope(project)), observed
        )

    route: dict[str, Any]
    if admission["state"] == "admitted":
        route = bind_route(
            config, principal_id, admission.get("room") or identity, collective
        )
        if route["state"] != "bound" and config.bind_tell:
            degraded.append(f"tell-unbound: {route.get('reason', 'unknown')}")
    else:
        route = {
            "state": "unbound",
            "expected": False,
            "reason": "no room was admitted, so no live route was bound",
            "authority": session_vault.ROUTE_AUTHORITY_NOTE,
            "memory_retained": True,
        }

    work_root: dict[str, Any] = {"path": str(config.work_root), "exists": True}
    if admission.get("room") and admission.get("role") == "builder":
        bounded = config.work_root / (child_session or "")
        work_root["bounded_path"] = str(bounded)
        work_root["bounded_exists"] = bounded.is_dir()
        work_root["note"] = "bootstrap reports the bounded work root; it never creates it"

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "action": "resume",
        "requested_identity": args.identity,
        "identity": identity,
        "resolved_at": observed,
        "config_path": str(config.path),
        "node": config.node,
        "registry": str(config.registry),
        "collective": collective,
        "principal": principal_id,
        "principal_source": principal_source,
        "runtime": runtime,
        "domain": domain,
        "domain_label": domains.domain_label(domain),
        "domain_source": "project manifest" if source == "local-session-vault"
        else "central publish manifest snapshot",
        "source": source,
        "mode": EXACT_NATIVE if exact_native else SEMANTIC,
        "native_transcript_resumption": exact_native,
        "reentry_note": origin_note,
        "native_origin": origin,
        "native_origin_verified": False,
        "memory": memory,
        "central": central,
        "ownership": {
            "room": admission.get("ownership"),
            "project": project_ownership,
            "available": project_root is not None,
            "authority": ownership.AUTHORITY_NOTE,
        },
        "admission": admission,
        "route": route,
        "work_root": work_root,
        "atlas_executable": str(config.atlas_executable) if config.atlas_executable else None,
        "session_vault_executable": (
            str(config.session_vault_executable) if config.session_vault_executable else None
        ),
        "degraded": degraded,
        "authority": AUTHORITY_NOTE,
    }
    receipt["next_actions"] = next_actions(receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return EXIT_OK if admission["state"] == "admitted" else EXIT_REFUSED


def next_actions(receipt: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    admission = receipt["admission"]
    identity = receipt["identity"]
    room = admission.get("room") or identity
    if admission["state"] == "refused":
        actions.append(admission["reason"])
        return actions
    if receipt["mode"] == EXACT_NATIVE:
        actions.append(f"session-vault resume {identity}")
    else:
        actions.append(f"read the portable record at {receipt['memory']['path']}")
    if admission["writer"]:
        actions.append(
            f"checkpoint your own room: session-vault checkpoint {room} "
            f"--reporter {receipt['principal']} --state working --next '<next action>'"
        )
    else:
        actions.append("observer: read only; do not checkpoint or publish this room")
    if receipt["route"].get("expected") and receipt["route"]["state"] != "bound":
        actions.append(f"live routing is degraded: {receipt['route']['reason']}")
    return actions


def add_resume_parser(commands: argparse._SubParsersAction) -> None:
    resume = commands.add_parser(
        "resume",
        help="cold-agent bootstrap: resolve identity, memory, stewardship, and routing",
        description=(
            "One deterministic entrypoint for an agent that knows only a "
            "project/session. Reads the trusted node bootstrap configuration "
            "(ATLAS_BOOTSTRAP_CONFIG, else /etc/demo-platform/atlas-bootstrap.json), "
            "resolves the enrolled principal, node, Collective, central store and "
            "read cache, and work root, resumes the local Session Vault record or "
            "falls back to an exact central catalog lookup and verified pull, "
            "reports exact-native versus semantic re-entry honestly, resolves "
            "stewardship without silently creating a second writer, binds the "
            "receiver-owned Tell route when configured, and emits one JSON receipt. "
            "It never searches the home directory."
        ),
    )
    resume.add_argument("identity", help="composite identity project/session")
    resume.add_argument(
        "--principal",
        help="the enrolled principal supplied by the launcher; never inferred",
    )
    resume.add_argument(
        "--role", choices=ROLES, default="observer",
        help="observer (read-only), coordinator, builder, or takeover (default: observer)",
    )
    resume.add_argument(
        "--child-session",
        help="required for --role builder: the distinct child room this builder owns",
    )
    resume.add_argument("--slice", dest="slice_label", help="bounded implementation slice label")
    resume.add_argument("--runtime", help="runtime id for attribution, e.g. claude or codex")
    resume.add_argument("--collective", help="override the configured Collective id")
    resume.add_argument(
        "--claim", action="store_true",
        help="become the first steward when the room has none; never displaces one",
    )
    resume.add_argument(
        "--config", help=f"explicit bootstrap configuration path (default: ${CONFIG_ENVIRONMENT})"
    )
