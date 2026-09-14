"""Owner's layout metadata, shared across devices; never runtime ownership."""
import json
import os
import re
import sqlite3
from pathlib import Path


class Conflict(Exception):
    def __init__(self, current):
        self.current = current
        super().__init__("desktop state changed")


def validate(state):
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise ValueError("Unsupported desktop format")
    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 60000:
        raise ValueError("Desktop layouts exceed 60 KB")

    def label(value, limit=120):
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError("Use a short, nonempty display name")

    def target(value):
        if not isinstance(value, dict):
            raise ValueError("Invalid session reference")
        if not isinstance(value.get("agent"), str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["agent"]):
            raise ValueError("Invalid agent reference")
        if not isinstance(value.get("room"), str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}", value["room"]):
            raise ValueError("Invalid room reference")
        return value["agent"], value["room"]

    aliases, layouts = state.get("aliases"), state.get("desktops")
    if not isinstance(aliases, list) or len(aliases) > 200 or not isinstance(layouts, list) or len(layouts) > 40:
        raise ValueError("Invalid desktop or alias list")
    seen = set()
    for alias in aliases:
        key = target(alias)
        label(alias.get("label"))
        if key in seen:
            raise ValueError("Duplicate session alias")
        seen.add(key)
    def layout(desktop, kind):
        if not isinstance(desktop, dict):
            raise ValueError("Invalid " + kind)
        label(desktop.get("id"), 96)
        label(desktop.get("name"))
        tabs = desktop.get("tabs")
        if not isinstance(tabs, list) or len(tabs) > 100:
            raise ValueError("Invalid " + kind + " tabs")
        keys = set()
        for tab in tabs:
            key = target(tab)
            if key in keys:
                raise ValueError("Duplicate " + kind + " tab")
            keys.add(key)
            if tab.get("customLabel"):
                label(tab["customLabel"])
        if desktop.get("active") and target(desktop["active"]) not in keys:
            raise ValueError("Active session must belong to this " + kind)
        if not isinstance(desktop.get("customizations", {}), dict):
            raise ValueError("Invalid " + kind + " customizations")

    seen = set()
    for desktop in layouts:
        layout(desktop, "desktop")
        if desktop["id"] in seen:
            raise ValueError("Duplicate desktop")
        seen.add(desktop["id"])

    # Live desktops are named shared arrangements. They are intentionally a
    # separate field from immutable saved layouts, while extensions inside a
    # layout continue to round-trip unchanged.
    live = state.get("liveDesktops", [])
    if not isinstance(live, list) or len(live) > 40:
        raise ValueError("Invalid live desktop list")
    seen = set()
    for desktop in live:
        if not isinstance(desktop, dict):
            raise ValueError("Invalid live desktop")
        label(desktop.get("id"), 96)
        label(desktop.get("name"))
        if desktop["id"] in seen:
            raise ValueError("Duplicate live desktop")
        seen.add(desktop["id"])
        held = desktop.get("layout")
        if not isinstance(held, dict):
            raise ValueError("Invalid live desktop layout")
        # Reuse the snapshot validation without accepting a separate identity
        # inside the nested arrangement.
        layout({**held, "id": desktop["id"], "name": desktop["name"]}, "live desktop")
    return encoded


class DesktopStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS layout (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, state TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO layout VALUES(1,0,?)", (json.dumps({"schema_version": 1, "aliases": [], "desktops": []}),))
        os.chmod(self.path, 0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _read(db):
        version, state = db.execute("SELECT version,state FROM layout WHERE id=1").fetchone()
        return {"version": version, "state": json.loads(state)}

    def read(self):
        with self._connect() as db:
            return self._read(db)

    def save(self, base_version, state):
        if type(base_version) is not int or base_version < 0:
            raise ValueError("A desktop version is required")
        encoded = validate(state)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._read(db)
            if current["version"] != base_version:
                raise Conflict(current)
            db.execute("UPDATE layout SET version=?,state=? WHERE id=1", (base_version + 1, encoded))
            return self._read(db)
