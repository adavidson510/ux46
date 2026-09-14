#!/usr/bin/env python3
"""Local-first Atlas workspace server.

The UI is intentionally dependency-free: Python serves trusted project/session
metadata and uses tmux as the terminal boundary. It never copies transcripts,
executes commands from project data, or binds beyond loopback.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "app" / "static"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
SAFE_PANEL = re.compile(r"^[a-f0-9]{12}$")
SAFE_ENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
STOP_WORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "did", "do",
    "file", "files", "find", "for", "from", "in", "is", "it", "me",
    "my", "of", "on", "or", "project", "projects", "that", "the", "this",
    "to", "was", "we", "what", "where", "which", "with",
}


class UiError(RuntimeError):
    """A response-safe Atlas Studio error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def entity_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def search_terms(query: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", query.casefold())
    return [term for term in raw if term not in STOP_WORDS] or raw


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


class DeterministicSearchIndex:
    """Prepared pointer search with no model or per-query subprocess."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._rows: list[tuple[dict[str, Any], dict[str, str], dict[str, set[str]]]] = []
        self._lock = threading.RLock()

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError as exc:
            raise UiError(f"Atlas index not found; run atlas index: {self.path}") from exc
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        prepared = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise UiError(f"Could not read Atlas index: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise UiError(f"Invalid Atlas index line {number}: {exc}") from exc
            if not isinstance(item, dict):
                continue
            fields = {
                "name": str(item.get("name", "")),
                "path": str(item.get("path", "")),
                "projects": " ".join(item.get("projects", []) + item.get("project_names", [])),
                "keywords": " ".join(item.get("keywords", [])),
                "kinds": " ".join(item.get("kinds", []) + item.get("roles", [])),
            }
            normalized_fields = {key: normalized(value) for key, value in fields.items()}
            field_terms = {key: set(value.split()) for key, value in normalized_fields.items()}
            prepared.append((item, normalized_fields, field_terms))
        self._rows = prepared
        self._stamp = stamp

    @staticmethod
    def _score(
        query: str, query_terms: list[str], phrase: str,
        fields: dict[str, str], field_terms: dict[str, set[str]],
    ) -> tuple[float, int]:
        score = 0.0
        matched = 0
        if phrase:
            if phrase == fields["name"]:
                score += 45
            elif phrase in fields["name"]:
                score += 30
            elif phrase in fields["path"]:
                score += 20
            elif phrase in fields["keywords"]:
                score += 5
        for term in query_terms:
            term_score = 0.0
            for field, weight in (
                ("name", 16), ("projects", 10), ("keywords", 3),
                ("path", 7), ("kinds", 4),
            ):
                if term in field_terms[field]:
                    term_score = max(term_score, weight)
            if term_score:
                matched += 1
                score += term_score
        if not matched:
            return 0.0, 0
        coverage = matched / max(len(query_terms), 1)
        if len(query_terms) >= 3 and coverage < 0.60:
            return 0.0, 0
        return score + coverage * 10, matched

    def search(self, query: str, project: str = "", limit: int = 16) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            query_terms = search_terms(query)
            phrase = normalized(query)
            wanted = project.casefold()
            ranked = []
            for item, fields, field_terms in self._rows:
                if wanted and not any(str(value).casefold() == wanted for value in item.get("projects", [])):
                    continue
                score, matched = self._score(query, query_terms, phrase, fields, field_terms)
                if score:
                    ranked.append({**item, "type": "file", "score": round(score, 2), "matched_terms": matched})
            ranked.sort(key=lambda item: (-item["score"], str(item.get("path", "")).casefold()))
            return ranked[:limit]

    def warm(self) -> None:
        with self._lock:
            self._refresh()


class AtlasStateStore:
    """Atomic, durable workspace and Attention state owned by Atlas.

    Session content and native runtime state remain pointers. This store owns
    only presentation, control, notification, and re-entry metadata.
    """

    ACTIVE_ATTENTION = {"new", "seen", "opened", "answered", "resuming"}
    CLASSES = {"needs_now", "needs_soon", "review_ready"}

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _seed(sessions: list[dict[str, Any]]) -> dict[str, Any]:
        views = []
        for index, session in enumerate(sessions[:4]):
            views.append({
                "id": entity_id("view"),
                "session_id": session["identity"],
                "representation": "context_window",
                "control_state": "controlled" if index == 0 else "watched",
                "geometry": {"region": f"slot-{index + 1}"},
            })
        focused = views[0]["id"] if views else None
        return {
            "schema_version": 1,
            "workspace": {
                "id": "workspace-morning-build",
                "name": "Morning Build",
                "last_opened_at": utc_now(),
                "template_origin": "template-morning-build",
                "active_audio_target": views[0]["session_id"] if views else None,
                "attention_drawer": "collapsed",
                "layout": "adaptive-four",
                "windows": [{
                    "id": "window-main",
                    "display_id": "display-main",
                    "bounds": {"x": 0, "y": 0, "width": 1440, "height": 900},
                    "focused_view_id": focused,
                    "views": views,
                }],
            },
            "attention": [],
            "history": [],
        }

    def load(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self.path.is_file():
                value = load_object(self.path, "Atlas workspace state")
            else:
                value = self._seed(sessions)
                self.save(value)
            value.setdefault("workspace", self._seed(sessions)["workspace"])
            value.setdefault("attention", [])
            value.setdefault("history", [])
            self._reconcile(value, sessions)
            return value

    def _reconcile(self, state: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
        valid = {item["identity"] for item in sessions}
        workspace = state["workspace"]
        windows = workspace.get("windows")
        if not isinstance(windows, list) or not windows:
            workspace["windows"] = self._seed(sessions)["workspace"]["windows"]
            windows = workspace["windows"]
        for window in windows:
            if not isinstance(window, dict):
                continue
            window.setdefault("views", [])
            for view in window["views"]:
                if isinstance(view, dict):
                    view["session_available"] = view.get("session_id") in valid
        all_views = [view for window in windows for view in window.get("views", [])]
        valid_views = [
            view for view in all_views
            if view.get("session_id") in valid
        ]
        if all_views and not valid_views and sessions:
            main = next((window for window in windows if window.get("id") == "window-main"), windows[0])
            for index, session in enumerate(sessions[:4]):
                main["views"].append({
                    "id": entity_id("view"), "session_id": session["identity"],
                    "representation": "context_window",
                    "control_state": "controlled" if index == 0 else "watched",
                    "geometry": {"region": f"slot-{len(main['views']) + 1}"},
                    "session_available": True,
                })
            main["focused_view_id"] = main["views"][-len(sessions[:4])]["id"]
        workspace["last_opened_at"] = utc_now()

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)

    @staticmethod
    def _workspace_snapshot(state: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(state["workspace"])

    def _transaction(self, state: dict[str, Any], label: str) -> None:
        history = state.setdefault("history", [])
        history.append({"label": label, "at": utc_now(), "workspace": self._workspace_snapshot(state)})
        del history[:-12]

    @staticmethod
    def _window(state: dict[str, Any], window_id: str) -> dict[str, Any]:
        for window in state["workspace"]["windows"]:
            if window.get("id") == window_id:
                return window
        raise UiError(f"Workspace window not found: {window_id}")

    @staticmethod
    def _view(state: dict[str, Any], view_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for window in state["workspace"]["windows"]:
            for view in window.get("views", []):
                if view.get("id") == view_id:
                    return window, view
        raise UiError(f"Workspace view not found: {view_id}")

    def open_view(
        self, state: dict[str, Any], session_id: str, window_id: str = "window-main",
        representation: str = "context_window", duplicate: bool = False,
    ) -> dict[str, Any]:
        if not SAFE_ENTITY.fullmatch(session_id):
            raise UiError("Invalid session identity")
        if representation not in {"tui", "context_window", "monitor", "activity", "artifact_companion"}:
            raise UiError("Invalid session representation")
        if not duplicate:
            for window in state["workspace"]["windows"]:
                for view in window.get("views", []):
                    if view.get("session_id") == session_id:
                        window["focused_view_id"] = view["id"]
                        return {"view": view, "window_id": window["id"], "existing": True}
        self._transaction(state, "Open session view")
        window = self._window(state, window_id)
        view = {
            "id": entity_id("view"), "session_id": session_id,
            "representation": representation, "control_state": "watched",
            "geometry": {"region": f"slot-{len(window.get('views', [])) + 1}"},
            "session_available": True,
        }
        if not any(v.get("control_state") == "controlled" for w in state["workspace"]["windows"] for v in w.get("views", [])):
            view["control_state"] = "controlled"
        window.setdefault("views", []).append(view)
        window["focused_view_id"] = view["id"]
        self.save(state)
        return {"view": view, "window_id": window["id"], "existing": False}

    def close_view(self, state: dict[str, Any], view_id: str) -> None:
        self._transaction(state, "Close view")
        window, view = self._view(state, view_id)
        window["views"].remove(view)
        if window.get("focused_view_id") == view_id:
            window["focused_view_id"] = window["views"][0]["id"] if window["views"] else None
        self.save(state)

    def focus_view(self, state: dict[str, Any], view_id: str) -> None:
        window, _ = self._view(state, view_id)
        window["focused_view_id"] = view_id
        self.save(state)

    def transfer_control(self, state: dict[str, Any], view_id: str) -> None:
        self._transaction(state, "Transfer session control")
        _, target = self._view(state, view_id)
        session_id = target["session_id"]
        for window in state["workspace"]["windows"]:
            for view in window.get("views", []):
                if view.get("session_id") == session_id:
                    view["control_state"] = "controlled" if view["id"] == view_id else "watched"
        self.save(state)

    def set_layout(self, state: dict[str, Any], layout: str) -> None:
        allowed = {"focus", "split", "adaptive-four", "stack"}
        if layout not in allowed:
            raise UiError("Unknown workspace layout")
        self._transaction(state, f"Change layout to {layout}")
        state["workspace"]["layout"] = layout
        self.save(state)

    def compose(self, state: dict[str, Any], session_ids: list[str], template: str | None = None) -> None:
        """Arrange sessions while reusing existing views and never restarting work."""
        self._transaction(state, "Compose workspace")
        main = self._window(state, "window-main")
        existing = {
            view.get("session_id"): view
            for window in state["workspace"]["windows"]
            for view in window.get("views", [])
            if isinstance(view, dict)
        }
        arranged = []
        for index, session_id in enumerate(dict.fromkeys(session_ids)):
            view = existing.get(session_id) or {
                "id": entity_id("view"), "session_id": session_id,
                "representation": "context_window", "control_state": "watched",
                "session_available": True,
            }
            view["geometry"] = {"region": f"slot-{index + 1}"}
            arranged.append(view)
        controlled = next((view for view in arranged if view.get("control_state") == "controlled"), None)
        if not controlled and arranged:
            arranged[0]["control_state"] = "controlled"
        main["views"] = arranged
        main["focused_view_id"] = arranged[0]["id"] if arranged else None
        for window in state["workspace"]["windows"]:
            if window is not main:
                window["views"] = [view for view in window.get("views", []) if view.get("session_id") not in session_ids]
        state["workspace"]["windows"] = [
            window for window in state["workspace"]["windows"]
            if window.get("id") == "window-main" or window.get("views")
        ]
        state["workspace"]["template_origin"] = template
        state["workspace"]["layout"] = "adaptive-four"
        self.save(state)

    def detach(self, state: dict[str, Any], view_id: str) -> dict[str, Any]:
        self._transaction(state, "Detach view")
        origin, view = self._view(state, view_id)
        origin["views"].remove(view)
        if origin.get("focused_view_id") == view_id:
            origin["focused_view_id"] = origin["views"][0]["id"] if origin["views"] else None
        window = {
            "id": entity_id("window"), "display_id": "display-main",
            "bounds": {"x": 40, "y": 80, "width": 520, "height": 620},
            "focused_view_id": view_id, "views": [view],
        }
        state["workspace"]["windows"].append(window)
        self.save(state)
        return window

    def attach(self, state: dict[str, Any], view_id: str) -> None:
        self._transaction(state, "Attach view")
        origin, view = self._view(state, view_id)
        if origin["id"] == "window-main":
            return
        origin["views"].remove(view)
        main = self._window(state, "window-main")
        main["views"].append(view)
        main["focused_view_id"] = view_id
        state["workspace"]["windows"] = [
            window for window in state["workspace"]["windows"]
            if window.get("id") == "window-main" or window.get("views")
        ]
        self.save(state)

    def set_audio_target(self, state: dict[str, Any], session_id: str | None) -> None:
        state["workspace"]["active_audio_target"] = session_id
        self.save(state)

    def set_attention_drawer(self, state: dict[str, Any], value: str) -> None:
        if value not in {"collapsed", "expanded", "pinned"}:
            raise UiError("Invalid Attention drawer state")
        state["workspace"]["attention_drawer"] = value
        self.save(state)

    def undo(self, state: dict[str, Any]) -> str:
        history = state.get("history", [])
        if not history:
            raise UiError("There is no workspace change to undo")
        item = history.pop()
        state["workspace"] = item["workspace"]
        self.save(state)
        return str(item.get("label", "Workspace change"))

    @classmethod
    def attention_counts(cls, state: dict[str, Any]) -> dict[str, int]:
        counts = {"needs_now": 0, "needs_soon": 0, "review_ready": 0}
        for item in state.get("attention", []):
            if item.get("lifecycle_state") in cls.ACTIVE_ATTENTION:
                key = item.get("interruption_class")
                if key in counts:
                    counts[key] += 1
        return counts

    def create_attention(self, state: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        required = [
            "project_id", "session_id", "requested_by", "source", "interruption_class",
            "type", "priority", "need_from_aaron", "why_aaron", "paused",
            "still_continuing", "if_no_action", "after_response", "authoritative_action",
        ]
        missing = [key for key in required if not request.get(key)]
        if missing:
            raise UiError(f"Attention request is missing: {', '.join(missing)}")
        if request["interruption_class"] not in self.CLASSES:
            raise UiError("Invalid Attention interruption class")
        source = request.get("source")
        if not isinstance(source, dict) or not source.get("runtime") or not source.get("machine"):
            raise UiError("Attention source must name a runtime and machine")
        authority = request.get("authoritative_action")
        if not isinstance(authority, dict) or authority.get("mode") not in {"inline", "external"}:
            raise UiError("Attention request must declare inline or external authority")
        key = str(request.get("blocked_dependency_key") or request["need_from_aaron"]).strip().casefold()
        for item in state.get("attention", []):
            same = (
                item.get("project_id") == request["project_id"]
                and item.get("session_id") == request["session_id"]
                and str(item.get("blocked_dependency_key") or item.get("need_from_aaron", "")).strip().casefold() == key
                and item.get("lifecycle_state") in self.ACTIVE_ATTENTION
            )
            if same:
                reporter = str(request["requested_by"])
                item.setdefault("reporters", [])
                if reporter not in item["reporters"]:
                    item["reporters"].append(reporter)
                item["verified_at"] = utc_now()
                self.save(state)
                return item
        now = utc_now()
        item = {
            **request,
            "id": str(request.get("id") or entity_id("attention")),
            "blocked_dependency_key": key,
            "reporters": list(dict.fromkeys(request.get("reporters", []) + [request["requested_by"]])),
            "created_at": str(request.get("created_at") or now),
            "verified_at": now,
            "lifecycle_state": "new",
            "options": request.get("options", []),
            "recommendation": request.get("recommendation"),
            "evidence": request.get("evidence", []),
            "answer": None,
        }
        state.setdefault("attention", []).append(item)
        self.save(state)
        return item

    def transition_attention(
        self, state: dict[str, Any], attention_id: str, action: str,
        answer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = next((entry for entry in state.get("attention", []) if entry.get("id") == attention_id), None)
        if not item:
            raise UiError("Attention request not found")
        current = item.get("lifecycle_state")
        if action == "seen" and current == "new":
            item["lifecycle_state"] = "seen"
        elif action == "open" and current in {"new", "seen"}:
            item["lifecycle_state"] = "opened"
        elif action == "answer" and current in {"new", "seen", "opened"}:
            authority = item.get("authoritative_action", {})
            if authority.get("mode") == "external" and (answer or {}).get("answer_type") != "external_completed":
                raise UiError("Complete this action in its authoritative system")
            if not answer or answer.get("answer_type") not in {"option", "text", "defer", "external_completed"}:
                raise UiError("Attention answer is invalid")
            item["answer"] = {**answer, "answered_at": utc_now()}
            item["lifecycle_state"] = "answered"
        elif action == "acknowledge" and current == "answered":
            item["lifecycle_state"] = "resuming"
        elif action == "resolve" and current == "resuming":
            item["lifecycle_state"] = "resolved"
        elif action in {"stale", "supersede"} and current in self.ACTIVE_ATTENTION:
            item["lifecycle_state"] = "stale" if action == "stale" else "superseded"
        else:
            raise UiError(f"Cannot {action} an Attention request in state {current}")
        self.save(state)
        return item


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UiError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UiError(f"Invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise UiError(f"{label} must be a JSON object")
    return value


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw = value.strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = raw
            if isinstance(decoded, (str, int, float, bool)):
                raw = str(decoded)
        fields[key.strip()] = raw
    return fields


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    paragraphs = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    return " ".join(paragraphs)[:720]


@dataclass
class Terminal:
    panel_id: str
    tmux_name: str
    identity: str
    title: str
    cwd: str
    launched_at: float


class TerminalManager:
    """Launches only Atlas-resolved local Codex resume commands."""

    def __init__(self, registry: Path, vault: Path | None = None) -> None:
        self.registry = registry
        self.vault = vault or Path.home() / ".codex" / "bin" / "session-vault"
        self.tmux = shutil.which("tmux")
        self._terminals: dict[str, Terminal] = {}
        self._lock = threading.Lock()

    @staticmethod
    def validate_resume(payload: dict[str, Any]) -> tuple[list[str], str]:
        command = payload.get("command")
        if not isinstance(command, list) or len(command) != 3:
            raise UiError("Session resolver did not return a supported command")
        executable, action, native_id = [str(value) for value in command]
        if Path(executable).name != "codex" or action != "resume":
            raise UiError("Atlas Studio only launches allowlisted Codex resume commands")
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", native_id):
            raise UiError("Native Codex session ID is invalid")
        cwd = str(payload.get("saved_cwd") or "")
        if not cwd or not Path(cwd).expanduser().is_dir():
            raise UiError("The saved session working directory is unavailable")
        return [executable, action, native_id], str(Path(cwd).expanduser().resolve())

    def resolve(self, identity: str) -> dict[str, Any]:
        if not SAFE_ID.fullmatch(identity) or "/" not in identity:
            raise UiError("Expected a project/session identity")
        completed = subprocess.run(
            [
                str(self.vault), "--registry", str(self.registry),
                "resume", identity, "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise UiError(message or f"{identity} has no local resumable session")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise UiError("Session resolver returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UiError("Session resolver returned an invalid result")
        return payload

    def launch(self, identity: str, title: str) -> dict[str, Any]:
        if not self.tmux:
            raise UiError("tmux is required for live terminal panes")
        resolution = self.resolve(identity)
        command, cwd = self.validate_resume(resolution)
        panel_id = hashlib.sha256(f"{identity}:{time.time_ns()}".encode()).hexdigest()[:12]
        tmux_name = f"atlas-{panel_id}"
        completed = subprocess.run(
            [
                self.tmux, "new-session", "-d", "-s", tmux_name,
                "-x", "132", "-y", "38", "-c", cwd,
                *command,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if completed.returncode != 0:
            raise UiError(completed.stderr.strip() or "Could not launch the Codex pane")
        subprocess.run(
            [self.tmux, "set-option", "-t", tmux_name, "remain-on-exit", "on"],
            capture_output=True,
            check=False,
        )
        terminal = Terminal(panel_id, tmux_name, identity, title, cwd, time.time())
        with self._lock:
            self._terminals[panel_id] = terminal
        return self.snapshot(panel_id)

    def _terminal(self, panel_id: str) -> Terminal:
        if not SAFE_PANEL.fullmatch(panel_id):
            raise UiError("Invalid terminal pane")
        with self._lock:
            terminal = self._terminals.get(panel_id)
        if not terminal:
            raise UiError("Terminal pane is not owned by this Atlas Studio process")
        return terminal

    def alive(self, terminal: Terminal) -> bool:
        if not self.tmux:
            return False
        return subprocess.run(
            [self.tmux, "has-session", "-t", terminal.tmux_name],
            capture_output=True,
            check=False,
        ).returncode == 0

    def snapshot(self, panel_id: str) -> dict[str, Any]:
        terminal = self._terminal(panel_id)
        alive = self.alive(terminal)
        content = ""
        if alive and self.tmux:
            completed = subprocess.run(
                [self.tmux, "capture-pane", "-p", "-t", terminal.tmux_name, "-S", "-160"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=3,
            )
            content = ANSI.sub("", completed.stdout).rstrip()
        return {
            "panel_id": panel_id,
            "identity": terminal.identity,
            "title": terminal.title,
            "cwd": terminal.cwd,
            "alive": alive,
            "content": content,
        }

    def input(self, panel_id: str, text: str, enter: bool = True) -> dict[str, Any]:
        terminal = self._terminal(panel_id)
        if not self.alive(terminal) or not self.tmux:
            raise UiError("Terminal pane is no longer running")
        clean = str(text)
        if len(clean) > 20_000:
            raise UiError("Terminal input is too large")
        if clean:
            subprocess.run(
                [self.tmux, "send-keys", "-t", terminal.tmux_name, "-l", clean],
                check=False,
                timeout=3,
            )
        if enter:
            subprocess.run(
                [self.tmux, "send-keys", "-t", terminal.tmux_name, "Enter"],
                check=False,
                timeout=3,
            )
        return {"ok": True}

    def interrupt(self, panel_id: str) -> dict[str, Any]:
        terminal = self._terminal(panel_id)
        if self.alive(terminal) and self.tmux:
            subprocess.run(
                [self.tmux, "send-keys", "-t", terminal.tmux_name, "C-c"],
                check=False,
                timeout=3,
            )
        return {"ok": True}

    def close(self, panel_id: str) -> dict[str, Any]:
        terminal = self._terminal(panel_id)
        if self.alive(terminal) and self.tmux:
            subprocess.run(
                [self.tmux, "kill-session", "-t", terminal.tmux_name],
                capture_output=True,
                check=False,
                timeout=3,
            )
        with self._lock:
            self._terminals.pop(panel_id, None)
        return {"ok": True}


class AtlasService:
    def __init__(self, registry: Path, index_file: Path, state_file: Path | None = None) -> None:
        self.registry_path = registry.resolve()
        self.index_file = index_file.resolve()
        self.registry = load_object(self.registry_path, "Atlas registry")
        self.node_id = str(self.registry.get("node_id", "local"))
        self.atlas_cli = Path(__file__).resolve().parent / "atlas.py"
        self.vault_cli = Path.home() / ".codex" / "bin" / "session-vault"
        self.projects = self._projects()
        self.terminals = TerminalManager(self.registry_path, self.vault_cli)
        self.search_index = DeterministicSearchIndex(self.index_file)
        self.search_index.warm()
        configured_state = os.environ.get("ATLAS_STUDIO_STATE")
        self.state_store = AtlasStateStore(
            state_file or (Path(configured_state) if configured_state else Path.home() / ".codex" / "atlas-studio" / "state.json")
        )

    def _projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        raw_projects = self.registry.get("projects", [])
        if not isinstance(raw_projects, list):
            raise UiError("Atlas registry has no projects list")
        for pointer in raw_projects:
            if not isinstance(pointer, dict):
                continue
            root = Path(str(pointer.get("root", ""))).expanduser()
            manifest = load_object(root / "project.json", f"manifest for {pointer.get('id')}")
            sources = manifest.get("sources", [])
            projects.append({
                "id": str(pointer.get("id", manifest.get("id", ""))),
                "name": str(pointer.get("name", manifest.get("name", ""))),
                "root": str(root.resolve()),
                "status": str(manifest.get("status", "")),
                "visibility": str(manifest.get("visibility", "private")),
                "aliases": pointer.get("aliases", manifest.get("aliases", [])),
                "keywords": manifest.get("keywords", []),
                "source_count": len(sources) + 1 if isinstance(sources, list) else 1,
                "sources": sources if isinstance(sources, list) else [],
            })
        return sorted(projects, key=lambda item: item["name"].casefold())

    def sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for project in self.projects:
            session_dir = Path(project["root"]) / "sessions"
            if not session_dir.is_dir():
                continue
            for path in session_dir.glob("*.md"):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                meta = frontmatter(text)
                session_id = meta.get("session", path.stem)
                identity = f"{project['id']}/{session_id}"
                origins_path = path.with_suffix(".origins.json")
                origins: list[Any] = []
                if origins_path.is_file():
                    try:
                        raw = load_object(origins_path, "session origins").get("origins", [])
                        origins = raw if isinstance(raw, list) else []
                    except UiError:
                        origins = []
                primary = next(
                    (item for item in origins if isinstance(item, dict) and item.get("primary")),
                    origins[0] if origins and isinstance(origins[0], dict) else {},
                )
                sessions.append({
                    "id": identity,
                    "identity": identity,
                    "project_id": project["id"],
                    "project": project["id"],
                    "project_name": project["name"],
                    "name": meta.get("title", session_id.replace("-", " ").title()),
                    "session": session_id,
                    "title": meta.get("title", session_id.replace("-", " ").title()),
                    "date": meta.get("date", ""),
                    "updated": meta.get("updated", meta.get("date", "")),
                    "status": meta.get("status", ""),
                    "summary": section(text, "Summary"),
                    "path": str(path.resolve()),
                    "native_origins": len(origins),
                    "runtime": str(primary.get("runtime", "")) if isinstance(primary, dict) else "",
                    "node": str(primary.get("node", "")) if isinstance(primary, dict) else "",
                    "exact_resume": bool(
                        isinstance(primary, dict)
                        and primary.get("runtime") == "codex"
                        and primary.get("node") == self.node_id
                    ),
                    "execution_state": (
                        "archived" if meta.get("status") == "archived" else
                        "idle_open" if meta.get("status") in {"active", "saved", ""} else "stopped"
                    ),
                    "availability_state": (
                        "local_online" if isinstance(primary, dict)
                        and primary.get("runtime") == "codex"
                        and primary.get("node") == self.node_id else "unknown"
                    ),
                    "continuation": {
                        "mode": "exact" if (
                            isinstance(primary, dict)
                            and primary.get("runtime") == "codex"
                            and primary.get("node") == self.node_id
                        ) else "portable",
                        "native_resume_pointer": (
                            f"codex://{self.node_id}/{primary.get('session_id')}"
                            if isinstance(primary, dict) and primary.get("session_id") else None
                        ),
                        "portable_context_ref": str(path.resolve()),
                        "verified_at": None,
                    },
                    "attention_state": "none",
                    "runtime_endpoint": {
                        "runtime_type": str(primary.get("runtime", "memory")) if isinstance(primary, dict) else "memory",
                        "agent_name": str(primary.get("agent", "")) if isinstance(primary, dict) else "",
                        "machine_id": str(primary.get("node", self.node_id)) if isinstance(primary, dict) else self.node_id,
                        "transport": "local" if isinstance(primary, dict) and primary.get("node") == self.node_id else None,
                    },
                    "created_at": f"{meta.get('date', '1970-01-01')}T00:00:00Z",
                    "meaningful_activity_at": f"{meta.get('updated', meta.get('date', '1970-01-01'))}T00:00:00Z",
                })
        return sorted(
            sessions,
            key=lambda item: (item["updated"], item["title"].casefold()),
            reverse=True,
        )

    def bootstrap(self, csrf: str) -> dict[str, Any]:
        sessions = self.sessions()
        state = self.state_store.load(sessions)
        active_attention: dict[str, str] = {}
        priority = {"review_ready": 1, "needs_soon": 2, "needs_now": 3}
        for item in state.get("attention", []):
            if item.get("lifecycle_state") not in AtlasStateStore.ACTIVE_ATTENTION:
                continue
            session_id = str(item.get("session_id", ""))
            level = str(item.get("interruption_class", "none"))
            if priority.get(level, 0) > priority.get(active_attention.get(session_id, "none"), 0):
                active_attention[session_id] = level
        for session in sessions:
            session["attention_state"] = active_attention.get(session["identity"], "none")
        counts: dict[str, int] = {}
        for item in sessions:
            counts[item["project"]] = counts.get(item["project"], 0) + 1
        projects = [dict(project, session_count=counts.get(project["id"], 0)) for project in self.projects]
        return {
            "product": "Atlas",
            "version": "0.3.0",
            "node": self.node_id,
            "projects": projects,
            "sessions": sessions,
            "workspace": state["workspace"],
            "attention": state.get("attention", []),
            "attention_counts": self.state_store.attention_counts(state),
            "csrf": csrf,
            "capabilities": {
                "terminal_panes": bool(self.terminals.tmux),
                "pane_url": os.environ.get("ATLAS_PANE_URL", "http://127.0.0.1:4626"),
                "max_panes": 4,
                "workspace_persistence": True,
                "attention_lifecycle": True,
                "detached_windows": True,
            },
        }

    def state(self) -> dict[str, Any]:
        return self.state_store.load(self.sessions())

    def workspace_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        state = self.state()
        store = self.state_store
        result: dict[str, Any] = {"ok": True}
        if action == "open":
            identity = str(body.get("session_id") or body.get("identity") or "")
            if not any(item["identity"] == identity for item in self.sessions()):
                raise UiError(f"Session not found: {identity}")
            result.update(store.open_view(
                state, identity, str(body.get("window_id") or "window-main"),
                str(body.get("representation") or "context_window"), bool(body.get("duplicate", False)),
            ))
        elif action == "close":
            store.close_view(state, str(body.get("view_id", "")))
        elif action == "focus":
            store.focus_view(state, str(body.get("view_id", "")))
        elif action == "control":
            store.transfer_control(state, str(body.get("view_id", "")))
        elif action == "layout":
            store.set_layout(state, str(body.get("layout", "")))
        elif action == "template":
            identities = body.get("session_ids")
            if not isinstance(identities, list) or not all(isinstance(item, str) for item in identities):
                raise UiError("Template requires session identities")
            valid = {item["identity"] for item in self.sessions()}
            if any(item not in valid for item in identities):
                raise UiError("Template contains an unknown session")
            store.compose(state, identities, str(body.get("template") or "ad-hoc"))
        elif action == "detach":
            result["window"] = store.detach(state, str(body.get("view_id", "")))
        elif action == "attach":
            store.attach(state, str(body.get("view_id", "")))
        elif action == "audio":
            target = body.get("session_id")
            store.set_audio_target(state, str(target) if target else None)
        elif action == "attention-drawer":
            store.set_attention_drawer(state, str(body.get("value", "")))
        elif action == "undo":
            result["undone"] = store.undo(state)
        else:
            raise UiError("Unknown workspace action")
        refreshed = self.state()
        result["workspace"] = refreshed["workspace"]
        result["attention_counts"] = store.attention_counts(refreshed)
        return result

    def attention_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        state = self.state()
        if action == "create":
            item = self.state_store.create_attention(state, body)
        else:
            item = self.state_store.transition_attention(
                state, str(body.get("id", "")), action,
                body.get("answer") if isinstance(body.get("answer"), dict) else None,
            )
        return {
            "attention": item,
            "attention_items": state.get("attention", []),
            "attention_counts": self.state_store.attention_counts(state),
        }

    def command(self, text: str) -> dict[str, Any]:
        """Interpret safe Atlas intents; never pass command text to a shell."""
        query = " ".join(text.strip().split())
        if not query:
            raise UiError("Tell Atlas what you want to see")
        lowered = query.casefold()
        sessions = self.sessions()
        projects = self.projects
        mentioned_projects = [
            project for project in projects
            if project["id"].casefold() in lowered
            or project["name"].casefold() in lowered
            or any(str(alias).casefold() in lowered for alias in project.get("aliases", []))
        ]
        matched_project = next(iter(mentioned_projects), None)
        if "morning build" in lowered or (lowered.startswith("give me ") and len(mentioned_projects) > 1):
            selected_projects = mentioned_projects
            if "morning build" in lowered and not selected_projects:
                preferred = {"orbit", "demo-pet", "nightwatch", "general"}
                selected_projects = [project for project in projects if project["id"] in preferred]
            chosen = []
            missing = []
            for project in selected_projects:
                candidates = [item for item in sessions if item["project"] == project["id"]]
                if candidates:
                    chosen.append(candidates[0])
                else:
                    missing.append({"id": project["id"], "name": project["name"], "reason": "No saved session"})
            state = self.state()
            self.state_store.compose(state, [item["identity"] for item in chosen], "template-morning-build")
            return {
                "kind": "workspace_composed", "query": query, "sessions": chosen,
                "unresolved": missing, "workspace": state["workspace"],
            }
        if ("show" in lowered or "list" in lowered) and "session" in lowered:
            results = [item for item in sessions if not matched_project or item["project"] == matched_project["id"]]
            return {"kind": "session_list", "query": query, "project": matched_project, "sessions": results}
        if lowered.startswith(("open ", "resume ", "put ", "give me ")):
            candidates = [item for item in sessions if not matched_project or item["project"] == matched_project["id"]]
            tokens = {token for token in re.findall(r"[a-z0-9]+", lowered) if token not in {
                "open", "resume", "put", "give", "me", "the", "a", "an", "session", "window", "pane", "section", "new", "current",
            }}
            def score(item: dict[str, Any]) -> tuple[int, str]:
                haystack = f"{item['identity']} {item['title']} {item['summary']}".casefold()
                return (sum(1 for token in tokens if token in haystack), item.get("updated", ""))
            if candidates:
                target = max(candidates, key=score)
                if score(target)[0] or matched_project:
                    state = self.state()
                    opened = self.state_store.open_view(state, target["identity"])
                    if "new window" in lowered or "separate window" in lowered or "pop out" in lowered:
                        opened["window"] = self.state_store.detach(state, opened["view"]["id"])
                    return {"kind": "session_opened", "query": query, "session": target, **opened}
        if lowered in {"undo", "undo that", "put it back"}:
            state = self.state()
            return {"kind": "workspace_undo", "undone": self.state_store.undo(state)}
        results = self.search(query, matched_project["id"] if matched_project else "")
        return {"kind": "search", "query": query, "project": matched_project, "results": results}

    def search(self, query: str, project: str = "") -> list[dict[str, Any]]:
        clean = query.strip()
        if not clean:
            return []
        query_terms = search_terms(clean)
        phrase = normalized(clean)
        session_hits = []
        for session in self.sessions():
            if project and session["project"].casefold() != project.casefold():
                continue
            fields = {
                "name": normalized(session["title"]),
                "path": normalized(f"{session['identity']} {session['path']}"),
                "projects": normalized(f"{session['project']} {session['project_name']}"),
                "keywords": normalized(session["summary"]),
                "kinds": "session memory",
            }
            field_terms = {key: set(value.split()) for key, value in fields.items()}
            score, matched = DeterministicSearchIndex._score(
                clean, query_terms, phrase, fields, field_terms
            )
            if score:
                session_hits.append({
                    **session, "type": "session", "score": round(score + 8, 2),
                    "matched_terms": matched,
                })
        file_hits = self.search_index.search(clean, project, 16)
        combined = session_hits + file_hits
        combined.sort(key=lambda item: (
            -float(item.get("score", 0)),
            str(item.get("identity", item.get("path", ""))).casefold(),
        ))
        return combined[:16]

    def session(self, identity: str) -> dict[str, Any]:
        match = next((item for item in self.sessions() if item["identity"] == identity), None)
        if not match:
            raise UiError(f"Session not found: {identity}")
        payload = dict(match)
        payload["record"] = Path(match["path"]).read_text(encoding="utf-8")
        return payload

    def project(self, project_id: str) -> dict[str, Any]:
        match = next((item for item in self.projects if item["id"] == project_id), None)
        if not match:
            raise UiError(f"Project not found: {project_id}")
        return dict(match, sessions=[s for s in self.sessions() if s["project"] == project_id])

    def runtime_status(self) -> dict[str, Any]:
        pane_url = os.environ.get("ATLAS_PANE_URL", "http://127.0.0.1:4626")
        pane = {"state": "offline", "url": pane_url}
        try:
            with urllib.request.urlopen(f"{pane_url.rstrip('/')}/healthz", timeout=0.45) as response:
                if response.status == 200:
                    pane = {"state": "online", "url": pane_url}
        except (OSError, urllib.error.URLError):
            pass

        tell = {"state": "unavailable", "server": "unknown", "biggie": "unknown"}
        tell_cli = Path.home() / "telld" / "src" / "tell" / "cli.py"
        if tell_cli.is_file():
            try:
                completed = subprocess.run(
                    ["python3", str(tell_cli), "--as", "local-workspace", "status"],
                    cwd=tell_cli.parents[3],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=7,
                )
                if completed.returncode == 0:
                    value = json.loads(completed.stdout)
                    peers = value.get("peers", {}) if isinstance(value, dict) else {}
                    tell = {
                        "state": value.get("daemon", "unknown"),
                        "server": "online" if peers.get("server", {}).get("ok") else "offline",
                        "biggie": "online" if peers.get("biggie", {}).get("ok") else "offline",
                        "queued": value.get("outbound_queued", 0),
                    }
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
        return {"pane": pane, "tell": tell}


class AtlasHandler(BaseHTTPRequestHandler):
    server_version = "Atlas/0.3"

    @property
    def app(self) -> "AtlasServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: object) -> None:
        if self.app.verbose:
            super().log_message(fmt, *args)

    def _headers(self, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-src http://127.0.0.1:* http://localhost:*; "
            "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*",
        )
        self.end_headers()

    def json_response(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(body), status)
        self.wfile.write(body)

    def error_response(self, error: Exception, status: int = 400) -> None:
        self.json_response({"error": str(error)}, status)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise UiError("Invalid request length") from exc
        if length > 100_000:
            raise UiError("Request is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise UiError("Request body must be JSON") from exc
        if not isinstance(value, dict):
            raise UiError("Request body must be an object")
        return value

    def require_csrf(self) -> None:
        if not secrets.compare_digest(self.headers.get("X-Atlas-CSRF", ""), self.app.csrf):
            raise UiError("Atlas request token is missing or invalid")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                return self.json_response({
                    "status": "ok", "product": "Atlas", "node": self.app.service.node_id
                })
            if parsed.path == "/api/bootstrap":
                return self.json_response(self.app.service.bootstrap(self.app.csrf))
            if parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                return self.json_response(self.app.service.search(
                    params.get("q", [""])[0], params.get("project", [""])[0]
                ))
            if parsed.path == "/api/session":
                identity = parse_qs(parsed.query).get("identity", [""])[0]
                return self.json_response(self.app.service.session(identity))
            if parsed.path == "/api/project":
                project_id = parse_qs(parsed.query).get("id", [""])[0]
                return self.json_response(self.app.service.project(project_id))
            if parsed.path == "/api/runtime-status":
                return self.json_response(self.app.service.runtime_status())
            if parsed.path == "/api/workspace":
                state = self.app.service.state()
                return self.json_response({
                    "workspace": state["workspace"],
                    "attention": state.get("attention", []),
                    "attention_counts": self.app.service.state_store.attention_counts(state),
                })
            terminal_match = re.fullmatch(r"/api/terminals/([a-f0-9]{12})", parsed.path)
            if terminal_match:
                return self.json_response(self.app.service.terminals.snapshot(terminal_match.group(1)))
            return self.serve_static(parsed.path)
        except UiError as exc:
            return self.error_response(exc, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # fail closed without a debug trace in the browser
            return self.error_response(UiError(f"Atlas Studio error: {exc}"), 500)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            relative = "index.html" if parsed.path in ("", "/") else unquote(parsed.path.lstrip("/"))
            target = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
                raise UiError("Asset not found")
            mime = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            self._headers(mime, target.stat().st_size)
        except UiError as exc:
            self.error_response(exc, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self.require_csrf()
            body = self.read_json()
            if parsed.path == "/api/terminals":
                return self.json_response(self.app.service.terminals.launch(
                    str(body.get("identity", "")), str(body.get("title", "Session"))[:160]
                ), HTTPStatus.CREATED)
            workspace_match = re.fullmatch(
                r"/api/workspace/(open|close|focus|control|layout|template|detach|attach|audio|attention-drawer|undo)",
                parsed.path,
            )
            if workspace_match:
                return self.json_response(self.app.service.workspace_action(workspace_match.group(1), body))
            attention_match = re.fullmatch(
                r"/api/attention/(create|seen|open|answer|acknowledge|resolve|stale|supersede)",
                parsed.path,
            )
            if attention_match:
                status = HTTPStatus.CREATED if attention_match.group(1) == "create" else HTTPStatus.OK
                return self.json_response(
                    self.app.service.attention_action(attention_match.group(1), body), status
                )
            if parsed.path == "/api/command":
                return self.json_response(self.app.service.command(str(body.get("text", ""))))
            match = re.fullmatch(r"/api/terminals/([a-f0-9]{12})/(input|interrupt|close)", parsed.path)
            if match:
                panel_id, action = match.groups()
                manager = self.app.service.terminals
                if action == "input":
                    return self.json_response(manager.input(
                        panel_id, str(body.get("text", "")), bool(body.get("enter", True))
                    ))
                if action == "interrupt":
                    return self.json_response(manager.interrupt(panel_id))
                return self.json_response(manager.close(panel_id))
            return self.error_response(UiError("Unknown Atlas Studio action"), HTTPStatus.NOT_FOUND)
        except UiError as exc:
            return self.error_response(exc, HTTPStatus.CONFLICT)
        except Exception as exc:
            return self.error_response(UiError(f"Atlas Studio error: {exc}"), 500)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else unquote(request_path.lstrip("/"))
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            raise UiError("Invalid asset path")
        if not target.is_file():
            raise UiError("Asset not found")
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self._headers(mime, len(body))
        self.wfile.write(body)


class AtlasServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: AtlasService, verbose: bool = False) -> None:
        super().__init__(address, AtlasHandler)
        self.service = service
        self.verbose = verbose
        self.csrf = secrets.token_urlsafe(24)


def serve(
    registry: Path,
    index_file: Path,
    host: str = "127.0.0.1",
    port: int = 4732,
    open_browser: bool = True,
    verbose: bool = False,
    state_file: Path | None = None,
) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise UiError("Atlas v0 only binds to loopback")
    service = AtlasService(registry, index_file, state_file)
    server = AtlasServer((host, port), service, verbose=verbose)
    url = f"http://{host}:{server.server_port}"
    print(f"Atlas is ready at {url}")
    print("Press Ctrl-C to stop it. Live panes are isolated in Atlas-owned tmux sessions.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAtlas stopped.")
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--index-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4732)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--state-file", type=Path)
    args = parser.parse_args()
    try:
        return serve(
            args.registry.expanduser(), args.index_file.expanduser(), args.host,
            args.port, not args.no_browser, args.verbose,
            args.state_file.expanduser() if args.state_file else None,
        )
    except UiError as exc:
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
