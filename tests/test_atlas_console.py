"""Focused checks for the Atlas console.

Everything that touches a runtime here talks to tests/fixtures/fake_app_server.py,
which speaks the documented app-server wire format. One test reads a real
existing native session read-only (metadata and counts only, no transcript, no
resume, no send) and is skipped when the local Codex CLI is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from argparse import Namespace
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_console as console  # noqa: E402
import atlas_journal as journal  # noqa: E402
import atlas_native as native  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_app_server.py"
THREAD_ONE = "01a00000-0000-7000-8000-000000000001"
THREAD_TWO = "01a00000-0000-7000-8000-000000000002"


def write_vault(base: Path) -> Path:
    """A tiny registry and Vault: one project, three rooms of different kinds."""

    project_root = base / "Projects" / "fixture"
    sessions = project_root / "sessions"
    sessions.mkdir(parents=True)

    def record(name: str, title: str, origins: list[dict], extra: str = "") -> None:
        (sessions / f"{name}.md").write_text(
            "---\n"
            f"project: fixture\nsession: {name}\ntitle: {title}\n"
            "date: 2026-09-05\nupdated: \"2026-09-05\"\nstatus: active\n"
            f"keywords: fixture, {name}\norigins: \"{name}.origins.json\"\n"
            "checkpoint_schema: 1\ncheckpoint_at: \"2026-09-05T12:00:00Z\"\n"
            "checkpoint_node: \"user-mac\"\ncheckpoint_reporter: \"local-workspace\"\n"
            f"{extra}"
            "---\n\n# fixture\n\nSample body.\n",
            encoding="utf-8",
        )
        (sessions / f"{name}.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "fixture", "session": name, "origins": origins,
        }), encoding="utf-8")

    record("console-work", "Fixture console work",
           [{"runtime": "codex", "node": "user-mac", "session_id": THREAD_ONE,
             "cwd": "/tmp/atlas-fixture", "captured": "2026-09-05", "primary": True}],
           extra="checkpoint_state: \"needs-human\"\ncheckpoint_need: \"decision\"\n"
                 "checkpoint_next: \"Choose the fixture label set.\"\n")
    record("second-room", "Fixture second room",
           [{"runtime": "codex", "node": "user-mac", "session_id": THREAD_TWO,
             "cwd": "/tmp/atlas-fixture-2", "captured": "2026-09-05", "primary": True}],
           extra="checkpoint_state: \"working\"\ncheckpoint_need: \"none\"\n"
                 "checkpoint_next: \"Continue the fixture build.\"\n")
    record("claude-notes", "Fixture Claude notes",
           [{"runtime": "claude-code", "node": "user-mac", "session_id": "",
             "cwd": "/tmp/atlas-fixture-3", "captured": "2026-09-05", "primary": True}],
           extra="checkpoint_state: \"working\"\ncheckpoint_need: \"none\"\n"
                 "checkpoint_next: \"Nothing pending.\"\n")

    registry = base / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": 1, "updated": "2026-09-05", "node_id": "user-mac",
        "collective_id": "workspace",
        "nodes": [{"id": "user-mac", "name": "Fixture Mac", "status": "local"}],
        "projects": [{"id": "fixture", "name": "Fixture", "root": str(project_root),
                      "aliases": ["fixture project"]}],
    }), encoding="utf-8")
    return registry


class StubProbe:
    """Deterministic ownership for tests: no real processes are inspected."""

    def __init__(self):
        self.table: dict[str, set[int]] = {}
        self.available = True

    def check(self, thread_id, *, own_pids=(), atlas_owned=False, refresh=False):
        if not self.available:
            return native.Ownership(thread_id, (), atlas_owned, False, "probe unavailable")
        external = sorted(self.table.get(thread_id, set()) - set(own_pids))
        return native.Ownership(thread_id, tuple(external), atlas_owned, True)


class ConsoleHarness:
    """A console under test.

    ``public=False`` is the local-only deployment (loopback owner). Pass
    ``public=True`` to configure a private HTTPS origin; in that mode there is
    no local-owner exemption, so calls must name the public host and identity.
    """

    def __init__(self, mode: str = "normal", public: bool = False):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-console-test-"))
        registry = write_vault(self.tmp)
        env_command = [sys.executable, str(FIXTURE)]
        os.environ["ATLAS_FIXTURE_MODE"] = mode
        args = Namespace(
            host="127.0.0.1", port=0,
            state_dir=str(self.tmp / "state"),
            registry=str(registry),
            public_origin="https://workspace.example.com" if public else "",
            public_user="user@example.com" if public else "",
            codex_command=env_command,
            allow_test_thread=False, test_thread_cwd="",
            start_runtime=False, quiet=True,
        )
        self.service = console.ConsoleService(args)
        self.server = console.ConsoleServer(("127.0.0.1", 0), console.ConsoleHandler, self.service)
        self.port = self.server.server_address[1]
        self.service.auth.local_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.service.config.port = self.port
        self.probe = StubProbe()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def start_runtime(self):
        """Start the read/catalog runtime and give every worker one probe."""

        sessions = self.service.sessions
        self.service.workers.set_probe(self.probe)
        sessions.probe = self.probe
        return sessions

    def worker_sessions(self, thread_id=THREAD_ONE):
        """The runtime that holds one attached conversation."""

        worker = self.service.workers.worker(thread_id)
        return worker.sessions if worker else None

    def close(self):
        try:
            self.service.workers.shutdown()
        finally:
            self.server.shutdown()
            self.server.server_close()
            shutil.rmtree(self.tmp, ignore_errors=True)
            os.environ.pop("ATLAS_FIXTURE_MODE", None)

    # -- HTTP --------------------------------------------------------------
    def call(self, method: str, path: str, body=None, host=None, headers=None, csrf=True,
             identity=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        head = {"Host": host or f"127.0.0.1:{self.port}"}
        if identity:
            head["Tailscale-User-Login"] = identity
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
            head["Origin"] = f"http://127.0.0.1:{self.port}" if not host or host.startswith("127.") \
                else "https://workspace.example.com"
            if csrf:
                head["X-Atlas-CSRF"] = self.service.csrf_token
        head.update(headers or {})
        conn.request(method, path, body=payload, headers=head)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", "replace")[:200]}
        return response.status, parsed


class SecurityTests(unittest.TestCase):
    """Local-only deployment: loopback is the owner, nothing else is."""

    @classmethod
    def setUpClass(cls):
        cls.h = ConsoleHarness(public=False)

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_local_host_is_local_owner(self):
        status, payload = self.h.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "local-owner")

    def test_unknown_host_is_denied(self):
        status, payload = self.h.call("GET", "/api/bootstrap", host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(payload["message"], "unknown host")

    def test_mutation_requires_matching_origin(self):
        status, payload = self.h.call(
            "PUT", "/api/room/fixture/console-work/draft",
            body={"body": "x", "base_version": 0},
            headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_origin")

    def test_mutation_requires_csrf_token(self):
        status, payload = self.h.call(
            "PUT", "/api/room/fixture/console-work/draft",
            body={"body": "x", "base_version": 0}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")

    def test_security_headers_and_no_cors(self):
        conn = HTTPConnection("127.0.0.1", self.h.port, timeout=10)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.h.port}",
                                          "Origin": "https://elsewhere.example"})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("Cache-Control"), "no-store, private")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        conn.close()

    def test_oversized_body_is_refused(self):
        status, payload = self.h.call(
            "PUT", "/api/room/fixture/console-work/draft",
            body={"body": "x" * (console.MAX_BODY + 10), "base_version": 0})
        self.assertEqual(status, 413)

    def test_unknown_room_and_bad_room_id(self):
        self.assertEqual(self.h.call("GET", "/api/room/fixture/nope")[0], 404)
        self.assertIn(self.h.call("GET", "/api/room/..%2F..%2Fetc/passwd")[0], (400, 404))


class PrivateOriginTests(unittest.TestCase):
    """Configured HTTPS mode: the shared listener trusts only the proxy."""

    @classmethod
    def setUpClass(cls):
        cls.h = ConsoleHarness(public=True)
        cls.public = "workspace.example.com"

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_exact_identity_at_the_public_host_is_allowed(self):
        status, payload = self.h.call("GET", "/api/bootstrap", host=self.public,
                                      identity="user@example.com")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "private-network")
        self.assertEqual(payload["identity"], "user@example.com")

    def test_public_host_without_identity_is_denied(self):
        status, payload = self.h.call("GET", "/api/bootstrap", host=self.public)
        self.assertEqual(status, 403)
        self.assertIn("identity", payload["message"])

    def test_public_host_with_wrong_identity_is_denied(self):
        status, payload = self.h.call("GET", "/api/bootstrap", host=self.public,
                                      identity="someone-else@example.com")
        self.assertEqual(status, 403)
        self.assertEqual(payload["message"], "identity not allowed")

    def test_spoofed_local_host_never_becomes_local_owner(self):
        """The exact bypass Local found through Tailscale Serve."""

        for host in (f"localhost:{self.h.port}", f"127.0.0.1:{self.h.port}",
                     "localhost:8878", "127.0.0.1:8878", "[::1]:8878"):
            for identity in (None, "user@example.com", "someone-else@example.com",
                             " user@example.com "):
                status, payload = self.h.call("GET", "/api/bootstrap", host=host,
                                              identity=identity)
                self.assertEqual(status, 403, f"{host} / {identity} was not denied")
                self.assertNotEqual(payload.get("mode"), "local-owner")
                self.assertEqual(payload["error"], "denied")

    def test_data_and_control_routes_are_denied_on_a_spoofed_host(self):
        for method, path, body in (
            ("GET", "/api/rooms", None),
            ("GET", "/api/room/fixture/console-work", None),
            ("GET", "/api/room/fixture/console-work/history", None),
            ("POST", "/api/room/fixture/console-work/continue", {}),
            ("POST", "/api/room/fixture/console-work/submit",
             {"client_id": "spoof123456", "body": "hello"}),
            ("POST", "/api/connection/refresh", {}),
        ):
            status, payload = self.h.call(method, path, body=body,
                                          host=f"localhost:{self.h.port}",
                                          identity="user@example.com")
            self.assertEqual(status, 403, f"{method} {path} leaked on a spoofed host")
            self.assertEqual(payload["error"], "denied")

    def test_mutations_still_need_origin_and_csrf_at_the_public_host(self):
        status, payload = self.h.call(
            "PUT", "/api/room/fixture/console-work/draft",
            body={"body": "x", "base_version": 0}, host=self.public,
            identity="user@example.com", headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_origin")
        status, payload = self.h.call(
            "PUT", "/api/room/fixture/console-work/draft",
            body={"body": "x", "base_version": 0}, host=self.public,
            identity="user@example.com", csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")
        status, payload = self.h.call(
            "POST", "/api/connection/refresh", body={}, host=self.public,
            identity="user@example.com", csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ConsoleHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_rooms_carry_honest_capability_labels(self):
        status, payload = self.h.call("GET", "/api/rooms")
        self.assertEqual(status, 200)
        rooms = {room["id"]: room for room in payload["rooms"]}
        self.assertEqual(rooms["fixture/console-work"]["capability"], "codex-local")
        self.assertTrue(rooms["fixture/console-work"]["controllable"])
        self.assertEqual(rooms["fixture/claude-notes"]["capability"], "claude")
        self.assertFalse(rooms["fixture/claude-notes"]["controllable"])
        self.assertIn("no control", rooms["fixture/claude-notes"]["capability_label"])

    def test_search_matches_and_reports_totals(self):
        status, payload = self.h.call("GET", "/api/rooms?query=second")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in payload["rooms"]], ["fixture/second-room"])
        self.assertEqual(payload["total"], 1)

    def test_checkpoint_is_reported_not_verified(self):
        status, payload = self.h.call("GET", "/api/attention")
        self.assertEqual(status, 200)
        needs = [room["id"] for room in payload["needs_person"]]
        self.assertIn("fixture/console-work", needs)
        room = payload["needs_person"][0]
        self.assertEqual(room["checkpoint"]["reporter"], "local-workspace")
        self.assertEqual(room["attention"]["source"], "checkpoint")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.h = ConsoleHarness()
        self.sessions = self.h.start_runtime()

    def tearDown(self):
        self.h.close()

    def take(self, room="fixture/console-work"):
        return self.h.call("POST", f"/api/room/{room}/continue", body={})

    def test_history_pagination_is_honest(self):
        status, payload = self.h.call(
            "GET", "/api/room/fixture/console-work/history?limit=3&direction=desc")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 3)
        self.assertFalse(payload["complete"])          # never claim a full history
        self.assertTrue(payload["next_cursor"])
        kinds = {item["type"] for item in payload["items"]}
        self.assertTrue(kinds <= {"userMessage", "agentMessage", "commandExecution",
                                  "fileChange", "reasoning"})
        # Follow the runtime's own cursors to the end; only the last page may
        # call itself complete.
        cursor = payload["next_cursor"]
        seen = len(payload["items"])
        for _ in range(40):
            status, page = self.h.call(
                "GET", "/api/room/fixture/console-work/history?limit=20&direction=desc"
                       f"&cursor={cursor}")
            self.assertEqual(status, 200)
            seen += len(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                self.assertTrue(page["complete"])
                break
            self.assertFalse(page["complete"])
        else:
            self.fail("the history cursor chain did not terminate")
        self.assertGreater(seen, 40)

    def test_reading_a_room_does_not_take_control(self):
        status, payload = self.h.call("GET", "/api/room/fixture/console-work")
        self.assertEqual(status, 200)
        self.assertEqual(payload["ownership"]["state"], "idle")
        self.assertEqual(self.sessions.owned_threads(), {})

    def test_continue_here_resumes_the_exact_thread(self):
        status, payload = self.take()
        self.assertEqual(status, 200)
        self.assertEqual(payload["continued"]["thread_id"], THREAD_ONE)
        self.assertFalse(payload["continued"]["forked"])
        self.assertEqual(payload["room"]["ownership"]["state"], "atlas_owned")
        self.assertEqual(
            [r["thread_id"] for r in self.h.service.journal.remembered_owned()], [THREAD_ONE])

    def test_send_requires_ownership_first(self):
        status, payload = self.h.call(
            "POST", "/api/room/fixture/console-work/submit",
            body={"client_id": "aaaaaaaabbbbbbbb", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_owned")

    def test_send_is_refused_when_another_process_holds_the_thread(self):
        self.take()
        self.h.probe.table[THREAD_ONE] = {99999}
        status, payload = self.h.call(
            "POST", "/api/room/fixture/console-work/submit",
            body={"client_id": "ccccccccdddddddd", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "held_elsewhere")
        self.assertIn("another terminal", payload["message"])
        stored = self.h.service.journal.get("ccccccccdddddddd")
        self.assertEqual(stored.status, journal.FAILED)  # journaled, never dispatched

    def test_send_fails_closed_when_ownership_cannot_be_checked(self):
        self.take()
        self.h.probe.available = False
        status, payload = self.h.call(
            "POST", "/api/room/fixture/console-work/submit",
            body={"client_id": "eeeeeeeeffffffff", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "ownership_unavailable")

    def test_wrong_target_is_refused(self):
        self.take()
        status, payload = self.h.call(
            "POST", "/api/room/fixture/console-work/submit",
            body={"client_id": "11111111aaaaaaaa", "body": "hello", "thread_id": THREAD_TWO})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "wrong_target")
        self.assertEqual(payload["detail"]["expected"], THREAD_ONE)
        self.assertIsNone(self.h.service.journal.get("11111111aaaaaaaa"))

    def test_uncontrollable_room_cannot_be_sent_to(self):
        status, payload = self.h.call(
            "POST", "/api/room/fixture/claude-notes/submit",
            body={"client_id": "22222222aaaaaaaa", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "not_controllable")

    def test_duplicate_submission_does_not_call_the_runtime_twice(self):
        self.take()
        first = self.h.call("POST", "/api/room/fixture/console-work/submit",
                            body={"client_id": "33333333aaaaaaaa", "body": "same text"})
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["submission"]["status"], journal.ACCEPTED)
        self.assertTrue(first[1]["dispatched"])
        turn = first[1]["submission"]["native_turn_id"]
        second = self.h.call("POST", "/api/room/fixture/console-work/submit",
                             body={"client_id": "33333333aaaaaaaa", "body": "same text"})
        self.assertEqual(second[0], 200)
        self.assertTrue(second[1]["duplicate"])
        self.assertFalse(second[1]["dispatched"])
        self.assertEqual(second[1]["submission"]["native_turn_id"], turn)

    def test_same_id_different_body_is_refused(self):
        self.take()
        self.h.call("POST", "/api/room/fixture/console-work/submit",
                    body={"client_id": "44444444aaaaaaaa", "body": "first text"})
        status, payload = self.h.call("POST", "/api/room/fixture/console-work/submit",
                                      body={"client_id": "44444444aaaaaaaa", "body": "other text"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")

    def test_accepted_means_accepted_not_finished(self):
        self.take()
        status, payload = self.h.call("POST", "/api/room/fixture/console-work/submit",
                                      body={"client_id": "55555555aaaaaaaa", "body": "hello"})
        self.assertEqual(status, 200)
        self.assertIn("not a completed answer", payload["submission"]["meaning"])

    def test_shared_native_id_does_not_create_two_writers(self):
        """A second Vault room pointing at the same thread cannot also own it."""

        sessions_dir = self.h.tmp / "Projects" / "fixture" / "sessions"
        (sessions_dir / "shadow.md").write_text(
            "---\nproject: fixture\nsession: shadow\ntitle: Shadow of console work\n"
            "date: 2026-09-05\nupdated: \"2026-09-05\"\nstatus: active\nkeywords: shadow\n"
            "origins: \"shadow.origins.json\"\n---\n\n# shadow\n", encoding="utf-8")
        (sessions_dir / "shadow.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "fixture", "session": "shadow",
            "origins": [{"runtime": "codex", "node": "user-mac", "session_id": THREAD_ONE,
                         "cwd": "/tmp/atlas-fixture", "captured": "2026-09-05", "primary": True}],
        }), encoding="utf-8")
        self.h.service.discovery.refresh(force=True)
        self.take()
        status, payload = self.h.call("POST", "/api/room/fixture/shadow/continue", body={})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "held_by_room")
        status, payload = self.h.call("POST", "/api/room/fixture/shadow/submit",
                                      body={"client_id": "66666666aaaaaaaa", "body": "hello"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "held_by_room")

    def test_two_clients_share_one_server_side_runtime(self):
        """Two independent browsers see the same thread, journal and history."""

        self.take()
        self.h.call("POST", "/api/room/fixture/console-work/submit",
                    body={"client_id": "77777777aaaaaaaa", "body": "from the laptop"})
        first = self.h.call("GET", "/api/room/fixture/console-work")[1]
        second = self.h.call("GET", "/api/room/fixture/console-work")[1]
        self.assertEqual(first["native"]["thread_id"], second["native"]["thread_id"])
        self.assertEqual(first["ownership"]["state"], "atlas_owned")
        self.assertEqual(second["ownership"]["state"], "atlas_owned")
        ids = {s["client_id"] for s in second["submissions"]}
        self.assertIn("77777777aaaaaaaa", ids)


class UncertainTests(unittest.TestCase):
    def test_no_answer_becomes_uncertain_and_is_never_replayed(self):
        h = ConsoleHarness(mode="hang")
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            native.DEFAULT_TIMEOUT  # documented default; the call below overrides it
            # The conversation's own worker is what dispatches the turn.
            worker = h.worker_sessions()
            worker.server.request = _short_timeout(worker.server.request)
            status, payload = h.call("POST", "/api/room/fixture/console-work/submit",
                                     body={"client_id": "88888888aaaaaaaa", "body": "hello"})
            self.assertEqual(status, 202)
            self.assertTrue(payload["uncertain"])
            self.assertEqual(payload["submission"]["status"], journal.UNCERTAIN)
            self.assertIn("will not resend", payload["submission"]["meaning"].replace(
                "Atlas will not resend on its own", "will not resend"))
            # A retry of the same id must not dispatch again.
            status, again = h.call("POST", "/api/room/fixture/console-work/submit",
                                   body={"client_id": "88888888aaaaaaaa", "body": "hello"})
            self.assertEqual(status, 200)
            self.assertFalse(again["dispatched"])
            self.assertEqual(again["submission"]["status"], journal.UNCERTAIN)
        finally:
            h.close()

    def test_runtime_rejection_is_reported_as_failed(self):
        h = ConsoleHarness(mode="reject")
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            status, payload = h.call("POST", "/api/room/fixture/console-work/submit",
                                     body={"client_id": "99999999aaaaaaaa", "body": "hello"})
            self.assertEqual(status, 502)
            self.assertEqual(payload["detail"]["submission"]["status"], journal.FAILED)
        finally:
            h.close()


class ConnectionRefreshTests(unittest.TestCase):
    def test_refresh_uses_the_fixed_managed_auth_call_without_a_turn(self):
        h = ConsoleHarness()
        try:
            status, payload = h.call("POST", "/api/connection/refresh", body={})
            self.assertEqual(status, 200)
            self.assertEqual(payload["connection"]["state"], "refreshed")
            self.assertIn("does not prove", payload["connection"]["message"])
            self.assertFalse(h.service.sessions.owned_threads())
        finally:
            h.close()


class NativeCommandTests(unittest.TestCase):
    """The slash surface only selects fixed app-server operations."""

    def test_catalog_settings_and_new_thread_are_native_and_idempotent(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            status, _ = h.call("POST", "/api/room/fixture/console-work/continue", body={})
            self.assertEqual(status, 200)
            path = "/api/room/fixture/console-work/command"
            status, payload = h.call("POST", path, body={"command": "/model"})
            self.assertEqual(status, 200)
            catalog = payload["command"]["catalog"]
            self.assertTrue(catalog["available"])
            self.assertEqual(catalog["models"][0]["model"], "gpt-6-astra")

            status, payload = h.call("POST", path, body={"command": "/model gpt-5.6-terra"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["native"]["model"], "gpt-5.6-terra")
            status, payload = h.call("POST", path, body={"command": "/reasononing low"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["native"]["reasoning_effort"], "low")

            status, payload = h.call("POST", path, body={"command": "/goal clear", "client_id": "goal-clear-0001"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["state"], "cleared")

            client = "new-command-0001"
            body = {"command": "/new Fresh planning", "client_id": client, "thread_id": THREAD_ONE}
            status, payload = h.call("POST", path, body=body)
            self.assertEqual(status, 200)
            created = payload["command"]
            self.assertEqual(created["state"], "created")
            new_room = created["new_room"]
            self.assertEqual(new_room["project_id"], "fixture")
            self.assertNotEqual(created["native"]["thread_id"], THREAD_ONE)
            self.assertEqual(created["native"]["cwd"], str(h.tmp / "Projects" / "fixture"))
            status, again = h.call("POST", path, body=body)
            self.assertEqual(status, 200)
            self.assertTrue(again["duplicate"])
            self.assertEqual(again["command"]["new_room"]["id"], new_room["id"])
        finally:
            h.close()

    def test_new_record_failure_after_native_creation_is_uncertain(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            room = h.service.require_room("fixture/console-work")
            with patch.object(console.discovery.sv, "link_origin", side_effect=OSError("fixture write failure")):
                result = h.service.command(room, "/new blank", "new-record-failure-01")
            self.assertEqual(result["command"]["state"], "uncertain")
            self.assertEqual(h.service.journal.get("new-record-failure-01").status, journal.UNCERTAIN)
        finally:
            h.close()

    def test_new_preparation_failure_is_settled_without_starting_a_thread(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            room = h.service.require_room("fixture/console-work")
            with patch.object(console.discovery.sv, "command_start", side_effect=AttributeError("removed field")), patch.object(h.service.workers, "start_fresh") as spawn:
                result = h.service.command(room, "/new blank", "broken-new-command-01")
            self.assertEqual(result["command"]["state"], "failed")
            self.assertEqual(h.service.journal.get("broken-new-command-01").status, journal.FAILED)
            spawn.assert_not_called()
        finally:
            h.close()

    def test_rolling_chapter_preview_and_handoff_are_exact_and_turn_free(self):
        h = ConsoleHarness()
        start_log = h.tmp / "thread-start.jsonl"
        turn_log = h.tmp / "turn.jsonl"
        os.environ["ATLAS_FIXTURE_THREAD_START_LOG"] = str(start_log)
        os.environ["ATLAS_FIXTURE_TURN_LOG"] = str(turn_log)
        try:
            (h.tmp / "Projects" / "fixture" / ".ux46").mkdir()
            (h.tmp / "Projects" / "fixture" / ".ux46" / "continuation.md").write_text(
                "Projectwide fallback should not be selected.", encoding="utf-8")
            (h.tmp / "Projects" / "fixture" / ".ux46" / "continuations").mkdir()
            (h.tmp / "Projects" / "fixture" / ".ux46" / "continuations" / "console-work.md").write_text(
                "Keep the fixture’s next decision visible.", encoding="utf-8")
            h.start_runtime()
            path = "/api/room/fixture/console-work/command"
            status, _ = h.call("POST", "/api/room/fixture/console-work/continue", body={})
            self.assertEqual(status, 200)
            status, preview_before = h.call("GET", "/api/room/fixture/console-work/chapter-preview")
            self.assertEqual(status, 200)
            self.assertTrue(preview_before["eligible"], "a usage-limited dormant goal stays on the old chapter")
            status, preview = h.call("GET", "/api/room/fixture/console-work/chapter-preview")
            self.assertEqual(status, 200)
            self.assertTrue(preview["eligible"])
            self.assertIn("Keep the fixture", preview["chapter"]["brief"])
            self.assertNotIn("Projectwide fallback", preview["chapter"]["brief"])
            self.assertIn({"kind": "explicit-continuation", "path": ".ux46/continuations/console-work.md"},
                          preview["chapter"]["sources"])
            self.assertLessEqual(preview["chapter"]["size_chars"], 6000)

            body = {"command": "/new", "client_id": "rolling-chapter-01", "thread_id": THREAD_ONE}
            status, created = h.call("POST", path, body=body)
            self.assertEqual(status, 200)
            command = created["command"]
            self.assertEqual(command["chapter"]["mode"], "replace")
            self.assertEqual(command["chapter"]["brief"], preview["chapter"]["brief"])
            self.assertEqual(command["chapter"]["sources"], preview["chapter"]["sources"])
            params = json.loads(start_log.read_text(encoding="utf-8").strip())
            self.assertIn(preview["chapter"]["brief"], params["developerInstructions"])
            self.assertIn(command["new_room"]["id"], params["developerInstructions"])
            self.assertFalse(turn_log.exists())
            self.assertIsNone(h.service.workers.worker(THREAD_ONE))

            status, duplicate = h.call("POST", path, body=body)
            self.assertEqual(status, 200)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["command"]["chapter"], command["chapter"])
            self.assertEqual(len(start_log.read_text(encoding="utf-8").splitlines()), 1)
        finally:
            os.environ.pop("ATLAS_FIXTURE_THREAD_START_LOG", None)
            os.environ.pop("ATLAS_FIXTURE_TURN_LOG", None)
            h.close()

    def test_chapter_ignores_delivered_and_cancelled_queue_history(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            room = h.service.require_room("fixture/console-work")
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            for index, state in enumerate((journal.ACCEPTED, "cancelled")):
                key = f"chapter-history-{index}"
                h.service.journal.enqueue(key, room.id, THREAD_ONE, "old input", [], time.time()+3600)
                h.service.journal.queue_mark(key, state)
                h.service.journal.settle(key, journal.ACCEPTED if state == journal.ACCEPTED else journal.FAILED)
            self.assertTrue(h.service.chapter_preview(room)["eligible"])
            h.service.journal.enqueue("chapter-pending-1", room.id, THREAD_ONE, "still pending", [], time.time()+3600)
            for state in (journal.PENDING, journal.EDITING, journal.DISPATCHING, journal.UNCERTAIN, journal.FAILED):
                h.service.journal.queue_mark("chapter-pending-1", state)
                # Isolate the queue check from the separate submission check.
                h.service.journal.settle("chapter-pending-1", journal.FAILED)
                self.assertFalse(h.service.chapter_preview(room)["eligible"], state)
        finally:
            h.close()

    def test_parallel_chapter_keeps_the_source_worker(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            path = "/api/room/fixture/console-work/command"
            status, _ = h.call("POST", "/api/room/fixture/console-work/continue", body={})
            self.assertEqual(status, 200)
            status, payload = h.call("POST", path, body={
                "command": "/new parallel", "client_id": "parallel-chapter-01",
            })
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["chapter"]["mode"], "parallel")
            self.assertIsNotNone(h.service.workers.worker(THREAD_ONE))
            self.assertEqual(payload["command"]["chapter"]["lineage"]["source_release"]["state"], "not_attached")
        finally:
            h.close()

    def test_steer_never_starts_a_turn_and_unknown_commands_stay_unavailable(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            path = "/api/room/fixture/console-work/command"
            status, payload = h.call("POST", path, body={
                "command": "/streer revise that", "client_id": "steer-command-01",
            })
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["state"], "failed")
            self.assertIn("no active", payload["command"]["message"])
            status, payload = h.call("POST", path, body={"command": "/shell rm -rf /"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["state"], "unsupported")
        finally:
            h.close()

    def test_goal_controls_read_back_native_state_without_touching_permissions(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            path = "/api/room/fixture/console-work/command"
            status, payload = h.call("POST", path, body={"command": "/goal"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["native"]["goal"]["status"], "usageLimited")
            status, payload = h.call("POST", path, body={
                "command": "/goal resume", "client_id": "goal-command-0001"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["command"]["native"]["goal"]["status"], "active")
            self.assertIn("does not change account usage limits", payload["command"]["message"])
            status, payload = h.call("POST", path, body={
                "command": "/goal clear", "client_id": "goal-command-0002"})
            self.assertEqual(status, 200)
            self.assertIsNone(payload["command"]["native"]["goal"])
        finally:
            h.close()

    def test_terminal_recovery_falls_back_once_when_turn_paging_is_rejected(self):
        h = ConsoleHarness(mode="noturnslist")
        try:
            sessions = h.start_runtime()
            original = sessions.server.request
            calls = []

            def counted(method, params, timeout=native.DEFAULT_TIMEOUT):
                calls.append((method, dict(params)))
                return original(method, params, timeout=timeout)

            sessions.server.request = counted
            h.call("GET", "/api/room/fixture/console-work")
            h.call("GET", "/api/room/fixture/console-work")
            full_reads = [params for method, params in calls
                          if method == "thread/read" and params.get("includeTurns")]
            self.assertEqual(len(full_reads), 1)
        finally:
            h.close()

    def test_refresh_replaces_the_read_runtime_and_leaves_attached_work_running(self):
        """Superseded shape.

        One shared app-server had to be torn down and every remembered thread
        re-resumed to load a new login. Each attached conversation now owns its
        process, so a connection refresh replaces only the read/catalog runtime
        and says plainly that attached work keeps the login it started with.
        """

        h = ConsoleHarness()
        try:
            h.start_runtime()
            status, _ = h.call("POST", "/api/room/fixture/console-work/continue", body={})
            self.assertEqual(status, 200)
            old_reader = h.service.sessions.server
            old_worker = h.worker_sessions().server
            status, payload = h.call("POST", "/api/connection/refresh", body={})
            self.assertEqual(status, 200)
            self.assertEqual(payload["connection"]["scope"], "connection")
            self.assertEqual(payload["connection"]["attached"], 1)
            self.assertIsNot(old_reader, h.service.sessions.server)
            # the conversation's own process was not restarted
            self.assertIs(old_worker, h.worker_sessions().server)
            self.assertIn(THREAD_ONE, h.service.workers.owned_threads())
        finally:
            h.close()

    def test_refresh_refuses_active_or_unknown_hosted_work(self):
        for mode, expected in (("active", "connection_busy"),
                               ("loaded_paged_active", "connection_busy"),
                               ("unknown_activity", "refresh_safety_unknown")):
            h = ConsoleHarness(mode=mode)
            try:
                status, payload = h.call("POST", "/api/connection/refresh", body={})
                self.assertEqual(status, 409)
                self.assertEqual(payload["error"], expected)
            finally:
                h.close()

    def test_refresh_refuses_a_submit_or_refresh_race(self):
        h = ConsoleHarness()
        try:
            self.assertTrue(h.service._connection_lock.acquire(blocking=False))
            status, payload = h.call("POST", "/api/connection/refresh", body={})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"], "connection_busy")
        finally:
            if h.service._connection_lock.locked():
                h.service._connection_lock.release()
            h.close()

    def test_usage_classification_is_specific_and_safe(self):
        for error in [{"code": "usage_limit_exceeded"}, {"codex_error_info": "usageLimitExceeded"},
                      {"message": "You’ve hit your usage limit. private detail"}]:
            self.assertEqual(native.terminal_failure({"status": "failed", "error": error})["kind"], "usage_limit")
        self.assertIsNone(native.terminal_failure({"willRetry": True, "error": {"code": "usage_limit_exceeded"}}))
        self.assertIsNone(native.terminal_failure({"status": "completed", "items": [
            {"error": {"message": "hit your usage limit"}}]}))
        self.assertEqual(native.terminal_failure({"status": "failed", "error": {
            "code": "httpConnectionFailed"}})["kind"], "failed")

    def test_usage_failure_survives_reopen_and_live_notification(self):
        h = ConsoleHarness(mode="usagefail")
        try:
            status, room = h.call("GET", "/api/room/fixture/console-work")
            self.assertEqual(status, 200)
            self.assertEqual(room["native_terminal"]["kind"], "usage_limit")
            self.assertIn("does not reset", room["native_terminal"]["message"])
            h.service._on_native_event({"type": "notification", "method": "error",
                "params": {"threadId": THREAD_ONE, "turnId": "usage-live",
                           "error": {"codexErrorInfo": "usageLimitExceeded", "message": "private detail"}}})
            events = h.service.events.since(0, timeout=0.1, room="fixture/console-work")["events"]
            terminal = next(e["terminal"] for e in events if e.get("terminal"))
            self.assertEqual(terminal["kind"], "usage_limit")
            self.assertEqual(terminal["turn_id"], "usage-live")
            self.assertNotIn("private detail", json.dumps(events))
            # No synthetic turn, retry, login change, or reset is initiated.
            self.assertEqual(h.service.journal.recent(), [])
        finally:
            h.close()

    def test_auth_failure_is_recovered_for_every_vault_alias(self):
        h = ConsoleHarness(mode="authfail")
        try:
            sessions_dir = h.tmp / "Projects" / "fixture" / "sessions"
            (sessions_dir / "shadow.md").write_text(
                "---\nproject: fixture\nsession: shadow\ntitle: Same native thread\n"
                "date: 2026-09-05\nupdated: \"2026-09-05\"\nstatus: active\n"
                "origins: \"shadow.origins.json\"\n---\n\n# shadow\n", encoding="utf-8")
            (sessions_dir / "shadow.origins.json").write_text(json.dumps({
                "schema_version": 1, "project": "fixture", "session": "shadow",
                "origins": [{"runtime": "codex", "node": "user-mac", "session_id": THREAD_ONE,
                             "cwd": "/tmp/atlas-fixture", "captured": "2026-09-05", "primary": True}],
            }), encoding="utf-8")
            h.service.discovery.refresh(force=True)
            status, alias = h.call("GET", "/api/room/fixture/shadow")
            self.assertEqual(status, 200)
            self.assertEqual(alias["native_terminal"]["kind"], "auth")
            self.assertIn("sign in again", alias["native_terminal"]["message"])
            # A live terminal event reaches both records, not whichever alias
            # discovery happened to return first.
            h.service._on_native_event({"type": "notification", "method": "turn/completed",
                "params": {"threadId": THREAD_ONE, "turn": {"id": "failed-turn", "status": "failed",
                "error": {"code": "unauthorized", "message": "Please sign in again."}}}})
            events = h.service.events.since(0, timeout=0.1, room="fixture/shadow")["events"]
            self.assertTrue(any(event.get("terminal", {}).get("kind") == "auth" for event in events))
        finally:
            h.close()


def _short_timeout(request):
    def wrapped(method, params, timeout=native.DEFAULT_TIMEOUT):
        return request(method, params, timeout=2.0 if method == "turn/start" else timeout)
    return wrapped


class ApprovalTests(unittest.TestCase):
    def test_command_approval_round_trip_uses_the_documented_shape(self):
        h = ConsoleHarness(mode="approval")
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            h.call("POST", "/api/room/fixture/console-work/submit",
                   body={"client_id": "aaaa1111bbbb2222", "body": "run something"})
            pending = _wait_for(lambda: h.call("GET", "/api/approvals")[1]["approvals"])
            self.assertEqual(len(pending), 1)
            request = pending[0]
            self.assertEqual(request["kind"], "command")
            self.assertEqual(request["room"], "fixture/console-work")
            self.assertIn("rm -rf", request["params"]["command"])
            self.assertEqual(
                native.response_for_decision("command", "accept"), {"decision": "accept"})
            self.assertEqual(
                native.response_for_decision("command", "decline"), {"decision": "decline"})
            status, payload = h.call("POST", "/api/approvals/answer",
                                     body={"key": request["key"], "kind": "command",
                                           "decision": "accept"})
            self.assertEqual(status, 200)
            # exactly one response per request
            status, again = h.call("POST", "/api/approvals/answer",
                                   body={"key": request["key"], "kind": "command",
                                         "decision": "accept"})
            self.assertEqual(again["error"], "request_unknown")
            self.assertEqual(h.call("GET", "/api/approvals")[1]["approvals"], [])
        finally:
            h.close()

    def test_user_input_question_round_trip(self):
        h = ConsoleHarness(mode="question")
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            h.call("POST", "/api/room/fixture/console-work/submit",
                   body={"client_id": "cccc1111dddd2222", "body": "ask me"})
            pending = _wait_for(lambda: h.call("GET", "/api/approvals")[1]["approvals"])
            request = pending[0]
            self.assertEqual(request["kind"], "user_input")
            self.assertTrue(request["params"]["blocking"])
            self.assertEqual(request["params"]["questions"][0]["options"], ["alpha", "beta"])
            status, _ = h.call("POST", "/api/approvals/answer",
                               body={"key": request["key"], "kind": "user_input",
                                     "decision": "answer", "answers": {"q1": "alpha"}})
            self.assertEqual(status, 200)
        finally:
            h.close()

    def test_permission_grant_is_once_and_never_widens_settings(self):
        self.assertEqual(native.response_for_decision("permissions", "accept"),
                         {"permissions": {"kind": "granted"}, "scope": "once"})
        self.assertEqual(native.response_for_decision("permissions", "decline"),
                         {"permissions": {"kind": "denied"}})

    def test_unknown_server_request_is_refused_not_hung(self):
        h = ConsoleHarness(mode="unknown")
        try:
            h.start_runtime()
            h.call("POST", "/api/room/fixture/console-work/continue", body={})
            h.call("POST", "/api/room/fixture/console-work/submit",
                   body={"client_id": "eeee1111ffff2222", "body": "go"})
            unsupported = _wait_for(lambda: [
                event for event in h.service.events.since(0, timeout=0.2)["events"]
                if event.get("type") == "request/unsupported"])
            self.assertEqual(unsupported[0]["method"], "fixture/unsupportedRequest")
            self.assertEqual(h.call("GET", "/api/approvals")[1]["approvals"], [])
        finally:
            h.close()


class DraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ConsoleHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_draft_versions_and_conflict(self):
        room = "fixture/second-room"
        status, first = self.h.call("PUT", f"/api/room/{room}/draft",
                                    body={"body": "from the laptop", "base_version": 0,
                                          "device": "laptop"})
        self.assertEqual(status, 200)
        self.assertEqual(first["version"], 1)
        status, second = self.h.call("PUT", f"/api/room/{room}/draft",
                                     body={"body": "from the phone", "base_version": 1,
                                           "device": "phone"})
        self.assertEqual(second["version"], 2)
        status, conflict = self.h.call("PUT", f"/api/room/{room}/draft",
                                       body={"body": "stale laptop text", "base_version": 1,
                                             "device": "laptop"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "draft_conflict")
        self.assertEqual(conflict["detail"]["body"], "from the phone")
        current = self.h.call("GET", f"/api/room/{room}/draft")[1]
        self.assertEqual(current["body"], "from the phone")  # nothing was clobbered

    def test_draft_is_pinned_to_its_room(self):
        self.h.call("PUT", "/api/room/fixture/console-work/draft",
                    body={"body": "console text", "base_version": 0})
        other = self.h.call("GET", "/api/room/fixture/claude-notes/draft")[1]
        self.assertEqual(other["body"], "")


class JournalUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-journal-"))
        self.j = journal.Journal(self.tmp / "j.sqlite3")

    def tearDown(self):
        self.j.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reserve_is_atomic_and_idempotent(self):
        first, created = self.j.reserve("id-1", "a/b", "t-1", "hello")
        self.assertTrue(created)
        self.assertEqual(first.status, journal.PENDING)
        again, created_again = self.j.reserve("id-1", "a/b", "t-1", "hello")
        self.assertFalse(created_again)
        self.assertEqual(again.client_id, "id-1")

    def test_reserve_refuses_a_changed_target(self):
        self.j.reserve("id-2", "a/b", "t-1", "hello")
        with self.assertRaises(journal.DuplicateMismatch):
            self.j.reserve("id-2", "a/b", "t-2", "hello")
        with self.assertRaises(journal.DuplicateMismatch):
            self.j.reserve("id-2", "a/b", "t-1", "different")

    def test_uncertain_stays_uncertain(self):
        self.j.reserve("id-3", "a/b", "t-1", "hello")
        settled = self.j.settle("id-3", journal.UNCERTAIN, detail="no answer")
        self.assertEqual(settled.status, journal.UNCERTAIN)
        self.assertIn("id-3", {s.client_id for s in self.j.unsettled()})

    def test_concurrent_reservations_yield_one_winner(self):
        results = []
        barrier = threading.Barrier(6)

        def attempt():
            barrier.wait()
            try:
                results.append(self.j.reserve("race", "a/b", "t-1", "hello")[1])
            except journal.JournalError:
                results.append(False)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 1)


@unittest.skipUnless(os.environ.get('UX46_TEST_REAL_RUNTIME') == '1' and os.environ.get('UX46_TEST_THREAD'),
                     'Explicit native-runtime opt-in and a dedicated test thread required')
class RealNativeReadTests(unittest.TestCase):
    """One read-only look at a real local session: no resume, no send."""

    def test_read_one_existing_session_metadata_only(self):
        codex = os.environ.get("ATLAS_CODEX_BIN") or str(Path.home() / ".local/bin/codex")
        if not Path(codex).exists():
            self.skipTest("local Codex CLI not present")
        server = native.AppServer(command=[codex, "app-server"])
        try:
            server.start()
            thread_id = os.environ['UX46_TEST_THREAD']
            read = server.request("thread/read", {"threadId": thread_id}, timeout=30)
            thread = read.get("thread") or {}
            self.assertEqual(thread.get("id"), thread_id)
            self.assertTrue(thread.get("cwd"))
            page = server.request("thread/items/list", {
                "threadId": thread_id, "limit": 3, "sortDirection": "desc"}, timeout=30)
            items = page.get("data") or []
            kinds = sorted({(entry.get("item") or {}).get("type") for entry in items})
            # Counts and shapes only: no transcript text is asserted or printed.
            self.assertLessEqual(len(items), 3)
            self.assertTrue(all(isinstance(k, str) for k in kinds if k))
            self.assertEqual(server.pending_requests(), [])
        finally:
            server.stop()


def _wait_for(fn, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting for a value")


if __name__ == "__main__":
    unittest.main()
