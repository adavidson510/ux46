"""Project and session discovery for the UX46 console.

Everything here comes from the existing registry and Session Vault records on
disk — the console never invents a project, a session or a native target, and
never asks the browser for one. Rooms are refreshed from disk, so a session
created elsewhere shows up without anyone re-registering it here.

Capability labels are deliberately conservative: a room is controllable only
when its primary origin is a Codex session recorded on this node. Claude and
remote records stay visible with portable memory and no pretend control.

Several Vault records often point at one native conversation. Grouping them by
native identity is what turns "460 sessions" back into the handful of actual
conversations a person had, without deleting or reclassifying any record.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

import atlas_native_meta as meta
import session_vault as sv

CONTROLLABLE = "codex-local"
CLAUDE = "claude"
REMOTE = "remote"
VAULT_ONLY = "vault-only"

# Short words for the console chrome; the long sentence stays available for
# the connection-details view.
CAPABILITY_SHORT = {
    CONTROLLABLE: "Codex on this Mac",
    CLAUDE: "Claude Code · no control",
    REMOTE: "another node · no control",
    VAULT_ONLY: "no native session",
}

CAPABILITY_LABEL = {
    CONTROLLABLE: "Codex on this machine — readable and controllable",
    CLAUDE: "Claude Code — portable memory only, no control from UX46",
    REMOTE: "recorded on another node — portable memory only, no control from UX46",
    VAULT_ONLY: "no native origin recorded — portable memory only",
}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Session records may declare that they are a worker/sub-session. Nothing is
# inferred from substrings of a name.
WORKER_FIELDS = ("role", "kind", "session_role")
WORKER_VALUES = {"worker", "sub-agent", "subagent", "child"}


@dataclass(frozen=True)
class Room:
    """One Vault record, plus whatever native target it honestly points at."""

    project_id: str
    project_name: str
    project_root: str
    session: str
    title: str
    status: str
    updated: str
    capability: str
    thread_id: str
    cwd: str
    runtime: str
    node: str
    origin_count: int
    origins_status: str
    checkpoint: dict | None
    attention: dict
    worker_provenance: str
    record_path: str
    keywords: str
    aliases: tuple[str, ...] = ()
    native_role: str = meta.UNKNOWN
    native_provenance: str = ""
    native_updated_ms: int = 0
    native_archived: bool = False
    native_parent: str = ""
    native_names_me: bool = False
    chapter_parent: str = ""
    chapter_mode: str = ""

    @property
    def id(self) -> str:
        return f"{self.project_id}/{self.session}"

    @property
    def controllable(self) -> bool:
        return self.capability == CONTROLLABLE and bool(self.thread_id)

    @property
    def identity_key(self) -> str:
        """What counts as one conversation.

        A native thread id is only unique within the runtime and node that
        issued it, so the identity carries both. Without a native session the
        record is its own conversation and nothing is merged into it.
        """

        if not self.thread_id:
            return f"record:{self.id}"
        return f"{self.runtime or 'unknown'}@{self.node or 'unknown'}:{self.thread_id}"

    @property
    def conversation_key(self) -> str:
        """Identity as the console groups it: within one project only.

        Two projects that happen to reference one native thread stay two rows.
        A preference is applied to exactly the records that are grouped, so a
        hidden conversation can never come back under a sibling record.
        """

        return f"{self.project_id}/{self.identity_key}"

    @property
    def last_active(self) -> str:
        """The best dated fact available, said plainly."""

        if self.native_updated_ms:
            return _iso_day(self.native_updated_ms)
        return self.updated

    @property
    def recency_source(self) -> str:
        return "native" if self.native_updated_ms else ("record" if self.updated else "none")

    @property
    def recency_key(self) -> tuple:
        return (self.native_updated_ms, self.updated or "", self.session)

    def as_json(self, *, full: bool = False) -> dict:
        payload = {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "session": self.session,
            "title": self.title,
            "status": self.status,
            "updated": self.updated,
            "capability": self.capability,
            "capability_label": CAPABILITY_LABEL.get(self.capability, self.capability),
            "capability_short": CAPABILITY_SHORT.get(self.capability, self.capability),
            "controllable": self.controllable,
            "runtime": self.runtime,
            "node": self.node,
            "origin_count": self.origin_count,
            "origins_status": self.origins_status,
            "worker": bool(self.worker_provenance),
            "worker_provenance": self.worker_provenance,
            "identity_key": self.identity_key,
            "conversation_key": self.conversation_key,
            "last_active": self.last_active,
            "recency_source": self.recency_source,
            # human / worker / unknown — unknown is never read as either.
            "native_role": self.native_role,
            "native_provenance": self.native_provenance,
            "native_archived": self.native_archived,
            "record_aliases": list(self.aliases),
            "chapter_parent": self.chapter_parent,
            "chapter_mode": self.chapter_mode,
            "attention": self.attention,
            # Reported by whoever wrote the checkpoint, at that time. Not a
            # live verification of what the runtime is doing now.
            "checkpoint": self.checkpoint,
        }
        if full:
            payload.update({
                "thread_id": self.thread_id,
                "cwd": self.cwd,
                "project_root": self.project_root,
                "record_path": self.record_path,
            })
        return payload


@dataclass(frozen=True)
class _Catalog:
    node: str
    projects: tuple[sv.Project, ...]
    rooms: tuple[Room, ...]
    errors: tuple[str, ...]
    loaded_at: float


class Discovery:
    """Cached view over the registry and Vault, refreshed from disk."""

    def __init__(
        self,
        registry_path: Path | None = None,
        ttl: float = 15.0,
        native_db: Path | str | None = None,
        exploration_root: Path | None = None,
    ):
        self.registry_path = Path(registry_path) if registry_path else sv.default_registry()
        self.ttl = ttl
        # Private unfiled conversation metadata. This is not registered as a
        # user project and contributes no existing project context.
        self.exploration_root = exploration_root
        # One batched, read-only pass over the native catalogue per refresh.
        # No per-record RPC, no transcript, no runtime call.
        self.native = meta.NativeMetadata(native_db)
        # Only publication/scheduling uses _lock. Disk scans are serialized by
        # _refresh_lock so readers can keep using the last complete catalogue.
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._catalog: _Catalog | None = None
        self._refreshing = False
        self._retry_at = 0.0
        self._refresh_error = ""

    # -- loading -----------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and self._catalog is not None:
                now = time.monotonic()
                if (now - self._catalog.loaded_at >= self.ttl
                        and now >= self._retry_at and not self._refreshing):
                    self._refreshing = True
                    threading.Thread(target=self._refresh_background,
                                     name="ux46-discovery-refresh", daemon=True).start()
                return
        # First load and explicit refreshes must return a current catalogue.
        # A forced refresh waits for an older scan, then reads disk again: an
        # in-flight scan cannot overwrite a newly created/linked room.
        with self._refresh_lock:
            with self._lock:
                if not force and self._catalog is not None:
                    return
            self._publish(self._load_catalog())

    def _refresh_background(self) -> None:
        try:
            with self._refresh_lock:
                try:
                    self._publish(self._load_catalog())
                except Exception as exc:
                    # Keep the complete previous catalogue and expose the failed
                    # scan. Back off even with ttl=0 so bootstrap polls cannot
                    # start a tight retry loop while the registry is unavailable.
                    with self._lock:
                        self._refresh_error = f"Discovery refresh failed: {exc}"
                        self._retry_at = time.monotonic() + max(self.ttl, 1.0)
        finally:
            with self._lock:
                self._refreshing = False

    def _publish(self, catalog: _Catalog) -> None:
        with self._lock:
            self._catalog = catalog
            self._refresh_error = ""
            self._retry_at = 0.0

    def _load_catalog(self) -> _Catalog:
        payload = sv.load_registry(self.registry_path)
        node = sv.registry_node(payload)
        projects = sv.load_projects(payload, self.registry_path)
        if self.exploration_root is not None:
            if any(p.id == "ux46-explorations" for p in projects):
                raise sv.VaultError("Reserved exploration namespace conflicts with a project")
            projects.append(sv.Project("ux46-explorations", "Explorations",
                                       self.exploration_root, ()))
        records, errors = sv.records(projects)
        drafts = [self._room_for(record, node) for record in records]
        native = self.native.lookup(room.thread_id for room in drafts)
        rooms = [_with_native(room, native.get(room.thread_id)) for room in drafts]
        rooms.sort(
            key=lambda room: (
                room.last_active or "", room.native_updated_ms,
                room.updated or "", room.session,
            ),
            reverse=True,
        )
        return _Catalog(node, tuple(projects), tuple(rooms), tuple(errors), time.monotonic())

    def _current(self) -> _Catalog:
        self.refresh()
        assert self._catalog is not None
        return self._catalog

    @property
    def _projects(self) -> tuple[sv.Project, ...]:
        # Existing session-creation callers explicitly refresh before reading.
        return self._catalog.projects if self._catalog is not None else ()

    def _room_for(self, record: sv.Record, node: str) -> Room:
        try:
            origins = sv.load_origins(record)["origins"]
            origins_status = "valid"
        except sv.VaultError:
            origins, origins_status = [], "invalid"

        chosen = _primary_origin(origins, node)
        capability, thread_id, cwd, runtime, node = _capability(chosen, node)
        checkpoint = sv.checkpoint_payload(record)
        attention = sv.attention_projection(record)
        return Room(
            project_id=record.project.id,
            project_name=record.project.name,
            project_root=str(record.project.root),
            session=record.session,
            title=record.title,
            status=record.metadata.get("status", ""),
            updated=record.metadata.get("updated", "") or record.metadata.get("date", ""),
            capability=capability,
            thread_id=thread_id,
            cwd=cwd,
            runtime=runtime,
            node=node,
            origin_count=len(origins),
            origins_status=origins_status,
            checkpoint=checkpoint,
            attention=attention,
            worker_provenance=_worker_provenance(record),
            record_path=str(record.path),
            keywords=record.metadata.get("keywords", ""),
            aliases=record.aliases,
            chapter_parent=record.metadata.get("chapter_parent", ""),
            chapter_mode=record.metadata.get("chapter_mode", ""),
        )

    # -- queries -----------------------------------------------------------
    @property
    def node(self) -> str:
        return self._current().node

    @property
    def errors(self) -> list[str]:
        self.refresh()
        with self._lock:
            assert self._catalog is not None
            return ([self._refresh_error] if self._refresh_error else []) + list(self._catalog.errors)

    def rooms(self) -> list[Room]:
        return list(self._current().rooms)

    def room(self, room_id: str) -> Room | None:
        for room in self._current().rooms:
            if room.id == room_id:
                return room
        return None

    def conversation(self, room: Room) -> list[Room]:
        """Every record of one conversation, canonical representative first.

        Scoped to that room's own project and to its runtime and node, so a
        preference never reaches an unrelated group.
        """

        members = [
            other for other in self.rooms()
            if other.conversation_key == room.conversation_key
        ]
        return sorted(members, key=_representative_key, reverse=True) or [room]

    def rooms_for_thread(self, thread_id: str) -> list[Room]:
        if not thread_id:
            return []
        return [room for room in self.rooms() if room.thread_id == thread_id]

    def projects(self) -> list[dict]:
        catalog = self._current()
        rooms = catalog.rooms
        by_project: dict[str, dict] = {}
        for project in catalog.projects:
            by_project[project.id] = {
                "id": project.id,
                "name": project.name,
                "root": str(project.root),
                "sessions": 0,
                "controllable": 0,
                "needs_person": 0,
                "updated": "",
            }
        for room in rooms:
            entry = by_project.setdefault(room.project_id, {
                "id": room.project_id,
                "name": room.project_name,
                "root": room.project_root,
                "sessions": 0,
                "controllable": 0,
                "needs_person": 0,
                "updated": "",
            })
            entry["sessions"] += 1
            if room.controllable:
                entry["controllable"] += 1
            if str(room.attention.get("state")) in ("needs_now", "review_ready"):
                entry["needs_person"] += 1
            if room.updated > entry["updated"]:
                entry["updated"] = room.updated
        return sorted(
            by_project.values(),
            key=lambda p: (p["updated"], p["sessions"]),
            reverse=True,
        )

    def search(
        self,
        query: str = "",
        project: str = "",
        *,
        ids: list[str] | None = None,
        include_workers: bool = False,
        only_workers: bool = False,
        include_complete: bool = True,
        group: bool = False,
        limit: int = 40,
        offset: int = 0,
    ) -> dict:
        rooms = self.rooms()
        if ids:
            # An exact lookup of remembered rooms: no filters, no ranking, and
            # the caller's order is preserved.
            found = {room.id: room for room in rooms}
            picked = [found[room_id] for room_id in dict.fromkeys(ids) if room_id in found]
            return {
                "total": len(picked), "offset": 0, "returned": len(picked),
                "grouped": False, "rooms": [room.as_json() for room in picked],
                "truncated": False,
            }
        if project:
            rooms = [room for room in rooms if room.project_id == project]
        if only_workers:
            rooms = [room for room in rooms if room.worker_provenance]
        elif not include_workers:
            rooms = [room for room in rooms if not room.worker_provenance]
        if not include_complete:
            rooms = [room for room in rooms if room.status != "complete"]
        query = (query or "").strip()
        if query:
            needle = query.casefold()
            scored: list[tuple[float, Room]] = []
            for room in rooms:
                haystack = " ".join([
                    room.id, room.title, room.keywords, room.project_name, room.session,
                    room.thread_id, *room.aliases,
                ]).casefold()
                if needle in haystack:
                    # Exact identity first, then title, then anything else.
                    weight = 3.0 if needle in room.id.casefold() else (
                        2.0 if needle in room.title.casefold() else 1.0
                    )
                    scored.append((weight, room))
            scored.sort(key=lambda pair: (pair[0], pair[1].updated), reverse=True)
            rooms = [room for _, room in scored]
        if group:
            # One native conversation is one row. Its other Vault records stay
            # listed as aliases rather than as separate chats.
            groups = group_rooms(rooms)
            if query:
                # Grouping must not replace query relevance with date order.
                # The best matching record ranks the entire conversation;
                # representative selection within it remains unchanged.
                ranks = {room.id: index for index, room in enumerate(rooms)}
                groups.sort(key=lambda pair: min(ranks[room.id] for room in pair[1]))
            payloads = [_group_json(members) for _key, members in groups]
        else:
            payloads = None
        total = len(payloads) if payloads is not None else len(rooms)
        size = max(1, min(int(limit), 200))
        if payloads is not None:
            window = payloads[offset:offset + size]
        else:
            window = [room.as_json() for room in rooms[offset:offset + size]]
        return {
            "total": total,
            "offset": offset,
            "returned": len(window),
            "grouped": bool(group),
            "rooms": window,
            "truncated": offset + len(window) < total,
        }

    # -- the working set ---------------------------------------------------
    def workspace(self, prefs: dict | None = None, *, suggest: int = 2) -> dict:
        """One or two conversations per project, plus what the person kept.

        The catalogue is not the working set. This projection answers "what
        would you most likely resume here", and says why for each row. Every
        other record stays reachable through history and search — nothing is
        deleted, reclassified or hidden from a query.
        """

        prefs = prefs or {}
        catalog = self._current()
        rooms = catalog.rooms
        by_project: dict[str, list[Room]] = {}
        for room in rooms:
            by_project.setdefault(room.project_id, []).append(room)
        known = {project.id: project for project in catalog.projects}
        projects: list[dict] = []
        for project_id, members in by_project.items():
            project = known.get(project_id)
            projects.append(_project_workspace(
                project_id=project_id,
                name=project.name if project else members[0].project_name,
                root=str(project.root) if project else members[0].project_root,
                rooms=members,
                prefs=prefs,
                suggest=suggest,
            ))
        for project_id, project in known.items():
            if project_id in by_project:
                continue
            projects.append({
                "id": project_id, "name": project.name, "root": str(project.root),
                "last_active": "", "recency_source": "none",
                "pinned": [], "suggested": [],
                "more_total": 0, "agent_work_total": 0, "hidden_total": 0,
                "record_total": 0, "conversation_total": 0,
            })
        projects.sort(key=lambda entry: (entry["last_active"], entry["name"]), reverse=True)
        return {
            "projects": projects,
            "suggest_limit": suggest,
            "native_metadata": self.native.status()["available"],
        }


SUGGEST_REASONS = {
    "current_chapter": "current chapter",
    "pinned": "you pinned this",
    "recent": "most recent conversation here",
    "also_recent": "recently active here",
    "open": "open right now",
}

# A checkpoint older than this is presented as dated history rather than as a
# live obligation. It is still shown, still dated, still searchable.
STALE_CHECKPOINT_DAYS = 14


def _iso_day(updated_ms: int) -> str:
    """A calendar day from a native millisecond stamp, in local time."""

    try:
        return time.strftime("%Y-%m-%d", time.localtime(updated_ms / 1000))
    except (OverflowError, OSError, ValueError):
        return ""


def _with_native(room: Room, thread: meta.ThreadMeta | None) -> Room:
    """Attach what the native catalogue actually said, and nothing more."""

    if thread is None:
        return room
    provenance = room.worker_provenance
    if not provenance and thread.role == meta.WORKER:
        provenance = thread.provenance
    return replace(
        room,
        worker_provenance=provenance,
        native_role=thread.role,
        native_provenance=thread.provenance,
        native_updated_ms=thread.updated_ms,
        native_archived=thread.archived,
        native_parent=thread.parent_thread_id,
        # Presentation only: when a person named this exact room when they
        # started the thread, that room is the better face for it. The title
        # itself never leaves this process.
        native_names_me=thread.mentions(room.id) or thread.mentions(room.session),
    )


def checkpoint_age_days(room: Room, now: float | None = None) -> float | None:
    """How old the saved report is, or None when it is not dated."""

    stamp = str((room.attention or {}).get("checkpoint_at") or "")
    if not stamp:
        return None
    try:
        at = sv.parse_checkpoint_at(stamp)
    except ValueError:
        return None
    reference = now if now is not None else time.time()
    return max(0.0, (reference - at.timestamp()) / 86400.0)


def group_rooms(rooms: list[Room]) -> list[tuple[str, list[Room]]]:
    """Group records by the conversation they point at, newest group first.

    Records that name no native session are their own group: two unrelated
    portable records are never merged just because UX46 cannot see a thread.
    """

    groups: dict[str, list[Room]] = {}
    # Sorting by name first makes every later tie resolve the same way, since
    # Python's sort is stable: equal dates keep the earliest session name.
    for room in sorted(rooms, key=lambda room: room.session):
        groups.setdefault(room.conversation_key, []).append(room)
    ordered = []
    for key, members in groups.items():
        members = sorted(members, key=_representative_key, reverse=True)
        ordered.append((key, members))
    ordered.sort(key=lambda pair: _recency_key(pair[1][0]), reverse=True)
    return ordered


def _representative_key(room: Room) -> tuple:
    """Which record is the best face for one conversation.

    The native title hint only decides between records of the *same* thread;
    it never lets one conversation outrank another, because what someone typed
    when they started a thread is not evidence about a different thread.
    """

    return (1 if room.native_names_me else 0, *_live_key(room))


def _recency_key(room: Room) -> tuple:
    """Pure date order, so history reads as history."""

    return (room.last_active or "", room.native_updated_ms, room.updated or "")


def _live_key(room: Room) -> tuple:
    """Date order, with finished work below work that is still open."""

    return (0 if room.status == "complete" else 1, *_recency_key(room))


def group_role(members: list[Room]) -> str:
    """A conversation is only a worker's when the runtime says every record of
    it is. One proven human record settles it the other way."""

    roles = {room.native_role for room in members}
    if any(room.native_role == meta.HUMAN for room in members):
        return meta.HUMAN
    if members and all(room.worker_provenance for room in members):
        return meta.WORKER
    if roles == {meta.UNKNOWN} or not roles:
        return meta.UNKNOWN
    return meta.UNKNOWN


def _group_json(members: list[Room], reason: str = "", *,
                pinned: bool | None = None, hidden: bool | None = None) -> dict:
    payload = members[0].as_json()
    if pinned is not None:
        payload["pinned"] = pinned
    if hidden is not None:
        payload["hidden"] = hidden
    payload["aliases"] = [room.id for room in members[1:]]
    payload["alias_count"] = len(members) - 1
    payload["group_role"] = group_role(members)
    if reason:
        payload["reason"] = reason
        payload["reason_label"] = SUGGEST_REASONS.get(reason, reason)
    return payload


def group_pref(members: list[Room], prefs: dict) -> dict:
    """Resolve one conversation's preference from the records that make it up.

    Preferences are stored per record, because a record is what a person can
    point at. They are *read* per conversation, so hiding one record hides the
    conversation rather than handing it back under a sibling record, and
    several pinned records render one row.

    A pin and a hide on one conversation contradict each other. The more
    recent explicit act wins; with no timestamps to compare, a pin is read as
    the deliberate keep it is.
    """

    hidden = False
    hidden_at = 0.0
    pinned_at = 0.0
    pinned_members: list[Room] = []
    for room in members:
        pref = prefs.get(room.id) or {}
        if pref.get("hidden"):
            hidden = True
            hidden_at = max(hidden_at, float(pref.get("updated_at") or 0.0))
        if pref.get("pinned"):
            pinned_members.append(room)
            pinned_at = max(pinned_at, float(pref.get("pinned_at") or 0.0))
    pinned = bool(pinned_members)
    if hidden and pinned:
        hidden = hidden_at > pinned_at
    # One explicitly pinned record is a chosen face; otherwise the canonical
    # representative of the conversation speaks for it.
    face = pinned_members[0] if len(pinned_members) == 1 else members[0]
    return {"pinned": pinned, "hidden": hidden, "face": face}


def _ordered(members: list[Room], face: Room) -> list[Room]:
    return [face, *[room for room in members if room.id != face.id]]


def _project_workspace(
    *, project_id: str, name: str, root: str, rooms: list[Room], prefs: dict, suggest: int
) -> dict:
    groups = group_rooms(rooms)
    resolved = [(key, members, group_pref(members, prefs)) for key, members in groups]
    hidden_total = sum(1 for _key, _members, pref in resolved if pref["hidden"])

    pinned_rows = [
        _group_json(_ordered(members, pref["face"]), "pinned", pinned=True, hidden=False)
        for _key, members, pref in resolved
        if pref["pinned"] and not pref["hidden"]
    ]
    candidates = [
        (key, members) for key, members, pref in resolved
        if not pref["pinned"] and not pref["hidden"]
        and group_role(members) != meta.WORKER
    ]
    # A successful replacement chapter is the chosen continuation, even when
    # the native catalogue has not classified its fresh app-server thread.
    # Keep native role truthful and retain predecessors in history. Parallel
    # chapters do not supersede their parents; pins/hidden choices still win.
    by_id = {room.id: room for room in rooms}
    successors = {
        room.id for room in rooms
        if room.chapter_mode == "replace" and room.chapter_parent in by_id
        and room.identity_key != by_id[room.chapter_parent].identity_key
        and room.thread_id and room.origins_status == "valid"
        and not room.worker_provenance and room.native_role != meta.WORKER
    }
    predecessors = {by_id[identity].chapter_parent for identity in successors}
    current = {identity for identity in successors - predecessors
               if by_id[identity].status == "active" and not by_id[identity].native_archived}

    def chapter_rank(members):
        ids = {room.id for room in members}
        return 1 if ids & current else (-1 if ids & predecessors else 0)

    candidates.sort(
        key=lambda pair: (
            chapter_rank(pair[1]),
            1 if any(room.controllable for room in pair[1]) else 0,
            1 if group_role(pair[1]) == meta.HUMAN else 0,
            _live_key(pair[1][0]),
        ),
        reverse=True,
    )
    picked = candidates[: max(0, int(suggest))]
    suggested = [
        _group_json(members, "current_chapter" if chapter_rank(members) == 1 else
                    ("recent" if index == 0 else "also_recent"),
                    pinned=False, hidden=False)
        for index, (_key, members) in enumerate(picked)
    ]
    worker_groups = [pair for pair in groups if group_role(pair[1]) == meta.WORKER]
    return {
        "id": project_id,
        "name": name,
        "root": root,
        "last_active": groups[0][1][0].last_active if groups else "",
        "recency_source": groups[0][1][0].recency_source if groups else "none",
        "pinned": pinned_rows,
        "suggested": suggested,
        # Everything below stays one query away; none of it is a default view.
        "more_total": max(0, len(candidates) - len(picked)),
        "agent_work_total": len(worker_groups),
        "hidden_total": hidden_total,
        "record_total": len(rooms),
        "conversation_total": len(groups),
    }


def _primary_origin(origins: list[dict], node: str) -> dict | None:
    """Prefer a controllable local Codex origin, then primary, then newest."""

    if not origins:
        return None

    def sort_key(origin: dict) -> tuple:
        local_codex = (
            origin.get("runtime") == "codex"
            and origin.get("node") == node
            and bool(origin.get("session_id"))
        )
        return (
            1 if local_codex else 0,
            1 if origin.get("primary") else 0,
            str(origin.get("last_seen") or origin.get("captured") or ""),
        )

    return sorted(origins, key=sort_key, reverse=True)[0]


def _capability(origin: dict | None, node: str) -> tuple[str, str, str, str, str]:
    if not origin:
        return VAULT_ONLY, "", "", "", ""
    runtime = str(origin.get("runtime") or "")
    origin_node = str(origin.get("node") or "")
    session_id = str(origin.get("session_id") or "")
    cwd = str(origin.get("cwd") or "")
    if origin_node and origin_node != node:
        return REMOTE, "", cwd, runtime, origin_node
    if runtime.startswith("claude"):
        return CLAUDE, "", cwd, runtime, origin_node
    if runtime == "codex" and UUID_RE.match(session_id):
        # This origin is on this node by elimination, so say which node that
        # is: a blank one must not split a conversation from its own records.
        return CONTROLLABLE, session_id, cwd, runtime, origin_node or node
    return VAULT_ONLY, "", cwd, runtime, origin_node


def _worker_provenance(record: sv.Record) -> str:
    """Only a declared role marks a worker. Names are never pattern-matched."""

    for field in WORKER_FIELDS:
        value = record.metadata.get(field, "").strip().casefold()
        if value in WORKER_VALUES:
            return f"declared:{field}={value}"
    return ""


def worker_from_thread(thread: dict) -> str:
    """Native provenance: a sub-agent thread names its parent or role."""

    if not isinstance(thread, dict):
        return ""
    if thread.get("parentThreadId"):
        return "native:parentThreadId"
    if thread.get("agentRole") or thread.get("agentNickname"):
        return "native:agentRole"
    return ""


__all__ = [
    "Discovery",
    "Room",
    "group_rooms",
    "group_pref",
    "checkpoint_age_days",
    "group_role",
    "STALE_CHECKPOINT_DAYS",
    "CONTROLLABLE",
    "CLAUDE",
    "REMOTE",
    "VAULT_ONLY",
    "CAPABILITY_LABEL",
    "CAPABILITY_SHORT",
    "worker_from_thread",
]
