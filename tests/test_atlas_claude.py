"""Focused checks for the CP (Claude Code) adapter.

Nothing here runs the real `claude` binary or reaches a model. A tiny fake CLI
stands in for it: it is handed the same argv and the same stdin the real one
would get, records both, and answers with the native stream-json shape. The
transcripts are written by these tests into a temporary directory that looks
like `~/.claude/projects`, and no session, project or file belonging to anybody
is read or touched.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unittest
import uuid
from argparse import Namespace
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_claude as cp  # noqa: E402

NODE = "test-node"
UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "99999999-8888-4777-8666-555555555555"
UUID_SUB = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


# ---------------------------------------------------------------------------
# a fake Claude Code CLI
# ---------------------------------------------------------------------------

FAKE_CLI = r'''#!/usr/bin/env python3
"""Stands in for the Claude Code CLI: same argv, same stdin, native shapes."""
import json, os, sys, time
from pathlib import Path

argv = sys.argv[1:]
record = Path(os.environ["FAKE_CLI_LOG"])
mode = os.environ.get("FAKE_CLI_MODE", "ok")

if "--version" in argv:
    print("9.9.9 (Fake Claude Code)")
    raise SystemExit(0)

if argv[:1] == ["agents"]:
    # The CLI's own inventory of live sessions, interactive ones included.
    mode = os.environ.get("FAKE_CLI_INVENTORY", "[]")
    if mode == "broken":
        sys.stderr.write("inventory unavailable\n")
        raise SystemExit(1)
    if mode == "garbage":
        print("not json at all")
        raise SystemExit(0)
    print(mode)
    raise SystemExit(0)

stdin = sys.stdin.read()
session = ""
for flag in ("--resume", "--session-id"):
    if flag in argv:
        session = argv[argv.index(flag) + 1]
with record.open("a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()}) + "\n")

if mode == "hang":
    time.sleep(30)
    raise SystemExit(0)
if mode == "slow":
    # A turn that takes a while, the way a real one does.
    time.sleep(float(os.environ.get("FAKE_CLI_DELAY", "3")))
if mode == "crash":
    sys.stderr.write("the fake CLI refused\n")
    raise SystemExit(3)

def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

emit({"type": "system", "subtype": "init", "session_id": session,
      "model": "claude-fake-5", "tools": ["Bash", "Read"], "mcp_servers": [],
      "cwd": os.getcwd(), "permissionMode": "bypassPermissions"})
emit({"type": "assistant", "session_id": session,
      "message": {"role": "assistant", "model": "claude-fake-5",
                  "content": [{"type": "text", "text": "answered"}]}})
if mode == "error":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "session_id": session, "result": "a tool failed"})
    raise SystemExit(0)
if mode == "noresult":
    raise SystemExit(0)

# The real CLI writes the transcript; so does this, so history has something
# true to read afterwards.
transcripts = Path(os.environ["FAKE_CLI_TRANSCRIPTS"])
message = json.loads(stdin.splitlines()[0]) if stdin.strip() else {}
folder = transcripts / os.environ.get("FAKE_CLI_FOLDER", "-fake")
folder.mkdir(parents=True, exist_ok=True)
with (folder / (session + ".jsonl")).open("a") as handle:
    handle.write(json.dumps({"type": "user", "uuid": "u-" + session[:8],
                             "sessionId": session, "cwd": os.getcwd(),
                             "timestamp": "2026-09-09T00:00:00Z",
                             "message": message.get("message")}) + "\n")
    handle.write(json.dumps({"type": "assistant", "uuid": "a-" + session[:8],
                             "sessionId": session, "cwd": os.getcwd(),
                             "timestamp": "2026-09-09T00:00:01Z",
                             "message": {"role": "assistant", "model": "claude-fake-5",
                                         "content": [{"type": "text",
                                                      "text": "answered"}]}}) + "\n")
emit({"type": "result", "subtype": "success", "is_error": False,
      "session_id": session, "result": "answered", "duration_ms": 12,
      "total_cost_usd": 0.0})
'''


def write_transcript(folder: Path, native_id: str, records: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{native_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def conversation(native_id: str, cwd: str, text: str = "hello there") -> list[dict]:
    return [
        {"type": "mode", "mode": "normal", "sessionId": native_id},
        {"type": "user", "uuid": "u1", "sessionId": native_id, "cwd": cwd,
         "timestamp": "2026-09-08T10:00:00Z",
         "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": "a1", "sessionId": native_id, "cwd": cwd,
         "requestId": "req_1", "timestamp": "2026-09-08T10:00:01Z",
         "message": {"role": "assistant", "model": "claude-fable-5", "content": [
             {"type": "thinking", "thinking": "considering", "signature": "x"},
             {"type": "text", "text": "an answer"},
             {"type": "tool_use", "id": "tool_1", "name": "Bash",
              "input": {"command": "git status"}}]}},
        {"type": "user", "uuid": "u2", "sessionId": native_id, "cwd": cwd,
         "timestamp": "2026-09-08T10:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool_1", "content": "clean",
              "is_error": False}]}},
    ]


# ---------------------------------------------------------------------------
# one adapter, one fake world
# ---------------------------------------------------------------------------

class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="ux46-cp-")
        root = Path(self.tmp.name)
        self.root = root
        self.transcripts = root / "claude-projects"
        self.folder = self.transcripts / "-Users-fake-project"
        self.project_root = root / "Projects" / "fixture"
        (self.project_root / "sessions").mkdir(parents=True)
        self.registry = root / "registry.json"
        self.registry.write_text(json.dumps({"projects": [
            {"id": "fixture", "name": "Fixture Project", "root": str(self.project_root)}]}))

        # One conversation the Vault knows about, by an exact native id.
        write_transcript(self.folder, UUID_A, conversation(UUID_A, str(self.project_root)))
        self.write_origins("linked", [
            {"runtime": "claude", "node": NODE, "session_id": UUID_A,
             "cwd": str(self.project_root), "primary": True},
            # A reference that names no Claude Code session on this machine.
            {"runtime": "claude", "node": NODE, "session_id": "session_01FFBXZ7UwAcdp2",
             "cwd": str(self.project_root), "primary": False},
        ])
        # One the Vault has never heard of.
        write_transcript(self.folder, UUID_B, conversation(UUID_B, str(self.project_root),
                                                           "an unfiled question"))
        # And one that is a subagent's own conversation.
        write_transcript(self.folder, UUID_SUB, [
            {"type": "user", "uuid": "s1", "isSidechain": True, "sessionId": UUID_SUB,
             "cwd": str(self.project_root),
             "message": {"role": "user", "content": "subagent work"}}])

        self.cli_log = root / "cli.jsonl"
        self.cli = root / "fake-claude"
        self.cli.write_text(FAKE_CLI)
        os.chmod(self.cli, 0o700)
        os.environ["FAKE_CLI_LOG"] = str(self.cli_log)
        os.environ["FAKE_CLI_TRANSCRIPTS"] = str(self.transcripts)
        os.environ["FAKE_CLI_FOLDER"] = self.folder.name
        os.environ["FAKE_CLI_MODE"] = "ok"
        os.environ["FAKE_CLI_INVENTORY"] = "[]"
        os.environ["FAKE_CLI_DELAY"] = "3"

        self.config = Namespace(
            host="127.0.0.1", port=0, agent_id="cp", agent_name="CP", node=NODE,
            cli=str(self.cli), projects_dir=str(self.transcripts),
            registry=str(self.registry), state_dir=str(root / "state"),
            unfiled_project="unfiled-claude", permission_mode="bypassPermissions",
            model="", lsof="", identity_header="X-Forwarded-User", path_prefix="",
            suggest=2, quiet=True)
        self.service = cp.ClaudeService(self.config)
        self.server = cp.ClaudeServer(("127.0.0.1", 0), self.service)
        self.config.port = self.server.server_address[1]
        self.port = self.config.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.csrf = self.get("/api/bootstrap")[1]["csrf"]

    def tearDown(self):
        self.service.close()
        self.server.shutdown()
        self.server.server_close()
        self.service.journal.close()
        self.tmp.cleanup()

    # -- talking to it -----------------------------------------------------
    def ask(self, method, path, body=None, csrf=None, host=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        token = getattr(self, "csrf", "") if csrf is None else csrf
        if token:
            headers["X-Atlas-CSRF"] = token
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, {"raw": raw}
        finally:
            connection.close()

    def get(self, path):
        return self.ask("GET", path)

    def post(self, path, body=None, **kw):
        return self.ask("POST", path, body if body is not None else {}, **kw)

    def settle(self, room=None, timeout=30):
        """Wait for the background turn to end, the way the console waits."""
        room = room or self.linked_room()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.service.running(room) is None:
                # Process exit precedes catalog refresh and the persisted
                # completion receipt. Wait for that receipt, not a fixed delay.
                records = self.service.journal.recent(room)
                if all(record.mode != "running" for record in records):
                    return
            time.sleep(0.05)
        raise AssertionError("the turn never finished")

    def submission(self, client_id):
        found = self.service.journal.get(client_id)
        return found.as_json() if found else None

    def cli_calls(self):
        if not self.cli_log.exists():
            return []
        return [json.loads(line) for line in
                self.cli_log.read_text().splitlines() if line.strip()]

    def linked_room(self):
        return "fixture/linked"

    def write_origins(self, session: str, origins: list[dict]) -> None:
        (self.project_root / "sessions" / f"{session}.origins.json").write_text(
            json.dumps({"schema_version": 1, "project": "fixture", "session": session,
                        "origins": origins}))


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

class Discovery(AdapterCase):
    def test_star_persists_and_hide_restore_updates_workspace_without_a_native_turn(self):
        room = self.linked_room()
        path = f"/api/room/{room}/pref"
        status, payload = self.post(path, {"pinned": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["pref"]["pinned"])
        saved = cp.journal_store.Journal(self.service.state_dir / "claude-console.sqlite3")
        try:
            self.assertTrue(saved.room_pref(room)["pinned"])
        finally:
            saved.close()

        def project():
            workspace = self.get("/api/workspace")[1]
            self.assertTrue(workspace["prefs"][room]["pinned"])
            return next(p for p in workspace["projects"] if p["id"] == "fixture")

        self.assertEqual([r["id"] for r in project()["pinned"]], [room])
        self.assertEqual(project()["suggested"], [])
        self.post(path, {"hidden": True})
        self.assertEqual(project()["pinned"], [])
        self.assertEqual(project()["hidden_total"], 1)
        self.post(path, {"hidden": False})
        self.assertEqual([r["id"] for r in project()["pinned"]], [room])
        self.post(path, {"pinned": False})
        self.assertFalse(self.get(path)[1]["pref"]["pinned"])
        self.assertEqual(self.cli_calls(), [])

    def test_star_requires_boolean_patch_and_csrf(self):
        path = f"/api/room/{self.linked_room()}/pref"
        for body in ({}, {"pinned": "true"}, {"hidden": 1}):
            self.assertEqual(self.post(path, body)[0], 400)
        self.assertEqual(self.post(path, {"pinned": True}, csrf="wrong")[0], 403)

    def test_picker_recency_is_a_machine_timestamp_for_rooms_and_projects(self):
        # The UI reads updated_ms for exact recency. A human last_active string
        # with hours/minutes is deliberately not interpreted as a date-only day.
        rooms = self.get("/api/rooms")[1]["rooms"]
        room = next(r for r in rooms if r["id"] == self.linked_room())
        expected = int((self.folder / (UUID_A + ".jsonl")).stat().st_mtime * 1000)
        self.assertEqual(room["updated_ms"], expected)
        self.assertLess(abs(time.time() * 1000 - room["updated_ms"]), 86400000)
        workspace = self.get("/api/workspace")[1]
        project = next(p for p in workspace["projects"] if p["id"] == "fixture")
        self.assertEqual(project["updated_ms"], expected)
        self.assertEqual(project["suggested"][0]["updated_ms"], expected)
        projects = self.get("/api/projects")[1]["projects"]
        self.assertEqual(next(p for p in projects if p["id"] == "fixture")["updated_ms"], expected)

    def test_a_verified_origin_files_the_conversation_under_its_project(self):
        status, payload = self.get("/api/rooms")
        self.assertEqual(status, 200)
        rooms = {room["id"]: room for room in payload["rooms"]}
        self.assertIn(self.linked_room(), rooms)
        room = rooms[self.linked_room()]
        self.assertEqual(room["session_id"], UUID_A)
        self.assertEqual(room["origins_status"], "linked")
        self.assertEqual(room["runtime"], "claude")
        self.assertTrue(room["controllable"])

    def test_a_conversation_the_vault_never_heard_of_is_unfiled(self):
        rooms = {r["id"]: r for r in self.get("/api/rooms")[1]["rooms"]}
        self.assertIn(f"unfiled-claude/{UUID_B}", rooms)
        self.assertTrue(rooms[f"unfiled-claude/{UUID_B}"]["origins_status"] == "unlinked")

    def test_a_subagent_conversation_is_not_a_room(self):
        rooms = {r["id"] for r in self.get("/api/rooms")[1]["rooms"]}
        self.assertNotIn(f"unfiled-claude/{UUID_SUB}", rooms)
        self.assertFalse([r for r in rooms if UUID_SUB in r])

    def test_an_unresolvable_reference_is_reported_and_never_remapped(self):
        payload = self.get("/api/workspace")[1]
        unresolved = payload["unresolved_references"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["native_id"], "session_01FFBXZ7UwAcdp2")
        self.assertEqual(unresolved[0]["project_id"], "fixture")
        self.assertIn("not a Claude Code session id", unresolved[0]["reason"])
        # And nothing was invented for it: no room carries that reference.
        rooms = {r["id"]: r for r in self.get("/api/rooms")[1]["rooms"]}
        self.assertFalse([r for r in rooms.values()
                          if r["session_id"] == "session_01FFBXZ7UwAcdp2"])
        # The one conversation the same record does resolve is still exactly one.
        self.assertEqual(len([r for r in rooms.values()
                              if r["id"] == self.linked_room()]), 1)

    def test_bootstrap_says_what_this_adapter_is(self):
        payload = self.get("/api/bootstrap")[1]
        self.assertEqual(payload["agent"]["runtime"], "claude")
        self.assertEqual(payload["agent"]["id"], "cp")
        self.assertEqual(payload["node"], NODE)
        self.assertTrue(payload["capabilities"]["send"])
        self.assertFalse(payload["capabilities"]["voice"])
        self.assertIn("/new", payload["commands"]["supported"])
        self.assertEqual(payload["agent"]["cli_version"], "9.9.9 (Fake Claude Code)")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

class History(AdapterCase):
    def test_native_records_become_the_items_the_console_draws(self):
        status, payload = self.get(f"/api/room/{self.linked_room()}/history?direction=asc")
        self.assertEqual(status, 200)
        kinds = [item["type"] for item in payload["items"]]
        self.assertEqual(kinds, ["userMessage", "reasoning", "agentMessage",
                                 "commandExecution"])
        first = payload["items"][0]
        self.assertEqual(first["text"], "hello there")
        self.assertEqual(payload["items"][2]["phase"], "final_answer")
        self.assertEqual(payload["items"][3]["command"], "git status")
        self.assertEqual(payload["items"][3]["status"], "completed")
        self.assertEqual(payload["items"][3]["output"], "clean")
        self.assertTrue(payload["items"][3]["result_id"])
        self.assertEqual(payload["source"], "transcript")
        self.assertTrue(payload["complete"])

    def test_newest_first_is_what_the_console_asks_for(self):
        payload = self.get(f"/api/room/{self.linked_room()}/history")[1]
        self.assertEqual(payload["items"][0]["type"], "commandExecution")
        self.assertEqual(payload["items"][-1]["type"], "userMessage")

    def test_a_page_smaller_than_the_conversation_says_so(self):
        payload = self.get(f"/api/room/{self.linked_room()}/history?limit=2")[1]
        self.assertEqual(len(payload["items"]), 2)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["next_cursor"], "2")
        earlier = self.get(
            f"/api/room/{self.linked_room()}/history?limit=2&cursor=2")[1]
        self.assertEqual(len(earlier["items"]), 2)

    def test_searching_finds_what_was_said(self):
        payload = self.get(f"/api/room/{self.linked_room()}/search?q=answer")[1]
        self.assertEqual(len(payload["hits"]), 1)
        self.assertEqual(payload["hits"][0]["type"], "agentMessage")

    def test_file_paths_and_failed_output_stay_with_the_matching_call(self):
        records = [
            {"type": "assistant", "uuid": "calls", "message": {"content": [
                {"type": "tool_use", "id": "edit", "name": "Edit",
                 "input": {"file_path": "/project/worker.js"}},
                {"type": "tool_use", "id": "bash", "name": "Bash",
                 "input": {"command": "git commit"}}]}},
            {"type": "user", "uuid": "results", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "bash",
                 "is_error": True, "content": "Exit code 128: failed"},
                {"type": "tool_result", "tool_use_id": "edit", "content": "updated"},
                {"type": "tool_result", "tool_use_id": "missing", "content": "keep me"}]}},
        ]
        items = cp.transcript_items(records)
        cp._settle_tool_items(items)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["changes"][0]["path"], "/project/worker.js")
        self.assertEqual(items[0]["output"], "updated")
        self.assertEqual(items[1]["status"], "failed")
        self.assertEqual(items[1]["output"], "Exit code 128: failed")
        self.assertEqual(items[1]["result_id"], "results:0")
        self.assertEqual(items[2]["output"], "keep me")


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

class Sending(AdapterCase):
    def test_a_message_is_handed_to_the_cli_with_the_documented_flags(self):
        status, payload = self.post(
            f"/api/room/{self.linked_room()}/submit",
            {"client_id": "client-aaaaaaaa", "body": "please look"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "accepted")
        self.assertTrue(payload["delivered"])
        self.settle()
        calls = self.cli_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        for flag in ("-p", "--verbose", "--include-partial-messages"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        # The exact conversation, resumed by its exact native id.
        self.assertEqual(argv[argv.index("--resume") + 1], UUID_A)
        self.assertNotIn("--session-id", argv)
        sent = json.loads(calls[0]["stdin"].strip())
        self.assertEqual(sent["type"], "user")
        self.assertEqual(sent["message"]["content"],
                         [{"type": "text", "text": "please look"}])

    def test_the_turn_runs_where_the_conversation_lives(self):
        self.post(f"/api/room/{self.linked_room()}/submit",
                  {"client_id": "client-bbbbbbbb", "body": "hello"})
        self.settle()
        self.assertEqual(Path(self.cli_calls()[0]["cwd"]).resolve(),
                         self.project_root.resolve())

    def test_the_same_client_id_is_never_sent_twice(self):
        first = self.post(f"/api/room/{self.linked_room()}/submit",
                          {"client_id": "client-cccccccc", "body": "once only"})
        self.assertEqual(first[0], 200)
        self.settle()
        second = self.post(f"/api/room/{self.linked_room()}/submit",
                           {"client_id": "client-cccccccc", "body": "once only"})
        self.assertEqual(second[0], 200)
        self.assertTrue(second[1]["duplicate"])
        self.settle()
        self.assertEqual(len(self.cli_calls()), 1)

    def test_the_same_client_id_with_different_words_is_refused(self):
        self.post(f"/api/room/{self.linked_room()}/submit",
                  {"client_id": "client-dddddddd", "body": "one thing"})
        self.settle()
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-dddddddd", "body": "another"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")
        self.assertEqual(len(self.cli_calls()), 1)

    def test_a_turn_that_never_reported_a_result_stays_delivered_and_unfinished(self):
        os.environ["FAKE_CLI_MODE"] = "noresult"
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-eeeeeeee", "body": "hm"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "accepted")
        self.settle()
        # Delivered stays delivered; what the turn did is said separately.
        settled = self.submission("client-eeeeeeee")
        self.assertEqual(settled["status"], "accepted")
        self.assertEqual(settled["mode"], "no_result")
        self.assertIn("cannot say the turn finished", settled["detail"])
        self.assertEqual(len(self.cli_calls()), 1)

    def test_an_error_after_handoff_does_not_unsay_the_handoff(self):
        os.environ["FAKE_CLI_MODE"] = "error"
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-ffffffff", "body": "hm"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "accepted")
        self.assertTrue(payload["delivered"])
        self.settle()
        settled = self.submission("client-ffffffff")
        self.assertEqual(settled["status"], "accepted")
        self.assertEqual(settled["mode"], "error_during_execution")
        self.assertIn("was delivered", settled["detail"])
        self.assertIn("error_during_execution", settled["detail"])
        # And it is not resent, ever.
        again = self.post(f"/api/room/{self.linked_room()}/submit",
                          {"client_id": "client-ffffffff", "body": "hm"})
        self.assertTrue(again[1]["duplicate"])
        self.assertEqual(len(self.cli_calls()), 1)

    def test_a_cli_that_exits_badly_after_handoff_is_still_delivered(self):
        os.environ["FAKE_CLI_MODE"] = "crash"
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-gggggggg", "body": "hm"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["delivered"])
        self.settle()
        settled = self.submission("client-gggggggg")
        self.assertEqual(settled["status"], "accepted")
        self.assertEqual(settled["mode"], "exit_3")
        self.assertIn("refused", settled["detail"])

    def test_a_cli_that_cannot_be_started_was_never_delivered(self):
        self.service.cli = str(self.root / "no-such-binary")
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-tttttttt", "body": "hm"})
        self.assertEqual(status, 502)
        self.assertEqual(payload["state"], "failed")
        self.assertFalse(payload["delivered"])
        self.assertIn("could not be started", payload["message"])
        self.assertEqual(self.cli_calls(), [])

    def test_a_slow_turn_answers_the_request_straight_away(self):
        os.environ["FAKE_CLI_MODE"] = "slow"
        os.environ["FAKE_CLI_DELAY"] = "4"
        started = time.monotonic()
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-uuuuuuuu", "body": "take your time"})
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "accepted")
        self.assertTrue(payload["running"])
        self.assertLess(elapsed, 2.5, f"the request waited {elapsed:.1f}s for the turn")
        # And reading the room while it runs is fine.
        state = self.get(f"/api/room/{self.linked_room()}")[1]
        self.assertTrue(state["native"]["active_run"])
        self.assertEqual(state["native"]["active_turn"], "client-uuuuuuuu")
        self.settle(timeout=40)
        self.assertEqual(self.submission("client-uuuuuuuu")["mode"], "success")

    def test_two_conversations_run_at_the_same_time(self):
        os.environ["FAKE_CLI_MODE"] = "slow"
        os.environ["FAKE_CLI_DELAY"] = "4"
        other = f"unfiled-claude/{UUID_B}"
        started = time.monotonic()
        first = self.post(f"/api/room/{self.linked_room()}/submit",
                          {"client_id": "client-vvvvvvvv", "body": "one"})
        second = self.post(f"/api/room/{other}/submit",
                           {"client_id": "client-wwwwwwww", "body": "two"})
        elapsed = time.monotonic() - started
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200, second[1])
        self.assertLess(elapsed, 3.0, "the second room waited for the first")
        self.assertIsNotNone(self.service.running(self.linked_room()))
        self.assertIsNotNone(self.service.running(other))
        self.settle(timeout=40)
        self.settle(other, timeout=40)
        self.assertEqual(len(self.cli_calls()), 2)
        for client in ("client-vvvvvvvv", "client-wwwwwwww"):
            self.assertEqual(self.submission(client)["mode"], "success")

    def test_a_second_message_while_a_turn_runs_is_refused_not_queued_silently(self):
        os.environ["FAKE_CLI_MODE"] = "slow"
        os.environ["FAKE_CLI_DELAY"] = "4"
        self.post(f"/api/room/{self.linked_room()}/submit",
                  {"client_id": "client-xxxxxxxx", "body": "first"})
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-yyyyyyyy", "body": "second"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "busy")
        # Refused before anything was reserved, so the same id still works later.
        self.assertIsNone(self.submission("client-yyyyyyyy"))
        self.settle(timeout=40)

    def test_the_message_reaches_history_afterwards(self):
        self.post(f"/api/room/{self.linked_room()}/submit",
                  {"client_id": "client-hhhhhhhh", "body": "written down"})
        self.settle()
        payload = self.get(f"/api/room/{self.linked_room()}/history?direction=asc")[1]
        texts = [i.get("text") for i in payload["items"] if i["type"] == "userMessage"]
        self.assertIn("written down", texts)

    def test_a_draft_for_another_conversation_is_refused(self):
        status, payload = self.post(
            f"/api/room/{self.linked_room()}/submit",
            {"client_id": "client-iiiiiiii", "body": "hi", "thread_id": UUID_B})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "wrong_target")
        self.assertEqual(self.cli_calls(), [])

    def test_a_slash_command_is_never_delivered_as_chat_text(self):
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-jjjjjjjj", "body": "/compact"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "is_command")
        self.assertEqual(self.cli_calls(), [])


# ---------------------------------------------------------------------------
# who is in the transcript
# ---------------------------------------------------------------------------

class Ownership(AdapterCase):
    def test_a_transcript_somebody_else_has_open_is_not_written_into(self):
        # Another process entirely, the way a live Claude Code session is.
        path = self.folder / f"{UUID_A}.jsonl"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; f=open(sys.argv[1]); sys.stdout.write('open\\n');"
             "sys.stdout.flush(); time.sleep(60)", str(path)],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "open")
            state = self.get(f"/api/room/{self.linked_room()}")[1]
            self.assertEqual(state["ownership"]["state"], "held_elsewhere")
            self.assertIn(str(holder.pid), state["ownership"]["detail"])
            status, payload = self.post(
                f"/api/room/{self.linked_room()}/submit",
                {"client_id": "client-kkkkkkkk", "body": "may I"})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"], "held_elsewhere")
            self.assertIn("has this transcript open", payload["message"])
            self.assertEqual(self.cli_calls(), [])
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        # It was refused, not taken: that process is still whatever it was.
        state = self.get(f"/api/room/{self.linked_room()}")[1]
        self.assertEqual(state["ownership"]["state"], "atlas_owned")

    def test_an_unverifiable_answer_is_held_not_assumed_free(self):
        self.service.guard.available = False
        state = self.get(f"/api/room/{self.linked_room()}")[1]
        self.assertEqual(state["ownership"]["state"], "held_elsewhere")
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-llllllll", "body": "may I"})
        self.assertEqual(status, 409)
        self.assertIn("cannot check", payload["message"])
        self.assertEqual(self.cli_calls(), [])

    def test_release_stops_only_this_adapter_and_keeps_the_transcript(self):
        os.environ["FAKE_CLI_MODE"] = "hang"
        room = self.linked_room()
        before = (self.folder / f"{UUID_A}.jsonl").read_bytes()
        status, payload = self.post(f"/api/room/{room}/submit",
                                    {"client_id": "client-mmmmmmmm", "body": "wait"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["delivered"])
        turn = self.service.running(room)
        self.assertIsNotNone(turn, "the turn never started")
        pid = turn.process.pid
        status, payload = self.post(f"/api/room/{room}/release")
        self.assertEqual(status, 200)
        self.assertTrue(payload["stopped_turn"])
        self.settle(timeout=20)
        # It was delivered and then stopped here; both are said, neither is
        # mistaken for the other.
        settled = self.submission("client-mmmmmmmm")
        self.assertEqual(settled["status"], "accepted")
        self.assertEqual(settled["mode"], "stopped")
        self.assertIn("stopped here", settled["detail"])
        # The process this adapter started is gone; the transcript is not.
        with self.assertRaises(OSError):
            os.kill(pid, 0)
        self.assertEqual((self.folder / f"{UUID_A}.jsonl").read_bytes(), before)

    def test_a_claude_process_running_this_exact_session_is_evidence_too(self):
        # No descriptor held, but a process whose command line names the
        # session. That is somebody working in it.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; sys.stdout.write('claude --resume ' + sys.argv[1] + '\\n');"
             "sys.stdout.flush(); time.sleep(60)", UUID_A],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn(UUID_A, holder.stdout.readline())
            status, payload = self.post(
                f"/api/room/{self.linked_room()}/submit",
                {"client_id": "client-qqqqqqqq", "body": "may I"})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"], "held_elsewhere")
            self.assertIn("is running this exact session", payload["message"])
            self.assertEqual(self.cli_calls(), [])
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    def test_a_session_the_cli_says_it_is_running_is_left_alone(self):
        # An interactive context sitting idle: no descriptor held, nothing in
        # anybody's argv, and the CLI's own inventory naming it.
        os.environ["FAKE_CLI_INVENTORY"] = json.dumps([
            {"pid": 4242, "cwd": "/somewhere", "kind": "interactive",
             "sessionId": UUID_A, "name": "user's terminal"}])
        self.service.guard.inventory(force=True)
        state = self.get(f"/api/room/{self.linked_room()}")[1]
        self.assertEqual(state["ownership"]["state"], "held_elsewhere")
        self.assertIn("4242", state["ownership"]["detail"])
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-zzzzzzzz", "body": "may I"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "held_elsewhere")
        self.assertIn("already running this session", payload["message"])
        self.assertEqual(self.cli_calls(), [])
        # A different session in the inventory is nothing to do with this one.
        os.environ["FAKE_CLI_INVENTORY"] = json.dumps([
            {"pid": 4242, "kind": "interactive", "sessionId": UUID_B}])
        self.service.guard.inventory(force=True)
        self.assertEqual(
            self.get(f"/api/room/{self.linked_room()}")[1]["ownership"]["state"],
            "atlas_owned")

    def test_an_inventory_that_cannot_be_read_fails_closed(self):
        for broken in ("broken", "garbage"):
            os.environ["FAKE_CLI_INVENTORY"] = broken
            self.service.guard.inventory(force=True)
            state = self.get(f"/api/room/{self.linked_room()}")[1]
            self.assertEqual(state["ownership"]["state"], "held_elsewhere", broken)
            self.assertFalse(state["ownership"]["detected"])
            status, payload = self.post(
                f"/api/room/{self.linked_room()}/submit",
                {"client_id": "client-0000000" + broken[0], "body": "may I"})
            self.assertEqual(status, 409, broken)
            self.assertIn("inventory", payload["message"])
        self.assertEqual(self.cli_calls(), [])

    def test_release_with_nothing_running_touches_nothing(self):
        status, payload = self.post(f"/api/room/{self.linked_room()}/release")
        self.assertEqual(status, 200)
        self.assertEqual(payload["release_state"], "released")
        self.assertIn("untouched", payload["message"])
        self.assertEqual(self.cli_calls(), [])


# ---------------------------------------------------------------------------
# new conversations
# ---------------------------------------------------------------------------

class Creation(AdapterCase):
    def test_a_new_conversation_gets_a_record_an_origin_and_an_exact_id(self):
        status, payload = self.post("/api/sessions", {
            "client_id": "make-a-new-one1", "title": "Fresh CP work",
            "project_id": "fixture"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["state"], "created")
        native = payload["session_id"]
        self.assertTrue(cp.UUID_RE.match(native), native)
        room_id = payload["new_room"]
        sidecars = list((self.project_root / "sessions").glob("*.origins.json"))
        written = [json.loads(p.read_text()) for p in sidecars]
        origins = [o for data in written for o in data["origins"]
                   if o.get("session_id") == native]
        self.assertEqual(len(origins), 1)
        self.assertEqual(origins[0]["runtime"], "claude")
        self.assertEqual(origins[0]["node"], NODE)
        # It is a room straight away, and honest about having no transcript.
        state = self.get(f"/api/room/{room_id}")[1]
        self.assertEqual(state["native"]["session_id"], native)
        self.assertFalse(state["native"]["started"])
        history = self.get(f"/api/room/{room_id}/history")[1]
        self.assertEqual(history["items"], [])
        self.assertEqual(history["source"], "unstarted")
        self.assertIn("has not had a turn yet", history["note"])

    def test_the_first_turn_uses_that_id_and_not_a_resume(self):
        payload = self.post("/api/sessions", {
            "client_id": "make-a-new-one2", "title": "Second fresh",
            "project_id": "fixture"})[1]
        native = payload["session_id"]
        status, sent = self.post(f"/api/room/{payload['new_room']}/submit",
                                 {"client_id": "client-nnnnnnnn", "body": "first words"})
        self.assertEqual(status, 200, sent)
        self.settle(payload["new_room"])
        argv = self.cli_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], native)
        self.assertNotIn("--resume", argv)

    def test_a_project_that_is_not_there_is_refused(self):
        status, payload = self.post("/api/sessions", {
            "client_id": "make-a-new-one3", "title": "Nowhere",
            "project_id": "not-a-project"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_session")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

class Commands(AdapterCase):
    def test_new_retains_project_and_repeated_request_keeps_native_id(self):
        body = {"command": "/new Fresh work", "client_id": "cp-new-repeat-111"}
        status, first = self.post(f"/api/room/{self.linked_room()}/command", body)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["command"]["state"], "completed")
        self.assertTrue(first["new_room"].startswith("fixture/"))
        _, again = self.post(f"/api/room/{self.linked_room()}/command", body)
        self.assertEqual(first["session_id"], again["session_id"])
        self.assertEqual(self.cli_calls(), [])

    def test_effort_aliases_and_refresh(self):
        for text in ("/reasoning high", "/reasononing low", "/refresh"):
            status, result = self.post(f"/api/room/{self.linked_room()}/command", {"command": text})
            self.assertEqual(status, 200, result)
            self.assertEqual(result["command"]["state"], "completed")
        self.assertEqual(self.cli_calls(), [])

    def test_status_says_what_the_next_turn_will_use(self):
        payload = self.post(f"/api/room/{self.linked_room()}/command",
                            {"command": "/status"})[1]
        native = payload["command"]["native"]
        self.assertEqual(native["permission_mode"], "bypassPermissions")
        self.assertEqual(native["session_id"], UUID_A)
        self.assertEqual(Path(native["cwd"]).resolve(), self.project_root.resolve())

    def test_model_and_effort_change_the_next_launch_only(self):
        self.post(f"/api/room/{self.linked_room()}/command", {"command": "/model opus"})
        self.post(f"/api/room/{self.linked_room()}/command", {"command": "/effort high"})
        self.post(f"/api/room/{self.linked_room()}/submit",
                  {"client_id": "client-oooooooo", "body": "go"})
        self.settle()
        argv = self.cli_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_an_effort_the_cli_does_not_take_is_refused_here(self):
        status, payload = self.post(f"/api/room/{self.linked_room()}/command",
                                    {"command": "/effort enormous"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_effort")

    def test_an_unsupported_command_is_named_not_forwarded(self):
        for command in ("/compact", "/goal", "/steer now"):
            status, payload = self.post(f"/api/room/{self.linked_room()}/command",
                                        {"command": command})
            self.assertEqual(status, 400, command)
            self.assertEqual(payload["error"], "unsupported_command")
        self.assertEqual(self.cli_calls(), [])

    def test_help_lists_exactly_what_works(self):
        payload = self.post(f"/api/room/{self.linked_room()}/command",
                            {"command": "/help"})[1]
        self.assertEqual(payload["command"]["supported"], list(cp.COMMANDS_SUPPORTED))


# ---------------------------------------------------------------------------
# the boundary
# ---------------------------------------------------------------------------

class Boundary(AdapterCase):
    def test_a_mutation_without_the_token_is_refused(self):
        status, payload = self.post(f"/api/room/{self.linked_room()}/submit",
                                    {"client_id": "client-pppppppp", "body": "hi"},
                                    csrf="")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")
        status, _ = self.post(f"/api/room/{self.linked_room()}/submit",
                              {"client_id": "client-pppppppp", "body": "hi"},
                              csrf="not-the-token")
        self.assertEqual(status, 403)
        self.assertEqual(self.cli_calls(), [])

    def test_a_request_addressed_to_another_host_is_refused(self):
        status, payload = self.ask("GET", "/api/bootstrap", host="ux46.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_host")

    def test_reading_needs_no_token(self):
        status, _ = self.ask("GET", "/api/workspace", csrf="")
        self.assertEqual(status, 200)

    def test_a_room_id_that_is_not_one_is_refused(self):
        status, payload = self.get("/api/room/../../etc/passwd")
        self.assertIn(status, (400, 404))
        self.assertNotIn("root:", json.dumps(payload))

    def test_an_unknown_conversation_is_a_plain_404(self):
        status, payload = self.get(f"/api/room/fixture/{uuid.uuid4()}")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "room_unknown")

    def test_the_state_directory_is_private(self):
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o700)

    def test_unsupported_surfaces_say_what_they_are(self):
        for path in ("goal", "speak"):
            status, payload = self.post(f"/api/room/{self.linked_room()}/{path}")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "unsupported")
            self.assertTrue(payload["message"])


if __name__ == "__main__":
    unittest.main()
