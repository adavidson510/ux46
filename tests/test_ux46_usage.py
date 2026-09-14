import json
import os
import datetime as dt
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ux46_usage import UsageStore


THREAD = "00000000-0000-4000-8000-000000000099"


def message(stamp, *, last=None, total=None, response_id=None, model=None, context_window=None, compact=False):
    if compact:
        return {"timestamp": stamp, "type": "compacted", "payload": {}}
    if model:
        return {"timestamp": stamp, "type": "turn_context", "payload": {"model": model}}
    info = {"last_token_usage": last or {}, "total_token_usage": total or {}}
    if response_id:
        info["response_id"] = response_id
    if context_window:
        info["model_context_window"] = context_window
    return {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "info": info}}


def usage_record(stamp, response_id, usage):
    return {"timestamp": stamp, "type": "token_usage_record",
            "payload": {"response_id": response_id, "usage": usage}}


def write(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


class UsageStoreTests(unittest.TestCase):
    def rollout(self, root):
        return Path(root) / f"rollout-2026-09-11T01-00-00-{THREAD}.jsonl"

    def test_connection_context_closes_its_descriptor(self):
        with tempfile.TemporaryDirectory() as root:
            store = UsageStore(Path(root) / "state")
            closed = []
            for _ in range(40):
                with store._connect() as conn:
                    conn.execute("SELECT 1").fetchone()
                closed.append(conn)
            for conn in closed:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_native_ids_replace_snapshots_and_room_aliases_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.rollout(root)
            write(path, [
                message("2026-09-11T01:00:00Z", model="gpt-6-astra"),
                message("2026-09-11T01:00:01Z", response_id="r1", last={"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20, "reasoning_output_tokens": 7}, total={"input_tokens": 100},),
                # A later receipt for r1 replaces it, instead of adding r1 twice.
                message("2026-09-11T01:00:02Z", response_id="r1", context_window=258400, last={"input_tokens": 120, "cached_input_tokens": 90, "output_tokens": 24, "reasoning_output_tokens": 9}, total={"input_tokens": 120}),
                message("2026-09-11T01:00:03Z", compact=True),
            ])
            store = UsageStore(Path(root) / "state")
            first = store.room(path, "alpha", project="atlas", day="2026-09-11")
            second = store.room(path, "beta", project="atlas", day="2026-09-11")
            self.assertEqual(first["usage"]["input_tokens"], 120)
            self.assertEqual(first["usage"]["cached_input_tokens"], 90)
            self.assertEqual(first["usage"]["output_tokens"], 24)
            self.assertEqual(first["usage"]["reasoning_output_tokens"], 9)
            self.assertEqual(first["usage"]["recent_context_window_tokens"], 258400)
            self.assertEqual(first["by_model"][0]["model"], "gpt-6-astra")
            self.assertEqual(len(second["alias"]["same_native_thread_rooms"]), 2)
            summary = store.summary(project="atlas", day="2026-09-11")
            self.assertEqual(summary["usage"]["input_tokens"], 120)
            self.assertEqual(len(summary["native_threads"]), 1)
            self.assertIn({"kind": "compacted", "count": 1}, first["annotations"])

    def test_legacy_snapshots_skip_consecutive_duplicates_and_annotate_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.rollout(root)
            write(path, [
                message("2026-09-11T01:00:01Z", last={"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}, total={"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}),
                message("2026-09-11T01:00:02Z", last={"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}, total={"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}),
                message("2026-09-11T01:00:03Z", last={"input_tokens": 4, "cached_input_tokens": 1, "output_tokens": 2}, total={"input_tokens": 4, "cached_input_tokens": 1, "output_tokens": 2}),
            ])
            result = UsageStore(Path(root) / "state").room(path, "room", day="2026-09-11")
            self.assertEqual(result["usage"]["steps"], 2)
            self.assertEqual(result["usage"]["input_tokens"], 34)
            self.assertGreaterEqual(result["usage"]["input_tokens"], 0)
            self.assertIn({"kind": "counter_reset", "count": 1}, result["annotations"])
            self.assertIn("legacy_snapshot_approximation", str(result["coverage"]["identity"]))

    def test_paired_native_record_and_token_count_are_one_usage_step_with_latest_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.rollout(root)
            actual = {"input_tokens": 18, "cached_input_tokens": 10, "output_tokens": 4}
            write(path, [
                message("2026-09-11T04:00:00Z", model="gpt-6-astra"),
                usage_record("2026-09-11T04:00:01Z", "resp-one", actual),
                message("2026-09-11T04:00:02Z", last=actual, total=actual, context_window=258400),
            ])
            result = UsageStore(Path(root) / "state").room(path, "room", day="2026-09-11")
            self.assertEqual(result["usage"]["steps"], 1)
            self.assertEqual(result["usage"]["input_tokens"], 18)
            self.assertEqual(result["usage"]["latest_input_tokens"], 18)
            self.assertEqual(result["usage"]["latest_model"], "gpt-6-astra")
            self.assertEqual(result["usage"]["model_context_window_tokens"], 258400)
            self.assertIsNone(result["usage"]["reasoning_output_tokens"])
            self.assertFalse(result["coverage"]["field_coverage"]["reasoning_output_tokens"])
            self.assertIn("native_response_id", str(result["coverage"]["identity"]))

    def test_truncation_is_safe_and_new_tail_is_ingested_without_negative_delta(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.rollout(root)
            write(path, [message("2026-09-11T01:00:01Z", response_id="old", last={"input_tokens": 40}, total={"input_tokens": 40})])
            store = UsageStore(Path(root) / "state")
            store.ingest(path)
            write(path, [message("2026-09-11T02:00:01Z", response_id="new", last={"input_tokens": 7}, total={"input_tokens": 7})])
            # Rotation is dated from the file's mtime; keep that fixture on
            # the queried day rather than depending on the machine's clock.
            stamp = dt.datetime(2026, 9, 11, 2, tzinfo=dt.timezone.utc).timestamp()
            os.utime(path, (stamp, stamp))
            result = store.room(path, "room", day="2026-09-11")
            self.assertEqual(result["usage"]["input_tokens"], 47)
            self.assertGreaterEqual(result["source"]["rotations"], 1)
            self.assertIn({"kind": "rotation", "count": 1}, result["annotations"])

    def test_large_first_read_returns_pending_then_finishes_without_a_ui_block(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.rollout(root)
            filler = {"type": "event_msg", "payload": {"type": "noise", "pad": "x" * 1024}}
            path.write_text("".join(json.dumps(filler) + "\n" for _ in range(400)) + json.dumps(
                message("2026-09-11T03:00:01Z", response_id="tail", last={"input_tokens": 11}, total={"input_tokens": 11})
            ) + "\n")
            store = UsageStore(Path(root) / "state", initial_read_bytes=256 * 1024)
            first = store.room(path, "room", day="2026-09-11")
            self.assertTrue(first["incremental"]["pending"])
            for _ in range(100):
                if not store.poll(path)["pending"]:
                    break
                time.sleep(.01)
            self.assertFalse(store.poll(path)["pending"])
            self.assertEqual(store.room(path, "room", day="2026-09-11")["usage"]["input_tokens"], 11)

    def test_tell_receipts_keep_missing_usage_unknown_and_separate(self):
        with tempfile.TemporaryDirectory() as root:
            receipts = Path(root) / "receipts"; receipts.mkdir()
            (receipts / "one.json").write_text(json.dumps({"date": "2026-09-11", "usage": {"input_tokens": 8, "cached_input_tokens": 3, "output_tokens": 2, "reasoning_output_tokens": 1, "model": "sol"}}))
            (receipts / "two.json").write_text(json.dumps({"date": "2026-09-11", "status": "failed"}))
            result = UsageStore(Path(root) / "state", tell_receipts_dir=receipts).tell_receipts(day="2026-09-11")
            self.assertEqual(result["usage"]["input_tokens"], 8)
            self.assertEqual(result["unknown_cost_receipts"], 1)
            self.assertIsNone(result["usage"]["cache_write_input_tokens"])
            self.assertFalse(result["field_coverage"]["cache_write_input_tokens"])
            self.assertNotIn("dollars", result)


if __name__ == "__main__":
    unittest.main()
