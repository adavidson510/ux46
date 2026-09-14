"""Focused checks for the UX46 Agent3 OpenClaw adapter.

Nothing here touches the real gateway, the real Agent3 agent or a real model.
Every test drives ``tools/atlas_rivet.py`` against a synthetic gateway that
speaks the same newline-JSON bridge protocol as
``tools/ux46_rivet_gateway.mjs``, records the exact method and params it was
called with, and can be told to fail, hang or push an event. The point of the
fake is that the *request and response forms this adapter maps to* are asserted
against something, not that the mapping is described in a comment.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from argparse import Namespace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_rivet as agent3  # noqa: E402

AGENT = "main"
MAIN_KEY = "agent:main:main"
DISCORD_KEY = "agent:main:discord:channel:1501661986818363512"
OTHER_AGENT_KEY = "agent:orbit-agent3:main"

# A synthetic gateway. It is deliberately dumb: it answers the eight methods
# the adapter is allowed to call, logs every call, and does exactly what the
# scenario file tells it to.
FAKE_GATEWAY = r'''
import json, sys, time

scenario = json.loads(open(sys.argv[1]).read())
log_path = sys.argv[2]
state_path = log_path + ".created"
try:
    created_sessions = json.loads(open(state_path).read())
except Exception:
    created_sessions = []

def log(entry):
    with open(log_path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")

def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

emit({"type": "ready", "ok": True,
      "gateway": {"url": "ws://127.0.0.1:18790", "auth_mode": "password",
                  "gateway_version": "2026.7.1-2", "scopes": ["operator.read"]}})

sent = {}
appended = []
creates = []

# The create log lives on disk so a restarted adapter can still be checked
# against every call the gateway ever received.

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, params, rid = req["method"], req.get("params") or {}, req.get("id")
    log({"method": method, "params": params})

    if method == "agent.identity.get":
        emit({"type": "response", "id": rid, "ok": True, "result": scenario["identity"]})
    elif method == "agents.list":
        emit({"type": "response", "id": rid, "ok": True, "result": scenario["agents"]})
    elif method == "sessions.list":
        emit({"type": "response", "id": rid, "ok": True,
              "result": {"sessions": list(scenario["sessions"]) + created_sessions,
                         "defaults": {}}})
    elif method in ("sessions.patch", "sessions.compact"):
        emit({"type": "response", "id": rid, "ok": True, "result": {"ok": True, "compacted": method == "sessions.compact"}})

    elif method == "sessions.create":
        creates.append(params.get("key"))
        behaviour = scenario.get("create_behaviour", "ok")
        if behaviour == "hang":
            continue                      # no answer, ever: an unknown outcome
        if behaviour == "error":
            emit({"type": "response", "id": rid, "ok": False,
                  "error": {"code": "invalid_request", "message": "no room for a new session"}})
        elif behaviour == "no_ok":
            emit({"type": "response", "id": rid, "ok": True, "result": {}})
        else:
            # `canonical` answers with a normalised key; `invisible` answers ok
            # but leaves the session out of the list, which is list lag.
            key = params.get("key")
            if behaviour == "canonical":
                key = str(key).lower()
            if behaviour != "invisible":
                created_sessions.append({
                    "key": key, "kind": "direct", "updatedAt": 1787999999999,
                    "sessionId": "new-" + str(len(created_sessions) + 1),
                    "status": "idle", "hasActiveRun": False,
                    "model": "gpt-5.6-sol", "modelProvider": "openai"})
            open(state_path, "w").write(json.dumps(created_sessions))
            emit({"type": "response", "id": rid, "ok": True,
                  "result": {"ok": True, "key": key,
                             "sessionId": "new-" + str(len(created_sessions)),
                             "entry": {"key": key}, "runStarted": False}})
    elif method == "chat.history":
        if scenario.get("history_error"):
            emit({"type": "response", "id": rid, "ok": False,
                  "error": {"code": "unavailable", "message": scenario["history_error"]}})
        else:
            page = dict(scenario["history"])
            page["messages"] = list(page["messages"]) + list(appended)
            page["totalMessages"] = len(page["messages"])
            emit({"type": "response", "id": rid, "ok": True, "result": page})
    elif method == "chat.send":
        behaviour = scenario.get("send_behaviour", "ok")
        if behaviour == "hang":
            continue                      # no answer, ever: an unknown outcome
        if behaviour == "error":
            emit({"type": "response", "id": rid, "ok": False,
                  "error": {"code": "session_busy", "message": "the gateway refused it"}})
        else:
            key = params.get("idempotencyKey")
            sent[key] = sent.get(key, 0) + 1
            ack = {"runId": key, "attemptId": "attempt-" + str(sent[key]),
                   "status": "accepted", "sessionKey": params.get("sessionKey"),
                   "turnKind": "main", "expiresAtMs": 1788813240508}
            if scenario.get("send_ack") == "empty":
                ack = {}
            elif scenario.get("send_ack") == "no_run_id":
                ack = {"status": "accepted"}
            elif scenario.get("send_ack") == "wrong_run":
                ack = {"runId": "somebody-elses-run", "status": "accepted"}
            elif scenario.get("send_ack") == "odd_status":
                ack = {"runId": key, "status": "queued-somewhere"}
            emit({"type": "response", "id": rid, "ok": True, "result": ack})
            for extra in scenario.get("append_after_send") or []:
                appended.append(extra)
            if scenario.get("emit_message_after_send"):
                emit({"type": "event", "event": "session.message",
                      "payload": {"sessionKey": params.get("sessionKey"),
                                  "messageId": "m-1", "messageSeq": 7,
                                  "hasActiveRun": True, "activeRunIds": [key]}})
    elif method == "chat.abort":
        emit({"type": "response", "id": rid, "ok": True,
              "result": {"aborted": True, "runIds": ["run-1"]}})
    elif method in ("sessions.messages.subscribe", "sessions.messages.unsubscribe"):
        emit({"type": "response", "id": rid, "ok": True,
              "result": {"subscribed": method.endswith("subscribe"),
                         "key": params.get("key")}})
    else:
        emit({"type": "response", "id": rid, "ok": False,
              "error": {"code": "method_not_allowed", "message": method}})

    if scenario.get("drop_transport_after") == method:
        emit({"type": "transport", "state": "disconnected", "code": 1006, "reason": "test"})
'''


def message(role: str, blocks: list, *, mid: str, seq: int,
            idempotency: str = "") -> dict:
    """One chat.history message in the shape the installed gateway returns."""

    return {
        "role": role,
        "content": blocks,
        "timestamp": 1787582736649,
        "__openclaw": {
            "id": mid,
            "mirrorIdentity": f"turn-1:{role}",
            "recordTimestampMs": 1787582736649,
            "seq": seq,
            **({"idempotencyKey": idempotency} if idempotency else {}),
        },
    }


DEFAULT_SCENARIO = {
    "identity": {"agentId": AGENT, "name": "Agent3", "emoji": "\U0001f6e0️"},
    # `main` is the default; `orbit-agent3` is a separate older profile that
    # this adapter must never address or drift onto.
    "agents": {"defaultId": AGENT, "mainKey": AGENT,
               "agents": [{"id": AGENT, "workspace": "/home/server/.openclaw/workspace"},
                          {"id": "orbit-agent3", "name": "orbit-agent3",
                           "workspace": "/home/server/.openclaw/workspace-orbit-agent3"}]},
    "sessions": [
        {"key": MAIN_KEY, "kind": "direct", "updatedAt": 1787582737119,
         "sessionId": "436d269b", "status": "done", "hasActiveRun": False,
         "model": "gpt-5.6-sol", "modelProvider": "openai", "thinkingLevel": "high"},
        {"key": DISCORD_KEY, "kind": "group", "updatedAt": 1787750730428,
         "sessionId": "410b8437", "status": "done", "hasActiveRun": False,
         "origin": {"label": "discord:channel:1501661986818363512",
                    "provider": "discord", "chatType": "channel"}},
        # Another agent on the same gateway. This adapter must never address it.
        {"key": OTHER_AGENT_KEY, "kind": "direct", "updatedAt": 1787750730429,
         "sessionId": "deadbeef"},
    ],
    "history": {
        "sessionKey": MAIN_KEY,
        "sessionId": "436d269b",
        "offset": 0,
        "totalMessages": 3,
        "messages": [
            message("user", [{"type": "text", "text": "status please"}],
                    mid="msg-1", seq=1, idempotency="ux46-agent3:client-aaaa1111"),
            message("assistant", [{"type": "toolCall", "id": "t1", "name": "bash",
                                   "arguments": "{}"}], mid="msg-2", seq=2),
            message("assistant", [{"type": "text", "text": "all green"}],
                    mid="msg-3", seq=3),
        ],
        "sessionInfo": {"key": MAIN_KEY, "hasActiveRun": False},
    },
}


def tiny_png() -> bytes:
    """A real one-pixel PNG, built here so the tests own the exact bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\x0a" + chunk(b"IHDR", header)
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


PNG_BYTES = tiny_png()
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


class Harness:
    """One adapter, one synthetic gateway, one loopback port."""

    def __init__(self, **scenario_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        scenario = json.loads(json.dumps(DEFAULT_SCENARIO))
        scenario.update(scenario_overrides)
        self.scenario_path = base / "scenario.json"
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        self.log_path = base / "calls.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        fake = base / "fake_gateway.py"
        fake.write_text(FAKE_GATEWAY, encoding="utf-8")

        # Two real Vault projects. Only ORBIT carries a verified openclaw
        # origin, and only for the main session key: everything else on this
        # gateway must land in the internal Unfiled namespace.
        orbit = base / "Projects" / "orbit" / "sessions"
        orbit.mkdir(parents=True)
        (orbit / "agent3-main.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "orbit", "session": "agent3-main",
            "origins": [{"runtime": "openclaw", "node": "server-agent3",
                         "session_key": MAIN_KEY, "captured": "2026-09-07",
                         "primary": True}],
        }), encoding="utf-8")
        other = base / "Projects" / "notes" / "sessions"
        other.mkdir(parents=True)
        (other / "unrelated.origins.json").write_text(json.dumps({
            "schema_version": 1, "project": "notes", "session": "unrelated",
            "origins": [{"runtime": "codex", "node": "user-mac",
                         "session_id": "01a00000-0000-7000-8000-000000000001"}],
        }), encoding="utf-8")

        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "server-agent3",
            "projects": [
                {"id": "orbit", "name": "ORBIT", "root": str(base / "Projects" / "orbit")},
                {"id": "notes", "name": "Notes", "root": str(base / "Projects" / "notes")},
            ],
        }), encoding="utf-8")

        self.config = Namespace(
            host="127.0.0.1", port=0, agent_id=AGENT, unfiled_project="unfiled",
            registry=str(registry), node="server-agent3",
            state_dir=str(base / "state"), transport="sidecar",
            sidecar=str(fake), node_bin=sys.executable, openclaw_bin="openclaw",
            gateway_url="", send_timeout=3.0, command_timeout=3.0,
            suggest=2, browser_origin="",
            identity_header="X-Forwarded-User", path_prefix="", quiet=True,
        )
        holder: dict = {}
        self._fake_command = [sys.executable, str(fake), str(self.scenario_path),
                              str(self.log_path)]
        self.transport = agent3.SidecarTransport(
            list(self._fake_command),
            on_event=lambda event, payload: holder["service"].on_gateway_event(event, payload),
        )
        self.transport.start()
        self.service = agent3.RivetService(self.config, self.transport)
        holder["service"] = self.service
        self.service.start()
        self.server = agent3.RivetServer(("127.0.0.1", 0), agent3.RivetHandler, self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.csrf = self.get("/api/bootstrap")[1]["csrf"]

    def restart(self) -> None:
        """Stop and rebuild the adapter over the same journal and fake gateway.

        Nothing about the state directory changes, so this is what a service
        restart really looks like to the durable command journal.
        """

        self.server.shutdown()
        self.server.server_close()
        self.service.stop()
        self.transport.stop()
        holder: dict = {}
        self.transport = agent3.SidecarTransport(
            list(self._fake_command),
            on_event=lambda event, payload: holder["service"].on_gateway_event(event, payload),
        )
        self.transport.start()
        self.service = agent3.RivetService(self.config, self.transport)
        holder["service"] = self.service
        self.service.start()
        self.server = agent3.RivetServer(("127.0.0.1", 0), agent3.RivetHandler, self.service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.csrf = self.get("/api/bootstrap")[1]["csrf"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.service.stop()
        self.transport.stop()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = json.dumps(body).encode() if body is not None else b""
        sent = {"Content-Length": str(len(payload))}
        if body is not None:
            sent["Content-Type"] = "application/json"
        if method != "GET" and getattr(self, "csrf", None):
            sent["X-Atlas-CSRF"] = self.csrf
        sent.update(headers or {})
        conn.request(method, path, payload, sent)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw or b"{}")
        except ValueError:
            return response.status, {"raw": raw.decode("utf-8", "replace")}

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None, **kwargs) -> tuple[int, dict]:
        return self.request("POST", path, body or {}, **kwargs)

    def upload(self, room: str, name: str, data: bytes,
               mime: str = "application/octet-stream") -> tuple[int, dict]:
        boundary = "----ux46test"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", f"/api/room/{room}/files", body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "X-Atlas-CSRF": self.csrf,
        })
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw or b"{}")

    def raw_get(self, path: str) -> tuple[int, bytes, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", path)
        response = conn.getresponse()
        raw = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, raw, headers

    def calls(self, method: str = "") -> list[dict]:
        entries = [json.loads(line) for line in
                   self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in entries if not method or e["method"] == method]


class RivetAdapterTest(unittest.TestCase):
    scenario: dict = {}

    def setUp(self) -> None:
        self.harness = Harness(**self.scenario)
        self.addCleanup(self.harness.close)

    @property
    def main_room(self) -> str:
        # Linked by a verified openclaw origin, so it keeps its real project
        # and its record's own session name.
        return "orbit/agent3-main"

    @property
    def unfiled_room(self) -> str:
        return "unfiled/discord-channel-1501661986818363512"


class BootstrapTest(RivetAdapterTest):
    def test_bootstrap_names_the_gateway_agent_and_the_transport(self) -> None:
        status, payload = self.harness.get("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(payload["csrf"])
        # The name is the gateway's own answer, not one this adapter chose.
        self.assertEqual(payload["agent"]["name"], "Agent3")
        self.assertEqual(payload["agent"]["id"], AGENT)
        self.assertEqual(payload["agent"]["identity_source"],
                         "gateway.agent.identity.get")
        self.assertEqual(payload["adapter"]["transport"], "sidecar")
        self.assertTrue(payload["adapter"]["connected"])
        self.assertTrue(payload["capabilities"]["stream"])
        self.assertEqual(self.harness.calls("agent.identity.get")[0]["params"], {})

    def test_bootstrap_states_every_unsupported_action(self) -> None:
        _status, payload = self.harness.get("/api/bootstrap")
        for action in ("goal", "effort", "approvals"):
            self.assertIn(action, payload["unsupported"])
            self.assertTrue(payload["unsupported"][action])
        self.assertFalse(payload["capabilities"]["approvals"])
        self.assertEqual(payload["capabilities"]["attach_scope"], "view_only")
        # These two are now implemented, and say so rather than being silent.
        self.assertTrue(payload["capabilities"]["attachments"])
        self.assertTrue(payload["capabilities"]["drafts"])
        self.assertNotIn("files", payload["unsupported"])
        self.assertNotIn("draft", payload["unsupported"])
        # /command is handled now, so it is no longer a blanket refusal; the
        # per-command refusals live under `commands.unsupported`.
        self.assertNotIn("command", payload["unsupported"])
        self.assertEqual(payload["commands"]["supported"], list(agent3.COMMANDS_SUPPORTED))

    def test_no_credential_reaches_the_adapter(self) -> None:
        _status, payload = self.harness.get("/api/bootstrap")

        def walk(node) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    # The bridge reports the auth *mode* so an operator can see
                    # whether the gateway wanted auth at all. It never reports
                    # the thing that satisfied it.
                    self.assertNotIn(key, ("token", "password", "secret", "credential"))
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(payload)
        self.assertEqual(payload["adapter"]["gateway"]["auth_mode"], "password")


class RoomTest(RivetAdapterTest):
    def test_rooms_come_only_from_sessions_list(self) -> None:
        status, payload = self.harness.get("/api/rooms")
        self.assertEqual(status, 200)
        keys = {room["session_key"] for room in payload["rooms"]}
        self.assertEqual(keys, {MAIN_KEY, DISCORD_KEY})
        # A session belonging to another agent on the same gateway is not a room.
        self.assertNotIn(OTHER_AGENT_KEY, keys)
        for room in payload["rooms"]:
            self.assertEqual(room["session_key_source"], "gateway.sessions.list")
            self.assertEqual(room["runtime"], "openclaw")
            self.assertEqual(room["capability"], agent3.CAPABILITY)

    def test_room_state_reports_the_exact_key_and_a_detached_view(self) -> None:
        status, payload = self.harness.get(f"/api/room/{self.main_room}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["session_key"], MAIN_KEY)
        self.assertEqual(payload["thread_id"], MAIN_KEY)
        self.assertEqual(payload["ownership"]["state"], "idle")
        self.assertEqual(payload["ownership"]["adapter_state"], "detached")
        self.assertEqual(payload["ownership"]["scope"], "ux46_view")
        self.assertTrue(payload["record_linked"])

    def test_an_unfiled_room_invents_no_lifecycle(self) -> None:
        _status, payload = self.harness.get(f"/api/room/{self.unfiled_room}")
        # No Vault record claims this session, so no lifecycle is invented.
        self.assertEqual(payload["status"], "unrecorded")
        self.assertFalse(payload["record_linked"])

    def test_an_unknown_room_is_refused_not_invented(self) -> None:
        status, payload = self.harness.get("/api/room/orbit/not-a-session")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "room_unknown")

    def test_workspace_projects_the_gateway_sessions(self) -> None:
        status, payload = self.harness.get("/api/workspace")
        self.assertEqual(status, 200)
        project = payload["projects"][0]
        self.assertEqual(project["id"], "orbit")
        self.assertEqual(project["record_total"], 1)
        self.assertEqual(len(project["suggested"]), 1)
        self.assertTrue(project["suggested"][0]["reason_label"])


class HistoryTest(RivetAdapterTest):
    def test_history_request_and_item_mapping(self) -> None:
        status, payload = self.harness.get(
            f"/api/room/{self.main_room}/history?limit=40&direction=asc")
        self.assertEqual(status, 200)
        params = self.harness.calls("chat.history")[0]["params"]
        # The request form: the exact key, the agent id, a bounded limit.
        self.assertEqual(params["sessionKey"], MAIN_KEY)
        self.assertEqual(params["agentId"], AGENT)
        self.assertEqual(params["limit"], 40)
        self.assertNotIn("offset", params)

        types = [item["type"] for item in payload["items"]]
        self.assertEqual(types, ["userMessage", "mcpToolCall", "agentMessage"])
        first, tool, last = payload["items"]
        self.assertEqual(first["id"], "msg-1")
        self.assertEqual(first["text"], "status please")
        # An id this adapter minted comes back as its own client id.
        self.assertEqual(first["client_id"], "client-aaaa1111")
        self.assertEqual(first["turn_id"], "turn-1")
        self.assertEqual(first["seq"], 1)
        self.assertEqual(tool["tool"], "bash")
        self.assertEqual(last["text"], "all green")
        self.assertEqual(payload["session_key"], MAIN_KEY)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["source"], "chat.history")

    def test_a_foreign_idempotency_key_is_not_claimed(self) -> None:
        _status, payload = self.harness.get(f"/api/room/{self.main_room}/history")
        self.assertNotIn("client_id", payload["items"][1])

    def test_descending_api_renders_question_before_answer(self) -> None:
        _status, payload = self.harness.get(
            f"/api/room/{self.main_room}/history?direction=desc")
        self.assertEqual([item["type"] for item in reversed(payload["items"])],
                         ["userMessage", "mcpToolCall", "agentMessage"])

    def test_history_cursor_becomes_an_offset(self) -> None:
        self.harness.get(f"/api/room/{self.main_room}/history?cursor=40")
        self.assertEqual(self.harness.calls("chat.history")[0]["params"]["offset"], 40)

    def test_a_bad_cursor_is_refused(self) -> None:
        status, payload = self.harness.get(
            f"/api/room/{self.main_room}/history?cursor=nonsense")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_cursor")

    def test_search_maps_hits_to_stable_ids(self) -> None:
        status, payload = self.harness.get(
            f"/api/room/{self.main_room}/search?q=green")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["hits"][0]["item_id"], "msg-3")
        self.assertEqual(payload["hits"][0]["kind"], "final")
        self.assertTrue(payload["complete"])


class UnreadableHistoryTest(RivetAdapterTest):
    scenario = {"history_error": "the session store is locked"}

    def test_an_unreadable_history_is_never_shown_as_an_empty_one(self) -> None:
        status, payload = self.harness.get(f"/api/room/{self.main_room}/history")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        self.assertTrue(payload["unavailable"])
        self.assertTrue(payload["retryable"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["source"], "unavailable")
        self.assertIn("locked", payload["message"])

    def test_an_unreadable_history_makes_search_say_so(self) -> None:
        _status, payload = self.harness.get(f"/api/room/{self.main_room}/search?q=x")
        self.assertEqual(payload["source"], "unavailable")
        self.assertEqual(payload["hits"], [])
        self.assertTrue(payload["retryable"])


class AttachReleaseTest(RivetAdapterTest):
    def test_attach_subscribes_and_release_only_detaches_the_view(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.main_room}/continue")
        self.assertEqual(status, 200)
        self.assertEqual(payload["scope"], "ux46_view")
        self.assertTrue(payload["attached"]["streaming"])
        subscribe = self.harness.calls("sessions.messages.subscribe")
        self.assertEqual(subscribe[0]["params"], {"key": MAIN_KEY, "agentId": AGENT})

        status, payload = self.harness.post(f"/api/room/{self.main_room}/release")
        self.assertEqual(status, 200)
        self.assertEqual(payload["release_state"], "detached")
        self.assertEqual(payload["released"], MAIN_KEY)
        self.assertTrue(payload["gateway_untouched"])
        self.assertTrue(payload["native_work_continues"])
        self.assertEqual(len(self.harness.calls("sessions.messages.unsubscribe")), 1)
        # Release is not a stop: no run is aborted on the way out.
        self.assertEqual(self.harness.calls("chat.abort"), [])

    def test_releasing_a_view_that_was_never_attached_changes_nothing(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.main_room}/release")
        self.assertEqual(status, 200)
        self.assertEqual(payload["release_state"], "not_attached")
        self.assertEqual(self.harness.calls("sessions.messages.unsubscribe"), [])

    def test_stop_is_the_only_thing_that_aborts_a_run(self) -> None:
        status, payload = self.harness.post(f"/api/room/{self.main_room}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(self.harness.calls("chat.abort")[0]["params"],
                         {"sessionKey": MAIN_KEY, "agentId": AGENT})
        self.assertEqual(payload["run_ids"], ["run-1"])


class SubmitTest(RivetAdapterTest):
    scenario = {"emit_message_after_send": True}

    def test_a_send_is_accepted_once_and_never_repeated(self) -> None:
        body = {"client_id": "client-aaaa1111", "body": "ping"}
        status, payload = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["submission"]["status"], agent3.ACCEPTED)
        self.assertTrue(payload["dispatched"])
        params = self.harness.calls("chat.send")[0]["params"]
        self.assertEqual(params["sessionKey"], MAIN_KEY)
        self.assertEqual(params["agentId"], AGENT)
        self.assertEqual(params["message"], "ping")
        # The gateway's own dedupe key, derived from the client id and stable.
        self.assertEqual(params["idempotencyKey"], "ux46-agent3:client-aaaa1111")
        self.assertEqual(payload["submission"]["run_id"], "ux46-agent3:client-aaaa1111")

        status, repeat = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(status, 200)
        self.assertTrue(repeat["duplicate"])
        self.assertFalse(repeat["dispatched"])
        self.assertEqual(repeat["submission"]["status"], agent3.ACCEPTED)
        # The retry called the gateway exactly zero more times.
        self.assertEqual(len(self.harness.calls("chat.send")), 1)

    def test_reusing_a_client_id_for_different_text_is_a_conflict(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-aaaa1111", "body": "ping"})
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "something else"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")
        self.assertEqual(len(self.harness.calls("chat.send")), 1)

    def test_a_send_aimed_at_another_session_is_refused(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-bbbb2222", "body": "ping", "thread_id": DISCORD_KEY})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "wrong_target")
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_an_unknown_file_id_is_refused_before_anything_is_sent(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-cccc3333", "body": "here",
             "attachments": [{"file_id": "never-uploaded"}]})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "file_unknown")
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_a_stream_push_becomes_one_addressed_adapter_event(self) -> None:
        before = self.harness.get("/api/bootstrap")[1]["seq"]
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-dddd4444", "body": "ping"})
        deadline = time.time() + 5
        kinds: list = []
        while time.time() < deadline:
            _status, payload = self.harness.get(f"/api/events?after={before}&timeout=1")
            kinds = [event for event in payload["events"]
                     if event["type"] == "native"]
            if kinds:
                break
        self.assertTrue(kinds, "the gateway's session.message never reached /api/events")
        self.assertEqual(kinds[0]["room"], self.main_room)
        self.assertEqual(kinds[0]["session_key"], MAIN_KEY)
        # Normalised to the shared type the console polls, with the gateway's
        # own event name kept and no turn lifecycle invented.
        self.assertEqual(kinds[0]["method"], "session.message")
        self.assertEqual(kinds[0]["gateway_event"], "session.message")
        self.assertNotIn(kinds[0]["method"], ("turn/started", "turn/completed"))
        self.assertTrue(kinds[0]["lifecycle"])

    def test_a_submission_can_be_recovered_by_its_client_id(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-eeee5555", "body": "ping"})
        status, payload = self.harness.get("/api/submissions/client-eeee5555")
        self.assertEqual(status, 200)
        self.assertEqual(payload["submission"]["session_key"], MAIN_KEY)
        status, payload = self.harness.get("/api/submissions/client-ffff6666")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_submission")


class RefusedSendTest(RivetAdapterTest):
    scenario = {"send_behaviour": "error"}

    def test_a_refused_send_is_never_reported_accepted(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "ping"})
        self.assertEqual(status, 502)
        self.assertEqual(payload["submission"]["status"], agent3.FAILED)
        self.assertFalse(payload["dispatched"])
        self.assertEqual(payload["error_code"], "session_busy")

    def test_a_refused_send_is_not_retried_by_a_repeat_of_the_same_id(self) -> None:
        body = {"client_id": "client-aaaa1111", "body": "ping"}
        self.harness.post(f"/api/room/{self.main_room}/submit", body)
        status, payload = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["duplicate"])
        self.assertEqual(payload["submission"]["status"], agent3.FAILED)
        self.assertEqual(len(self.harness.calls("chat.send")), 1)


class UncertainSendTest(RivetAdapterTest):
    scenario = {"send_behaviour": "hang"}

    def test_an_unanswered_send_is_uncertain_and_is_never_resent(self) -> None:
        body = {"client_id": "client-aaaa1111", "body": "ping"}
        status, payload = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(status, 202)
        self.assertTrue(payload["uncertain"])
        self.assertEqual(payload["submission"]["status"], agent3.UNCERTAIN)
        self.assertIn("will not resend", payload["submission"]["meaning"])

        status, repeat = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["submission"]["status"], agent3.UNCERTAIN)
        self.assertEqual(len(self.harness.calls("chat.send")), 1)

    def test_a_view_with_an_unsettled_send_is_not_detached_silently(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/continue")
        # A send that is still in flight leaves a dispatching row behind.
        thread = threading.Thread(
            target=self.harness.post,
            args=(f"/api/room/{self.main_room}/submit",
                  {"client_id": "client-bbbb2222", "body": "ping"}),
            daemon=True)
        thread.start()
        time.sleep(0.5)
        status, payload = self.harness.post(f"/api/room/{self.main_room}/release")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "send_unsettled")
        thread.join(timeout=20)


class QueueTest(RivetAdapterTest):
    def test_a_queued_message_waits_until_the_view_is_attached(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/pending",
            {"client_id": "client-aaaa1111", "body": "later"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["pending"]["status"], agent3.PENDING)
        self.assertIn("attach", payload["pending"]["queued_reason"])
        time.sleep(0.5)
        self.assertEqual(self.harness.calls("chat.send"), [])

        self.harness.post(f"/api/room/{self.main_room}/continue")
        deadline = time.time() + 10
        while time.time() < deadline and not self.harness.calls("chat.send"):
            time.sleep(0.2)
        sends = self.harness.calls("chat.send")
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["params"]["idempotencyKey"],
                         "ux46-agent3:client-aaaa1111")

    def test_a_queued_message_can_be_cancelled_before_it_is_sent(self) -> None:
        _status, payload = self.harness.post(
            f"/api/room/{self.main_room}/pending",
            {"client_id": "client-bbbb2222", "body": "never mind"})
        version = payload["pending"]["version"]
        status, cancelled = self.harness.request(
            "DELETE", f"/api/room/{self.main_room}/pending/client-bbbb2222",
            {"version": version})
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["pending"]["status"], agent3.CANCELLED)
        self.harness.post(f"/api/room/{self.main_room}/continue")
        time.sleep(0.8)
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_a_stale_cancel_is_refused(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/pending",
                          {"client_id": "client-cccc3333", "body": "hold"})
        status, payload = self.harness.request(
            "DELETE", f"/api/room/{self.main_room}/pending/client-cccc3333",
            {"version": 99})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "queue_stale")


class BoundaryTest(RivetAdapterTest):
    def test_unsupported_actions_are_explicit_and_never_forwarded_as_text(self) -> None:
        for action in ("goal", "effort", "reasoning", "refresh"):
            status, payload = self.harness.post(f"/api/room/{self.main_room}/{action}",
                                                {"command": "/model gpt-5"})
            self.assertEqual(status, 400, action)
            self.assertEqual(payload["error"], "unsupported", action)
            self.assertTrue(payload["message"], action)
        status, payload = self.harness.post("/api/approvals/answer", {"decision": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unsupported")
        # An unsupported *slash command* is refused by name at the command
        # endpoint rather than by a blanket rejection of the endpoint itself.
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/command",
            {"client_id": "client-aaaa1111", "command": "/goal",
             "thread_id": MAIN_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["state"], "unsupported")
        self.assertFalse(payload["command"]["sent_as_text"])
        # Not one of those refusals turned into a message for Agent3.
        self.assertEqual(self.harness.calls("chat.send"), [])
        self.assertEqual(self.harness.calls("sessions.create"), [])

    def test_the_transport_refuses_a_method_outside_the_allowlist(self) -> None:
        with self.assertRaises(agent3.GatewayError) as caught:
            self.harness.transport.call("sessions.delete", {"key": MAIN_KEY})
        self.assertEqual(caught.exception.code, "method_not_allowed")
        self.assertEqual(self.harness.calls("sessions.delete"), [])

    def test_a_mutation_without_the_csrf_token_is_refused(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.harness.port, timeout=10)
        conn.request("POST", f"/api/room/{self.main_room}/continue", b"{}",
                     {"Content-Length": "2"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"], "bad_csrf")

    def test_a_proxy_path_prefix_is_stripped(self) -> None:
        self.harness.service.config.path_prefix = "/api/agents/agent3"
        status, payload = self.harness.get("/api/agents/agent3/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(payload["projects"][0]["id"], "orbit")

    def test_attention_and_approvals_claim_nothing(self) -> None:
        _status, attention = self.harness.get("/api/attention")
        self.assertEqual(attention["needs"], [])
        self.assertTrue(attention["note"])
        _status, approvals = self.harness.get("/api/approvals")
        self.assertFalse(approvals["supported"])


class ReconnectTest(RivetAdapterTest):
    scenario = {"drop_transport_after": "chat.abort"}

    def test_a_dropped_stream_is_reported_rather_than_hidden(self) -> None:
        before = self.harness.get("/api/bootstrap")[1]["seq"]
        self.harness.post(f"/api/room/{self.main_room}/continue")
        self.harness.post(f"/api/room/{self.main_room}/stop")
        deadline = time.time() + 5
        dropped: list = []
        while time.time() < deadline:
            _status, payload = self.harness.get(f"/api/events?after={before}&timeout=1")
            dropped = [e for e in payload["events"] if e["type"] == "connection"]
            if dropped:
                break
        self.assertTrue(dropped, "the transport drop never reached /api/events")
        self.assertEqual(dropped[0]["state"], "disconnected")
        self.assertIn("behind", dropped[0]["detail"])

    def test_a_reattach_after_a_drop_subscribes_again(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/continue")
        self.harness.post(f"/api/room/{self.main_room}/stop")
        time.sleep(0.5)
        self.harness.post(f"/api/room/{self.main_room}/continue")
        # The first subscribe was cached; after the drop the adapter asks again
        # instead of assuming the gateway still has it.
        self.assertEqual(len(self.harness.calls("sessions.messages.subscribe")), 2)


class ProjectFilingTest(RivetAdapterTest):
    """Item 1: a conversation is filed where it belongs, or not at all."""

    def test_a_linked_session_keeps_its_real_project_and_record_name(self) -> None:
        _status, payload = self.harness.get(f"/api/room/{self.main_room}")
        self.assertEqual(payload["project_id"], "orbit")
        self.assertEqual(payload["project_name"], "ORBIT")
        # The room takes the record's own session name, not a derived slug.
        self.assertEqual(payload["session"], "agent3-main")
        self.assertEqual(payload["session_key"], MAIN_KEY)
        self.assertEqual(payload["project_source"], "vault_origin")
        self.assertTrue(payload["vault_project"])
        self.assertFalse(payload["unfiled"])

    def test_an_unlinked_session_is_unfiled_not_filed_under_orbit(self) -> None:
        _status, rooms = self.harness.get("/api/rooms")
        by_key = {room["session_key"]: room for room in rooms["rooms"]}
        unlinked = by_key[DISCORD_KEY]
        self.assertEqual(unlinked["project_id"], "unfiled")
        self.assertTrue(unlinked["unfiled"])
        self.assertFalse(unlinked["vault_project"])
        self.assertEqual(unlinked["project_source"], "unfiled")
        # And nothing was invented on disk for it.
        _status, workspace = self.harness.get("/api/workspace")
        unfiled = [p for p in workspace["projects"] if p["id"] == "unfiled"][0]
        self.assertEqual(unfiled["root"], "")
        base = Path(self.harness.tmp.name) / "Projects"
        self.assertEqual(sorted(child.name for child in base.iterdir()),
                         ["notes", "orbit"])

    def test_workspace_groups_by_real_project_with_unfiled_last(self) -> None:
        _status, payload = self.harness.get("/api/workspace")
        ids = [project["id"] for project in payload["projects"]]
        self.assertEqual(ids, ["orbit", "unfiled"])
        orbit, unfiled = payload["projects"]
        self.assertTrue(orbit["vault_project"])
        self.assertEqual(orbit["record_total"], 1)
        self.assertTrue(unfiled["unfiled"])
        self.assertEqual(unfiled["record_total"], 1)
        self.assertIn("no project was created", unfiled["note"])
        # A registry project with no gateway conversation is not listed as a
        # room holder just because it exists.
        self.assertNotIn("notes", ids)

    def test_projects_endpoint_agrees_with_the_workspace(self) -> None:
        _status, payload = self.harness.get("/api/projects")
        rows = {row["id"]: row for row in payload["projects"]}
        self.assertEqual(rows["orbit"]["sessions"], 1)
        self.assertEqual(rows["unfiled"]["sessions"], 1)
        self.assertTrue(rows["unfiled"]["unfiled"])

    def test_the_selected_main_identity_is_preserved_and_reported(self) -> None:
        _status, payload = self.harness.get("/api/bootstrap")
        self.assertEqual(payload["agent"]["id"], AGENT)
        self.assertEqual(payload["agent"]["gateway_default_id"], AGENT)
        self.assertTrue(payload["agent"]["is_gateway_default"])
        # The older separate profile is named, never addressed.
        self.assertIn("orbit-agent3", payload["agent"]["other_agent_ids"])
        _status, rooms = self.harness.get("/api/rooms")
        self.assertNotIn(OTHER_AGENT_KEY,
                         {room["session_key"] for room in rooms["rooms"]})

    def test_an_unfiled_name_that_collides_with_a_real_project_is_refused(self) -> None:
        config = self.harness.config
        config.unfiled_project = "orbit"
        with self.assertRaises(SystemExit) as caught:
            agent3.RivetService(config, self.harness.transport)
        self.assertIn("already has a project called", str(caught.exception))


class DraftTest(RivetAdapterTest):
    """Item 2: cross-device drafts, deterministic and local."""

    def test_a_draft_starts_empty_and_saves_under_compare_and_set(self) -> None:
        status, payload = self.harness.get(f"/api/room/{self.main_room}/draft")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"room": self.main_room, "body": "", "version": 0,
                                   "updated_at": 0.0, "device": ""})
        status, saved = self.harness.request(
            "PUT", f"/api/room/{self.main_room}/draft",
            {"body": "half a thought", "base_version": 0, "device": "phone"})
        self.assertEqual(status, 200)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["device"], "phone")
        _status, read_back = self.harness.get(f"/api/room/{self.main_room}/draft")
        self.assertEqual(read_back["body"], "half a thought")
        self.assertEqual(read_back["version"], 1)

    def test_two_devices_collide_and_neither_silently_wins(self) -> None:
        self.harness.request("PUT", f"/api/room/{self.main_room}/draft",
                             {"body": "from the phone", "base_version": 0,
                              "device": "phone"})
        # The laptop still holds version 0 and must not clobber the phone.
        status, payload = self.harness.request(
            "PUT", f"/api/room/{self.main_room}/draft",
            {"body": "from the laptop", "base_version": 0, "device": "laptop"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "draft_conflict")
        self.assertEqual(payload["detail"]["body"], "from the phone")
        self.assertEqual(payload["detail"]["version"], 1)
        self.assertEqual(payload["detail"]["device"], "phone")
        # Merging from the version it was told about does go through.
        status, merged = self.harness.request(
            "PUT", f"/api/room/{self.main_room}/draft",
            {"body": "from the phone + laptop", "base_version": 1, "device": "laptop"})
        self.assertEqual(status, 200)
        self.assertEqual(merged["version"], 2)

    def test_drafts_are_isolated_per_room(self) -> None:
        self.harness.request("PUT", f"/api/room/{self.main_room}/draft",
                             {"body": "orbit text", "base_version": 0})
        self.harness.request("PUT", f"/api/room/{self.harness_unfiled}/draft",
                             {"body": "unfiled text", "base_version": 0})
        _status, first = self.harness.get(f"/api/room/{self.main_room}/draft")
        _status, second = self.harness.get(f"/api/room/{self.harness_unfiled}/draft")
        self.assertEqual(first["body"], "orbit text")
        self.assertEqual(second["body"], "unfiled text")
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 1)

    def test_a_draft_never_reaches_the_gateway(self) -> None:
        self.harness.request("PUT", f"/api/room/{self.main_room}/draft",
                             {"body": "not sent", "base_version": 0})
        self.assertEqual(self.harness.calls("chat.send"), [])
        _status, payload = self.harness.get(f"/api/room/{self.main_room}")
        self.assertEqual(payload["draft"]["body"], "not sent")

    @property
    def harness_unfiled(self) -> str:
        return self.unfiled_room


class UploadTest(RivetAdapterTest):
    """Item 3: managed uploads that survive as genuine gateway attachments."""

    def test_an_image_upload_is_stored_previewable_and_downloadable(self) -> None:
        status, payload = self.harness.upload(
            self.main_room, "pixel.png", PNG_BYTES, "image/png")
        self.assertEqual(status, 201)
        record = payload["file"]
        self.assertEqual(record["mime"], "image/png")
        self.assertEqual(record["size"], len(PNG_BYTES))
        self.assertTrue(record["sendable"])
        # A raster gets a preview url, which is what the console renders as a
        # thumbnail on the composer and on the sent message.
        self.assertEqual(record["preview_url"],
                         f"/api/atlas/files/{record['id']}/preview")

        status, body, headers = self.harness.raw_get(record["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))

        status, body, headers = self.harness.raw_get(record["download_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

    def test_an_image_is_sent_as_a_real_gateway_attachment_byte_for_byte(self) -> None:
        _status, payload = self.harness.upload(
            self.main_room, "pixel.png", PNG_BYTES, "image/png")
        file_id = payload["file"]["id"]
        status, sent = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "look",
             "attachments": [{"file_id": file_id}]})
        self.assertEqual(status, 200)
        params = self.harness.calls("chat.send")[0]["params"]
        attachments = params["attachments"]
        self.assertEqual(len(attachments), 1)
        # The documented wire shape of the installed build, and the exact bytes.
        self.assertEqual(set(attachments[0]), {"mimeType", "fileName", "content"})
        self.assertEqual(attachments[0]["mimeType"], "image/png")
        self.assertEqual(attachments[0]["fileName"], "pixel.png")
        self.assertEqual(base64.b64decode(attachments[0]["content"]), PNG_BYTES)
        # The exact managed id is journaled against the accepted run.
        self.assertEqual(sent["submission"]["attachments"], [{"file_id": file_id}])

    def test_a_generic_file_is_sent_too_because_the_gateway_accepts_one(self) -> None:
        _status, payload = self.harness.upload(
            self.main_room, "notes.pdf", PDF_BYTES, "application/pdf")
        self.assertTrue(payload["file"]["sendable"])
        # A non-raster has no preview; it is still downloadable.
        self.assertIsNone(payload["file"]["preview_url"])
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-bbbb2222", "body": "here it is",
                           "attachments": [{"file_id": payload["file"]["id"]}]})
        attachment = self.harness.calls("chat.send")[0]["params"]["attachments"][0]
        self.assertEqual(attachment["fileName"], "notes.pdf")
        self.assertEqual(base64.b64decode(attachment["content"]), PDF_BYTES)

    def test_an_oversize_image_is_refused_and_the_bytes_are_kept(self) -> None:
        big = PNG_BYTES + b"\x00" * (agent3.GATEWAY_IMAGE_MAX_BYTES + 1 - len(PNG_BYTES))
        status, payload = self.harness.upload(
            self.main_room, "huge.png", big, "image/png")
        self.assertEqual(status, 201)
        # The refusal is stated at upload time, not discovered at send time.
        self.assertFalse(payload["file"]["sendable"])
        self.assertIn("stays stored and downloadable", payload["file"]["send_note"])
        file_id = payload["file"]["id"]
        status, refusal = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-cccc3333", "body": "too big",
             "attachments": [{"file_id": file_id}]})
        self.assertEqual(status, 413)
        self.assertEqual(refusal["error"], "attachment_too_large")
        self.assertEqual(self.harness.calls("chat.send"), [])
        # Nothing was dropped: the file is still there, byte for byte.
        status, body, _headers = self.harness.raw_get(
            f"/api/atlas/files/{file_id}/download")
        self.assertEqual(status, 200)
        self.assertEqual(body, big)

    def test_a_file_id_survives_queueing_editing_and_delivery(self) -> None:
        _status, first = self.harness.upload(
            self.main_room, "one.png", PNG_BYTES, "image/png")
        _status, second = self.harness.upload(
            self.main_room, "two.pdf", PDF_BYTES, "application/pdf")
        one, two = first["file"]["id"], second["file"]["id"]
        _status, queued = self.harness.post(
            f"/api/room/{self.main_room}/pending",
            {"client_id": "client-dddd4444", "body": "draft",
             "attachments": [{"file_id": one}]})
        self.assertEqual(queued["pending"]["attachments"], [{"file_id": one}])

        status, edited = self.harness.request(
            "PATCH", f"/api/room/{self.main_room}/pending/client-dddd4444",
            {"version": queued["pending"]["version"], "body": "edited",
             "attachments": [{"file_id": one}, {"file_id": two}]})
        self.assertEqual(status, 200)
        self.assertEqual(edited["pending"]["attachments"],
                         [{"file_id": one}, {"file_id": two}])

        self.harness.post(f"/api/room/{self.main_room}/continue")
        deadline = time.time() + 10
        while time.time() < deadline and not self.harness.calls("chat.send"):
            time.sleep(0.2)
        params = self.harness.calls("chat.send")[0]["params"]
        self.assertEqual(params["message"], "edited")
        self.assertEqual([a["fileName"] for a in params["attachments"]],
                         ["one.png", "two.pdf"])
        self.assertEqual(base64.b64decode(params["attachments"][0]["content"]), PNG_BYTES)
        self.assertEqual(base64.b64decode(params["attachments"][1]["content"]), PDF_BYTES)

    def test_a_sent_message_shows_its_attachments_back_in_history(self) -> None:
        _status, payload = self.harness.upload(
            self.main_room, "pixel.png", PNG_BYTES, "image/png")
        file_id = payload["file"]["id"]
        # The default history fixture already carries this exact client id, so
        # the association is made through the idempotency key and nothing else.
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-aaaa1111", "body": "look",
                           "attachments": [{"file_id": file_id}]})
        _status, history = self.harness.get(f"/api/room/{self.main_room}/history?direction=asc")
        sent = history["items"][0]
        self.assertEqual(sent["client_id"], "client-aaaa1111")
        self.assertEqual(sent["attachments_source"], "adapter_journal")
        self.assertEqual(sent["attachments"][0]["id"], file_id)
        self.assertEqual(sent["attachments"][0]["preview_url"],
                         f"/api/atlas/files/{file_id}/preview")
        # A message from any other client is untouched.
        self.assertNotIn("attachments", history["items"][2])

    def test_reusing_a_client_id_with_a_different_file_set_is_a_conflict(self) -> None:
        _status, one = self.harness.upload(
            self.main_room, "one.png", PNG_BYTES, "image/png")
        _status, two = self.harness.upload(
            self.main_room, "two.pdf", PDF_BYTES, "application/pdf")
        body = {"client_id": "client-eeee5555", "body": "same words",
                "attachments": [{"file_id": one["file"]["id"]}]}
        self.harness.post(f"/api/room/{self.main_room}/submit", body)
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {**body, "attachments": [{"file_id": two["file"]["id"]}]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")
        self.assertEqual(len(self.harness.calls("chat.send")), 1)

    def test_attachments_that_cannot_fit_one_frame_are_refused_together(self) -> None:
        ids = []
        chunk = b"\x00" * (5 * 1024 * 1024)
        for index in range(4):
            _status, payload = self.harness.upload(
                self.main_room, f"blob{index}.bin", chunk, "application/octet-stream")
            ids.append({"file_id": payload["file"]["id"]})
        status, refusal = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-gggg7777", "body": "all of them", "attachments": ids})
        self.assertEqual(status, 413)
        self.assertEqual(refusal["error"], "attachments_too_large")
        self.assertIn("stays stored here", refusal["message"])
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_a_file_from_another_room_is_not_reachable(self) -> None:
        _status, payload = self.harness.upload(
            self.main_room, "pixel.png", PNG_BYTES, "image/png")
        status, refusal = self.harness.post(
            f"/api/room/{self.unfiled_room}/submit",
            {"client_id": "client-ffff6666", "body": "borrowed",
             "attachments": [{"file_id": payload["file"]["id"]}]})
        self.assertEqual(status, 404)
        self.assertEqual(refusal["error"], "file_unknown")


class AcknowledgementTest(RivetAdapterTest):
    """Item 4: only a documented run acknowledgement counts as accepted."""

    def test_a_documented_ack_is_recorded_with_its_run_and_attempt(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "ping"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["submission"]["status"], agent3.ACCEPTED)
        self.assertEqual(payload["submission"]["ack_status"], "accepted")
        self.assertEqual(payload["submission"]["attempt_id"], "attempt-1")
        self.assertEqual(payload["native"]["run_id"], "ux46-agent3:client-aaaa1111")

    def test_every_send_carries_deliver_false(self) -> None:
        self.harness.post(f"/api/room/{self.main_room}/submit",
                          {"client_id": "client-bbbb2222", "body": "ping"})
        params = self.harness.calls("chat.send")[0]["params"]
        self.assertIs(params["deliver"], False)
        # No originating route is ever supplied, so nothing can be republished
        # into the session's own Discord or other channel.
        for field in ("originatingChannel", "originatingTo",
                      "originatingAccountId", "originatingThreadId"):
            self.assertNotIn(field, params)

    def test_the_delivery_boundary_is_reported_to_the_browser(self) -> None:
        _status, payload = self.harness.get("/api/bootstrap")
        self.assertIs(payload["delivery"]["deliver"], False)
        self.assertEqual(payload["delivery"]["reply_channel"], "webchat")
        self.assertFalse(payload["delivery"]["publishes_to_origin_channel"])

    def test_a_discord_origin_session_is_still_sent_without_a_route(self) -> None:
        self.harness.post(f"/api/room/{self.unfiled_room}/submit",
                          {"client_id": "client-cccc3333", "body": "ping"})
        params = self.harness.calls("chat.send")[0]["params"]
        self.assertEqual(params["sessionKey"], DISCORD_KEY)
        self.assertIs(params["deliver"], False)
        self.assertNotIn("originatingChannel", params)


class MalformedAckTest(RivetAdapterTest):
    scenario = {"send_ack": "empty"}

    def test_an_empty_answer_is_uncertain_not_accepted(self) -> None:
        body = {"client_id": "client-aaaa1111", "body": "ping"}
        status, payload = self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(status, 202)
        self.assertTrue(payload["uncertain"])
        self.assertEqual(payload["reason"], "no_run_acknowledgement")
        self.assertEqual(payload["submission"]["status"], agent3.UNCERTAIN)
        # And it is not retried on a repeat of the same id.
        self.harness.post(f"/api/room/{self.main_room}/submit", body)
        self.assertEqual(len(self.harness.calls("chat.send")), 1)


class MissingRunIdAckTest(RivetAdapterTest):
    scenario = {"send_ack": "no_run_id"}

    def test_an_ack_without_a_run_id_is_uncertain(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "ping"})
        self.assertEqual(status, 202)
        self.assertEqual(payload["submission"]["status"], agent3.UNCERTAIN)


class WrongRunAckTest(RivetAdapterTest):
    scenario = {"send_ack": "wrong_run"}

    def test_an_ack_for_somebody_elses_run_is_uncertain(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "ping"})
        self.assertEqual(status, 202)
        self.assertEqual(payload["submission"]["status"], agent3.UNCERTAIN)
        self.assertEqual(payload["submission"]["run_id"], "")


class OddStatusAckTest(RivetAdapterTest):
    scenario = {"send_ack": "odd_status"}

    def test_an_unknown_status_is_uncertain(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/submit",
            {"client_id": "client-aaaa1111", "body": "ping"})
        self.assertEqual(status, 202)
        self.assertEqual(payload["submission"]["status"], agent3.UNCERTAIN)
        self.assertIn("without an accepted run", payload["submission"]["detail"])


class NewCommandTest(RivetAdapterTest):
    """`/new` really starts a fresh gateway conversation, exactly once."""

    def command(self, room: str, text: str, client_id: str,
                thread_id: str | None = None) -> tuple[int, dict]:
        body = {"client_id": client_id, "command": text,
                "thread_id": MAIN_KEY if thread_id is None else thread_id}
        return self.harness.post(f"/api/room/{room}/command", body)

    def created_keys(self) -> list:
        return [call["params"].get("key")
                for call in self.harness.calls("sessions.create")]

    def test_new_starts_a_separate_conversation_and_sends_nothing(self) -> None:
        status, payload = self.command(self.main_room, "/new", "client-aaaa1111")
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "completed")
        self.assertEqual(command["message"], "Started a new Agent3 conversation.")

        # A brand new key under the configured agent, not the source's.
        created = self.created_keys()
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].startswith(f"agent:{AGENT}:"))
        self.assertNotEqual(created[0], MAIN_KEY)
        self.assertEqual(command["new_room"]["session_key"], created[0])
        self.assertNotEqual(command["new_room"]["id"], self.main_room)
        self.assertEqual(payload["room"]["id"], command["new_room"]["id"])

        # Nothing was said to the model, and no run was started.
        self.assertEqual(self.harness.calls("chat.send"), [])
        self.assertFalse(command["run_started"])
        params = self.harness.calls("sessions.create")[0]["params"]
        self.assertEqual(set(params), {"key", "agentId", "label"})
        self.assertEqual(params["label"], "New Agent3 conversation")
        self.assertEqual(params["agentId"], AGENT)

        # The new view is attached, so the composer works straight away.
        self.assertTrue(command["attached"])
        self.assertEqual(payload["room"]["ownership"]["state"], "atlas_owned")
        self.assertEqual(
            self.harness.calls("sessions.messages.subscribe")[-1]["params"]["key"],
            created[0])

    def test_the_source_conversation_is_left_exactly_as_it_was(self) -> None:
        _status, upload = self.harness.upload(
            self.main_room, "pixel.png", PNG_BYTES, "image/png")
        self.harness.request("PUT", f"/api/room/{self.main_room}/draft",
                             {"body": "kept text", "base_version": 0})
        _status, before = self.harness.get(f"/api/room/{self.main_room}/history")

        _status, payload = self.command(self.main_room, "/new", "client-aaaa1111")
        self.assertEqual(payload["command"]["source_room"], self.main_room)
        self.assertTrue(payload["command"]["source_intact"])
        self.assertFalse(payload["command"]["source_released"])
        # Nothing detached the source, so no unsubscribe was ever sent for it.
        self.assertEqual(
            [call["params"]["key"]
             for call in self.harness.calls("sessions.messages.unsubscribe")], [])

        _status, after = self.harness.get(f"/api/room/{self.main_room}/history")
        self.assertEqual([item["id"] for item in after["items"]],
                         [item["id"] for item in before["items"]])
        _status, draft = self.harness.get(f"/api/room/{self.main_room}/draft")
        self.assertEqual(draft["body"], "kept text")
        status, body, _headers = self.harness.raw_get(upload["file"]["download_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_BYTES)

    def test_a_repeat_of_the_same_command_id_opens_the_same_conversation(self) -> None:
        _status, first = self.command(self.main_room, "/new", "client-aaaa1111")
        status, second = self.command(self.main_room, "/new", "client-aaaa1111")
        self.assertEqual(status, 200)
        self.assertEqual(second["command"]["state"], "completed")
        self.assertEqual(second["command"]["new_room"]["id"],
                         first["command"]["new_room"]["id"])
        self.assertTrue(second["command"]["recovered"])
        self.assertIn("already started", second["command"]["message"])
        # One conversation, one create call.
        self.assertEqual(len(self.created_keys()), 1)

    def test_two_concurrent_identical_commands_create_once(self) -> None:
        results: list = []
        lock = threading.Lock()

        def run() -> None:
            outcome = self.command(self.main_room, "/new", "client-bbbb2222")
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 3)
        states = {payload["command"]["state"] for _status, payload in results}
        self.assertEqual(states, {"completed"})
        rooms = {payload["command"]["new_room"]["id"] for _status, payload in results}
        self.assertEqual(len(rooms), 1)
        self.assertEqual(len(self.created_keys()), 1)

    def test_the_same_command_id_survives_a_restart_without_creating_twice(self) -> None:
        _status, first = self.command(self.main_room, "/new", "client-cccc3333")
        room_id = first["command"]["new_room"]["id"]
        self.harness.restart()
        status, again = self.command(self.main_room, "/new", "client-cccc3333")
        self.assertEqual(status, 200)
        self.assertEqual(again["command"]["state"], "completed")
        self.assertEqual(again["command"]["new_room"]["id"], room_id)
        self.assertEqual(len(self.created_keys()), 1)

    def test_reusing_a_command_id_for_a_different_source_is_a_conflict(self) -> None:
        self.command(self.main_room, "/new", "client-dddd4444")
        status, payload = self.command(self.unfiled_room, "/new", "client-dddd4444",
                                       thread_id=DISCORD_KEY)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_mismatch")
        self.assertEqual(len(self.created_keys()), 1)

    def test_a_command_for_another_conversation_is_refused_before_anything_runs(self) -> None:
        status, payload = self.command(self.main_room, "/new", "client-eeee5555",
                                       thread_id=DISCORD_KEY)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "wrong_target")
        self.assertEqual(self.created_keys(), [])
        self.assertEqual(self.harness.calls("chat.send"), [])
        # Nothing was written down for that id, so a corrected retry is free.
        self.assertIsNone(self.harness.service.journal.command_record("client-eeee5555"))

    def test_an_unknown_command_is_refused_by_name_and_never_sent(self) -> None:
        for text in ("/goal", "/steer left", "/nonsense"):
            status, payload = self.command(self.main_room, text, "client-ffff6666")
            self.assertEqual(status, 200, text)
            command = payload["command"]
            self.assertEqual(command["state"], "unsupported", text)
            self.assertFalse(command["sent_as_text"], text)
            self.assertIn("not sent as a message", command["message"], text)
            self.assertEqual(command["supported"], list(agent3.COMMANDS_SUPPORTED))
        self.assertEqual(self.harness.calls("chat.send"), [])
        self.assertEqual(self.created_keys(), [])

    def test_new_with_title_retains_project_and_sends_no_prompt(self) -> None:
        status, payload = self.command(self.main_room, "/new look at this", "client-aaaa1111")
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["state"], "completed")
        new = payload["command"]["new_room"]
        self.assertEqual(new["id"].split("/")[0], self.main_room.split("/")[0])
        self.assertEqual(self.harness.calls("sessions.create")[0]["params"]["label"], "look at this")
        self.assertEqual(self.harness.calls("chat.send"), [])
        status, _ = self.command(self.main_room, "/new different title", "client-aaaa1111")
        self.assertEqual(status, 409)

    def test_native_settings_and_compaction_are_targeted_and_not_replayed(self):
        for i, text in enumerate(("/model test-model", "/reasoning high", "/compact")):
            client = "client-settings-" + str(i)
            status, payload = self.command(self.main_room, text, client)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["command"]["state"], "completed", payload)
            self.command(self.main_room, text, client)
        patches = self.harness.calls("sessions.patch")
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[0]["params"], {"key": MAIN_KEY, "agentId": AGENT, "model": "test-model"})
        self.assertEqual(patches[1]["params"]["thinkingLevel"], "high")
        self.assertEqual(len(self.harness.calls("sessions.compact")), 1)
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_help_status_refresh_do_not_send_or_create(self) -> None:
        for text in ("/help", "/status", "/refresh"):
            status, payload = self.command(self.main_room, text, "client-stats1111")
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["command"]["state"], "completed", payload)
        self.assertEqual(self.created_keys(), [])
        self.assertEqual(self.harness.calls("chat.send"), [])

    def test_the_supported_command_is_advertised_truthfully(self) -> None:
        _status, boot = self.harness.get("/api/bootstrap")
        self.assertEqual(boot["commands"]["supported"], list(agent3.COMMANDS_SUPPORTED))
        self.assertEqual(boot["capabilities"]["commands_supported"], list(agent3.COMMANDS_SUPPORTED))
        self.assertTrue(boot["capabilities"]["commands"])
        self.assertNotIn("command", boot["unsupported"])
        self.assertTrue(boot["commands"]["unsupported"]["/goal"])
        _status, room = self.harness.get(f"/api/room/{self.main_room}")
        self.assertEqual(set(entry["name"] for entry in room["commands"]), set(agent3.COMMANDS_SUPPORTED))


class NewCommandRefusedTest(RivetAdapterTest):
    scenario = {"create_behaviour": "error"}

    def test_a_refused_create_fails_safely_and_leaves_the_source_alone(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/command",
            {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY})
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "failed")
        self.assertIn("no room for a new session", command["message"])
        self.assertTrue(command["source_intact"])
        self.assertNotIn("new_room", command)
        # The source is still readable and still this view's room.
        _status, room = self.harness.get(f"/api/room/{self.main_room}")
        self.assertEqual(room["session_key"], MAIN_KEY)

    def test_a_retry_after_a_refusal_reports_the_same_refusal(self) -> None:
        body = {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY}
        self.harness.post(f"/api/room/{self.main_room}/command", body)
        _status, again = self.harness.post(f"/api/room/{self.main_room}/command", body)
        self.assertEqual(again["command"]["state"], "failed")
        self.assertEqual(len(self.harness.calls("sessions.create")), 1)


class NewCommandTimeoutTest(RivetAdapterTest):
    scenario = {"create_behaviour": "hang"}

    def test_an_unanswered_create_is_uncertain_and_is_never_reissued(self) -> None:
        body = {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY}
        status, payload = self.harness.post(f"/api/room/{self.main_room}/command", body)
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "uncertain")
        self.assertTrue(command["retry_is_safe"])
        self.assertIn("no second conversation was asked for", command["message"])
        self.assertTrue(command["source_intact"])
        # The destination key was written down before the call, so a retry
        # reconciles that one key instead of asking for another conversation.
        record = self.harness.service.journal.command_record("client-aaaa1111")
        self.assertTrue(record["destination_key"].startswith(f"agent:{AGENT}:"))
        self.assertEqual(record["status"], agent3.UNCERTAIN)

        _status, again = self.harness.post(f"/api/room/{self.main_room}/command", body)
        self.assertEqual(again["command"]["state"], "uncertain")
        self.assertEqual(again["command"]["session_key"], record["destination_key"])
        self.assertEqual(len(self.harness.calls("sessions.create")), 1)

    def test_a_restart_after_a_timeout_still_never_reissues(self) -> None:
        body = {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY}
        self.harness.post(f"/api/room/{self.main_room}/command", body)
        self.harness.restart()
        _status, again = self.harness.post(f"/api/room/{self.main_room}/command", body)
        self.assertEqual(again["command"]["state"], "uncertain")
        self.assertEqual(len(self.harness.calls("sessions.create")), 1)


class NewCommandLagTest(RivetAdapterTest):
    scenario = {"create_behaviour": "invisible"}

    def test_a_create_the_list_does_not_show_is_not_claimed_as_success(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/command",
            {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY})
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "uncertain")
        self.assertIn("not listed yet", command["message"])
        self.assertFalse(command["list_unavailable"])
        self.assertNotIn("new_room", command)
        self.assertEqual(len(self.harness.calls("sessions.create")), 1)


class NewCommandNoAckTest(RivetAdapterTest):
    scenario = {"create_behaviour": "no_ok"}

    def test_a_create_without_an_ok_acknowledgement_is_uncertain(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/command",
            {"client_id": "client-aaaa1111", "command": "/new", "thread_id": MAIN_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["state"], "uncertain")
        self.assertEqual(len(self.harness.calls("sessions.create")), 1)


class NewCommandCanonicalKeyTest(RivetAdapterTest):
    scenario = {"create_behaviour": "canonical"}

    def test_the_key_the_gateway_reports_is_the_one_that_is_used(self) -> None:
        status, payload = self.harness.post(
            f"/api/room/{self.main_room}/command",
            {"client_id": "client-AAAA1111", "command": "/new", "thread_id": MAIN_KEY})
        self.assertEqual(status, 200)
        command = payload["command"]
        self.assertEqual(command["state"], "completed")
        requested = self.harness.calls("sessions.create")[0]["params"]["key"]
        # The fake normalises the key; the adapter follows the gateway's answer
        # rather than insisting on what it asked for.
        self.assertEqual(command["new_room"]["session_key"], requested.lower())
        record = self.harness.service.journal.command_record("client-AAAA1111")
        self.assertEqual(record["created_key"], requested.lower())
        self.assertEqual(record["destination_key"], requested)


# ---------------------------------------------------------------------------
# One end-to-end browser journey against the real console front end
# ---------------------------------------------------------------------------

CONSOLE_DIR = Path("/Users/example/Projects/project-atlas/app/console")
PLAYWRIGHT = Path("/Users/example/Projects/demo-platform/tools/atlas-prototype-qa-1"
                  "/node_modules/playwright-core/index.mjs")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ConsoleFront(ThreadingHTTPServer):
    """Test-only origin: the real console assets, `/api` forwarded to the adapter.

    This stands in for the deployment's front proxy. It adds no behaviour: it
    serves the untouched files from app/console and relays every API call
    verbatim, so the browser exercises the adapter's own responses.
    """

    daemon_threads = True
    allow_reuse_address = True


class FrontHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    api_port = 0

    def log_message(self, *args) -> None:
        return

    def _relay(self, method: str) -> None:
        path = urlparse(self.path).path
        if method == "GET" and path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            body = (CONSOLE_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {}
        for key in ("Content-Type", "X-Atlas-CSRF", "Accept"):
            if self.headers.get(key):
                headers[key] = self.headers[key]
        conn = HTTPConnection("127.0.0.1", self.api_port, timeout=60)
        conn.request(method, self.path, body, headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        self.send_response(response.status)
        self.send_header("Content-Type",
                         response.getheader("Content-Type") or "application/json")
        for name in ("Content-Disposition",):
            if response.getheader(name):
                self.send_header(name, response.getheader(name))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._relay("GET")

    def do_POST(self):  # noqa: N802
        self._relay("POST")

    def do_PUT(self):  # noqa: N802
        self._relay("PUT")

    def do_PATCH(self):  # noqa: N802
        self._relay("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._relay("DELETE")


DRIVER = r"""
import { chromium } from "PLAYWRIGHT_PATH";
import fs from "node:fs";

const url = process.argv[2];
const profile = process.argv[3];
const steps = [];
const note = (name, extra) => steps.push({ step: name, ...(extra || {}) });

const context = await chromium.launchPersistentContext(profile, {
  executablePath: "CHROME_PATH",
  headless: true,
  args: ["--no-first-run", "--no-default-browser-check"],
});
const page = context.pages()[0] ?? (await context.newPage());
const errors = [];
page.on("pageerror", (err) => errors.push(String(err && err.message)));

try {
  await page.goto(url, { waitUntil: "domcontentloaded" });

  // -- read -------------------------------------------------------------
  await page.waitForSelector("#roomSearch", { timeout: 20000 });
  await page.fill("#roomSearch", "agent3-main");
  await page.waitForSelector("button.roomrow", { timeout: 20000 });
  note("room_listed", { label: await page.textContent("button.roomrow span.rn") });

  await page.click("button.roomrow");
  // `state` is a top-level const in the classic script, so it is reachable
  // here by name; nothing is added to the page to observe it.
  await page.waitForFunction(() => (state.items || []).length > 0, null,
                             { timeout: 20000 });
  note("history_read", {
    items: await page.evaluate(() => state.items.length),
    ownership: await page.evaluate(() => (state.detail?.ownership || {}).state || ""),
    thread_id: await page.evaluate(() => (state.detail?.native || {}).thread_id || ""),
    controllable: await page.evaluate(() => Boolean(state.detail?.controllable)),
    send_disabled: await page.isDisabled("#btnSend"),
    continue_visible: await page.isVisible("#btnContinue"),
    stream_text: (await page.textContent("#stream")).slice(0, 200),
  });

  // -- attach -----------------------------------------------------------
  await page.click("#btnContinue");
  await page.waitForFunction(
    () => (state.detail?.ownership || {}).state === "atlas_owned", null,
    { timeout: 20000 });
  note("attached", {
    ownership: await page.evaluate(() => state.detail.ownership.state),
    scope: await page.evaluate(() => state.detail.ownership.scope || ""),
    exclusive: await page.evaluate(() => state.detail.ownership.exclusive),
    send_disabled: await page.isDisabled("#btnSend"),
    send_state: await page.textContent("#sendState"),
  });

  // -- send -------------------------------------------------------------
  await page.fill("#draft", "status please");
  await page.click("#btnSend");
  await page.waitForFunction(
    () => (document.querySelector("#sendState")?.textContent || "").includes("accepted"),
    null, { timeout: 30000 });
  note("sent", {
    send_state: await page.textContent("#sendState"),
    accepted_turn: await page.evaluate(() => (state.accepted || {}).turn || ""),
  });

  // -- the gateway push refreshes the transcript ------------------------
  await page.waitForFunction(
    () => (state.items || []).some(
      (item) => item.type === "agentMessage" && (item.text || "").includes("PUSHED_TEXT")),
    null, { timeout: 40000 });
  note("transcript_refreshed", {
    items: await page.evaluate(() => state.items.length),
    text: await page.evaluate(() => state.items
      .filter((i) => i.type === "agentMessage").map((i) => i.text).join(" | ")),
    on_screen: (await page.textContent("#stream")).includes("PUSHED_TEXT"),
    active_turn: await page.evaluate(() => (state.detail?.native || {}).active_turn || ""),
    ownership: await page.evaluate(() => (state.detail?.ownership || {}).state || ""),
  });
} catch (error) {
  note("failed", { error: String(error && error.message).slice(0, 400) });
} finally {
  note("page_errors", { errors });
  await context.close();
}
fs.writeFileSync(process.argv[4], JSON.stringify(steps, null, 1));
"""

PUSHED_TEXT = "all green from the gateway"


@unittest.skipUnless(
    CONSOLE_DIR.joinpath("app.js").is_file() and PLAYWRIGHT.is_file() and CHROME.is_file(),
    "the console assets, playwright-core or Google Chrome are not on this machine")
class BrowserJourneyTest(unittest.TestCase):
    """One journey through the real console front end against a fake gateway.

    Read, attach, composer enabled, send, accepted, gateway push, transcript
    refreshed. Nothing here talks to a real gateway or a real agent, and no
    prompt is ever generated: the "reply" is a canned message the fake appends
    to its own history when it is asked to send.
    """

    def setUp(self) -> None:
        self.harness = Harness(
            emit_message_after_send=True,
            append_after_send=[message("assistant",
                                       [{"type": "text", "text": PUSHED_TEXT}],
                                       mid="msg-pushed", seq=4)],
        )
        self.addCleanup(self.harness.close)
        handler = type("BoundFront", (FrontHandler,), {"api_port": self.harness.port})
        self.front = ConsoleFront(("127.0.0.1", 0), handler)
        self.addCleanup(self.front.server_close)
        self.addCleanup(self.front.shutdown)
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        self.front_port = self.front.server_address[1]

    def test_read_attach_send_and_receive_in_a_real_browser(self) -> None:
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        base = Path(work.name)
        profile = base / "chrome-profile"      # fresh, isolated, thrown away
        profile.mkdir()
        script = base / "journey.mjs"
        script.write_text(
            DRIVER.replace("PLAYWRIGHT_PATH", PLAYWRIGHT.as_uri())
                  .replace("CHROME_PATH", str(CHROME))
                  .replace("PUSHED_TEXT", PUSHED_TEXT),
            encoding="utf-8")
        out = base / "steps.json"
        proc = subprocess.run(
            ["node", str(script), f"http://127.0.0.1:{self.front_port}/",
             str(profile), str(out)],
            capture_output=True, text=True, timeout=240)
        self.assertTrue(out.is_file(),
                        f"the browser driver produced nothing:\n{proc.stderr[-2000:]}")
        steps = {entry["step"]: entry for entry in json.loads(out.read_text())}
        self.assertNotIn("failed", steps, json.dumps(steps, indent=1))
        self.assertEqual(steps["page_errors"]["errors"], [])

        # read: the linked room is listed under its real project and its
        # history came back from the gateway.
        self.assertEqual(steps["room_listed"]["label"], "agent3-main")
        read = steps["history_read"]
        self.assertGreaterEqual(read["items"], 3)
        # Before attaching, the console must see an idle session so that it
        # offers Continue here and keeps Send disabled.
        self.assertEqual(read["ownership"], "idle")
        self.assertTrue(read["continue_visible"])
        self.assertTrue(read["send_disabled"])
        # The console addresses the conversation by native.thread_id.
        self.assertEqual(read["thread_id"], MAIN_KEY)

        # attach: Continue here really enables the composer.
        attached = steps["attached"]
        self.assertEqual(attached["ownership"], "atlas_owned")
        self.assertFalse(attached["send_disabled"])
        # ...without claiming exclusive ownership of Agent3's gateway.
        self.assertEqual(attached["scope"], "ux46_view")
        self.assertIs(attached["exclusive"], False)

        # send: it goes through the adapter and the native ack is accepted.
        params = self.harness.calls("chat.send")[0]["params"]
        self.assertEqual(params["sessionKey"], MAIN_KEY)
        self.assertEqual(params["message"], "status please")
        self.assertIs(params["deliver"], False)
        self.assertIn("accepted", steps["sent"]["send_state"])

        # push: the gateway's session.message reached the browser and the
        # transcript really refreshed.
        refreshed = steps["transcript_refreshed"]
        self.assertGreater(refreshed["items"], read["items"])
        self.assertIn(PUSHED_TEXT, refreshed["text"])
        self.assertTrue(refreshed["on_screen"], "the new message never rendered")
        # The run id the gateway acknowledged is the same one it then reported
        # as in flight; nothing about the turn was invented on the way.
        self.assertEqual(refreshed["active_turn"], params["idempotencyKey"])
        self.assertEqual(refreshed["ownership"], "atlas_owned")


class SlugTest(unittest.TestCase):
    def test_a_slug_is_room_safe_and_never_a_session_key(self) -> None:
        slug = agent3.session_slug(DISCORD_KEY, AGENT)
        self.assertEqual(slug, "discord-channel-1501661986818363512")
        self.assertTrue(agent3.ROOM_ID_RE.match(f"orbit/{slug}"))
        self.assertEqual(agent3.session_slug("agent:main:main", AGENT), "main")
        self.assertTrue(agent3.ROOM_ID_RE.match(
            "orbit/" + agent3.session_slug("agent:main:::weird::", AGENT)))


if __name__ == "__main__":
    unittest.main()
