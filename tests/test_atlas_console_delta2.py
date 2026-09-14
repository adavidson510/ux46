"""Delta checks for follow-up 2.

Covers only what changed: the capability-aware history fallback the installed
Codex 0.153.4 forces, full-history search and stable-id jumps, local voice,
private state permissions, and the test-thread target. The original suite in
tests/test_atlas_console.py still guards everything else.

One test reads a real local session through the adapter's own fallback path;
it costs no model call and skips when the CLI is absent.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import atlas_native as native  # noqa: E402
import atlas_voice as voice  # noqa: E402
from test_atlas_console import ConsoleHarness, THREAD_ONE  # noqa: E402

ROOM = "fixture/console-work"


class HistoryFallbackTests(unittest.TestCase):
    """The installed runtime refuses thread/items/list for some threads."""

    def test_falls_back_to_thread_read_turns(self):
        h = ConsoleHarness(mode="nolist")
        try:
            h.start_runtime()
            status, page = h.call("GET", f"/api/room/{ROOM}/history?limit=10&direction=desc")
            self.assertEqual(status, 200)
            self.assertEqual(page["source"], "thread_read")
            self.assertEqual(len(page["items"]), 10)
            self.assertTrue(page["next_cursor"].startswith("flat:"))
            self.assertFalse(page["complete"])
            # the newest item first, exactly as the paged API would order it
            self.assertEqual(page["items"][0]["id"], "i62")
            status, page2 = h.call(
                "GET", f"/api/room/{ROOM}/history?limit=10&direction=desc"
                       f"&cursor={page['next_cursor']}")
            self.assertEqual(status, 200)
            self.assertEqual(page2["source"], "thread_read")
            self.assertNotEqual(page2["items"][0]["id"], page["items"][0]["id"])
        finally:
            h.close()

    def test_paged_source_is_reported_when_the_runtime_serves_it(self):
        h = ConsoleHarness()
        try:
            h.start_runtime()
            status, page = h.call("GET", f"/api/room/{ROOM}/history?limit=5")
            self.assertEqual(status, 200)
            self.assertEqual(page["source"], "items_list")
        finally:
            h.close()

    def test_an_unreadable_thread_is_unavailable_but_the_room_still_works(self):
        """Superseded expectation.

        Revision 2 called "both read paths refused" an empty history. Follow-up
        5 narrows that: only a successful read with no turns is an empty
        conversation. Both paths refusing is an unavailable history — the room
        stays selectable and controllable either way, which was the point.
        """

        h = ConsoleHarness(mode="empty")
        try:
            h.start_runtime()
            status, page = h.call("GET", f"/api/room/{ROOM}/history?limit=10")
            self.assertEqual(status, 200)
            self.assertEqual(page["items"], [])
            self.assertTrue(page["unavailable"])
            self.assertTrue(page["retryable"])
            self.assertFalse(page["complete"])
            self.assertEqual(page["source"], "unavailable")
            # the room itself is still selectable and controllable
            status, room = h.call("GET", f"/api/room/{ROOM}")
            self.assertEqual(status, 200)
            self.assertTrue(room["controllable"])
        finally:
            h.close()

    def test_reading_history_never_resumes_the_thread(self):
        h = ConsoleHarness(mode="nolist")
        try:
            sessions = h.start_runtime()
            h.call("GET", f"/api/room/{ROOM}/history?limit=10")
            h.call("GET", f"/api/room/{ROOM}/search?q=switch")
            self.assertEqual(sessions.owned_threads(), {})
        finally:
            h.close()


class SearchAndJumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ConsoleHarness(mode="nolist")
        cls.h.start_runtime()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_an_early_question_is_found_outside_the_loaded_page(self):
        first = self.h.call("GET", f"/api/room/{ROOM}/history?limit=40&direction=desc")[1]
        loaded = {item["id"] for item in first["items"]}
        self.assertNotIn("i1", loaded)          # the early question is off-page

        status, found = self.h.call(
            "GET", f"/api/room/{ROOM}/search?kinds=human&q=switch%20rooms")
        self.assertEqual(status, 200)
        self.assertEqual([hit["item_id"] for hit in found["hits"]], ["i1"])
        self.assertEqual(found["hits"][0]["kind"], "human")
        self.assertIn("switch rooms", found["hits"][0]["snippet"])
        self.assertGreater(found["searched_items"], 40)

    def test_jump_returns_a_page_around_the_stable_id(self):
        status, page = self.h.call("GET", f"/api/room/{ROOM}/history?around=i1")
        self.assertEqual(status, 200)
        ids = [item["id"] for item in page["items"]]
        self.assertIn("i1", ids)
        self.assertEqual(page["focus_index"], ids.index("i1"))
        self.assertTrue(page["later_available"])
        self.assertFalse(page["earlier_available"])

    def test_updates_search_only_lists_reported_final_answers(self):
        status, found = self.h.call("GET", f"/api/room/{ROOM}/search?kinds=final&q=")
        self.assertEqual(status, 200)
        ids = {hit["item_id"] for hit in found["hits"]}
        self.assertIn("i4", ids)                # phase final_answer
        self.assertIn("i62", ids)
        self.assertNotIn("i2", ids)             # commentary is not an update
        self.assertTrue(all(hit["phase"] == "final_answer" for hit in found["hits"]))

    def test_unknown_item_is_refused_clearly(self):
        status, payload = self.h.call("GET", f"/api/room/{ROOM}/history?around=nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "item_unknown")


class VoiceTests(unittest.TestCase):
    def test_voice_is_off_and_says_why_when_not_configured(self):
        h = ConsoleHarness()
        try:
            status, payload = h.call("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertFalse(payload["voice"]["enabled"])
            self.assertIn("--voice-model-dir", payload["voice"]["reason"])
            status, refused = h.call("POST", f"/api/room/{ROOM}/speak",
                                     body={"item_id": "i4"})
            self.assertEqual(status, 404)
            self.assertEqual(refused["error"], "voice_off")
        finally:
            h.close()

    def test_missing_model_files_are_reported_not_guessed(self):
        service = voice.VoiceService("/tmp/atlas-voice-does-not-exist", "/tmp/atlas-voice-cache")
        status = service.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(len(status["missing"]), 2)
        with self.assertRaises(voice.VoiceError) as caught:
            service.speak("hello")
        self.assertEqual(caught.exception.code, "voice_unavailable")

    def test_bad_voice_and_empty_text_are_refused(self):
        service = voice.VoiceService("/tmp/atlas-voice-does-not-exist", "/tmp/atlas-voice-cache")
        with self.assertRaises(voice.VoiceError) as bad:
            service.speak("hello", voice="not-a-voice")
        self.assertEqual(bad.exception.code, "bad_voice")
        with self.assertRaises(voice.VoiceError) as empty:
            service.speak("   ")
        self.assertEqual(empty.exception.code, "empty_text")

    def test_audio_key_shape_is_enforced(self):
        service = voice.VoiceService("/tmp/x", "/tmp/atlas-voice-cache")
        with self.assertRaises(voice.VoiceError):
            service.path_for("../../etc/passwd")

    def test_long_text_is_partial_not_silently_truncated(self):
        speech = voice.Speech("k" * 40, Path("/tmp/x.wav"), "af_heart", 24000, 1.0,
                              voice.MAX_CHARS, voice.MAX_CHARS + 500, False, 10)
        payload = speech.as_json()
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["total_chars"], voice.MAX_CHARS + 500)


class HardeningTests(unittest.TestCase):
    def test_state_directory_and_journal_are_owner_only(self):
        h = ConsoleHarness()
        try:
            state_dir = h.service.state_dir
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            db = state_dir / "atlas-console.sqlite3"
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
        finally:
            h.close()

    def test_test_thread_uses_only_the_configured_directory(self):
        h = ConsoleHarness()
        try:
            h.service.config.allow_test_thread = True
            h.service.config.test_thread_cwd = ""
            h.start_runtime()
            status, refused = h.call("POST", "/api/test-thread",
                                     body={"cwd": "/etc"})
            self.assertEqual(status, 400)
            self.assertEqual(refused["error"], "bad_cwd")
            h.service.config.test_thread_cwd = str(h.tmp)
            status, made = h.call("POST", "/api/test-thread", body={"cwd": "/etc"})
            self.assertEqual(status, 200)
            self.assertEqual(made["cwd"], str(h.tmp))   # the browser's path is ignored
        finally:
            h.close()


class KeepAliveTests(unittest.TestCase):
    """A handler that ignores its body must not desynchronise the connection."""

    def test_body_ignoring_post_leaves_a_clean_connection(self):
        from http.client import HTTPConnection

        h = ConsoleHarness()
        try:
            h.start_runtime()
            conn = HTTPConnection("127.0.0.1", h.port, timeout=20)
            head = {
                "Host": f"127.0.0.1:{h.port}",
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{h.port}",
                "X-Atlas-CSRF": h.service.csrf_token,
            }
            # /release and /continue do not read their bodies
            for path in (f"/api/room/{ROOM}/release", f"/api/room/{ROOM}/continue"):
                conn.request("POST", path, body=json.dumps({"padding": "x" * 500}).encode(),
                             headers=head)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                json.loads(response.read())          # must parse, on this same socket
            # the very next request on the same connection must still be ours
            conn.request("GET", "/api/bootstrap", headers={"Host": f"127.0.0.1:{h.port}"})
            response = conn.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertIn("csrf", payload)
            conn.close()
        finally:
            h.close()


class RealNativeFallbackTests(unittest.TestCase):
    """The adapter's read path against the installed binary, no model call."""

    TEST_THREAD = "01a07566-fac1-7ee2-9139-e6b4f113d5bd"

    def setUp(self):
        codex = os.environ.get("ATLAS_CODEX_BIN") or str(Path.home() / ".local/bin/codex")
        if not Path(codex).exists():
            self.skipTest("local Codex CLI not present")
        self.server = native.AppServer(command=[codex, "app-server"])
        self.server.start()
        self.sessions = native.NativeSessions(self.server)

    def tearDown(self):
        self.server.stop()

    def test_full_history_of_the_dedicated_test_thread(self):
        entries, source = self.sessions.full_history(self.TEST_THREAD)
        self.assertIn(source, ("thread_read", "thread_read_truncated"))
        kinds = [(e.get("item") or {}).get("type") for e in entries]
        self.assertIn("userMessage", kinds)
        self.assertIn("agentMessage", kinds)
        for entry in entries:
            self.assertTrue(entry.get("turnId"))
            self.assertTrue((entry.get("item") or {}).get("id"))
        # nothing was taken over just to read it
        self.assertEqual(self.sessions.owned_threads(), {})

    def test_search_and_jump_work_on_the_real_thread(self):
        found = self.sessions.search_history(self.TEST_THREAD, "", ("human", "final"))
        self.assertGreaterEqual(found["total"], 1)
        hit = found["hits"][0]
        page = self.sessions.page_around(self.TEST_THREAD, hit["item_id"], radius=5)
        ids = [str((e.get("item") or {}).get("id")) for e in page["data"]]
        self.assertIn(hit["item_id"], ids)

    def test_paged_read_uses_whichever_source_the_runtime_supports(self):
        page = self.sessions.list_items(self.TEST_THREAD, limit=5, direction="desc")
        self.assertIn(page.get("source"), ("items_list", "thread_read", "thread_read_truncated"))
        self.assertLessEqual(len(page.get("data") or []), 5)


if __name__ == "__main__":
    unittest.main()
