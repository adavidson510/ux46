"""One native process per attached conversation.

Codex keeps the writer on a resumed thread after ``thread/unsubscribe``, so a
single shared app-server could never hand one session back while keeping the
others running. These checks drive the console over the protocol fixture with
two hosted conversations at once and assert the lifecycle User asked for:
switching rooms keeps work running, Release stops exactly one process and
proves the writer went with it, and nothing is ever called released while a
holder is still visible.

Everything here uses tests/fixtures/fake_app_server.py and synthetic thread
ids. No real session is read, resumed, interrupted or released.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import atlas_journal as journal  # noqa: E402
import atlas_native as native  # noqa: E402
import atlas_workers as workers  # noqa: E402
from test_atlas_console import ConsoleHarness, THREAD_ONE, THREAD_TWO  # noqa: E402

ROOM_A = "fixture/console-work"
ROOM_B = "fixture/second-room"


class LockProbe:
    """A writer-lock table that follows real process liveness.

    ``table[thread] = {pid}`` stands in for
    ``$CODEX_HOME/thread-writer-locks/<thread>.lock`` being held open. A dead
    pid holds nothing, which is exactly the property Release depends on.
    """

    def __init__(self):
        self.table: dict[str, set[int]] = {}
        self.available = True

    @staticmethod
    def _live(pids):
        live = set()
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            live.add(pid)
        return live

    def check(self, thread_id, *, own_pids=(), atlas_owned=False, refresh=False):
        if not self.available:
            return native.Ownership(thread_id, (), atlas_owned, False, "probe unavailable")
        holders = self._live(self.table.get(thread_id, set()))
        self.table[thread_id] = holders
        mine = set(int(p) for p in own_pids)
        return native.Ownership(thread_id, tuple(sorted(holders - mine)), atlas_owned,
                                True, native_retained=bool(holders & mine))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


class WorkerHarness(ConsoleHarness):
    """The console with a liveness-following writer-lock probe."""

    def __init__(self, mode: str = "normal"):
        super().__init__(mode=mode)
        self.probe = LockProbe()
        self.start_runtime()

    def attach(self, room: str):
        return self.call("POST", f"/api/room/{room}/continue", body={})

    def claim_lock(self, thread_id: str):
        """Record the attached worker as the holder of the writer lock."""

        worker = self.service.workers.worker(thread_id)
        assert worker is not None, thread_id
        self.probe.table[thread_id] = set(worker.server.pids)
        return worker

    def worker(self, thread_id: str):
        return self.service.workers.worker(thread_id)


class ProcessIsolationTests(unittest.TestCase):
    """Two conversations, two processes, one read runtime."""

    def setUp(self):
        self.h = WorkerHarness()

    def tearDown(self):
        self.h.close()

    def test_resume_does_not_hydrate_history_but_history_remains_readable(self):
        h = WorkerHarness(mode="metadata_resume")
        try:
            status, payload = h.attach(ROOM_A)
            self.assertEqual(status, 200, payload)
            status, history = h.call("GET", f"/api/room/{ROOM_A}/history")
            self.assertEqual(status, 200, history)
            self.assertTrue(history)
        finally:
            h.close()

    def test_two_devices_share_one_inflight_connection(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.h.service.workers._resume_in
        results = []

        def delayed(worker, thread_id, room):
            self.h.probe.table[thread_id] = set(worker.server.pids)
            entered.set()
            if not finish.wait(10):
                raise RuntimeError("test connection did not finish")
            return original(worker, thread_id, room)

        with patch.object(self.h.service.workers, "_resume_in", side_effect=delayed) as resume:
            first = threading.Thread(target=lambda: results.append(self.h.attach(ROOM_A)))
            second = threading.Thread(target=lambda: results.append(self.h.attach(ROOM_A)))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                second.start()
                status, room = self.h.call("GET", f"/api/room/{ROOM_A}")
                self.assertEqual(status, 200, room)
                self.assertEqual(room["ownership"]["state"], "connecting")
                self.assertFalse(room["ownership"]["atlas_owned"])
                self.assertEqual(room["ownership"]["external_pids"], [])
            finally:
                finish.set()
                first.join(10)
                if second.ident is not None:
                    second.join(10)
            self.assertEqual([r[0] for r in results], [200, 200], results)
            self.assertEqual(resume.call_count, 1)
            self.assertEqual(len(self.h.service.workers.attached()), 1)
            self.assertIsNone(self.h.service.workers.attaching_worker(THREAD_ONE))

    def test_each_attached_conversation_gets_its_own_process(self):
        self.assertEqual(self.h.attach(ROOM_A)[0], 200)
        self.assertEqual(self.h.attach(ROOM_B)[0], 200)
        a, b = self.h.worker(THREAD_ONE), self.h.worker(THREAD_TWO)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertTrue(a.running and b.running)
        self.assertNotEqual(set(a.server.pids), set(b.server.pids))
        reader = self.h.service.workers.reader()
        self.assertNotIn(reader.server.pids[0], set(a.server.pids) | set(b.server.pids))
        self.assertEqual(sorted(self.h.service.workers.owned_threads()),
                         sorted([THREAD_ONE, THREAD_TWO]))

    def test_release_stops_one_process_and_leaves_the_other_working(self):
        self.h.attach(ROOM_A)
        self.h.attach(ROOM_B)
        a = self.h.claim_lock(THREAD_ONE)
        b = self.h.claim_lock(THREAD_TWO)
        a_pid = a.server.pids[0]

        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["release_state"], "released")
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["released"], THREAD_ONE)
        self.assertEqual(payload["holders"], [])

        # A's process, and the writer handle it held, are gone.
        self.assertFalse(_alive(a_pid))
        self.assertEqual(self.h.probe.table[THREAD_ONE], set())
        self.assertIsNone(self.h.worker(THREAD_ONE))
        self.assertEqual([r["thread_id"] for r in self.h.service.journal.remembered_owned()],
                         [THREAD_TWO])

        # B is untouched and can still take a turn.
        self.assertIs(self.h.worker(THREAD_TWO), b)
        self.assertTrue(b.running)
        status, sent = self.h.call("POST", f"/api/room/{ROOM_B}/submit",
                                   body={"client_id": "keepworking0001", "body": "still here"})
        self.assertEqual(status, 200, sent)
        self.assertEqual(sent["submission"]["status"], journal.ACCEPTED)

    def test_release_never_claims_a_writer_it_can_still_see_held(self):
        self.h.attach(ROOM_A)
        self.h.claim_lock(THREAD_ONE)
        # A live holder that is not the worker process: releasing our process
        # cannot free this, and Atlas must not pretend otherwise.
        self.h.probe.table[THREAD_ONE] = {os.getpid()}
        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["release_state"], "releasing")
        self.assertFalse(payload["verified"])
        self.assertIn(os.getpid(), payload["holders"])
        self.assertIn("will not call it released", payload["message"])

    def test_release_is_unverified_when_ownership_cannot_be_read(self):
        self.h.attach(ROOM_A)
        worker = self.h.claim_lock(THREAD_ONE)
        pid = worker.server.pids[0]
        self.h.probe.available = False
        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["release_state"], "unverified")
        self.assertFalse(payload["verified"])
        self.assertIn("could not check", payload["message"])
        # The process really was stopped and the memory really was cleared.
        self.assertFalse(_alive(pid))
        self.assertEqual(self.h.service.journal.remembered_owned(), [])

    def test_releasing_an_unattached_session_says_so(self):
        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["release_state"], "not_attached")
        self.assertIn("no process to stop", payload["message"])

    def test_a_vault_alias_never_starts_a_second_writer(self):
        sessions_dir = self.h.tmp / "Projects" / "fixture" / "sessions"
        (sessions_dir / "shadow.md").write_text(
            "---\nproject: fixture\nsession: shadow\ntitle: Same native thread\n"
            "date: 2026-09-05\nupdated: \"2026-09-05\"\nstatus: active\n"
            "origins: \"shadow.origins.json\"\n---\n\n# shadow\n", encoding="utf-8")
        (sessions_dir / "shadow.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "fixture", "session": "shadow",
            "origins": [{"runtime": "codex", "node": "user-mac", "session_id": THREAD_ONE,
                         "cwd": "/tmp/atlas-fixture", "captured": "2026-09-05", "primary": True}],
        }), encoding="utf-8")
        self.h.service.discovery.refresh(force=True)
        self.assertEqual(self.h.attach(ROOM_A)[0], 200)
        first = self.h.worker(THREAD_ONE)

        status, payload = self.h.attach("fixture/shadow")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "held_by_room")

        # Even reached directly, the same thread id deduplicates to one process.
        again = self.h.service.workers.attach(THREAD_ONE, "fixture/shadow")
        self.assertTrue(again["duplicate"])
        self.assertIs(self.h.worker(THREAD_ONE), first)
        self.assertEqual(len(self.h.service.workers.attached()), 1)

    def test_release_is_refused_while_a_turn_is_running(self):
        self.h.attach(ROOM_A)
        self.h.attach(ROOM_B)
        a, b = self.h.worker(THREAD_ONE), self.h.worker(THREAD_TWO)
        a.sessions.note_turn(THREAD_ONE, "01a00000-turn", True)
        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"], "connection_busy")
        self.assertIn("still running", payload["message"])
        # Neither worker changed.
        self.assertIs(self.h.worker(THREAD_ONE), a)
        self.assertIs(self.h.worker(THREAD_TWO), b)
        self.assertTrue(a.running and b.running)
        self.assertEqual([r["thread_id"] for r in self.h.service.journal.remembered_owned()],
                         [THREAD_TWO, THREAD_ONE])

    def test_release_is_refused_while_a_dispatch_is_unsettled(self):
        self.h.attach(ROOM_A)
        worker = self.h.worker(THREAD_ONE)
        self.h.service.journal.reserve("unsettled0000001", ROOM_A, THREAD_ONE, "hello")
        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/release", body={})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"], "connection_busy")
        self.assertIs(self.h.worker(THREAD_ONE), worker)
        self.assertTrue(worker.running)

    def test_attach_refuses_a_session_another_process_holds(self):
        self.h.probe.table[THREAD_TWO] = {os.getpid()}
        status, payload = self.h.call("POST", f"/api/room/{ROOM_B}/continue", body={})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"], "held_elsewhere")
        self.assertIsNone(self.h.worker(THREAD_TWO))
        self.assertEqual(self.h.service.workers.attached(), {})

    def test_attach_recovers_when_only_the_atlas_read_runtime_holds_it(self):
        reader = self.h.service.workers.reader()
        reader_pid = reader.server.pids[0]
        self.h.probe.table[THREAD_ONE] = {reader_pid}
        status, payload = self.h.attach(ROOM_A)
        self.assertEqual(status, 200, payload)
        self.assertFalse(_alive(reader_pid))          # our own read process only
        self.assertIsNotNone(self.h.worker(THREAD_ONE))

    def test_refresh_replaces_one_conversation_and_leaves_the_other(self):
        self.h.attach(ROOM_A)
        self.h.attach(ROOM_B)
        old_a = self.h.worker(THREAD_ONE).server
        old_a_pid = old_a.pids[0]
        b = self.h.worker(THREAD_TWO)
        old_b_pid = b.server.pids[0]

        status, payload = self.h.call("POST", f"/api/room/{ROOM_A}/refresh", body={})
        self.assertEqual(status, 200, payload)
        connection = payload["connection"]
        self.assertEqual(connection["scope"], "conversation")
        self.assertEqual(connection["reconnected"], 1)
        self.assertEqual(connection["others_running"], 1)

        new_a = self.h.worker(THREAD_ONE)
        self.assertIsNotNone(new_a)
        self.assertIsNot(new_a.server, old_a)
        self.assertFalse(_alive(old_a_pid))
        self.assertIs(self.h.worker(THREAD_TWO), b)
        self.assertTrue(_alive(old_b_pid))

    def test_the_read_runtime_refuses_every_writer_method(self):
        reader = self.h.service.sessions
        for method in sorted(workers.WRITER_METHODS):
            with self.assertRaises(native.NativeError) as caught:
                reader.server.request(method, {"threadId": THREAD_ONE})
            self.assertEqual(caught.exception.code, "read_only_runtime", method)

    def test_reading_a_conversation_never_starts_a_writer(self):
        self.assertEqual(self.h.call("GET", f"/api/room/{ROOM_A}/history?limit=5")[0], 200)
        self.assertEqual(self.h.call("GET", f"/api/room/{ROOM_A}/search?q=switch")[0], 200)
        self.assertEqual(self.h.call("GET", f"/api/room/{ROOM_A}")[0], 200)
        self.assertEqual(self.h.service.workers.attached(), {})
        self.assertEqual(self.h.service.sessions.owned_threads(), {})


class RoutingTests(unittest.TestCase):
    """Queued work and approvals reach the conversation they belong to."""

    def test_queued_messages_dispatch_through_their_own_worker(self):
        log = Path(tempfile.mkdtemp(prefix="atlas-worker-turns-")) / "turns.jsonl"
        os.environ["ATLAS_FIXTURE_TURN_LOG"] = str(log)
        h = WorkerHarness()
        try:
            h.attach(ROOM_A)
            h.attach(ROOM_B)
            self.assertEqual(h.call("POST", f"/api/room/{ROOM_A}/pending",
                                    body={"client_id": "queue-room-a0001",
                                          "body": "for room a"})[0], 201)
            self.assertEqual(h.call("POST", f"/api/room/{ROOM_B}/pending",
                                    body={"client_id": "queue-room-b0001",
                                          "body": "for room b"})[0], 201)

            def dispatched():
                if not log.exists():
                    return []
                rows = [json.loads(line) for line in log.read_text().splitlines() if line]
                return rows if len(rows) >= 2 else []

            rows = _wait(dispatched)
            self.assertEqual(len(rows), 2, rows)
            routed = {row["clientId"]: row["threadId"] for row in rows}
            self.assertEqual(routed["queue-room-a0001"], THREAD_ONE)
            self.assertEqual(routed["queue-room-b0001"], THREAD_TWO)
        finally:
            os.environ.pop("ATLAS_FIXTURE_TURN_LOG", None)
            h.close()

    def test_approvals_route_to_the_conversation_that_asked(self):
        h = WorkerHarness(mode="approval")
        try:
            h.attach(ROOM_A)
            h.attach(ROOM_B)
            self.assertEqual(h.call("POST", f"/api/room/{ROOM_A}/submit",
                                    body={"client_id": "approval-route001",
                                          "body": "delete the scratch dir"})[0], 200)
            pending = _wait(lambda: h.call("GET", "/api/approvals")[1]["approvals"])
            self.assertEqual(len(pending), 1, pending)
            self.assertEqual(pending[0]["thread_id"], THREAD_ONE)
            self.assertEqual(pending[0]["room"], ROOM_A)
            # Only the conversation that asked is holding a question.
            self.assertEqual(
                h.worker(THREAD_TWO).sessions.server.pending_requests(), [])
            # The fixture's turn is still open around the approval, so the
            # turn is what release reports first; either way A is busy and B
            # is not.
            self.assertTrue(h.service.workers.busy(THREAD_ONE))
            self.assertEqual(h.service.workers.busy(THREAD_TWO), "")

            status, answered = h.call("POST", "/api/approvals/answer", body={
                "key": pending[0]["key"], "kind": "command", "decision": "decline"})
            self.assertEqual(status, 200, answered)
            self.assertEqual(answered["answered"]["thread_id"], THREAD_ONE)
            self.assertEqual(h.call("GET", "/api/approvals")[1]["approvals"], [])
        finally:
            h.close()

    def test_a_release_is_refused_while_that_conversation_has_an_approval(self):
        h = WorkerHarness(mode="approval")
        try:
            h.attach(ROOM_A)
            h.attach(ROOM_B)
            h.call("POST", f"/api/room/{ROOM_A}/submit",
                   body={"client_id": "approval-block01", "body": "delete the scratch dir"})
            _wait(lambda: h.call("GET", "/api/approvals")[1]["approvals"])
            worker = h.worker(THREAD_ONE)
            status, payload = h.call("POST", f"/api/room/{ROOM_A}/release", body={})
            self.assertEqual(status, 409, payload)
            self.assertEqual(payload["error"], "connection_busy")
            self.assertIn("will not release this session yet", payload["message"])
            self.assertIs(h.worker(THREAD_ONE), worker)
            self.assertTrue(worker.running)

            # Even with the turn settled, the unanswered approval alone still
            # blocks release: nobody is asked to interrupt implicitly.
            for pending in worker.sessions.server.pending_requests(THREAD_ONE):
                worker.sessions.note_turn(THREAD_ONE, pending["turn_id"], False)
            self.assertEqual(h.service.workers.busy(THREAD_ONE),
                             "a native approval is still waiting")
            status, payload = h.call("POST", f"/api/room/{ROOM_A}/release", body={})
            self.assertEqual(status, 409, payload)
            self.assertIn("approval", payload["message"])
            self.assertTrue(worker.running)

            # B is releasable while A is blocked.
            self.assertEqual(h.call("POST", f"/api/room/{ROOM_B}/release", body={})[0], 200)
            self.assertTrue(worker.running)
        finally:
            h.close()


class AdapterBoundaryTests(unittest.TestCase):
    """Agent-neutral shape, Codex-only reality."""

    def test_only_codex_has_an_adapter(self):
        adapter = workers.adapter_for("codex", ["/bin/true"])
        self.assertEqual(adapter.agent, "codex")
        self.assertEqual(workers.adapter_for("", ["/bin/true"]).agent, "codex")
        for name in ("claude", "claude-code", "gemini", "pane", "hermes"):
            with self.assertRaises(native.NativeError) as caught:
                workers.adapter_for(name, ["/bin/true"])
            self.assertEqual(caught.exception.code, "agent_unsupported")
            self.assertIn("only Codex", str(caught.exception))

    def test_attaching_a_foreign_agent_is_refused_not_faked(self):
        pool = workers.SessionWorkerPool(command=["/bin/true"], on_event=lambda event: None)
        with self.assertRaises(native.NativeError) as caught:
            pool.attach(THREAD_ONE, ROOM_A, agent="claude")
        self.assertEqual(caught.exception.code, "agent_unsupported")
        self.assertEqual(pool.attached(), {})


if __name__ == "__main__":
    unittest.main()
