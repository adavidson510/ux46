"""The agent selector and the scoped remote-agent proxy.

Two complete Atlas consoles run here. One is the console under test — the one a
browser on this machine would load. The other stands in for an agent account on
another host: it holds its *own* Vault fixture whose project and session names
are byte-identical to the first, which is the collision this slice has to
survive. They are joined by a ``DirectPort`` transport instead of an SSH
forward, so the proxy, the allowlist, the CSRF handling and the artifact URL
rewriting are all exercised for real without a network or a model call.

Nothing here starts, resumes or releases a native conversation on either side.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import atlas_remote as remote  # noqa: E402
from test_atlas_console import ConsoleHarness  # noqa: E402

APP_JS = (REPO_ROOT / "app" / "console" / "app.js").read_text(encoding="utf-8")
ROOM = "fixture/console-work"


def closed_port() -> int:
    """A loopback port with nothing behind it."""

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def registry_for(local: ConsoleHarness, *, reachable: int, dead: int) -> remote.AgentRegistry:
    """One agent2 we can reach and one agent3 we cannot, with no SSH involved."""

    definitions = {
        "agent2": reachable,
        "agent3": dead,
    }

    def transport(definition: dict):
        return remote.DirectPort(int(definition["remote_port"]))

    config = Path(local.tmp) / "agents.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "agents": [
            {"id": "agent2", "label": "Agent2", "runtime": "codex", "node": "server-agent2",
             "description": "stand-in agent account", "remote_port": definitions["agent2"]},
            {"id": "agent3", "label": "Agent3", "runtime": "codex", "node": "server-agent3",
             "remote_port": definitions["agent3"]},
        ],
    }), encoding="utf-8")
    return remote.AgentRegistry(local_node="user-mac", config_path=str(config),
                                transport_factory=transport)


class PrivateSocketForwardTests(unittest.TestCase):
    def test_private_socket_forward_retains_logical_http_port(self):
        tunnel = remote.SshTunnel(host="server", user="agent2", remote_port=8878,
                                  remote_socket="/home/agent2/.local/state/ux46/a.sock")
        argv = tunnel._argv(12345)
        self.assertIn("127.0.0.1:12345:/home/agent2/.local/state/ux46/a.sock", argv)
        self.assertEqual(tunnel.remote_port, 8878)
        self.assertEqual(argv[-1], "agent2@server")

    def test_socket_path_is_trusted_and_bounded(self):
        for path in ("relative.sock", "/tmp/../a.sock", "/tmp/a:123", "/tmp/a\n.sock", "/" + "a" * 101):
            with self.subTest(path=path), self.assertRaises(ValueError):
                remote.SshTunnel(host="server", remote_port=8878, remote_socket=path)


class ProxyHarness:
    """The local console, plus a second console standing in for a remote one."""

    def __init__(self):
        self.local = ConsoleHarness(public=False)
        self.remote = ConsoleHarness(public=False)
        self.local.start_runtime()
        self.remote.start_runtime()
        self.dead_port = closed_port()
        self.local.service.agents = registry_for(
            self.local, reachable=self.remote.port, dead=self.dead_port)

    def close(self):
        try:
            self.local.service.agents.close()
        finally:
            self.local.close()
            self.remote.close()

    # -- the browser -------------------------------------------------------
    def call(self, method, path, body=None, csrf=True):
        return self.local.call(method, path, body=body, csrf=csrf)

    def raw(self, method: str, path: str, *, payload: bytes | None = None,
            content_type: str = "", csrf: bool = True):
        """One request exactly as the page's fetch would frame it."""

        conn = HTTPConnection("127.0.0.1", self.local.port, timeout=30)
        head = {"Host": f"127.0.0.1:{self.local.port}"}
        if payload is not None:
            head["Content-Type"] = content_type
            head["Origin"] = f"http://127.0.0.1:{self.local.port}"
            if csrf:
                head["X-Atlas-CSRF"] = self.local.service.csrf_token
        conn.request(method, path, body=payload, headers=head)
        response = conn.getresponse()
        raw = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, raw

    def upload(self, prefix: str, name: str, data: bytes, mime: str = "text/plain"):
        boundary = "----atlas" + uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode())
        body.write(f"Content-Type: {mime}\r\n\r\n".encode())
        body.write(data)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        return self.raw("POST", f"{prefix}/api/room/{ROOM}/files",
                        payload=body.getvalue(),
                        content_type=f"multipart/form-data; boundary={boundary}")


class AgentListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_listing_is_identity_and_availability_only(self):
        status, payload = self.h.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["default"], "local")
        by_id = {agent["id"]: agent for agent in payload["agents"]}
        self.assertEqual(set(by_id), {"local", "agent2", "agent3"})
        self.assertEqual(by_id["local"]["kind"], "local")
        # Runtime and node are the agent's own identity, and are kept apart.
        self.assertEqual(by_id["agent2"]["runtime"], "codex")
        self.assertEqual(by_id["agent2"]["node"], "server-agent2")
        self.assertEqual(by_id["agent2"]["availability"]["state"], "available")
        self.assertEqual(by_id["agent3"]["availability"]["state"], "unavailable")
        # The transport is named as a kind; no address, account, key path or
        # token of any agent reaches the browser.
        self.assertEqual(by_id["agent2"]["transport"], "ssh-loopback")
        blob = json.dumps(payload)
        for secret in ("127.0.0.1", "identity_file", "ssh_host", "ssh_user",
                       "csrf", str(self.h.remote.port), str(self.h.dead_port)):
            self.assertNotIn(secret, blob)

    def test_listing_is_read_only(self):
        status, _payload = self.h.call("POST", "/api/agents", body={})
        self.assertEqual(status, 404)

    def test_unknown_agent_is_not_answered_locally(self):
        status, payload = self.h.call("GET", "/api/agents/nobody/api/workspace")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_agent")


class AllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_unlisted_operation_is_refused_before_any_connection(self):
        status, payload = self.h.call("POST", "/api/agents/agent2/api/test-thread", body={})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden_path")

    def test_arbitrary_path_is_refused(self):
        for path in ("/api/agents/agent2/etc/passwd",
                     "/api/agents/agent2/api/../api/bootstrap",
                     "/api/agents/agent2/api/room/fixture/console-work/../../files",
                     "/api/agents/agent2/api//bootstrap"):
            with self.subTest(path=path):
                status, payload = self.h.call("GET", path)
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "forbidden_path")

    def test_wrong_method_for_a_listed_operation_is_refused(self):
        status, payload = self.h.call("DELETE", "/api/agents/agent2/api/workspace", body={})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden_method")

    def test_the_allowlist_applies_to_the_local_agent_too(self):
        status, payload = self.h.call("POST", "/api/agents/local/api/test-thread", body={})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden_path")

    def test_local_agent_prefix_reaches_the_same_local_console(self):
        direct = self.h.call("GET", "/api/workspace")[1]
        scoped = self.h.call("GET", "/api/agents/local/api/workspace")[1]
        self.assertEqual(direct["projects"], scoped["projects"])


class AuthAndCsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_outer_csrf_is_required_for_a_proxied_mutation(self):
        status, payload = self.h.call(
            "PUT", f"/api/agents/agent2/api/room/{ROOM}/draft",
            body={"body": "x", "base_version": 0}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_csrf")

    def test_outer_origin_is_required_for_a_proxied_mutation(self):
        status, payload = self.h.local.call(
            "PUT", f"/api/agents/agent2/api/room/{ROOM}/draft",
            body={"body": "x", "base_version": 0},
            headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "bad_origin")

    def test_remote_csrf_is_obtained_internally_and_never_published(self):
        # The two consoles hold different tokens, and the browser only ever
        # sends the local one, yet the remote mutation is accepted.
        self.assertNotEqual(self.h.local.service.csrf_token,
                            self.h.remote.service.csrf_token)
        version = self.h.call("GET", f"/api/agents/agent2/api/room/{ROOM}/draft")[1]["version"]
        status, payload = self.h.call(
            "PUT", f"/api/agents/agent2/api/room/{ROOM}/draft",
            body={"body": "written through the proxy", "base_version": version})
        self.assertEqual(status, 200)
        self.assertEqual(payload["body"], "written through the proxy")
        # And the remote token is not handed back in any proxied answer.
        status, payload = self.h.call("GET", "/api/agents/agent2/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertNotIn("csrf", payload)

    def test_a_rotated_remote_token_recovers_without_a_reload(self):
        self.h.call("GET", "/api/agents/agent2/api/bootstrap")   # caches the token
        version = self.h.call("GET", f"/api/agents/agent2/api/room/{ROOM}/draft")[1]["version"]
        self.h.remote.service.csrf_token = "rotated-" + uuid.uuid4().hex
        status, payload = self.h.call(
            "PUT", f"/api/agents/agent2/api/room/{ROOM}/draft",
            body={"body": "after rotation", "base_version": version})
        self.assertEqual(status, 200)
        self.assertEqual(payload["body"], "after rotation")


class SwitchingTests(unittest.TestCase):
    """Two agents, the same room id, and no leakage in either direction."""

    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_identical_room_ids_keep_separate_state(self):
        local = self.h.call("PUT", f"/api/room/{ROOM}/draft",
                            body={"body": "local side", "base_version": 0})
        self.assertEqual(local[0], 200)
        far = self.h.call("PUT", f"/api/agents/agent2/api/room/{ROOM}/draft",
                          body={"body": "agent2 side", "base_version": 0})
        self.assertEqual(far[0], 200)
        self.assertEqual(self.h.call("GET", f"/api/room/{ROOM}/draft")[1]["body"], "local side")
        self.assertEqual(
            self.h.call("GET", f"/api/agents/agent2/api/room/{ROOM}/draft")[1]["body"],
            "agent2 side")
        # The two journals are genuinely distinct files, not one shared store.
        self.assertNotEqual(self.h.local.service.state_dir, self.h.remote.service.state_dir)

    def test_reading_a_remote_room_takes_no_native_writer(self):
        for path in (f"/api/agents/agent2/api/room/{ROOM}",
                     f"/api/agents/agent2/api/room/{ROOM}/history?limit=5",
                     "/api/agents/agent2/api/workspace",
                     "/api/agents/agent2/api/attention"):
            with self.subTest(path=path):
                self.assertEqual(self.h.call("GET", path)[0], 200)
        self.assertEqual(self.h.remote.service.workers.attached(), {})
        self.assertEqual(self.h.local.service.workers.attached(), {})

    def test_an_unavailable_agent_is_never_answered_by_another(self):
        status, payload = self.h.call("GET", "/api/agents/agent3/api/workspace")
        self.assertEqual(status, 503)
        self.assertIn(payload["error"], ("agent_unreachable", "tunnel_failed"))
        self.assertNotIn("projects", payload)
        listing = self.h.call("GET", "/api/agents")[1]
        agent3 = next(a for a in listing["agents"] if a["id"] == "agent3")
        self.assertEqual(agent3["availability"]["state"], "unavailable")


class WorkspaceSpeechTests(unittest.TestCase):
    def test_remote_message_uses_workspace_voice_without_forwarding_browser_text(self):
        h = ProxyHarness()
        captured = []
        class Speech:
            def as_json(self):
                return {"audio_url": "/api/audio/" + "a" * 48 + ".wav"}
        class Voice:
            def status(self):
                return {"enabled": True, "voices": ["af_heart"]}
            def speak(self, text, wanted):
                captured.append(text)
                return Speech()
        try:
            h.local.service.voice = Voice()
            boot = h.call("GET", "/api/agents/agent2/api/bootstrap")[1]
            self.assertTrue(boot["voice"]["enabled"])
            self.assertEqual(boot["voice_location"], "workspace")
            status, page = h.call("GET", f"/api/agents/agent2/api/room/{ROOM}/history?around=i4")
            item = next(i for i in page["items"] if i["id"] == "i4")
            status, reply = h.call("POST", f"/api/agents/agent2/api/room/{ROOM}/speak",
                                   body={"item_id": "i4", "text": "never trust these browser words"})
            self.assertEqual(status, 200)
            self.assertEqual(captured, [item["text"]])
            self.assertTrue(reply["audio_url"].startswith("/api/audio/"))
            self.assertEqual(h.remote.service.workers.attached(), {})
            bad, _ = h.call("POST", f"/api/agents/agent2/api/room/{ROOM}/speak",
                             body={"item_id": "absent", "text": "never speak this"})
            self.assertNotEqual(bad, 200)
            self.assertEqual(len(captured), 1)
        finally:
            h.close()


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_multipart_upload_and_download_keep_exact_bytes(self):
        data = bytes(range(256)) * 40
        status, _headers, raw = self.h.upload("/api/agents/agent2", "notes.bin", data,
                                              mime="application/octet-stream")
        self.assertEqual(status, 201, raw[:200])
        record = json.loads(raw)["file"]
        self.assertEqual(record["size"], len(data))
        # The remote minted its own URLs; the browser is given this origin's.
        self.assertEqual(record["download_url"],
                         f"/api/agents/agent2/api/atlas/files/{record['id']}/download")
        self.assertIsNone(record["preview_url"])   # not a raster: download only
        status, headers, body = self.h.raw("GET", record["download_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, data)
        self.assertIn("notes.bin", headers.get("Content-Disposition", ""))
        # The file landed on the agent that owns the room, not on this machine.
        self.assertTrue(self.h.remote.service.files.get(record["id"], room=ROOM))

    def test_a_thumbnail_url_points_at_the_owning_agent(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
               + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
               + b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
               + b"\x00\x00\x00\x00IEND\xaeB`\x82")
        status, _headers, raw = self.h.upload("/api/agents/agent2", "dot.png", png,
                                              mime="image/png")
        self.assertEqual(status, 201, raw[:200])
        record = json.loads(raw)["file"]
        self.assertEqual(record["preview_url"],
                         f"/api/agents/agent2/api/atlas/files/{record['id']}/preview")
        status, headers, body = self.h.raw("GET", record["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, png)
        self.assertEqual(headers.get("Content-Type"), "image/png")

    def test_local_uploads_are_untouched(self):
        data = b"local bytes"
        status, _headers, raw = self.h.upload("", "local.txt", data)
        self.assertEqual(status, 201, raw[:200])
        record = json.loads(raw)["file"]
        self.assertEqual(record["download_url"],
                         f"/api/atlas/files/{record['id']}/download")
        self.assertEqual(self.h.raw("GET", record["download_url"])[2], data)

    def test_only_whole_artifact_paths_are_rewritten(self):
        payload = json.dumps({
            "text": "see /api/atlas/files/abc/preview in the log",
            "download_url": "/api/atlas/files/abc/download",
            "nested": [{"audio_url": "/api/audio/" + "a" * 40 + ".wav"}],
            "elsewhere": "https://example.test/api/atlas/files/abc/preview",
        }).encode()
        out = json.loads(remote.rewrite_artifact_urls(payload, "agent2"))
        self.assertEqual(out["text"], "see /api/atlas/files/abc/preview in the log")
        self.assertEqual(out["download_url"], "/api/agents/agent2/api/atlas/files/abc/download")
        self.assertEqual(out["nested"][0]["audio_url"],
                         "/api/agents/agent2/api/audio/" + "a" * 40 + ".wav")
        self.assertEqual(out["elsewhere"], "https://example.test/api/atlas/files/abc/preview")


class RegressionTests(unittest.TestCase):
    """The routes this slice touched, driven without any agent prefix."""

    @classmethod
    def setUpClass(cls):
        cls.h = ProxyHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_bootstrap_and_rooms_are_unchanged(self):
        status, payload = self.h.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "local-owner")
        self.assertIn("csrf", payload)
        self.assertEqual(self.h.call("GET", f"/api/room/{ROOM}")[0], 200)
        self.assertEqual(self.h.call("GET", "/api/workspace")[0], 200)

    def test_oversize_upload_is_still_bounded_through_an_agent_prefix(self):
        # The limit is applied from Content-Length, before a byte is read, on
        # the agent-scoped path exactly as on the plain one.
        import atlas_console as console

        too_big = (self.h.local.service.files.max_upload_bytes
                   + console.MAX_UPLOAD_OVERHEAD + 1)
        for prefix in ("", "/api/agents/agent2"):
            with self.subTest(prefix=prefix or "local"):
                conn = HTTPConnection("127.0.0.1", self.h.local.port, timeout=20)
                conn.putrequest("POST", f"{prefix}/api/room/{ROOM}/files")
                conn.putheader("Host", f"127.0.0.1:{self.h.local.port}")
                conn.putheader("Origin", f"http://127.0.0.1:{self.h.local.port}")
                conn.putheader("X-Atlas-CSRF", self.h.local.service.csrf_token)
                conn.putheader("Content-Type", "multipart/form-data; boundary=x")
                conn.putheader("Content-Length", str(too_big))
                conn.endheaders()
                response = conn.getresponse()
                payload = json.loads(response.read() or b"{}")
                conn.close()
                self.assertEqual(response.status, 413)
                self.assertEqual(payload["error"], "too_large")

    def test_a_body_free_proxied_post_does_not_desync_the_connection(self):
        conn = HTTPConnection("127.0.0.1", self.h.local.port, timeout=20)
        head = {"Host": f"127.0.0.1:{self.h.local.port}",
                "Origin": f"http://127.0.0.1:{self.h.local.port}",
                "X-Atlas-CSRF": self.h.local.service.csrf_token,
                "Content-Type": "application/json", "Content-Length": "0"}
        conn.request("POST", "/api/agents/agent2/api/connection/refresh", body=b"", headers=head)
        first = conn.getresponse()
        first.read()
        conn.request("GET", "/api/agents/agent2/api/workspace",
                     headers={"Host": f"127.0.0.1:{self.h.local.port}"})
        second = conn.getresponse()
        body = second.read()
        conn.close()
        self.assertEqual(second.status, 200)
        self.assertIn("projects", json.loads(body))


class ClientScopingTests(unittest.TestCase):
    """The browser half of the collision guard, read off the shipped client.

    Every stored reference to a room is namespaced by agent and every API path
    goes through the one prefixing helper. These are cheap structural checks
    that keep a later edit from quietly reintroducing a shared key.
    """

    def test_every_room_scoped_storage_key_is_agent_scoped(self):
        for key in ('OUTBOX_KEY', 'UPLOAD_DRAFT_KEY',
                    '"atlas.room"', '"atlas.commandAttempt." + roomId'):
            with self.subTest(key=key):
                for match in re.finditer(re.escape(key), APP_JS):
                    line = APP_JS[APP_JS.rfind("\n", 0, match.start()) + 1:
                                  APP_JS.find("\n", match.end())]
                    if "localStorage" not in line:
                        continue
                    self.assertIn("agentKey(", line, line.strip())

    def test_the_device_identity_is_not_scoped_by_agent(self):
        line = next(ln for ln in APP_JS.splitlines() if 'setItem("atlas.device"' in ln)
        self.assertNotIn("agentKey", line)

    def test_the_scoping_helpers_behave_as_the_proxy_expects(self):
        """Run the client's own helpers and compare them with the server."""

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        start = APP_JS.index('const DEFAULT_AGENT = "local";')
        end = APP_JS.index("/* ---------------------------------------------------------------- helpers */")
        block = APP_JS[start:APP_JS.index("\n}\n", APP_JS.index("function agentLabel", start)) + 3]
        self.assertLess(start, end)
        script = (
            "const state = {agent: '', agents: [{id:'agent2', label:'Agent2'}]};\n"
            + block
            + "const out = [];\n"
            "for (const id of ['', 'agent2']) {\n"
            "  state.agent = id;\n"
            "  out.push([agentId(), apiUrl('/api/room/orbit/build/draft'),\n"
            "            agentKey('atlas.room'), isRemoteAgent()]);\n"
            "}\n"
            "console.log(JSON.stringify(out));\n"
        )
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        local, far = json.loads(result.stdout)
        # The local agent keeps the exact paths and keys it has always used.
        self.assertEqual(local, ["local", "/api/room/orbit/build/draft", "atlas.room", False])
        # A remote agent gets its own namespace on both sides.
        self.assertEqual(far, ["agent2", "/api/agents/agent2/api/room/orbit/build/draft",
                               "atlas.room@agent2", True])
        # And the path the client would build is one the proxy accepts.
        remote.check_allowed("PUT", far[1].split("/api/agents/agent2", 1)[1])
        self.assertNotEqual(local[2], far[2])

    def test_every_api_path_goes_through_the_prefixing_helper(self):
        # The CSRF retry helper takes a caller-scoped URL. Recovery now also
        # uses api(..., absolute:true); only bootstrap is a literal fetch.
        calls = re.findall(r"fetch\(([^,\n]+)", APP_JS)
        self.assertEqual(set(calls), {'url', '"/api/bootstrap"'})
        self.assertTrue('consoleFetch(opts.absolute ? path : apiUrl(path), opts)' in APP_JS)
        # And no literal "/api/..." string is handed to an element attribute.
        self.assertIn('return apiUrl("/api/atlas/files/"', APP_JS)


if __name__ == "__main__":
    os.environ.setdefault("ATLAS_FIXTURE_MODE", "normal")
    unittest.main()
