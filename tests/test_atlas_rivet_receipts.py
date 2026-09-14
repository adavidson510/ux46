"""Regressions from Agent3's installed OpenClaw 2026.7.1-2 wire responses."""
import sys
import unittest
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import atlas_rivet as r


class RivetWireTest(unittest.TestCase):
    def test_started_ack_belongs_to_exact_send(self):
        service = r.RivetService.__new__(r.RivetService)
        key = "ux46-agent3:983ab521-320b-4d36-9b27-b8dc33df4293"
        ack = service.read_acknowledgement({"runId": key, "status": "started"}, key)
        self.assertEqual(ack["status"], "started")
        self.assertIsNone(service.read_acknowledgement(
            {"runId": "another-run", "status": "started"}, key))
        self.assertIsNone(service.read_acknowledgement({"status": "started"}, key))

    def test_user_mirror_matches_journal_client(self):
        item = r._block_items({"role": "user", "content": "hi",
            "__openclaw": {"id": "message-1", "idempotencyKey":
                           "ux46-agent3:client-123:user"}}, 0)[0]
        self.assertEqual(item["client_id"], "client-123")

    def test_only_matching_tool_result_finishes_spinner(self):
        items = []
        for content, mirror in [
            ({"type": "toolCall", "id": "exec-1", "name": "bash"}, "turn-a:tool:call"),
            ({"type": "toolCall", "id": "exec-2", "name": "bash"}, "turn-a:tool:call"),
            ({"type": "toolResult", "toolCallId": "exec-1", "text": "done"}, "turn-a:tool:result"),
            ({"type": "toolResult", "toolCallId": "exec-2", "text": "elsewhere"}, "turn-b:tool:result"),
        ]:
            items.extend(r._block_items({"role": "assistant", "content": [content],
                "__openclaw": {"mirrorIdentity": mirror}}, 0))
        r._settle_tool_items(items)
        self.assertEqual(items[0]["status"], "completed")
        self.assertEqual(items[1]["status"], "called")

    def test_stream_callback_does_not_wait_on_its_own_reader(self):
        service = r.RivetService.__new__(r.RivetService)
        class Catalog:
            _rooms = {"room": SimpleNamespace(id="unfiled/main", session_key="agent:main:main")}
            def room_for_key(self, key):
                raise AssertionError("callback attempted a blocking catalog refresh")
        published = []
        service.catalog = Catalog()
        service.events = SimpleNamespace(publish=published.append)
        service._active_runs = {}
        service.on_gateway_event("sessions.changed", {
            "sessionKey": "agent:main:main", "hasActiveRun": False})
        self.assertFalse(service._active_runs["agent:main:main"]["active"])
        self.assertEqual(published[0]["room"], "unfiled/main")

    def test_history_repairs_missed_completion_but_preserves_newer_push(self):
        service = r.RivetService.__new__(r.RivetService)
        service.config = SimpleNamespace(agent_id="main")
        service.attach_sent_files = lambda items: None
        room = SimpleNamespace(id="unfiled/main", session_key="agent:main:main")
        service._active_runs = {room.session_key: {"active": True, "run_ids": ["old"]}}
        page = {"messages": [], "sessionInfo": {"hasActiveRun": False, "activeRunIds": []}}
        service.transport = SimpleNamespace(call=lambda *a, **kw: page)
        service.history(room, 40, "", "desc")
        self.assertFalse(service._active_runs[room.session_key]["active"])
        def read_with_newer_push(*args, **kwargs):
            service._active_runs[room.session_key] = {
                "active": True, "run_ids": ["new"], "observed_at": time.monotonic()}
            return page
        service.transport.call = read_with_newer_push
        service.history(room, 40, "", "desc")
        self.assertEqual(service._active_runs[room.session_key]["run_ids"], ["new"])


if __name__ == "__main__":
    unittest.main()
