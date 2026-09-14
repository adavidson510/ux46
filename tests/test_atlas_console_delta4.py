"""Delta checks for restore and exact runtime ownership (follow-up 7).

Three narrow behaviours: restoring a saved room that sits outside the first
catalogue page, excluding only the app-server chain from ownership, and keeping
dictation in the room it was spoken to (that last one is checked in the
browser). No native model calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import atlas_native as native  # noqa: E402
from test_atlas_console import ConsoleHarness, THREAD_ONE, THREAD_TWO  # noqa: E402

ROOM = "fixture/console-work"


class AppServerIdentityTests(unittest.TestCase):
    """Only the configured process and its native app-server child are us."""

    def test_direct_native_command_owns_only_itself(self):
        # Local's final startup path: --codex-command is the native binary, so
        # every child is tool work and none of it is the app-server.
        table = {100: (1, "codex"), 200: (100, "sh"), 300: (200, "codex")}
        self.assertEqual(native.app_server_pids(100, table=table), {100})

    def test_node_launcher_adds_exactly_its_native_child(self):
        table = {10: (1, "node"), 11: (10, "codex"), 12: (11, "sh"), 13: (12, "codex")}
        self.assertEqual(native.app_server_pids(10, table=table), {10, 11})

    def test_a_worker_cli_under_the_app_server_is_not_us(self):
        table = {10: (1, "node"), 11: (10, "codex"), 12: (11, "sh"), 13: (12, "codex")}
        mine = native.app_server_pids(10, table=table)
        self.assertNotIn(13, mine)      # the worker owns its own session
        self.assertNotIn(12, mine)

    def test_ambiguity_and_a_missing_process_fail_closed(self):
        for table in (
            {10: (1, "node"), 11: (10, "codex"), 12: (10, "codex")},  # two candidates
            {},                                                       # root gone
        ):
            with self.assertRaises(native.NativeError) as caught:
                native.app_server_pids(10, table=table)
            self.assertEqual(caught.exception.code, "ownership_unavailable")

    def test_an_unrecognised_child_is_simply_not_claimed(self):
        """Under-claiming is safe: an unknown holder means Atlas refuses."""

        table = {10: (1, "node"), 11: (10, "some-other-binary")}
        self.assertEqual(native.app_server_pids(10, table=table), {10})

    def test_live_launcher_resolves_to_exactly_its_own_app_server(self):
        """Against the real launcher, with the machine's other Codex processes.

        This host runs several unrelated codex-named processes, including
        another Atlas service. None of them may be claimed as ours.
        """

        launcher_path = Path.home() / ".local/bin/codex"
        if not launcher_path.exists():
            self.skipTest("local Codex launcher not present")
        server = native.AppServer(command=[str(launcher_path), "app-server"])
        server.start()
        try:
            time.sleep(1.0)
            launcher = server.pids[0]
            rows = native._process_table()
            mine = native.app_server_pids(launcher)

            children = [pid for pid, (ppid, name) in rows.items()
                        if ppid == launcher and name in native.APP_SERVER_NAMES]
            self.assertEqual(len(children), 1, "expected one native app-server child")
            self.assertEqual(mine, {launcher, children[0]})

            others = [pid for pid, (_ppid, name) in rows.items()
                      if name in native.APP_SERVER_NAMES and pid not in mine]
            self.assertTrue(others, "no unrelated codex processes to check against")
            for pid in others:
                self.assertNotIn(pid, mine)
            self.assertLessEqual(len(mine), 2)
        finally:
            server.stop()

    def test_a_session_held_by_a_worker_stays_held_elsewhere(self):
        h = ConsoleHarness()
        try:
            sessions = h.start_runtime()
            launcher = sessions.server.pids[0]
            app_server = launcher + 1        # the native child
            worker = launcher + 2            # a Codex CLI the agent started
            sessions.own_pids = lambda refresh=False: {launcher, app_server}

            # Our own app-server holding a rollout is us.
            sessions.probe.table[THREAD_ONE] = {app_server}
            self.assertEqual(sessions.ownership(THREAD_ONE).state, "idle")

            # A different live session held by the worker is not ours to take.
            sessions.probe.table[THREAD_TWO] = {worker}
            state = sessions.ownership(THREAD_TWO)
            self.assertEqual(state.state, "held_elsewhere")
            self.assertEqual(state.external_pids, (worker,))
            with self.assertRaises(native.NativeError) as caught:
                sessions.require_control(THREAD_TWO)
            self.assertEqual(caught.exception.code, "held_elsewhere")
        finally:
            h.close()

    def test_diagnostics_carry_pids_not_command_lines(self):
        h = ConsoleHarness()
        try:
            sessions = h.start_runtime()
            sessions.own_pids = lambda refresh=False: {1}
            sessions.probe.table[THREAD_ONE] = {4242}
            payload = sessions.ownership(THREAD_ONE).as_json()
            self.assertEqual(payload["external_pids"], [4242])
            blob = json.dumps(payload)
            self.assertNotIn("--", blob)          # no argv
            self.assertNotIn("/Users/", blob)     # no paths
        finally:
            h.close()


def write_many_rooms(base: Path, count: int) -> Path:
    """A registry with more sessions than one catalogue page."""

    project_root = base / "Projects" / "wide"
    sessions = project_root / "sessions"
    sessions.mkdir(parents=True)
    for index in range(count):
        name = f"session-{index:03d}"
        # Older dates sort later, so the last ones fall outside the first page.
        day = 28 - (index % 28)
        (sessions / f"{name}.md").write_text(
            "---\n"
            f"project: wide\nsession: {name}\ntitle: Wide room {index:03d}\n"
            f"date: 2026-08-{day:02d}\nupdated: \"2026-08-{day:02d}\"\nstatus: active\n"
            f"keywords: wide, {name}\norigins: \"{name}.origins.json\"\n"
            "---\n\n# wide\n\nSample body.\n", encoding="utf-8")
        (sessions / f"{name}.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "wide", "session": name,
            "origins": [{"runtime": "codex", "node": "user-mac",
                         "session_id": f"01a00000-0000-7000-8000-{index:012d}",
                         "cwd": "/tmp/atlas-fixture", "captured": "2026-08-01",
                         "primary": True}],
        }), encoding="utf-8")
    registry = base / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": 1, "updated": "2026-09-06", "node_id": "user-mac",
        "collective_id": "workspace",
        "nodes": [{"id": "user-mac", "name": "Fixture Mac", "status": "local"}],
        "projects": [{"id": "wide", "name": "Wide", "root": str(project_root), "aliases": []}],
    }), encoding="utf-8")
    return registry


class RoomRestoreTests(unittest.TestCase):
    """A room outside the first catalogue page is still addressable."""

    def setUp(self):
        self.h = ConsoleHarness()
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-wide-"))
        registry = write_many_rooms(self.tmp, 90)
        self.h.service.discovery.registry_path = registry
        self.h.service.discovery.refresh(force=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.h.close()

    def test_a_room_beyond_the_first_page_is_reachable_by_id(self):
        status, page = self.h.call("GET", "/api/rooms?limit=60")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 90)
        self.assertEqual(len(page["rooms"]), 60)
        listed = {room["id"] for room in page["rooms"]}
        outside = [f"wide/session-{i:03d}" for i in range(90)
                   if f"wide/session-{i:03d}" not in listed]
        self.assertTrue(outside, "every room fitted on the first page")

        # The room API answers for it regardless of where paging put it.
        target = outside[0]
        status, room = self.h.call("GET", f"/api/room/{target}")
        self.assertEqual(status, 200)
        self.assertEqual(room["id"], target)
        self.assertTrue(room["controllable"])

        # Its draft and native target round-trip by id, not by page position.
        self.assertEqual(self.h.call("PUT", f"/api/room/{target}/draft",
                                     body={"body": "kept across reload",
                                           "base_version": 0})[0], 200)
        self.assertEqual(self.h.call("GET", f"/api/room/{target}/draft")[1]["body"],
                         "kept across reload")

    def test_a_removed_room_is_definitively_gone(self):
        status, payload = self.h.call("GET", "/api/room/wide/session-999")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_room")


class AudioPolicyTests(unittest.TestCase):
    """Same-origin media is playable; nothing else is."""

    def test_policy_allows_only_same_origin_media(self):
        h = ConsoleHarness()
        try:
            conn = __import__("http.client", fromlist=["HTTPConnection"]).HTTPConnection(
                "127.0.0.1", h.port, timeout=10)
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{h.port}"})
            response = conn.getresponse()
            response.read()
            policy = response.getheader("Content-Security-Policy")
            conn.close()
        finally:
            h.close()
        self.assertIn("media-src 'self'", policy)
        media = [part for part in policy.split(";") if part.strip().startswith("media-src")]
        self.assertEqual(len(media), 1)
        for widened in ("data:", "blob:", "*", "http:", "https:"):
            self.assertNotIn(widened, media[0])
        # the rest of the policy is untouched
        for kept in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                     "connect-src 'self'", "form-action 'none'", "frame-ancestors 'none'",
                     "base-uri 'none'"):
            self.assertIn(kept, policy)


if __name__ == "__main__":
    unittest.main()
