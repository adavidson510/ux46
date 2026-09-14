"""Bounded disk-only discovery refreshes; no native runtime or real Vault."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import atlas_discovery as discovery


class DiscoveryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.catalog = discovery.Discovery(ttl=15, native_db="/nonexistent/fixture.sqlite")
        self.old = discovery._Catalog("old-node", (), (), ("old record error",), time.monotonic())
        self.catalog._publish(self.old)

    def expire(self):
        self.catalog._publish(replace(self.catalog._catalog, loaded_at=time.monotonic() - 30))

    def wait_for_refresh(self):
        deadline = time.monotonic() + 2
        while self.catalog._refreshing and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(self.catalog._refreshing, "background scan did not finish")

    def test_stale_reads_return_complete_snapshot_while_one_scan_runs(self):
        entered, release = threading.Event(), threading.Event()
        next_catalog = replace(self.old, node="new-node", errors=("new record error",))

        def scan():
            entered.set()
            self.assertTrue(release.wait(2))
            return replace(next_catalog, loaded_at=time.monotonic())

        old_room = SimpleNamespace(id="fixture/old-room")
        new_room = SimpleNamespace(id="fixture/new-room")
        self.catalog._publish(replace(self.old, rooms=(old_room,)))
        next_catalog = replace(next_catalog, rooms=(new_room,))
        self.expire()
        with patch.object(self.catalog, "_load_catalog", side_effect=scan) as loader:
            try:
                self.assertEqual(self.catalog.node, "old-node")
                self.assertTrue(entered.wait(1))
                for _ in range(20):
                    self.assertEqual(self.catalog.errors, ["old record error"])
                    self.assertEqual(self.catalog.rooms(), [old_room])
                    self.assertIs(self.catalog.room("fixture/old-room"), old_room)
                    self.assertIsNone(self.catalog.room("fixture/new-room"))
                self.assertEqual(loader.call_count, 1)
                self.assertEqual(self.catalog._catalog.node, "old-node")
            finally:
                release.set()
                self.wait_for_refresh()
            self.assertEqual(self.catalog.node, "new-node")
            self.assertEqual(self.catalog.rooms(), [new_room])
            self.assertEqual(self.catalog.errors, ["new record error"])
            self.assertEqual(loader.call_count, 1)

    def test_forced_refresh_after_old_scan_preserves_newer_write(self):
        entered, release, forced_done = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def scan():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                return replace(self.old, node="older-disk-read", loaded_at=time.monotonic())
            return replace(self.old, node="new-room-written", loaded_at=time.monotonic())

        def force():
            self.catalog.refresh(force=True)
            forced_done.set()

        self.expire()
        with patch.object(self.catalog, "_load_catalog", side_effect=scan):
            self.catalog.refresh()
            self.assertTrue(entered.wait(1))
            writer = threading.Thread(target=force)
            writer.start()
            try:
                self.assertFalse(forced_done.wait(.05), "force returned before older scan completed")
            finally:
                release.set()
                writer.join(2)
                self.wait_for_refresh()
            self.assertFalse(writer.is_alive())
            self.assertTrue(forced_done.is_set())
            self.assertEqual(calls, [1, 2])
            self.assertEqual(self.catalog.node, "new-room-written")

    def test_failed_refresh_retains_catalog_exposes_error_and_backs_off(self):
        self.expire()
        previous = self.catalog._catalog
        with patch.object(self.catalog, "_load_catalog", side_effect=OSError("fixture unreadable")) as loader:
            self.catalog.refresh()
            self.wait_for_refresh()
            for _ in range(20):
                self.assertEqual(self.catalog.node, "old-node")
                self.assertEqual(self.catalog.rooms(), [])
                self.assertEqual(self.catalog.errors,
                                 ["Discovery refresh failed: fixture unreadable", "old record error"])
            self.assertIs(self.catalog._catalog, previous)
            self.assertEqual(loader.call_count, 1)
        with patch.object(self.catalog, "_load_catalog", return_value=replace(self.old, errors=())):
            self.catalog.refresh(force=True)
            self.assertEqual(self.catalog.errors, [])

    def test_empty_catalog_is_loaded_once_until_ttl_expires(self):
        self.catalog._catalog = None
        with patch.object(self.catalog, "_load_catalog", return_value=self.old) as loader:
            for _ in range(20):
                self.assertEqual(self.catalog.rooms(), [])
                self.assertEqual(self.catalog.projects(), [])
                self.assertEqual(self.catalog.node, "old-node")
            self.assertEqual(loader.call_count, 1)


if __name__ == "__main__":
    unittest.main()
