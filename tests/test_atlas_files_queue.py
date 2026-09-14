"""Attachment HTTP boundary and queued native projection."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from test_atlas_console import ConsoleHarness, THREAD_ONE  # noqa: E402


ROOM = "fixture/console-work"
PNG = b"\x89PNG\r\n\x1a\nfixture-image"


class AttachmentQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = Path(tempfile.mkdtemp(prefix="atlas-file-turns-")) / "turns.jsonl"
        os.environ["ATLAS_FIXTURE_TURN_LOG"] = str(self.log)
        self.h = ConsoleHarness()
        self.h.start_runtime()

    def tearDown(self) -> None:
        self.h.close()
        os.environ.pop("ATLAS_FIXTURE_TURN_LOG", None)

    def upload(self, name: str, content_type: str, data: bytes) -> tuple[int, dict]:
        boundary = "atlas-file-test-boundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        conn = HTTPConnection("127.0.0.1", self.h.port, timeout=15)
        conn.request("POST", f"/api/room/{ROOM}/files", body=body, headers={
            "Host": f"127.0.0.1:{self.h.port}", "Origin": f"http://127.0.0.1:{self.h.port}",
            "X-Atlas-CSRF": self.h.service.csrf_token,
            "Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body)),
        })
        response = conn.getresponse(); raw = response.read(); status = response.status
        conn.close()
        return status, json.loads(raw)

    def test_upload_preview_room_auth_queue_projection_and_project_pref_cas(self) -> None:
        status, payload = self.upload("fixture.png", "application/octet-stream", PNG)
        self.assertEqual(status, 201, payload)
        file = payload["file"]
        self.assertEqual(file["mime"], "image/png")
        self.assertTrue(file["preview_url"].endswith("/preview"))

        conn = HTTPConnection("127.0.0.1", self.h.port, timeout=15)
        conn.request("GET", file["preview_url"] + "?room=" + ROOM, headers={"Host": f"127.0.0.1:{self.h.port}"})
        response = conn.getresponse(); self.assertEqual(response.status, 200); self.assertEqual(response.read(), PNG)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        conn.close()

        # A room mismatch cannot turn a known opaque id into another room's attachment.
        status, denied = self.h.call("POST", "/api/room/fixture/second-room/pending", body={
            "client_id": "filewrongroom1", "body": "wrong room", "attachments": [{"file_id": file["id"]}],
        })
        self.assertEqual(status, 400, denied)

        self.assertEqual(self.h.call("POST", f"/api/room/{ROOM}/continue", body={})[0], 200)
        status, queued = self.h.call("POST", f"/api/room/{ROOM}/pending", body={
            "client_id": "filequeue0001", "body": "inspect attachment", "attachments": [{"file_id": file["id"]}],
        })
        self.assertEqual(status, 201, queued)
        deadline = time.time() + 4
        while time.time() < deadline and not self.log.exists():
            time.sleep(.05)
        dispatched = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        turn = next(item for item in dispatched if item.get("clientId") == "filequeue0001")
        self.assertEqual(turn["threadId"], THREAD_ONE)
        self.assertTrue(any(piece.get("type") == "localImage" for piece in turn["input"]))

        status, pref = self.h.call("PATCH", "/api/projects/fixture/pref", body={
            "base_version": 0, "display_name": "Fixture Files", "appearance": "purple",
            "icon_file_id": file["id"],
        })
        self.assertEqual(status, 200, pref)
        self.assertEqual(pref["pref"]["version"], 1)
        status, stale = self.h.call("PATCH", "/api/projects/fixture/pref", body={
            "base_version": 0, "appearance": "violet",
        })
        self.assertEqual(status, 409, stale)


if __name__ == "__main__":
    unittest.main()
