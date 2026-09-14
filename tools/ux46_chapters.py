"""Deterministic, transcript-free handoffs for UX46 rolling chapters.

This module reads only durable project material.  It deliberately has no
native-runtime dependency: native Codex owns compaction, execution and its
transcript; UX46 supplies a small, inspectable entry brief at thread start.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import session_vault as vault


BRIEF_LIMIT = 6000
CONTINUATION_PATH = Path(".ux46") / "continuation.md"
CONTINUATIONS_DIR = Path(".ux46") / "continuations"


def parse_new_argument(argument: str) -> tuple[str, str]:
    """Return the requested chapter mode and optional human title.

    ``/new`` deliberately means the next chapter.  ``blank`` and ``parallel``
    are explicit words so a title such as "Blank slate" remains a normal
    rolling title.
    """

    value = (argument or "").strip()
    if value == "blank":
        return "blank", ""
    if value == "parallel":
        return "parallel", ""
    if value.startswith("parallel "):
        return "parallel", value[len("parallel "):].strip()
    return "replace", value


def chapter_title(project_name: str, mode: str, requested: str) -> str:
    if requested:
        return requested[:120]
    if mode == "parallel":
        return f"Parallel {project_name} chapter"
    if mode == "blank":
        return f"New {project_name} conversation"
    return f"Next {project_name} chapter"


def _compact(value: object, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit].rstrip() + ("…" if len(value) > limit else "")


def _section(body: str, title: str, limit: int) -> str:
    match = re.search(rf"(?ims)^## {re.escape(title)}\s*\n+(.*?)(?=\n## |\Z)", body)
    return _compact(match.group(1) if match else "", limit)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return str(path)


def _project_overview(root: Path) -> tuple[str, dict[str, object] | None]:
    descriptor = root / "project.json"
    raw = _read_text(descriptor)
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            selected = [
                f"{key}: {_compact(value[key], 850)}"
                for key in ("name", "summary", "description", "purpose", "status")
                if value.get(key)
            ]
            if selected:
                return "\n".join(selected), {"kind": "project.json", "path": "project.json"}
    readme = root / "README.md"
    text = _read_text(readme)
    if text:
        overview = _section(text, "Summary", 1200) or _compact(text, 1200)
        if overview:
            return overview, {"kind": "README", "path": "README.md"}
    return "", None


def build_brief(project: vault.Project, record: vault.Record) -> dict[str, Any]:
    """Make the exact bounded text supplied to a rolling native thread.

    Sources are reported alongside the brief so a preview is a faithful view of
    what a subsequent ``/new`` will receive.  No generated summary, transcript
    text, goal, model choice or permission state is included.
    """

    root = project.root
    sources: list[dict[str, object]] = []
    parts = [
        "UX46 rolling chapter entry brief",
        f"Project: {project.name} ({project.id})",
        f"Continuation source: {record.identity} ({_relative(record.path, root)})",
        "This is durable handoff context, not a native transcript. Re-check live state before acting.",
    ]
    checkpoint = vault.checkpoint_payload(record)
    if checkpoint:
        fields = [
            f"{key}: {_compact(checkpoint.get(key), 900)}"
            for key in ("at", "reporter", "state", "need", "next") if checkpoint.get(key)
        ]
        if fields:
            parts.extend(["Current checkpoint:", *fields])
            sources.append({"kind": "current-checkpoint", "path": _relative(record.path, root)})

    overview, overview_source = _project_overview(root)
    if overview:
        parts.extend(["Project overview:", overview])
        assert overview_source is not None
        sources.append(overview_source)

    # A room-specific file wins over the projectwide fallback, so two
    # parallel chapters can retain different next steps without overwriting
    # each other. Session names are Vault-safe components; retain a defensive
    # component check here because this is a path construction boundary.
    session_name = record.session
    specific = CONTINUATIONS_DIR / f"{session_name}.md"
    continuation_path = specific if Path(session_name).name == session_name and _read_text(root / specific) else CONTINUATION_PATH
    continuation = root / continuation_path
    continuation_text = _compact(_read_text(continuation), 1800)
    if continuation_text:
        parts.extend(["Explicit continuation:", continuation_text])
        sources.append({"kind": "explicit-continuation", "path": str(continuation_path)})

    if not sources:
        parts.append("No durable checkpoint, project overview, or explicit continuation file is available. No native transcript was copied.")

    brief = "\n\n".join(parts)
    if len(brief) > BRIEF_LIMIT:
        brief = brief[: BRIEF_LIMIT - 1].rstrip() + "…"
    return {"brief": brief, "size_chars": len(brief), "sources": sources}


def developer_instructions(brief: str, *, identity: str) -> str:
    """The one-time native start payload; it never asks native to take a turn."""

    return (
        "UX46 opened this as a rolling chapter. This new native thread belongs to the "
        f"new canonical room {identity}; the continuation source is history only and must not "
        "become this thread's own room or ledger. Use the following durable entry brief as "
        "context only. It is not a transcript, goal, authority, permission grant, or instruction "
        "to act before the user writes.\n\n" + brief
    )
