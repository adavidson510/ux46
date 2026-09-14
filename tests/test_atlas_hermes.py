"""Focused checks for the UX46 native Hermes adapter.

Nothing here touches the real Hermes install, the real Pane persona, the
messaging gateway that is already running, or a model. Every test drives
``tools/atlas_hermes.py`` against:

* a synthetic native gateway that speaks the same newline JSON-RPC protocol as
  ``python -m tui_gateway.entry``, records the exact method and params it was
  called with, and can be told to fail, hang, or push a real event sequence; and
* a real SQLite database with the columns the installed build's ``state.db``
  really has, so the read-only reader is exercised against the schema rather
  than against a mock of itself.

No prompt is ever sent to a model and no message is ever sent to a person.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_hermes as hermes  # noqa: E402

# Persisted Hermes session ids, in the runtime's own format.
S_LINKED = "20260901_101010_aaaaaa"
S_DISCORD = "20260902_111111_bbbbbb"
S_OPEN = "20260903_121212_cccccc"
S_SUB = "20260904_131313_dddddd"
S_TOOL = "20260905_141414_eeeeee"
DISCORD_KEY = "agent:main:discord:channel:1501661986818363512"

NOW = 1788800000.0


# A synthetic native TUI gateway. Deliberately dumb: it answers the methods the
# adapter is allowed to call, logs every one, and does exactly what the scenario
# file tells it to.
FAKE_GATEWAY = r'''
import json, os, sys, threading, time

scenario = json.loads(open(sys.argv[1]).read())
log_path = sys.argv[2]
state_path = log_path + ".created"
lock = threading.Lock()

def log(entry):
    with lock:
        with open(log_path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")

def emit(payload):
    with lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

def event(kind, sid, payload=None):
    params = {"type": kind, "session_id": sid}
    if payload is not None:
        params["payload"] = payload
    emit({"jsonrpc": "2.0", "method": "event", "params": params})

def ok(rid, result):
    emit({"jsonrpc": "2.0", "id": rid, "result": result})

def err(rid, code, message):
    emit({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

event("gateway.ready", "", {"skin": "default"})

# ephemeral native session id -> persisted session id
live = {}
status = {}
counter = [0]
created = []

def new_ephemeral():
    counter[0] += 1
    return "eph%04d" % counter[0]

def stream_turn(sid):
    """Emit the event sequence a real turn emits, in order."""
    for step in scenario.get("stream", []):
        kind = step["type"]
        if kind == "__hold__":
            marker = log_path + ".finish"
            for _ in range(600):
                if os.path.exists(marker):
                    break
                time.sleep(0.02)
            continue
        event(kind, sid, step.get("payload"))
        time.sleep(0.01)
    status[sid] = "idle"

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, params, rid = req["method"], req.get("params") or {}, req.get("id")
    log({"method": method, "params": params})

    if method == "session.list":
        ok(rid, {"sessions": list(scenario["sessions"])})

    elif method == "session.active_list":
        rows = []
        for eph, key in live.items():
            rows.append({"id": eph, "session_key": key, "status": status.get(eph, "idle"),
                         "title": "", "preview": "", "model": "gpt-5.6-sol",
                         "message_count": 0, "started_at": 1788800000.0,
                         "last_active": 1788800000.0, "current": False})
        ok(rid, {"sessions": rows})

    elif method == "session.resume":
        behaviour = scenario.get("resume_behaviour", "ok")
        if behaviour == "hang":
            continue
        if behaviour == "error":
            err(rid, 4007, "session not found")
            continue
        target = params.get("session_id", "")
        eph = new_ephemeral()
        live[eph] = target
        status[eph] = "idle"
        ok(rid, {"session_id": eph, "resumed": target, "session_key": target,
                 "message_count": 3, "messages": [], "running": False,
                 "status": "idle", "started_at": 1788800000.0,
                 "info": {"model": "gpt-5.6-sol", "cwd": "/tmp"}})

    elif method == "session.create":
        behaviour = scenario.get("create_behaviour", "ok")
        if behaviour == "hang":
            continue
        if behaviour == "error":
            err(rid, 4090, "Hermes is at the active session limit (4/4).")
            continue
        eph = new_ephemeral()
        key = scenario.get("created_key", "20260907_090909_ffffff")
        created.append(key)
        live[eph] = key
        status[eph] = "idle"
        open(state_path, "w").write(json.dumps(created))
        if behaviour == "no_key":
            ok(rid, {"session_id": eph, "message_count": 0})
            continue
        ok(rid, {"session_id": eph, "stored_session_id": key, "message_count": 0,
                 "messages": [], "info": {"model": "gpt-5.6-sol", "cwd": "/tmp"}})

    elif method == "session.close":
        sid = params.get("session_id", "")
        closed = live.pop(sid, None) is not None
        status.pop(sid, None)
        ok(rid, {"closed": closed})

    elif method == "session.interrupt":
        status[params.get("session_id", "")] = "idle"
        ok(rid, {"status": "interrupted"})

    elif method == "session.status":
        ok(rid, {"output": "Hermes TUI Status\n\nSession ID: "
                           + live.get(params.get("session_id", ""), "?")})

    elif method == "prompt.submit":
        behaviour = scenario.get("submit_behaviour", "ok")
        sid = params.get("session_id", "")
        if behaviour == "hang":
            continue
        if behaviour == "error":
            err(rid, 4009, "session busy")
            continue
        if behaviour == "odd":
            ok(rid, {"status": "queued"})
            continue
        status[sid] = "working"
        ok(rid, {"status": "streaming"})
        threading.Thread(target=stream_turn, args=(sid,), daemon=True).start()

    elif method in ("slash.exec", "session.compress"):
        ok(rid, {"output": "Native command applied", "message": "Compressed"})

    elif method == "commands.catalog":
        ok(rid, {"pairs": [["/new", "Start a new session"], ["/model", "Switch model"]],
                 "categories": [{"name": "Session", "pairs": []}],
                 "skill_count": 3, "warning": ""})

    elif method == "image.attach_bytes":
        ok(rid, {"attached": True, "path": "/tmp/images/upload1.png", "count": 1,
                 "bytes": len(params.get("content_base64", ""))})

    elif method == "file.attach":
        ok(rid, {"attached": True, "name": params.get("name", ""),
                 "path": "/tmp/work/" + params.get("name", "f"),
                 "ref_path": params.get("name", "f"),
                 "ref_text": "@file:" + params.get("name", "f"), "uploaded": True})

    elif method == "approval.respond":
        ok(rid, {"resolved": 1})

    elif method == "clarify.respond":
        ok(rid, {"ok": True})

    else:
        err(rid, -32601, "unknown method: " + method)
'''

DEFAULT_SESSIONS = [
    {"id": S_LINKED, "title": "Pane build notes", "preview": "let us start",
     "started_at": NOW - 8000, "message_count": 6, "source": "cli"},
    {"id": S_DISCORD, "title": "", "preview": "hello from a channel",
     "started_at": NOW - 6000, "message_count": 4, "source": "discord"},
    {"id": S_OPEN, "title": "Half finished", "preview": "still going",
     "started_at": NOW - 4000, "message_count": 2, "source": "cli"},
    # session.list denies only `tool`; a real list carries hundreds of these.
    {"id": S_SUB, "title": "", "preview": "subagent run",
     "started_at": NOW - 3000, "message_count": 9, "source": "subagent"},
    {"id": S_TOOL, "title": "", "preview": "tool run",
     "started_at": NOW - 2000, "message_count": 3, "source": "tool"},
]

DEFAULT_SCENARIO = {
    "sessions": DEFAULT_SESSIONS,
    "resume_behaviour": "ok",
    "create_behaviour": "ok",
    "submit_behaviour": "ok",
    "created_key": "20260907_090909_ffffff",
    "stream": [
        {"type": "message.start"},
        {"type": "message.delta", "payload": {"text": "Reading the notes"}},
        {"type": "__hold__"},
        {"type": "message.complete",
         "payload": {"text": "Reading the notes now.", "status": "complete"}},
    ],
}


def build_state_db(path: Path) -> None:
    """A real state.db with the columns the installed build really has."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, model TEXT,
            parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
            end_reason TEXT, message_count INTEGER DEFAULT 0, title TEXT,
            cwd TEXT, archived INTEGER NOT NULL DEFAULT 0, session_key TEXT,
            origin_json TEXT, profile_name TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
            tool_name TEXT, timestamp REAL NOT NULL, finish_reason TEXT,
            reasoning TEXT, reasoning_content TEXT,
            active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE gateway_routing (
            scope TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL,
            entry_json TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (scope, session_key));
        """
    )
    conn.executemany(
        "INSERT INTO sessions(id, source, started_at, ended_at, end_reason,"
        " message_count, title, cwd, session_key, parent_session_id, profile_name)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            # Cleanly ended: nothing is writing to it, so it is attachable.
            (S_LINKED, "cli", NOW - 8000, NOW - 7000, "cli_close", 6,
             "Pane build notes", "/Users/example/Projects/project-atlas", None, None, None),
            # Routed by the messaging gateway that is already running.
            (S_DISCORD, "discord", NOW - 6000, None, None, 4, None, None,
             DISCORD_KEY, None, None),
            # No ended_at: some surface may still be writing to it.
            (S_OPEN, "cli", NOW - 4000, None, None, 2, "Half finished", "/tmp",
             None, None, None),
            (S_SUB, "subagent", NOW - 3000, NOW - 2900, "agent_close", 9, None,
             None, None, S_LINKED, None),
            (S_TOOL, "tool", NOW - 2000, NOW - 1900, "agent_close", 3, None,
             None, None, S_LINKED, None),
        ],
    )
    conn.execute(
        "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at)"
        " VALUES(?,?,?,?)",
        ("/Users/example/.hermes/sessions", DISCORD_KEY,
         json.dumps({"session_key": DISCORD_KEY, "session_id": S_DISCORD}), NOW),
    )
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls,"
        " tool_name, timestamp, reasoning, active) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (S_LINKED, "user", "Summarise the notes about kettles", None, None,
             None, NOW - 7900, None, 1),
            (S_LINKED, "assistant", "", "call_1",
             json.dumps([{"id": "call_1", "function": {
                 "name": "read_file", "arguments": '{"path": "notes.md"}'}}]),
             None, NOW - 7890, "I should read the file first", 1),
            (S_LINKED, "tool", "kettle notes, three of them", "call_1", None,
             "read_file", NOW - 7880, None, 1),
            (S_LINKED, "assistant", "Let me check one more thing.", None, None,
             None, NOW - 7870, None, 1),
            (S_LINKED, "assistant", "There are three notes about kettles.", None,
             None, None, NOW - 7860, None, 1),
            # A row Hermes marked inactive is not part of the transcript.
            (S_LINKED, "assistant", "a rewound draft", None, None, None,
             NOW - 7850, None, 0),
            (S_DISCORD, "user", "hello from a channel", None, None, None,
             NOW - 5900, None, 1),
            (S_DISCORD, "assistant", "hello back", None, None, None,
             NOW - 5890, None, 1),
            (S_OPEN, "user", "still going", None, None, None, NOW - 3900, None, 1),
        ],
    )
    conn.commit()
    conn.close()


class Harness:
    """One adapter, one synthetic native gateway, one loopback port."""

    def __init__(self, **scenario_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        scenario = json.loads(json.dumps(DEFAULT_SCENARIO))
        scenario.update(scenario_overrides)
        self.scenario_path = base / "scenario.json"
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        self.log_path = base / "calls.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        fake = base / "fake_gateway.py"
        fake.write_text(FAKE_GATEWAY, encoding="utf-8")

        # A Hermes home with only what this adapter is allowed to read.
        self.home = base / "hermes"
        self.home.mkdir()
        build_state_db(self.home / "state.db")
        (self.home / "SOUL.md").write_text(
            "# Hermes Agent Persona\n\nYou are Pane for User: a Hermes instance.\n",
            encoding="utf-8")
        # A credential file that must never be opened. Its presence is the test.
        (self.home / "auth.json").write_text(
            json.dumps({"api_key": "sk-must-never-be-read"}), encoding="utf-8")

        # Two real Vault projects. Only one carries a verified `hermes` origin;
        # the other names the same session under a different runtime, which must
        # not be read as a Hermes link.
        atlas = base / "Projects" / "atlas" / "sessions"
        atlas.mkdir(parents=True)
        (atlas / "pane-notes.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "atlas", "session": "pane-notes",
            "origins": [{"runtime": "hermes", "node": "user-mac",
                         "session_id": S_LINKED, "captured": "2026-09-07",
                         "primary": True}],
        }), encoding="utf-8")
        notes = base / "Projects" / "notes" / "sessions"
        notes.mkdir(parents=True)
        (notes / "decoy.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "notes", "session": "decoy",
            "origins": [
                {"runtime": "openclaw", "node": "server-agent3",
                 "session_key": S_OPEN},
                {"runtime": "codex", "node": "user-mac", "session_id": S_DISCORD},
            ],
        }), encoding="utf-8")

        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "user-mac",
            "projects": [
                {"id": "atlas", "name": "Project Atlas",
                 "root": str(base / "Projects" / "atlas")},
                {"id": "notes", "name": "Notes",
                 "root": str(base / "Projects" / "notes")},
            ],
        }), encoding="utf-8")

        self.config = Namespace(
            host="127.0.0.1", port=0, hermes_home=str(self.home),
            hermes_repo=str(base), python=sys.executable,
            agent_name="Pane", source="ux46", registry=str(registry),
            unfiled_project="unfiled", node="user-mac",
            state_dir=str(base / "state"), session_limit=400,
            include_derived=False, new_cwd=str(base), send_timeout=4.0,
            attach_timeout=4.0, command_timeout=4.0, ready_timeout=20.0,
            suggest=2, browser_origin="", allow_host=[],
            identity_header="X-Forwarded-User", path_prefix="", quiet=True,
        )
        holder: dict = {}
        self._command = [sys.executable, "-u", str(fake), str(self.scenario_path),
                         str(self.log_path)]
        self.gateway = hermes.NativeGateway(
            list(self._command), cwd=str(base), env={"PATH": "/usr/bin:/bin"},
            on_event=lambda event, sid, payload:
                holder["service"].on_native_event(event, sid, payload),
            ready_timeout=20.0)
        self.gateway.start()
        self.service = hermes.HermesService(self.config, self.gateway)
        holder["service"] = self.service
        self.service.start()
        self.server = hermes.HermesServer(("127.0.0.1", 0), hermes.HermesHandler,
                                          self.service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.csrf = self.get("/api/bootstrap")[1]["csrf"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.service.stop()
        self.gateway.stop()
        self.tmp.cleanup()

    def finish_turn(self) -> None:
        """Let a held stream emit its message.complete."""
        Path(str(self.log_path) + ".finish").write_text("go", encoding="utf-8")

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = json.dumps(body).encode() if body is not None else b""
        sent = {"Content-Length": str(len(payload))}
        if body is not None:
            sent["Content-Type"] = "application/json"
        if method != "GET" and getattr(self, "csrf", None):
            sent["X-Atlas-CSRF"] = self.csrf
        sent.update(headers or {})
        conn.request(method, path, payload, sent)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw or b"{}")
        except ValueError:
            return response.status, {"raw": raw.decode("utf-8", "replace")}

    def get(self, path: str, **kwargs) -> tuple[int, dict]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: dict | None = None, **kwargs) -> tuple[int, dict]:
        return self.request("POST", path, body or {}, **kwargs)

    def upload(self, room: str, name: str, data: bytes,
               mime: str = "application/octet-stream") -> tuple[int, dict]:
        boundary = "----ux46hermes"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", f"/api/room/{room}/files", body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "X-Atlas-CSRF": self.csrf,
        })
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw or b"{}")

    def calls(self, method: str = "") -> list[dict]:
        entries = [json.loads(line) for line in
                   self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in entries if not method or e["method"] == method]

    def wait_for_event(self, after: int, predicate, timeout: float = 8.0) -> dict:
        deadline = time.time() + timeout
        seq = after
        while time.time() < deadline:
            status, payload = self.get(f"/api/events?after={seq}&timeout=2")
            if status != 200:
                continue
            seq = payload.get("seq", seq)
            for event in payload.get("events") or []:
                if predicate(event):
                    return event
        raise AssertionError("no matching adapter event arrived")


class HermesAdapterTest(unittest.TestCase):
    scenario: dict = {}

    def setUp(self) -> None:
        self.harness = Harness(**self.scenario)
        self.addCleanup(self.harness.close)

    # The linked conversation keeps its real project and its record's own name.
    linked_room = "atlas/pane-notes"

    def room_for(self, session_id: str) -> str:
        room = self.harness.service.catalog.room_for_session(session_id)
        self.assertIsNotNone(room, f"no room for {session_id}")
        return room.id


# ---------------------------------------------------------------------------


class BootstrapTest(HermesAdapterTest):
    def test_bootstrap_names_the_native_runtime_and_its_own_persona(self) -> None:
        status, payload = self.harness.get("/api/bootstrap")
        self.assertEqual(status, 200)
        agent = payload["agent"]
        self.assertEqual(agent["runtime"], "hermes")
        self.assertEqual(agent["name"], "Pane")
        # The name is confirmed against Hermes's own persona file, not asserted.
        self.assertTrue(agent["identity_confirmed"])
        self.assertEqual(agent["identity_declared"], "Pane")
        self.assertTrue(agent["identity_source"].endswith("SOUL.md"))
        self.assertEqual(payload["adapter"]["name"], "ux46-hermes")
        self.assertEqual(payload["adapter"]["transport"], "native")
        self.assertTrue(payload["adapter"]["connected"])
        self.assertTrue(payload["adapter"]["state_readable"])

    def test_bootstrap_says_the_lease_is_not_an_exclusive_lock(self) -> None:
        _, payload = self.harness.get("/api/bootstrap")
        model = payload["ownership_model"]
        self.assertFalse(model["lease_is_exclusive"])
        self.assertIn("capacity counter", model["lease_note"])
        self.assertEqual(sorted(model["attachable"]),
                         ["adapter_session", "closed", "live_here"])

    def test_bootstrap_states_every_unsupported_action_and_command(self) -> None:
        _, payload = self.harness.get("/api/bootstrap")
        for action in ("goal", "effort", "speak"):
            self.assertTrue(payload["unsupported"][action])
        self.assertEqual(payload["commands"]["supported"], ["/help", "/new", "/status", "/refresh", "/model"])
        # Refused by name, with the reason, rather than blanket-refused.
        self.assertIn("/goal", payload["commands"]["unsupported"])
        self.assertIn("did not advertise", payload["commands"]["unsupported"]["/goal"])
        # The runtime's own catalog is reported, but only two are reachable.
        native = payload["commands"]["native_catalog"]
        self.assertEqual(native["count"], 2)
        self.assertEqual(native["reachable_here"], payload["commands"]["supported"])
        self.assertFalse(payload["capabilities"]["voice"])

    def test_no_credential_is_read_or_reported(self) -> None:
        """The adapter never opens auth.json, and nothing leaks into a response."""
        _, payload = self.harness.get("/api/bootstrap")
        blob = json.dumps(payload)
        self.assertNotIn("sk-must-never-be-read", blob)
        self.assertNotIn("auth.json", blob)
        # And it was never asked for over the wire either.
        for call in self.harness.calls():
            self.assertNotIn("auth", json.dumps(call["params"]))


class CatalogTest(HermesAdapterTest):
    def test_only_a_verified_hermes_origin_files_a_conversation(self) -> None:
        _, payload = self.harness.get("/api/workspace")
        by_id = {project["id"]: project for project in payload["projects"]}
        self.assertIn("atlas", by_id)
        self.assertTrue(by_id["atlas"]["vault_project"])
        rooms = [room["id"] for room in by_id["atlas"]["suggested"]]
        self.assertEqual(rooms, [self.linked_room])
        # `notes` names S_OPEN under openclaw and S_DISCORD under codex. Neither
        # is a Hermes origin, so neither conversation is filed there.
        self.assertNotIn("notes", by_id)

    def test_unfiled_is_last_and_is_not_a_project(self) -> None:
        _, payload = self.harness.get("/api/workspace")
        last = payload["projects"][-1]
        self.assertEqual(last["id"], "unfiled")
        self.assertTrue(last["unfiled"])
        self.assertFalse(last["vault_project"])
        self.assertIn("no project was created", last["note"])
        # Nothing was written to disk for it.
        self.assertFalse((Path(self.harness.tmp.name) / "Projects" / "unfiled").exists())

    def test_tool_and_subagent_runs_are_filtered_out(self) -> None:
        _, payload = self.harness.get("/api/rooms?limit=100")
        listed = {room["session_id"] for room in payload["rooms"]}
        self.assertEqual(listed, {S_LINKED, S_DISCORD, S_OPEN})
        # session.list denies only `tool`; this adapter filters both.
        self.assertIn(S_SUB, {row["id"] for row in DEFAULT_SESSIONS})

    def test_attribution_survives_into_every_row(self) -> None:
        _, payload = self.harness.get("/api/rooms?limit=100")
        rows = {room["session_id"]: room for room in payload["rooms"]}
        self.assertEqual(rows[S_DISCORD]["native_provenance"], "discord")
        self.assertEqual(rows[S_LINKED]["runtime"], "hermes")
        self.assertEqual(rows[S_LINKED]["node"], "user-mac")
        self.assertEqual(rows[S_LINKED]["session_key_source"], "hermes.state.db")
        self.assertEqual(rows[S_LINKED]["session_id"], S_LINKED)

    def test_a_room_slug_is_a_stable_hash_of_the_exact_session_id(self) -> None:
        room = self.room_for(S_OPEN)
        self.assertTrue(room.endswith(hermes.session_slug(S_OPEN, "Half finished")))
        # And it is stable: computing it again gives the same name.
        self.assertEqual(room, "unfiled/" + hermes.session_slug(S_OPEN, "Half finished"))

    def test_an_unknown_room_is_refused_not_invented(self) -> None:
        status, payload = self.harness.get("/api/room/atlas/not-a-session")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_room")
        self.assertIn("never guesses", payload["message"])


class BrowseWithoutResumingTest(HermesAdapterTest):
    def test_catalog_skips_expensive_native_list_and_hides_cron(self) -> None:
        reader = self.harness.service.state
        with sqlite3.connect(reader.path) as db:
            db.execute("INSERT INTO sessions(id,source,title,started_at) VALUES(?,?,?,?)",
                       ("cron-new", "cron", "Automatic run", NOW + 1000))
        catalog = self.harness.service.catalog
        catalog.refresh(force=True)
        self.assertNotIn("cron-new", {r.session_id for r in catalog.rooms()})
        self.assertEqual(self.harness.calls("session.list"), [])
        self.assertIn("cron-new", {r["id"] for r in reader.catalog_rows(100, True)})

    def test_reading_a_conversation_resumes_nothing(self) -> None:
        for session_id in (S_LINKED, S_DISCORD, S_OPEN):
            room = self.room_for(session_id)
            status, detail = self.harness.get(f"/api/room/{room}")
            self.assertEqual(status, 200, session_id)
            status, history = self.harness.get(f"/api/room/{room}/history?limit=40")
            self.assertEqual(status, 200, session_id)
            self.assertFalse(history["unavailable"])
        # Browsing every room, including the linked one, opened no session.
        self.assertEqual(self.harness.calls("session.resume"), [])
        self.assertEqual(self.harness.calls("session.create"), [])
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_history_comes_from_the_read_only_database(self) -> None:
        room = self.room_for(S_DISCORD)
        status, history = self.harness.get(
            f"/api/room/{room}/history?limit=40&direction=asc")
        self.assertEqual(status, 200)
        self.assertEqual(history["source"], "hermes state.db (read-only)")
        kinds = [(item["type"], item["text"]) for item in history["items"]]
        self.assertEqual(kinds, [("userMessage", "hello from a channel"),
                                 ("agentMessage", "hello back")])

    def test_the_adapter_cannot_write_to_the_hermes_database(self) -> None:
        reader = self.harness.service.state
        with self.assertRaises(sqlite3.OperationalError):
            reader._connect().execute("UPDATE sessions SET title = 'x'")


class HistoryProjectionTest(HermesAdapterTest):
    def items(self) -> list[dict]:
        _, history = self.harness.get(
            f"/api/room/{self.linked_room}/history?limit=40&direction=asc")
        return history["items"]

    def test_every_item_maps_to_a_row_the_database_reported(self) -> None:
        items = self.items()
        shapes = [(item["type"], item.get("name", ""), item["text"]) for item in items]
        self.assertEqual(shapes, [
            ("userMessage", "", "Summarise the notes about kettles"),
            ("reasoning", "", "I should read the file first"),
            ("mcpToolCall", "read_file", "read_file"),
            ("functionCallOutput", "read_file", "kettle notes, three of them"),
            ("agentMessage", "", "Let me check one more thing."),
            ("agentMessage", "", "There are three notes about kettles."),
        ])
        # Ids trace to the exact messages.id, so they survive a refresh.
        self.assertEqual([item["id"] for item in items if item["type"] == "userMessage"],
                         ["h1"])

    def test_only_the_last_assistant_message_of_a_turn_is_the_answer(self) -> None:
        answers = [item for item in self.items() if item["type"] == "agentMessage"]
        self.assertEqual([item["phase"] for item in answers],
                         ["interim", "final_answer"])

    def test_a_row_hermes_marked_inactive_is_not_in_the_transcript(self) -> None:
        self.assertNotIn("a rewound draft",
                         [item["text"] for item in self.items()])

    def test_a_bad_cursor_is_refused(self) -> None:
        status, payload = self.harness.get(
            f"/api/room/{self.linked_room}/history?cursor=notanid")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_cursor")

    def test_search_finds_the_exact_item_and_says_what_it_scanned(self) -> None:
        status, payload = self.harness.get(
            f"/api/room/{self.linked_room}/search?q=kettles&kinds=human,final")
        self.assertEqual(status, 200)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["scan_cap"], hermes.SEARCH_SCAN_CAP)
        kinds = sorted(hit["kind"] for hit in payload["hits"])
        self.assertEqual(kinds, ["final", "human"])
        for hit in payload["hits"]:
            self.assertIn("kettle", hit["snippet"].casefold())
        # Every hit id is one this adapter can hand back to the transcript.
        ids = {item["id"] for item in self.items()}
        for hit in payload["hits"]:
            self.assertIn(hit["id"], ids)

    def test_search_can_be_narrowed_to_tool_work(self) -> None:
        _, payload = self.harness.get(
            f"/api/room/{self.linked_room}/search?q=kettle&kinds=tool")
        self.assertEqual([hit["kind"] for hit in payload["hits"]], ["tool"])


class OwnershipTest(HermesAdapterTest):
    def test_a_routed_conversation_is_held_elsewhere_and_read_only(self) -> None:
        room = self.room_for(S_DISCORD)
        _, detail = self.harness.get(f"/api/room/{room}")
        self.assertEqual(detail["ownership"]["state"], "held_elsewhere")
        self.assertEqual(detail["ownership"]["claim"], "gateway_routed")
        self.assertFalse(detail["controllable"])
        self.assertEqual(detail["capability_short"], hermes.CAPABILITY_READONLY_SHORT)

    def test_resuming_a_routed_conversation_is_refused_before_anything_is_called(self) -> None:
        room = self.room_for(S_DISCORD)
        status, payload = self.harness.post(f"/api/room/{room}/continue")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "owner_unverified")
        self.assertIn("messaging gateway", payload["message"])
        self.assertIn("/new", payload["message"])
        self.assertTrue(payload["detail"]["readable"])
        self.assertEqual(self.harness.calls("session.resume"), [])

    def test_an_unproven_open_conversation_is_unknown_and_read_only(self) -> None:
        room = self.room_for(S_OPEN)
        _, detail = self.harness.get(f"/api/room/{room}")
        self.assertEqual(detail["ownership"]["state"], "unknown")
        self.assertEqual(detail["ownership"]["claim"], "open_elsewhere")
        self.assertFalse(detail["controllable"])
        status, payload = self.harness.post(f"/api/room/{room}/continue")
        self.assertEqual(status, 409)
        self.assertIn("cannot prove", payload["message"])
        self.assertIn("capacity counter", payload["message"])
        self.assertEqual(self.harness.calls("session.resume"), [])

    def test_a_cleanly_ended_conversation_is_idle_and_offers_continue(self) -> None:
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertEqual(detail["ownership"]["state"], "idle")
        self.assertEqual(detail["ownership"]["claim"], "closed")
        self.assertTrue(detail["controllable"])
        self.assertFalse(detail["ownership"]["atlas_owned"])

    def test_nothing_is_reported_running_just_because_it_exists(self) -> None:
        for session_id in (S_LINKED, S_DISCORD, S_OPEN):
            _, detail = self.harness.get(f"/api/room/{self.room_for(session_id)}")
            self.assertEqual(detail["native"]["active_turn"], "")
            self.assertFalse(detail["native"]["active_run"])
        # A conversation nobody is connected to says so, rather than "idle".
        _, detail = self.harness.get(f"/api/room/{self.room_for(S_OPEN)}")
        self.assertEqual(detail["native"]["status"], "unconnected")
        self.assertEqual(detail["native"]["status_source"], "not connected here")


class AttachReleaseTest(HermesAdapterTest):
    def attach(self) -> dict:
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/continue")
        self.assertEqual(status, 200, payload)
        return payload

    def test_continue_resumes_the_exact_session_and_nothing_else(self) -> None:
        payload = self.attach()
        calls = self.harness.calls("session.resume")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"session_id": S_LINKED, "source": "ux46"})
        self.assertEqual(payload["ownership"]["state"], "atlas_owned")
        self.assertTrue(payload["ownership"]["atlas_owned"])
        self.assertEqual(payload["ownership"]["scope"], "native_session")
        # A native session opened here is ours to write to, but Hermes has no
        # cross-process writer lock and no claim is made on its behalf.
        self.assertFalse(payload["ownership"]["exclusive"])
        # The room is still addressed by the persisted id, not the live one.
        self.assertEqual(payload["native"]["thread_id"], S_LINKED)
        self.assertEqual(payload["native"]["native_session_id"], "eph0001")

    def test_release_closes_exactly_the_session_this_adapter_opened(self) -> None:
        self.attach()
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/release")
        self.assertEqual(status, 200)
        closes = self.harness.calls("session.close")
        self.assertEqual([call["params"] for call in closes],
                         [{"session_id": "eph0001"}])
        self.assertEqual(payload["closed_exactly"], "eph0001")
        self.assertTrue(payload["runtime_untouched"])
        self.assertTrue(payload["native_work_continues"])
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertEqual(detail["ownership"]["state"], "idle")

    def test_releasing_a_view_that_was_never_attached_closes_nothing(self) -> None:
        room = self.room_for(S_OPEN)
        status, payload = self.harness.post(f"/api/room/{room}/release")
        self.assertEqual(status, 200)
        self.assertEqual(payload["release_state"], "not_attached")
        self.assertEqual(self.harness.calls("session.close"), [])

    def test_two_attaches_reuse_one_native_session(self) -> None:
        self.attach()
        payload = self.attach()
        self.assertTrue(payload["reused"])
        self.assertEqual(len(self.harness.calls("session.resume")), 1)

    def test_stop_reaches_only_our_own_session(self) -> None:
        self.attach()
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/stop")
        self.assertEqual(status, 200)
        self.assertEqual([call["params"] for call in
                          self.harness.calls("session.interrupt")],
                         [{"session_id": "eph0001"}])
        self.assertIn("Background processes", payload["message"])

    def test_stopping_a_conversation_we_do_not_hold_is_refused(self) -> None:
        room = self.room_for(S_DISCORD)
        status, payload = self.harness.post(f"/api/room/{room}/stop")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_attached")
        self.assertEqual(self.harness.calls("session.interrupt"), [])


class BusyReleaseTest(HermesAdapterTest):
    def test_initialization_can_close_but_waiting_and_unknown_cannot(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        service = self.harness.service
        original = service._live_row
        try:
            for live in ({"status": "waiting"}, {}):
                service._live_row = lambda ephemeral: live
                status, _ = self.harness.post(f"/api/room/{self.linked_room}/release")
                self.assertEqual(status, 409)
                self.assertEqual(self.harness.calls("session.close"), [])
            service._live_row = lambda ephemeral: {"status": "starting"}
            status, result = self.harness.post(f"/api/room/{self.linked_room}/release")
            self.assertEqual(status, 200)
            self.assertTrue(result["released"])
            self.assertEqual(len(self.harness.calls("session.close")), 1)
        finally:
            service._live_row = original

    def test_a_running_turn_refuses_release_without_stopping_the_work(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        status, _ = self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "busyrelease01", "body": "read the notes"})
        self.assertEqual(status, 200)
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/release")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "session_busy")
        self.assertFalse(payload["detail"]["work_stopped"])
        self.assertFalse(payload["detail"]["interrupted"])
        # The turn was left running: nothing was closed and nothing interrupted.
        self.assertEqual(self.harness.calls("session.close"), [])
        self.assertEqual(self.harness.calls("session.interrupt"), [])
        self.harness.finish_turn()


class SubmitTest(HermesAdapterTest):
    def setUp(self) -> None:
        super().setUp()
        self.harness.post(f"/api/room/{self.linked_room}/continue")

    def test_a_send_reaches_the_exact_session_once(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "sendonce0001", "body": "how many kettle notes?",
            "thread_id": S_LINKED})
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["submission"]["status"], "accepted")
        self.assertEqual(payload["submission"]["runtime"], "hermes")
        self.assertIn("not a finished answer", payload["message"])
        submits = self.harness.calls("prompt.submit")
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0]["params"],
                         {"session_id": "eph0001", "text": "how many kettle notes?"})
        self.harness.finish_turn()

    def test_a_repeat_of_a_known_client_id_calls_the_runtime_zero_more_times(self) -> None:
        body = {"client_id": "dedupe000001", "body": "same words"}
        first = self.harness.post(f"/api/room/{self.linked_room}/submit", body)[1]
        second = self.harness.post(f"/api/room/{self.linked_room}/submit", body)[1]
        self.assertTrue(second["replayed"])
        self.assertEqual(second["submission"]["status"], first["submission"]["status"])
        self.assertEqual(len(self.harness.calls("prompt.submit")), 1)
        self.harness.finish_turn()

    def test_the_same_id_with_different_text_is_a_conflict_and_sends_nothing(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/submit",
                          {"client_id": "mismatch0001", "body": "first words"})
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit",
                                            {"client_id": "mismatch0001",
                                             "body": "different words"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")
        self.assertEqual(len(self.harness.calls("prompt.submit")), 1)
        self.harness.finish_turn()

    def test_a_send_naming_another_conversation_is_refused(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "wrongthread1", "body": "hello", "thread_id": S_OPEN})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "thread_mismatch")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_a_send_into_a_conversation_we_do_not_hold_is_refused(self) -> None:
        room = self.room_for(S_DISCORD)
        status, payload = self.harness.post(f"/api/room/{room}/submit", {
            "client_id": "notattached1", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_attached")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_a_submission_can_be_recovered_by_its_client_id(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/submit",
                          {"client_id": "recover00001", "body": "find me later"})
        status, payload = self.harness.get("/api/submissions/recover00001")
        self.assertEqual(status, 200)
        self.assertEqual(payload["submission"]["body"], "find me later")
        self.assertEqual(payload["submission"]["thread_id"], S_LINKED)
        self.harness.finish_turn()

    def test_the_person_sees_their_own_message_exactly_once(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/submit",
                          {"client_id": "echoonce0001", "body": "brand new question"})
        _, history = self.harness.get(
            f"/api/room/{self.linked_room}/history?limit=40&direction=asc")
        mine = [item for item in history["items"]
                if item.get("text") == "brand new question"]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["pending"])
        self.harness.finish_turn()


class RefusedSendTest(HermesAdapterTest):
    scenario = {"submit_behaviour": "error"}

    def test_a_refused_send_is_never_reported_accepted(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit",
                                            {"client_id": "refused00001",
                                             "body": "hello"})
        self.assertEqual(status, 502)
        self.assertTrue(payload["failed"])
        self.assertEqual(payload["submission"]["status"], "failed")
        self.assertIn("session busy", payload["submission"]["detail"])

    def test_a_refused_send_is_not_retried_by_a_repeat(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        body = {"client_id": "refused00002", "body": "hello"}
        self.harness.post(f"/api/room/{self.linked_room}/submit", body)
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit", body)
        self.assertEqual(status, 502)
        self.assertTrue(payload["replayed"])
        self.assertEqual(len(self.harness.calls("prompt.submit")), 1)


class UncertainSendTest(HermesAdapterTest):
    scenario = {"submit_behaviour": "hang"}

    def test_an_unanswered_send_is_uncertain_and_is_never_resent(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        body = {"client_id": "uncertain001", "body": "did this arrive?"}
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit", body)
        self.assertEqual(status, 202)
        self.assertTrue(payload["uncertain"])
        self.assertEqual(payload["submission"]["status"], "uncertain")
        self.assertIn("will not resend", payload["submission"]["detail"])
        # A retry reports the same unknown and calls the runtime zero more times.
        status, again = self.harness.post(f"/api/room/{self.linked_room}/submit", body)
        self.assertEqual(status, 202)
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.harness.calls("prompt.submit")), 1)

    def test_a_view_with_an_unsettled_send_is_not_detached_silently(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        self.harness.post(f"/api/room/{self.linked_room}/submit",
                          {"client_id": "unsettled001", "body": "hanging"})
        # Force the row back to dispatching, which is what a crash between the
        # journal write and the answer really leaves behind.
        self.harness.service.journal.settle("unsettled001", hermes.DISPATCHING)
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/release")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "send_unsettled")
        self.assertEqual(self.harness.calls("session.close"), [])


class OddAcknowledgementTest(HermesAdapterTest):
    scenario = {"submit_behaviour": "odd"}

    def test_an_unrecognised_answer_is_uncertain_not_accepted(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit",
                                            {"client_id": "oddack000001",
                                             "body": "hello"})
        self.assertEqual(status, 202)
        self.assertTrue(payload["uncertain"])
        self.assertIn("queued", payload["submission"]["detail"])


class StreamingTest(HermesAdapterTest):
    def test_a_turn_streams_and_then_settles_into_the_stored_transcript(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        before = self.harness.get("/api/bootstrap")[1]["seq"]
        self.harness.post(f"/api/room/{self.linked_room}/submit",
                          {"client_id": "streaming001", "body": "read the notes"})

        started = self.harness.wait_for_event(
            before, lambda e: e.get("type") == "native"
            and e.get("method") == "message.start")
        # The runtime's own event name is kept verbatim, never renamed into a
        # turn lifecycle it did not report.
        self.assertEqual(started["room"], self.linked_room)
        self.assertTrue(started["lifecycle"])
        self.assertTrue(started["active_run"])
        self.assertEqual(started["runtime"], "hermes")

        # While the turn is held open, the streamed text is really on the
        # transcript, marked provisional, before Hermes has stored a row for it.
        live: list[dict] = []
        deadline = time.time() + 8
        while time.time() < deadline:
            _, history = self.harness.get(
                f"/api/room/{self.linked_room}/history?limit=40&direction=asc")
            live = [item for item in history["items"]
                    if item.get("source") == "live" and item["type"] == "agentMessage"]
            if live:
                break
            time.sleep(0.1)
        self.assertEqual([item["text"] for item in live], ["Reading the notes"])
        self.assertTrue(live[0]["pending"])

        # And the room really reads Running, from our own gateway's own word.
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertTrue(detail["native"]["active_run"])
        self.assertTrue(detail["native"]["active_turn"])
        self.assertEqual(detail["native"]["status"], "working")

        self.harness.finish_turn()
        done = self.harness.wait_for_event(
            started["seq"], lambda e: e.get("type") == "native"
            and e.get("method") == "message.complete")
        self.assertTrue(done["lifecycle"])
        self.assertFalse(done["active_run"])

        # Once the runtime says the message is complete, the provisional text is
        # dropped rather than shown alongside the stored row.
        _, history = self.harness.get(
            f"/api/room/{self.linked_room}/history?limit=40&direction=asc")
        self.assertEqual(history["live_items"], 0)

    def test_an_event_for_a_session_we_do_not_hold_addresses_no_room(self) -> None:
        before = self.harness.get("/api/bootstrap")[1]["seq"]
        self.harness.service.on_native_event(
            "message.start", "eph9999", {})
        _, payload = self.harness.get(f"/api/events?after={before}&timeout=1")
        self.assertEqual([e for e in payload["events"] if e.get("type") == "native"], [])


class NewConversationTest(HermesAdapterTest):
    def test_new_creates_a_real_session_and_leaves_the_source_alone(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/command", {
            "command": "/new pane scratch", "client_id": "newcommand01"})
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "completed")
        self.assertFalse(command["sent_as_text"])
        self.assertFalse(command["run_started"])
        self.assertEqual(command["created_session_id"], "20260907_090909_ffffff")
        self.assertTrue(command["source_intact"])
        self.assertFalse(command["source_released"])
        self.assertEqual(command["new_room"], command["room"])

        creates = self.harness.calls("session.create")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["params"]["title"], "pane scratch")
        self.assertEqual(creates[0]["params"]["source"], "ux46")
        # The literal text "/new" is never sent to Pane.
        self.assertEqual(self.harness.calls("prompt.submit"), [])
        # The source conversation was not released.
        self.assertEqual(self.harness.calls("session.close"), [])

        # The new conversation is addressable and already connected.
        _, detail = self.harness.get(f"/api/room/{command['room']}")
        self.assertEqual(detail["ownership"]["state"], "atlas_owned")
        self.assertEqual(detail["native"]["thread_id"], "20260907_090909_ffffff")
        # With no Vault origin, it is Unfiled — the filing rule working.
        self.assertFalse(detail["unfiled"])
        self.assertEqual(detail["project_id"], self.linked_room.split("/")[0])

    def test_native_catalog_command_and_missing_capability(self):
        self.harness.post(f"/api/room/{self.linked_room}/continue", {})
        for text in ("/model test-model", "/refresh", "/help"):
            status, result = self.harness.post(f"/api/room/{self.linked_room}/command", {"command": text, "client_id": "native-command-test"})
            self.assertEqual(status, 200, result)
            self.assertEqual(result["command"]["state"], "completed", result)
        calls = self.harness.calls("slash.exec")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"]["command"], "/model test-model")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_a_repeat_of_one_new_id_creates_exactly_one_conversation(self) -> None:
        body = {"command": "/new", "client_id": "newrepeat001"}
        first = self.harness.post(f"/api/room/{self.linked_room}/command", body)[1]
        second = self.harness.post(f"/api/room/{self.linked_room}/command", body)[1]
        self.assertTrue(second["command"]["recovered"])
        self.assertEqual(second["command"]["room"], first["command"]["room"])
        self.assertEqual(len(self.harness.calls("session.create")), 1)

    def test_every_other_command_is_refused_by_name_and_never_sent_as_text(self) -> None:
        for command in ("/model gpt-5", "/compact", "/goal ship it", "/steer left"):
            status, payload = self.harness.post(
                f"/api/room/{self.linked_room}/command",
                {"command": command, "client_id": "refusedcmd01"})
            self.assertEqual(status, 200, command)
            self.assertEqual(payload["command"]["state"], "unsupported", command)
            self.assertFalse(payload["command"]["sent_as_text"], command)
            self.assertTrue(payload["command"]["reason"], command)
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_status_reads_the_native_block_for_a_session_we_hold(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/command",
                                            {"command": "/status",
                                             "client_id": "statuscmd001"})
        self.assertEqual(payload["command"]["state"], "unsupported")
        self.assertIn("Continue here first", payload["command"]["reason"])
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        _, payload = self.harness.post(f"/api/room/{self.linked_room}/command",
                                       {"command": "/status",
                                        "client_id": "statuscmd002"})
        self.assertEqual(payload["command"]["state"], "completed")
        self.assertIn(S_LINKED, payload["command"]["output"])


class RefusedNewTest(HermesAdapterTest):
    scenario = {"create_behaviour": "error"}

    def test_a_refused_new_creates_nothing_and_says_why(self) -> None:
        _, payload = self.harness.post(f"/api/room/{self.linked_room}/command",
                                       {"command": "/new", "client_id": "newfailed001"})
        self.assertEqual(payload["command"]["state"], "failed")
        self.assertIn("active session limit", payload["command"]["reason"])


class UncertainNewTest(HermesAdapterTest):
    scenario = {"create_behaviour": "hang"}

    def test_an_unanswered_new_is_a_dead_end_that_is_never_repeated(self) -> None:
        body = {"command": "/new", "client_id": "newhanging01"}
        _, payload = self.harness.post(f"/api/room/{self.linked_room}/command", body)
        self.assertEqual(payload["command"]["state"], "uncertain")
        self.assertIn("second conversation", payload["command"]["reason"])
        _, again = self.harness.post(f"/api/room/{self.linked_room}/command", body)
        self.assertEqual(again["command"]["state"], "uncertain")
        self.assertEqual(len(self.harness.calls("session.create")), 1)


class ApprovalTest(HermesAdapterTest):
    def waiting(self) -> dict:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        self.harness.service.on_native_event("approval.request", "eph0001", {
            "kind": "command", "command": "rm -rf /tmp/scratch", "cwd": "/tmp",
            "reason": "the agent wants to delete a directory",
            "request_id": "req-1", "choices": ["once", "session", "deny"]})
        status, payload = self.harness.get("/api/approvals")
        self.assertEqual(status, 200)
        self.assertTrue(payload["supported"])
        self.assertEqual(len(payload["approvals"]), 1)
        return payload["approvals"][0]

    def test_a_request_is_surfaced_with_what_it_is_asking(self) -> None:
        approval = self.waiting()
        self.assertEqual(approval["room"], self.linked_room)
        self.assertEqual(approval["kind"], "command")
        self.assertEqual(approval["params"]["command"], "rm -rf /tmp/scratch")
        self.assertEqual(approval["thread_id"], S_LINKED)
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertEqual(len(detail["approvals"]), 1)

    def test_answering_is_explicit_and_widens_no_permission(self) -> None:
        approval = self.waiting()
        status, payload = self.harness.post("/api/approvals/answer", {
            "key": approval["key"], "kind": approval["kind"], "decision": "accept"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["permissions_changed"])
        calls = self.harness.calls("approval.respond")
        self.assertEqual(calls[0]["params"],
                         {"session_id": "eph0001", "choice": "once", "all": False})
        self.assertEqual(self.harness.get("/api/approvals")[1]["approvals"], [])

    def test_a_request_that_is_not_pending_is_never_answered(self) -> None:
        self.waiting()
        status, payload = self.harness.post("/api/approvals/answer", {
            "key": "a" * 24, "kind": "command", "decision": "accept"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "approval_unknown")
        self.assertEqual(self.harness.calls("approval.respond"), [])

    def test_attention_reports_only_what_the_runtime_is_really_waiting_on(self) -> None:
        _, payload = self.harness.get("/api/attention")
        self.assertEqual(payload["approvals"], [])
        self.assertEqual(payload["checkpoints"], [])
        self.waiting()
        _, payload = self.harness.get("/api/attention")
        self.assertEqual(len(payload["approvals"]), 1)


class QueueTest(HermesAdapterTest):
    def test_a_queued_message_waits_until_a_native_session_is_open(self) -> None:
        room = self.room_for(S_LINKED)
        status, payload = self.harness.post(f"/api/room/{room}/pending", {
            "client_id": "queued000001", "body": "ask me later"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["queued"]["status"], "pending")
        time.sleep(3.0)
        self.assertEqual(self.harness.calls("prompt.submit"), [])

        self.harness.post(f"/api/room/{room}/continue")
        deadline = time.time() + 10
        while time.time() < deadline and not self.harness.calls("prompt.submit"):
            time.sleep(0.2)
        submits = self.harness.calls("prompt.submit")
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0]["params"]["text"], "ask me later")
        self.harness.finish_turn()

    def test_a_queued_message_can_be_cancelled_before_it_is_sent(self) -> None:
        room = self.room_for(S_LINKED)
        _, payload = self.harness.post(f"/api/room/{room}/pending", {
            "client_id": "queuedcancel", "body": "never mind"})
        version = payload["queued"]["version"]
        status, cancelled = self.harness.request(
            "DELETE", f"/api/room/{room}/pending/queuedcancel", {"version": version})
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["cancelled"]["status"], "cancelled")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_a_stale_version_cannot_clobber_a_queued_message(self) -> None:
        room = self.room_for(S_LINKED)
        self.harness.post(f"/api/room/{room}/pending",
                          {"client_id": "queuedstale1", "body": "first"})
        status, payload = self.harness.request(
            "PATCH", f"/api/room/{room}/pending/queuedstale1",
            {"version": 99, "body": "second"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "queue_stale")


class DraftTest(HermesAdapterTest):
    def test_a_draft_is_compare_and_set_and_never_reaches_the_runtime(self) -> None:
        room = self.linked_room
        status, payload = self.harness.request("PUT", f"/api/room/{room}/draft", {
            "body": "half a thought", "base_version": 0, "device": "laptop"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], 1)
        status, conflict = self.harness.request("PUT", f"/api/room/{room}/draft", {
            "body": "from the phone", "base_version": 0, "device": "phone"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "draft_conflict")
        self.assertEqual(conflict["detail"]["device"], "laptop")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_drafts_do_not_leak_between_rooms(self) -> None:
        self.harness.request("PUT", f"/api/room/{self.linked_room}/draft",
                             {"body": "mine", "base_version": 0})
        other = self.room_for(S_OPEN)
        _, payload = self.harness.get(f"/api/room/{other}/draft")
        self.assertEqual(payload["body"], "")


class AttachmentTest(HermesAdapterTest):
    PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + bytes.fromhex("0000000100000001080600000" "01f15c489")
           + b"\x00\x00\x00\x00IEND\xaeB`\x82")

    def test_an_uploaded_file_is_stored_and_served_back_exactly(self) -> None:
        status, payload = self.harness.upload(
            self.linked_room, "notes.txt", b"kettle notes", "text/plain")
        self.assertEqual(status, 201)
        record = payload["file"]
        self.assertTrue(record["sendable"])
        status, raw, headers = self._raw_get(record["download_url"])
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"kettle notes")
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_a_file_is_delivered_through_the_runtime_s_own_hook(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        _, payload = self.harness.upload(
            self.linked_room, "notes.txt", b"kettle notes", "text/plain")
        file_id = payload["file"]["id"]
        status, sent = self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "withfile0001", "body": "read this",
            "attachments": [{"file_id": file_id}]})
        self.assertEqual(status, 200)
        attach = self.harness.calls("file.attach")
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0]["params"]["session_id"], "eph0001")
        self.assertTrue(attach[0]["params"]["data_url"].startswith(
            "data:text/plain;base64,"))
        # The exact file id survives into the journal, and the reference the
        # runtime handed back rides with the message.
        self.assertEqual(sent["submission"]["attachments"], [{"file_id": file_id}])
        self.assertIn("@file:notes.txt",
                      self.harness.calls("prompt.submit")[0]["params"]["text"])
        self.harness.finish_turn()

    def test_an_image_uses_the_byte_upload_hook(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        _, payload = self.harness.upload(
            self.linked_room, "shot.png", self.PNG, "image/png")
        self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "withimage001", "body": "look",
            "attachments": [{"file_id": payload["file"]["id"]}]})
        calls = self.harness.calls("image.attach_bytes")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"]["filename"], "shot.png")
        self.assertEqual(self.harness.calls("file.attach"), [])
        self.harness.finish_turn()

    def test_an_unknown_file_id_is_refused_before_anything_is_sent(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/submit", {
            "client_id": "badfile00001", "body": "look",
            "attachments": [{"file_id": "not-a-real-file"}]})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "file_unknown")
        self.assertEqual(self.harness.calls("prompt.submit"), [])

    def test_a_file_belonging_to_another_room_cannot_be_borrowed(self) -> None:
        _, payload = self.harness.upload(
            self.linked_room, "notes.txt", b"mine", "text/plain")
        other = self.room_for(S_OPEN)
        status, refused = self.harness.post(f"/api/room/{other}/submit", {
            "client_id": "borrowed0001", "body": "look",
            "attachments": [{"file_id": payload["file"]["id"]}]})
        self.assertEqual(status, 404)
        self.assertEqual(refused["error"], "file_unknown")

    def _raw_get(self, path: str) -> tuple[int, bytes, dict]:
        conn = HTTPConnection("127.0.0.1", self.harness.port, timeout=20)
        conn.request("GET", path)
        response = conn.getresponse()
        raw = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, raw, headers


class RequestBoundaryTest(HermesAdapterTest):
    def test_a_mutation_without_the_csrf_token_is_refused(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.harness.port, timeout=20)
        conn.request("POST", f"/api/room/{self.linked_room}/continue", b"{}",
                     {"Content-Length": "2", "Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"], "bad_csrf")
        self.assertEqual(self.harness.calls("session.resume"), [])

    def test_a_mutation_with_somebody_else_s_token_is_refused(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.linked_room}/continue", {},
            headers={"X-Atlas-CSRF": "x" * 43})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")

    def test_a_request_under_a_foreign_host_is_refused(self) -> None:
        status, payload = self.harness.get(
            "/api/bootstrap", headers={"Host": "console.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_host")

    def test_a_configured_browser_origin_is_enforced_on_mutations(self) -> None:
        self.harness.service.config.browser_origin = "https://ux46.example"
        status, payload = self.harness.post(f"/api/room/{self.linked_room}/continue", {},
                                            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_origin")
        self.assertEqual(self.harness.calls("session.resume"), [])

    def test_the_allowlist_bounds_what_can_be_asked_of_the_runtime(self) -> None:
        with self.assertRaises(hermes.NativeError) as caught:
            self.harness.gateway.call("config.set", {"key": "model", "value": "x"})
        self.assertEqual(caught.exception.code, "method_not_allowed")
        # Nothing a browser can reach names a method at all.
        for call in self.harness.calls():
            self.assertIn(call["method"], hermes.ALLOWED_METHODS)

    def test_the_adapter_binds_loopback_only(self) -> None:
        self.assertEqual(hermes.main(["--host", "0.0.0.0"]), 2)

    def test_an_unsupported_action_is_refused_and_never_sent_as_text(self) -> None:
        for action in ("goal", "effort", "speak"):
            status, payload = self.harness.post(
                f"/api/room/{self.linked_room}/{action}", {})
            self.assertEqual(status, 400, action)
            self.assertEqual(payload["error"], "unsupported", action)
        self.assertEqual(self.harness.calls("prompt.submit"), [])


class RefreshTest(HermesAdapterTest):
    def test_refresh_checks_our_own_connection_and_reloads_no_config(self) -> None:
        status, payload = self.harness.post("/api/connection/refresh")
        self.assertEqual(status, 200)
        self.assertTrue(payload["refreshed"])
        self.assertFalse(payload["restarted"])
        self.assertFalse(payload["config_reloaded"])
        self.assertIn("own native gateway", payload["scope"])

    def test_a_dead_gateway_is_restarted_and_lost_sessions_are_admitted(self) -> None:
        self.harness.post(f"/api/room/{self.linked_room}/continue")
        self.harness.gateway.stop()
        status, payload = self.harness.post("/api/connection/refresh")
        self.assertEqual(status, 200)
        self.assertTrue(payload["restarted"])
        self.assertEqual(payload["attachments_dropped"], [self.linked_room])
        self.assertIn("no longer connected here", payload["message"])
        # Nothing was resent, and the room honestly reads as not connected.
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertEqual(detail["ownership"]["state"], "idle")


class DegradedStateTest(HermesAdapterTest):
    def test_an_unreadable_database_makes_everything_unverified(self) -> None:
        service = self.harness.service
        service.state.error = "state.db could not be read (simulated)"
        service.catalog.refresh(force=True)
        _, detail = self.harness.get(f"/api/room/{self.linked_room}")
        self.assertEqual(detail["ownership"]["state"], "unknown")
        self.assertEqual(detail["ownership"]["claim"], "unverified")
        self.assertFalse(detail["controllable"])
        # A history that could not be read is never rendered as "no messages".
        _, history = self.harness.get(f"/api/room/{self.linked_room}/history")
        self.assertTrue(history["unavailable"])
        self.assertTrue(history["retryable"])
        self.assertEqual(history["items"], [])
        # And resuming is refused rather than attempted on a guess.
        status, _ = self.harness.post(f"/api/room/{self.linked_room}/continue")
        self.assertEqual(status, 409)
        self.assertEqual(self.harness.calls("session.resume"), [])


if __name__ == "__main__":
    unittest.main()
