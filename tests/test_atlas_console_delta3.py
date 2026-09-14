"""Delta checks for the final admission (follow-ups 3, 4 and input recovery).

Only the observed branches: HTTP request framing on a persistent connection,
the private-origin Host spoof, native launcher ownership, uncertain delivery
recovered by exact id, and truthful history errors.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import atlas_console as console  # noqa: E402
import atlas_journal as journal  # noqa: E402
import atlas_native as native  # noqa: E402
from test_atlas_console import ConsoleHarness, THREAD_ONE  # noqa: E402

ROOM = "fixture/console-work"


class PersistentConnection:
    """One keep-alive connection, like a browser uses."""

    def __init__(self, harness):
        self.h = harness
        self.conn = HTTPConnection("127.0.0.1", harness.port, timeout=20)

    def send(self, method, path, body=None, csrf=True, headers=None, raw=None):
        head = {"Host": f"127.0.0.1:{self.h.port}"}
        payload = raw
        if body is not None and raw is None:
            payload = json.dumps(body).encode()
        if payload is not None:
            head["Content-Type"] = "application/json"
            head["Origin"] = f"http://127.0.0.1:{self.h.port}"
            head["Content-Length"] = str(len(payload))
            if csrf:
                head["X-Atlas-CSRF"] = self.h.service.csrf_token
        head.update(headers or {})
        self.conn.request(method, path, body=payload, headers=head)
        response = self.conn.getresponse()
        raw_body = response.read()
        try:
            parsed = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw_body.decode("utf-8", "replace")[:160]}
        return response.status, parsed

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class FramingTests(unittest.TestCase):
    """A body-free route must still consume its body (follow-up 3, item 1)."""

    def setUp(self):
        self.log = Path(tempfile.mkdtemp(prefix="atlas-turns-")) / "turns.jsonl"
        os.environ["ATLAS_FIXTURE_TURN_LOG"] = str(self.log)
        self.h = ConsoleHarness()
        self.h.start_runtime()

    def tearDown(self):
        os.environ.pop("ATLAS_FIXTURE_TURN_LOG", None)
        self.h.close()

    def dispatches(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def test_continue_then_send_on_one_connection(self):
        """The exact browser sequence that produced 501 Unsupported method."""

        pipe = PersistentConnection(self.h)
        try:
            status, payload = pipe.send("POST", f"/api/room/{ROOM}/continue", body={})
            self.assertEqual(status, 200)
            self.assertEqual(payload["continued"]["thread_id"], THREAD_ONE)

            status, payload = pipe.send("POST", f"/api/room/{ROOM}/submit",
                                        body={"client_id": "framing12345678", "body": "hello"})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["submission"]["status"], journal.ACCEPTED)

            status, payload = pipe.send("PUT", f"/api/room/{ROOM}/draft",
                                        body={"body": "after", "base_version": 0})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["body"], "after")

            status, payload = pipe.send("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertIn("csrf", payload)
        finally:
            pipe.close()
        self.assertEqual(len(self.dispatches()), 1)      # exactly one dispatch

    def test_release_and_interrupt_do_not_strand_bodies(self):
        pipe = PersistentConnection(self.h)
        try:
            self.assertEqual(pipe.send("POST", f"/api/room/{ROOM}/continue", body={})[0], 200)
            self.assertEqual(pipe.send("POST", f"/api/room/{ROOM}/interrupt", body={})[0], 200)
            self.assertEqual(pipe.send("POST", f"/api/room/{ROOM}/release", body={})[0], 200)
            status, payload = pipe.send("GET", f"/api/room/{ROOM}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["id"], ROOM)
        finally:
            pipe.close()

    def test_early_rejection_leaves_the_connection_usable(self):
        pipe = PersistentConnection(self.h)
        try:
            status, payload = pipe.send("PUT", f"/api/room/{ROOM}/draft",
                                        body={"body": "x", "base_version": 0}, csrf=False)
            self.assertEqual(status, 403)
            self.assertEqual(payload["error"], "bad_csrf")
            status, payload = pipe.send("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertIn("csrf", payload)
        finally:
            pipe.close()

    def test_unframeable_requests_close_the_connection(self):
        for headers, raw, expected in (
            ({"Transfer-Encoding": "chunked"}, b"{}", 501),
            ({"Content-Length": "not-a-number"}, b"{}", 400),
        ):
            conn = HTTPConnection("127.0.0.1", self.h.port, timeout=10)
            head = {"Host": f"127.0.0.1:{self.h.port}",
                    "Origin": f"http://127.0.0.1:{self.h.port}",
                    "X-Atlas-CSRF": self.h.service.csrf_token,
                    "Content-Type": "application/json"}
            head.update(headers)
            conn.request("PUT", f"/api/room/{ROOM}/draft", body=raw, headers=head)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, expected)
            self.assertTrue(response.will_close or response.getheader("Connection") == "close")
            conn.close()

    def test_oversize_body_is_refused_and_closed(self):
        conn = HTTPConnection("127.0.0.1", self.h.port, timeout=10)
        payload = json.dumps({"body": "x" * (console.MAX_BODY + 50), "base_version": 0}).encode()
        conn.request("PUT", f"/api/room/{ROOM}/draft", body=payload, headers={
            "Host": f"127.0.0.1:{self.h.port}",
            "Origin": f"http://127.0.0.1:{self.h.port}",
            "X-Atlas-CSRF": self.h.service.csrf_token,
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        })
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        conn.close()


class OwnershipTreeTests(unittest.TestCase):
    """A Node launcher's native child is still Atlas (follow-up 3, item 2)."""

    def test_the_launcher_chain_is_ours_and_nothing_deeper_is(self):
        """Superseded shape.

        fd82077 excluded the launcher's whole subtree. Follow-up 7 narrows that
        to the app-server chain, so a worker CLI an agent starts keeps its own
        session. tests.test_atlas_console_delta4 owns the detailed cases.
        """

        table = {10: (1, "node"), 11: (10, "codex"), 12: (11, "sh"), 13: (12, "codex")}
        self.assertEqual(native.app_server_pids(10, table=table), {10, 11})

    def test_a_child_holding_the_rollout_is_not_external(self):
        h = ConsoleHarness()
        try:
            sessions = h.start_runtime()
            launcher = sessions.server.pids[0]
            child = launcher + 1          # stand-in for the native child pid
            sessions.probe.table[THREAD_ONE] = {child}

            # Without the tree, the child looks like another terminal.
            plain = sessions.probe.check(THREAD_ONE, own_pids={launcher})
            self.assertEqual(plain.state, "held_elsewhere")

            # With it, Atlas recognises its own runtime.
            sessions.own_pids = lambda refresh=False: {launcher, child}
            self.assertEqual(sessions.ownership(THREAD_ONE).state, "idle")
            sessions.mark_owned(THREAD_ONE, room=ROOM)
            self.assertEqual(sessions.ownership(THREAD_ONE).state, "atlas_owned")
        finally:
            h.close()

    def test_unreadable_process_tree_fails_closed(self):
        h = ConsoleHarness()
        try:
            sessions = h.start_runtime()

            def broken(refresh=False):
                raise native.NativeError("no ps", code="ownership_unavailable")

            sessions.own_pids = broken
            state = sessions.ownership(THREAD_ONE)
            self.assertFalse(state.detected)
            self.assertEqual(state.state, "unknown")
            with self.assertRaises(native.NativeError) as caught:
                sessions.require_control(THREAD_ONE)
            self.assertEqual(caught.exception.code, "ownership_unavailable")
        finally:
            h.close()


class LostResponseTests(unittest.TestCase):
    """An accepted turn whose HTTP response never arrived."""

    def setUp(self):
        self.log = Path(tempfile.mkdtemp(prefix="atlas-turns-")) / "turns.jsonl"
        os.environ["ATLAS_FIXTURE_TURN_LOG"] = str(self.log)
        self.h = ConsoleHarness()
        self.h.start_runtime()
        self.h.call("POST", f"/api/room/{ROOM}/continue", body={})

    def tearDown(self):
        os.environ.pop("ATLAS_FIXTURE_TURN_LOG", None)
        self.h.close()

    def dispatches(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def test_the_exact_id_reports_its_journaled_outcome(self):
        client_id = "lostresponse01"
        status, payload = self.h.call("POST", f"/api/room/{ROOM}/submit",
                                      body={"client_id": client_id, "body": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["submission"]["status"], journal.ACCEPTED)

        # The browser never saw that response; it asks about the exact id.
        status, lookup = self.h.call("GET", f"/api/submissions/{client_id}")
        self.assertEqual(status, 200)
        self.assertEqual(lookup["submission"]["status"], journal.ACCEPTED)
        self.assertEqual(lookup["submission"]["room"], ROOM)
        self.assertEqual(lookup["submission"]["thread_id"], THREAD_ONE)
        self.assertEqual(len(self.dispatches()), 1)

    def test_looking_up_the_same_id_never_dispatches_again(self):
        client_id = "lostresponse02"
        self.h.call("POST", f"/api/room/{ROOM}/submit",
                    body={"client_id": client_id, "body": "hello"})
        for _ in range(3):
            self.assertEqual(self.h.call("GET", f"/api/submissions/{client_id}")[0], 200)
        # A retry of the same id is answered from the journal, not the runtime.
        status, again = self.h.call("POST", f"/api/room/{ROOM}/submit",
                                    body={"client_id": client_id, "body": "hello"})
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["dispatched"])
        self.assertEqual(len(self.dispatches()), 1)

    def test_an_id_that_never_arrived_is_reported_as_not_journaled(self):
        status, payload = self.h.call("GET", "/api/submissions/neverarrived99")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_submission")
        self.assertIn("not dispatched", payload["message"])
        self.assertEqual(self.dispatches(), [])

    def test_lookup_ids_are_validated(self):
        self.assertEqual(self.h.call("GET", "/api/submissions/../../etc/passwd")[0], 404)
        self.assertEqual(self.h.call("GET", "/api/submissions/short")[0], 404)


class HistoryTruthTests(unittest.TestCase):
    """An unreadable history is not an empty conversation."""

    def test_a_new_unpersisted_session_is_an_empty_conversation(self):
        h = ConsoleHarness(mode="nohistory")
        try:
            h.start_runtime()
            status, page = h.call("GET", f"/api/room/{ROOM}/history?limit=10")
            self.assertEqual(status, 200)
            self.assertEqual(page["items"], [])
            self.assertEqual(page["source"], "empty")
            self.assertTrue(page["complete"])
            self.assertFalse(page.get("unavailable"))
            self.assertIn("no saved history yet", page["note"])
        finally:
            h.close()

    def test_an_unreadable_history_is_reported_as_unavailable(self):
        h = ConsoleHarness(mode="badhistory")
        try:
            h.start_runtime()
            status, page = h.call("GET", f"/api/room/{ROOM}/history?limit=10")
            self.assertEqual(status, 200)
            self.assertTrue(page["unavailable"])
            self.assertTrue(page["retryable"])
            self.assertEqual(page["source"], "unavailable")
            self.assertFalse(page["complete"])       # never "that is all there is"
            self.assertIn("corrupt", page["message"].lower())
            # the room stays usable, with its draft intact
            self.assertEqual(h.call("PUT", f"/api/room/{ROOM}/draft",
                                    body={"body": "kept", "base_version": 0})[0], 200)
            self.assertEqual(h.call("GET", f"/api/room/{ROOM}/draft")[1]["body"], "kept")
        finally:
            h.close()

    def test_search_reports_an_unreadable_history_rather_than_no_results(self):
        h = ConsoleHarness(mode="badhistory")
        try:
            h.start_runtime()
            status, found = h.call("GET", f"/api/room/{ROOM}/search?q=anything")
            self.assertEqual(status, 200)
            self.assertEqual(found["source"], "unavailable")
            self.assertTrue(found["retryable"])
            self.assertIn("could not be read", found["note"])
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
