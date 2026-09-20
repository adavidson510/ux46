"""Native activity markers retain identity without claiming live state/output."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from atlas_console import _project_item


class SubagentActivityTest(unittest.TestCase):
    def test_native_lifecycle_markers_keep_the_child_reference(self):
        for kind in ("started", "interacted", "interrupted", "completed"):
            with self.subTest(kind=kind):
                projected = _project_item({"turnId": "parent-turn", "item": {
                    "type": "subAgentActivity", "id": "activity", "kind": kind,
                    "agentPath": "/root/review", "agentThreadId": "child-thread"}})
                self.assertEqual(projected["activity_kind"], kind)
                self.assertEqual(projected["agent_path"], "/root/review")
                self.assertEqual(projected["agent_thread_id"], "child-thread")
                self.assertEqual(projected["turn_id"], "parent-turn")
                self.assertNotIn("raw_keys", projected)
                self.assertNotIn("status", projected)
                self.assertNotIn("output", projected)

    def test_missing_or_malformed_references_do_not_become_strings(self):
        projected = _project_item({"item": {"type": "subAgentActivity",
            "agentPath": {"unexpected": "object"}, "agentThreadId": None}})
        self.assertEqual(projected["agent_path"], "")
        self.assertEqual(projected["agent_thread_id"], "")


if __name__ == "__main__":
    unittest.main()
