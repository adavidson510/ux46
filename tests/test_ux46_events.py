"""Event cursors must not silently acknowledge unread completions."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ux46_events import EventLog


class EventJourneys(unittest.TestCase):
    def test_completion_survives_multiple_batches(self):
        log = EventLog()
        log.publish({"room": "fixture/selected", "method": "turn/completed"})
        for _ in range(250):
            log.publish({"room": "fixture/other", "method": "item/updated"})
        cursor, received, pages = 0, [], []
        while True:
            page = log.since(cursor, timeout=0, epoch=log.epoch)
            pages.append(len(page["events"]))
            received.extend(page["events"])
            cursor = page["seq"]
            if not page["more"]:
                break
        self.assertEqual(pages, [100, 100, 51])
        self.assertEqual([e["seq"] for e in received], list(range(1, 252)))
        self.assertEqual(received[0]["method"], "turn/completed")
        self.assertEqual(log.since(cursor, timeout=0)["events"], [])

    def test_expired_events_require_snapshot_reconciliation(self):
        log = EventLog(limit=3)
        for _ in range(5):
            log.publish({"room": "fixture/other"})
        page = log.since(0, timeout=0, epoch=log.epoch)
        self.assertTrue(page["gap"])
        self.assertEqual(page["reason"], "events_expired")
        self.assertEqual([e["seq"] for e in page["events"]], [3, 4, 5])

    def test_restart_detected_even_when_sequence_has_caught_up(self):
        old, new = EventLog(), EventLog()
        for _ in range(5):
            new.publish({"room": "fixture/selected"})
        page = new.since(3, timeout=0, epoch=old.epoch)
        self.assertEqual(page["reason"], "epoch_changed")
        self.assertEqual(len(page["events"]), 5)
        empty = EventLog().since(8, timeout=0)
        self.assertEqual(empty["reason"], "cursor_ahead")
        self.assertEqual(empty["seq"], 0)

    def test_filtered_pages_advance_without_losing_relevant_events(self):
        log = EventLog()
        for _ in range(110):
            log.publish({"room": "fixture/other"})
            log.publish({"rooms": ["fixture/selected"]})
        first = log.since(0, timeout=0, room="fixture/selected")
        second = log.since(first["seq"], timeout=0, room="fixture/selected")
        self.assertEqual(len(first["events"]), 100)
        self.assertEqual(len(second["events"]), 10)
        self.assertEqual(second["seq"], 220)
        log.publish({"room": "fixture/other"})
        self.assertEqual(log.since(220, timeout=0, room="fixture/selected")["seq"], 221)


if __name__ == "__main__":
    unittest.main()
