"""Focused checks for the UX46 access gate.

Everything here runs against two local fixture upstreams that record exactly
what reached them. No console, no Tell service, no native runtime and no
network beyond loopback is involved, and no real password is used or stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import ux46_access_gateway as gate  # noqa: E402

# The gate stores an scrypt hash and nothing else, so it needs a Python built
# against OpenSSL. Apple's system Python is not one, so on that interpreter
# these skip rather than pretend; run them with the one the console runs under.
needs_scrypt = unittest.skipUnless(hasattr(hashlib, "scrypt"),
                                   "this Python has no hashlib.scrypt")

ORIGIN = "https://pane.example.ts.net:8443"
HOST = "pane.example.ts.net:8443"
USER = "user@example.com"
PASSWORD = "Fixture-Password9!"
BINARY = bytes(range(256)) * 64          # 16 KiB that no text pass survives


def basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# a fixture upstream that says what it was asked
# ---------------------------------------------------------------------------

class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _record(self, body: bytes) -> None:
        self.server.seen.append({
            "name": self.server.name,
            "method": self.command,
            "path": self.path,
            "headers": {k.casefold(): v for k, v in self.headers.items()},
            "body": body,
        })

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        path = self.path.split("?", 1)[0]
        if path == "/api/bootstrap":
            payload = json.dumps({'csrf':'fixture-csrf','upstream':self.server.name,'path':self.path}).encode()
        elif path == "/slow":
            time.sleep(1.0)
            payload = b'{"slow":true}'
        elif path == "/echo":
            payload = body
        elif path == "/binary" or "/api/audio/" in path:
            payload = BINARY
        elif path == "/provider401":
            payload = b'{"error":"the provider wants its own login"}'
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SomeProvider"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return
        elif path == "/nolength":
            # A response framed by closing, the way a stream can be.
            self.send_response_only(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"streamed")
            self.close_connection = True
            return
        else:
            payload = json.dumps({"upstream": self.server.name, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav" if "/api/audio/" in path else
                         "application/octet-stream" if path in ("/binary", "/echo") else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _answer


def start_upstream(name: str):
    server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    server.daemon_threads = True
    server.name = name
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# the gate under test
# ---------------------------------------------------------------------------

@needs_scrypt
class GateCase(unittest.TestCase):
    public_brand = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory(prefix="ux46-gate-")
        root = Path(cls.tmp.name)
        cls.password_file = root / "secrets" / "password.json"
        gate.write_password(cls.password_file, "user", PASSWORD)
        cls.brand_dir = root / "brand"
        cls.brand_dir.mkdir()
        (cls.brand_dir / "ux46-icon-1.svg").write_bytes(b"<svg id='mark'/>")
        (cls.brand_dir / "ux46-manifest-1.webmanifest").write_bytes(b'{"name":"UX46"}')
        cls.secret_source = root / "not-brand.txt"
        cls.secret_source.write_bytes(b"this must never be served")

        cls.console = start_upstream("console")
        cls.tell = start_upstream("tell")
        cls.gate_state = gate.Gate(gate.load_password(cls.password_file), "user")
        auth = gate.AuthBoundary(port=0, public_origin=ORIGIN, public_user=USER)
        cls.server = gate.GatewayServer(
            ("127.0.0.1", 0), cls.gate_state, auth,
            gate.Upstream(f"http://127.0.0.1:{cls.console.server_address[1]}"),
            gate.Upstream(f"http://127.0.0.1:{cls.tell.server_address[1]}"),
            cls.brand_dir, cls.public_brand)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.console, cls.tell):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.console.seen.clear()
        self.tell.seen.clear()

    # -- one request, with whatever headers the case is about ---------------
    def ask(self, method="GET", path="/", password=PASSWORD, identity=USER,
            host=HOST, body=None, headers=None, user="user", raw=None, cookie=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=30)
        sending = {"Host": host}
        if identity is not None:
            sending["Tailscale-User-Login"] = identity
        if password is not None:
            sending["Authorization"] = basic(user, password)
        if cookie is not None:
            sending["Cookie"] = cookie
        sending.update(headers or {})
        try:
            if raw is not None:
                connection.putrequest(method, path, skip_host=True,
                                      skip_accept_encoding=True)
                for name, value in list(sending.items()) + list(raw):
                    connection.putheader(name, value)
                connection.endheaders()
            else:
                connection.request(method, path, body=body, headers=sending)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


def set_cookie(headers: dict) -> str:
    return headers.get("Set-Cookie", "")


def cookie_value(headers: dict) -> str:
    return set_cookie(headers).split(";", 1)[0]


class Login(GateCase):
    def test_answering_the_password_hands_out_one_bounded_login(self):
        status, headers, _ = self.ask(path="/api/bootstrap")
        self.assertEqual(status, 200)
        issued = set_cookie(headers)
        self.assertTrue(issued.startswith("__Host-ux46="), issued)
        for attribute in ("Max-Age=604800", "Path=/", "Secure", "HttpOnly",
                          "SameSite=Strict"):
            self.assertIn(attribute, issued)
        self.assertNotIn("Domain", issued)
        self.assertNotIn(PASSWORD, issued)

    def test_the_login_is_enough_on_its_own_and_is_not_reissued(self):
        _, headers, _ = self.ask()
        held = cookie_value(headers)
        status, again, body = self.ask(path="/api/bootstrap", password=None, cookie=held)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["upstream"], "console")
        # Fixed for a week: a poll every second does not rewrite it.
        self.assertEqual(set_cookie(again), "")

    def test_a_login_is_not_read_from_the_console_side(self):
        _, headers, _ = self.ask()
        held = cookie_value(headers)
        self.console.seen.clear()
        self.ask(path="/api/bootstrap", password=None,
                 cookie=held + "; theme=dark; atlas=keep-me")
        sent = self.console.seen[0]["headers"].get("cookie", "")
        self.assertNotIn("__Host-ux46", sent)
        self.assertIn("theme=dark", sent)
        self.assertIn("atlas=keep-me", sent)

    def test_only_that_cookie_is_removed_when_it_is_the_only_one(self):
        _, headers, _ = self.ask()
        self.console.seen.clear()
        self.ask(path="/api/bootstrap", password=None, cookie=cookie_value(headers))
        self.assertNotIn("cookie", self.console.seen[0]["headers"])

    def test_a_tampered_login_is_refused(self):
        _, headers, _ = self.ask()
        self.console.seen.clear()
        token = cookie_value(headers).split("=", 1)[1]
        payload, _, signature = token.partition(".")
        spoiled = [
            payload + "." + signature[:-2] + ("aa" if signature[-2:] != "aa" else "bb"),
            payload[:-2] + ("aa" if payload[-2:] != "aa" else "bb") + "." + signature,
            payload, payload + "." + signature + ".extra", "not-a-token",
        ]
        for token in spoiled:
            status, _, _ = self.ask(password=None, cookie=f"__Host-ux46={token}")
            self.assertEqual(status, 401, token[:24])
        self.assertEqual(self.console.seen, [])

    def test_an_expired_login_is_refused(self):
        stale = self.server.login.mint(USER, max_age=-1)
        status, _, _ = self.ask(password=None, cookie=f"__Host-ux46={stale}")
        self.assertEqual(status, 401)
        self.assertEqual(self.console.seen, [])

    def test_a_login_issued_to_somebody_else_is_refused(self):
        theirs = self.server.login.mint("someone@else.example")
        status, _, _ = self.ask(password=None, cookie=f"__Host-ux46={theirs}")
        self.assertEqual(status, 401)
        # And the identity check still comes first, whatever the cookie says.
        mine = self.server.login.mint(USER)
        self.assertEqual(self.ask(password=None, cookie=f"__Host-ux46={mine}",
                                  identity="someone@else.example")[0], 403)
        self.assertEqual(self.ask(password=None, cookie=f"__Host-ux46={mine}",
                                  host="localhost:8881")[0], 403)
        self.assertEqual(self.console.seen, [])

    def test_a_valid_login_stands_even_beside_a_wrong_password(self):
        _, headers, _ = self.ask()
        held = cookie_value(headers)
        before = self.gate_state.kdf_calls
        status, _, _ = self.ask(path="/api/bootstrap", password="Wrong-Password9!",
                                cookie=held)
        self.assertEqual(status, 200)
        # It was not treated as an attempt at all: no derivation, and nothing
        # counted against this identity.
        self.assertEqual(self.gate_state.kdf_calls, before)
        self.assertEqual(self.gate_state._failures.get(USER, []), [])

    def test_a_login_survives_a_restart_and_not_a_new_password(self):
        token = self.server.login.mint(USER)
        # Same file, new process: the key is derived from the stored hash, so
        # the login the browser is holding still means something.
        restarted = gate.Login(gate.load_password(self.password_file))
        self.assertTrue(restarted.valid(token, USER))
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            rotated = Path(tmp) / "password.json"
            gate.write_password(rotated, "user", "Another-Fixture8!")
            self.assertFalse(gate.Login(gate.load_password(rotated)).valid(token, USER))
        # And the same password set again is a new salt, so a new key.
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            again = Path(tmp) / "password.json"
            gate.write_password(again, "user", PASSWORD)
            self.assertFalse(gate.Login(gate.load_password(again)).valid(token, USER))


class Challenges(GateCase):
    def test_a_navigation_is_asked_for_the_password(self):
        for headers in ({"Sec-Fetch-Mode": "navigate"}, {}):
            status, answered, _ = self.ask(password=None, headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(answered.get("WWW-Authenticate"),
                             'Basic realm="UX46", charset="UTF-8"')

    def test_a_background_fetch_is_refused_without_a_prompt(self):
        for mode in ("cors", "no-cors", "same-origin"):
            status, headers, body = self.ask(
                path="/api/events?after=1", password=None,
                headers={"Sec-Fetch-Mode": mode})
            self.assertEqual(status, 401)
            self.assertNotIn("WWW-Authenticate", headers)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(json.loads(body)["error"], "workspace_login_required")
        self.assertEqual(self.console.seen, [])

    def test_an_upstream_challenge_never_becomes_a_ux46_password_box(self):
        status, headers, body = self.ask(path="/provider401")
        self.assertEqual(status, 401)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual(json.loads(body)["error"], "the provider wants its own login")

    def test_a_basic_only_caller_keeps_working_without_ever_using_a_cookie(self):
        for _ in range(3):
            status, _, body = self.ask(path="/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["upstream"], "console")


class Identity(GateCase):
    def test_no_password_is_challenged_and_nothing_is_forwarded(self):
        status, headers, _ = self.ask(password=None)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"),
                         'Basic realm="UX46", charset="UTF-8"')
        self.assertEqual(self.console.seen, [])

    def test_a_wrong_password_is_refused_and_nothing_is_forwarded(self):
        status, _, body = self.ask(password="not it")
        self.assertEqual(status, 401)
        self.assertNotIn(b"not it", body)
        self.assertEqual(self.console.seen, [])

    def test_a_wrong_username_is_refused(self):
        self.assertEqual(self.ask(user="someone")[0], 401)
        self.assertEqual(self.console.seen, [])

    def test_the_right_password_reaches_the_console(self):
        status, _, body = self.ask(path="/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["upstream"], "console")
        self.assertEqual(len(self.console.seen), 1)

    def test_an_unknown_identity_is_refused_even_with_the_password(self):
        self.assertEqual(self.ask(identity="someone@else.example")[0], 403)
        self.assertEqual(self.ask(identity="")[0], 403)
        self.assertEqual(self.ask(identity=None)[0], 403)
        self.assertEqual(self.console.seen, [])

    def test_another_host_is_refused_even_with_the_password(self):
        self.assertEqual(self.ask(host=f"127.0.0.1:{self.port}")[0], 403)
        self.assertEqual(self.ask(host="localhost:8880")[0], 403)
        self.assertEqual(self.console.seen, [])

    def test_the_password_never_reaches_the_console(self):
        self.ask(path="/api/bootstrap")
        headers = self.console.seen[0]["headers"]
        self.assertNotIn("authorization", headers)
        self.assertFalse([v for v in headers.values() if PASSWORD in v])

    def test_what_the_console_needs_is_preserved(self):
        self.ask(method="POST", path="/api/room/a%2Fb/continue?x=1", body=b"{}",
                 headers={"Origin": ORIGIN, "X-Atlas-CSRF": "token-123",
                          "Content-Type": "application/json"})
        seen = self.console.seen[0]
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/api/room/a%2Fb/continue?x=1")
        self.assertEqual(seen["headers"]["host"], HOST)
        self.assertEqual(seen["headers"]["origin"], ORIGIN)
        self.assertEqual(seen["headers"]["x-atlas-csrf"], "token-123")
        self.assertEqual(seen["headers"]["tailscale-user-login"], USER)
        self.assertEqual(seen["headers"]["content-type"], "application/json")

    def test_scrypt_is_not_run_on_every_polling_request(self):
        before = self.gate_state.kdf_calls
        for _ in range(6):
            self.assertEqual(self.ask(path="/api/events?after=1")[0], 200)
        self.assertEqual(self.gate_state.kdf_calls - before, 1)
        # And what is kept is a keyed digest, not anything anybody typed.
        for key in list(self.gate_state._cache):
            self.assertNotIn(PASSWORD.encode(), key)


class Routing(GateCase):
    def test_the_app_and_its_apis_go_to_the_console(self):
        for path in ("/", "/app.js", "/tell.js", "/tell.css", "/api/bootstrap",
                     "/api/atlas/files/abc/download", "/api/agents/agent2/api/room/x"):
            self.assertEqual(self.ask(path=path)[0], 200)
        self.assertEqual(len(self.console.seen), 7)
        self.assertEqual(self.tell.seen, [])

    def test_tell_goes_to_tell(self):
        for path in ("/api/tell", "/api/tell/inbox?since=3"):
            self.assertEqual(self.ask(path=path)[0], 200)
        self.assertEqual([s["path"] for s in self.tell.seen],
                         ["/api/tell", "/api/tell/inbox?since=3"])
        self.assertEqual(self.console.seen, [])

    def test_a_lookalike_path_is_not_tell(self):
        self.assertEqual(self.ask(path="/api/tellingly")[0], 200)
        self.assertEqual(self.tell.seen, [])
        self.assertEqual(len(self.console.seen), 1)

    def test_every_route_is_gated(self):
        for path in ("/", "/app.js", "/api/bootstrap", "/api/tell",
                     "/api/atlas/files/abc/download", "/brand/ux46-icon-1.svg"):
            self.assertEqual(self.ask(path=path, password=None)[0], 401, path)
        self.assertEqual(self.console.seen, [])
        self.assertEqual(self.tell.seen, [])


class ModuleConfiguration(GateCase):
    def test_explicit_modules_are_authenticated_and_do_not_probe_upstream(self):
        self.server.modules = {'email': True, 'tell': True, 'constellation': True}
        status, _, body = self.ask(path='/api/modules')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.server.modules)
        self.assertEqual(self.console.seen, [])
        self.assertEqual(self.ask(path='/api/modules', password=None)[0], 401)
        self.assertEqual(self.ask('POST', '/api/modules', body=b'{}')[0], 405)


class Bodies(GateCase):
    def test_old_adapter_audio_supports_seek_through_authenticated_gateway(self):
        for path in ("/api/audio/fixture.wav", "/api/agents/agent2/api/audio/fixture.wav"):
            with self.subTest(path=path):
                status, headers, body = self.ask(path=path, headers={"Range": "bytes=20-79"})
                self.assertEqual(status, 206)
                self.assertEqual(body, BINARY[20:80])
                self.assertEqual(headers["Content-Range"], f"bytes 20-79/{len(BINARY)}")
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(headers["Content-Length"], "60")

    def test_audio_range_cannot_bypass_login(self):
        status, _, _ = self.ask(path="/api/audio/fixture.wav", password=None,
                                headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 401)
        self.assertEqual(self.console.seen, [])

    def test_an_upload_arrives_byte_for_byte(self):
        status, _, _ = self.ask(method="POST", path="/echo", body=BINARY,
                                headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(self.console.seen[0]["body"], BINARY)

    def test_a_download_arrives_byte_for_byte(self):
        status, headers, body = self.ask(path="/binary")
        self.assertEqual(status, 200)
        self.assertEqual(body, BINARY)
        self.assertEqual(headers["Content-Length"], str(len(BINARY)))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    def test_head_answers_with_headers_and_no_body(self):
        status, headers, body = self.ask(method="HEAD", path="/binary")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(BINARY)))
        self.assertEqual(body, b"")
        self.assertEqual(self.console.seen[0]["method"], "HEAD")

    def test_a_response_without_a_length_is_still_delivered(self):
        status, headers, body = self.ask(path="/nolength")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"streamed")
        self.assertNotIn("Content-Length", headers)

    def test_a_long_poll_is_waited_for(self):
        started = time.monotonic()
        status, _, body = self.ask(path="/slow")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"slow": True})
        self.assertGreaterEqual(time.monotonic() - started, 0.9)


class Framing(GateCase):
    def test_an_upgrade_is_refused_before_forwarding(self):
        status, _, _ = self.ask(headers={"Upgrade": "websocket"})
        self.assertEqual(status, 400)
        self.assertEqual(self.console.seen, [])

    def test_a_chunked_request_is_refused_before_forwarding(self):
        status, _, _ = self.ask(method="POST", path="/echo",
                                headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)
        self.assertEqual(self.console.seen, [])

    def test_conflicting_content_lengths_are_refused_before_forwarding(self):
        status, _, _ = self.ask(method="POST", path="/echo",
                                raw=[("Content-Length", "3"), ("Content-Length", "9")])
        self.assertEqual(status, 400)
        self.assertEqual(self.console.seen, [])

    def test_a_malformed_content_length_is_refused_before_forwarding(self):
        status, _, _ = self.ask(method="POST", path="/echo",
                                raw=[("Content-Length", "3, 9")])
        self.assertEqual(status, 400)
        self.assertEqual(self.console.seen, [])

    def test_an_oversized_upload_is_refused_before_forwarding(self):
        status, _, _ = self.ask(method="POST", path="/echo",
                                raw=[("Content-Length", str(gate.MAX_UPLOAD + 1))])
        self.assertEqual(status, 413)
        self.assertEqual(self.console.seen, [])

    def test_an_unauthorized_mutation_never_reaches_the_console(self):
        for password in (None, "not it"):
            status, _, _ = self.ask(method="POST", path="/api/room/a%2Fb/submit",
                                    password=password, body=b'{"body":"hello"}')
            self.assertEqual(status, 401)
        self.assertEqual(self.console.seen, [])


class Brand(GateCase):
    def test_a_listed_asset_is_served_byte_for_byte(self):
        status, headers, body = self.ask(path="/brand/ux46-icon-1.svg")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<svg id='mark'/>")
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(self.console.seen, [])

    def test_an_unlisted_name_is_not_served(self):
        self.assertEqual(self.ask(path="/brand/ux46-icon-2.svg")[0], 404)
        self.assertEqual(self.ask(path="/brand/")[0], 404)

    def test_traversal_cannot_reach_anything_else(self):
        for path in ("/brand/../not-brand.txt",
                     "/brand/%2e%2e/not-brand.txt",
                     "/brand/..%2fnot-brand.txt",
                     "/brand/../../tools/ux46_access_gateway.py"):
            status, _, body = self.ask(path=path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(b"must never be served", body)
            self.assertNotIn(b"scrypt", body)

    def test_brand_needs_the_password_unless_it_was_made_public(self):
        self.assertEqual(self.ask(path="/brand/ux46-icon-1.svg", password=None)[0], 401)


class PublicBrand(GateCase):
    public_brand = True

    def test_brand_is_served_without_a_password_but_never_without_identity(self):
        status, _, body = self.ask(path="/brand/ux46-manifest-1.webmanifest", password=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"name":"UX46"}')
        self.assertEqual(self.ask(path="/brand/ux46-manifest-1.webmanifest",
                                  password=None, identity="")[0], 403)
        self.assertEqual(self.ask(path="/brand/ux46-manifest-1.webmanifest",
                                  password=None, host="localhost:8880")[0], 403)

    def test_the_app_is_still_gated(self):
        self.assertEqual(self.ask(path="/", password=None)[0], 401)
        self.assertEqual(self.ask(path="/api/bootstrap", password=None)[0], 401)
        self.assertEqual(self.console.seen, [])


class RateLimit(GateCase):
    def test_repeated_wrong_passwords_are_slowed_to_a_stop(self):
        seen = set()
        for _ in range(gate.Gate.FAIL_LIMIT + 2):
            seen.add(self.ask(password="wrong")[0])
        self.assertIn(429, seen)
        # Still refused for the moment, even with the right one, and the
        # console has heard nothing about any of it.
        self.assertEqual(self.ask()[0], 429)
        self.assertEqual(self.console.seen, [])
        # Bounded state: one identity, not one entry per attempt.
        self.assertLessEqual(len(self.gate_state._failures), 1)
        self.assertLessEqual(len(self.gate_state._failures[USER]), gate.Gate.FAIL_LIMIT)


# ---------------------------------------------------------------------------
# the password file, without a server
# ---------------------------------------------------------------------------

@needs_scrypt
class PasswordFile(unittest.TestCase):
    def test_it_is_written_privately_and_holds_no_plaintext(self):
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            path = Path(tmp) / "state" / "password.json"
            gate.write_password(path, "user", PASSWORD)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            raw = path.read_bytes()
            self.assertNotIn(PASSWORD.encode(), raw)
            self.assertNotIn(b"password\":", raw)
            data = json.loads(raw)
            self.assertEqual(data["format"], gate.FORMAT)
            self.assertEqual(data["kdf"], "scrypt")
            self.assertEqual(data["username"], "user")
            self.assertEqual(set(data) >= {"n", "r", "p", "dklen", "salt", "hash"}, True)
            stored = gate.load_password(path)
            self.assertTrue(stored.matches(PASSWORD))
            self.assertFalse(stored.matches(PASSWORD + " "))

    def test_an_empty_password_is_refused(self):
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            path = Path(tmp) / "password.json"
            with self.assertRaises(gate.PasswordFileError):
                gate.write_password(path, "user", "")
            self.assertFalse(path.exists())

    def test_password_policy_rejects_each_missing_requirement(self):
        for password in ("Aa1!", "lowercase1!", "UPPERCASE1!",
                         "NoNumbers!", "NoSymbols123", "OnlySpace1 "):
            with self.subTest(password=password), TemporaryDirectory() as tmp:
                path = Path(tmp) / "password.json"
                with self.assertRaises(gate.PasswordFileError):
                    gate.write_password(path, "user", password)
                self.assertFalse(path.exists())

    def test_a_new_password_replaces_the_old_one_in_one_step(self):
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            path = Path(tmp) / "password.json"
            gate.write_password(path, "user", PASSWORD)
            first = gate.load_password(path)
            gate.write_password(path, "user", "Another-Fixture8!")
            second = gate.load_password(path)
            self.assertNotEqual(first.salt, second.salt)
            self.assertFalse(second.matches(PASSWORD))
            self.assertTrue(second.matches("Another-Fixture8!"))
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_startup_fails_closed_without_a_usable_file(self):
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            missing = Path(tmp) / "nothing.json"
            with self.assertRaises(gate.PasswordFileError):
                gate.load_password(missing)

            loose = Path(tmp) / "loose.json"
            gate.write_password(loose, "user", PASSWORD)
            os.chmod(loose, 0o644)
            with self.assertRaises(gate.PasswordFileError):
                gate.load_password(loose)

            junk = Path(tmp) / "junk.json"
            junk.write_text("{}")
            os.chmod(junk, 0o600)
            with self.assertRaises(gate.PasswordFileError):
                gate.load_password(junk)

            weak = Path(tmp) / "weak.json"
            data = json.loads(loose.read_text())
            data["n"] = 2
            weak.write_text(json.dumps(data))
            os.chmod(weak, 0o600)
            with self.assertRaises(gate.PasswordFileError):
                gate.load_password(weak)

    def test_serving_refuses_to_start_without_a_private_origin(self):
        with TemporaryDirectory(prefix="ux46-gate-") as tmp:
            path = Path(tmp) / "password.json"
            gate.write_password(path, "user", PASSWORD)
            args = gate.build_parser().parse_args(
                ["--password-file", str(path), "--public-user", USER])
            self.assertEqual(gate.serve(args), 2)
            args = gate.build_parser().parse_args(
                ["--public-origin", ORIGIN, "--public-user", USER,
                 "--password-file", str(Path(tmp) / "absent.json")])
            self.assertEqual(gate.serve(args), 2)

    def test_a_new_install_names_its_owner_and_ships_no_password(self):
        args = gate.build_parser().parse_args([])
        # Not a name that ships with the software: whoever is installing it.
        self.assertEqual(args.username, gate.default_username())
        self.assertTrue(args.username)
        # And nothing works until somebody sets a password of their own.
        self.assertEqual(args.password_file, "")
        self.assertFalse(any("password" in str(action.default or "").casefold()
                             for action in gate.build_parser()._actions))

    def test_only_a_loopback_upstream_is_accepted(self):
        gate.Upstream("http://127.0.0.1:8877")
        for origin in ("http://10.0.0.5:8877", "https://example.com", "http://evil.test"):
            with self.assertRaises(ValueError):
                gate.Upstream(origin)


class RecoveryGateway(GateCase):
    def test_recovery_is_local_to_gateway_and_requires_login_origin_and_token(self):
        from unittest.mock import patch
        from ux46_local import initialize
        with TemporaryDirectory() as temporary:
            root = Path(temporary); initialize(root)
            self.server.recovery_root = root
            try:
                status, headers, raw = self.ask(path='/api/recovery/status')
                self.assertEqual(status,200); self.assertEqual(self.console.seen,[])
                token = json.loads(raw)['recovery_csrf']; cookie = cookie_value(headers)
                body = json.dumps({'mode':'all','request_id':'gateway_fixture_1'})
                permitted = {'Origin':ORIGIN,'X-UX46-Recovery-CSRF':token,'Content-Type':'application/json'}
                with patch.object(gate.recovery,'launch',return_value={'id':'gateway_fixture_1','state':'running'}) as launch:
                    for held, supplied in [(None,permitted),(cookie,{}),(cookie,{**permitted,'Origin':'https://wrong.example'})]:
                        self.assertEqual(self.ask('POST','/api/recovery/start',body=body,cookie=held,headers=supplied)[0],403)
                    launch.assert_not_called()
                    self.assertEqual(self.ask('POST','/api/recovery/start',body=body,cookie=cookie,headers=permitted)[0],202)
                    launch.assert_called_once_with(root,'all',None,'gateway_fixture_1')
                with patch.object(gate.recovery,'launch') as launch:
                    malicious = json.dumps({'mode':'all','request_id':'gateway_fixture_2','command':'stop-anything'})
                    self.assertEqual(self.ask('POST','/api/recovery/start',body=malicious,cookie=cookie,headers=permitted)[0],400)
                    launch.assert_not_called()
                self.assertEqual(self.console.seen,[])
            finally: self.server.recovery_root = None


if __name__ == "__main__":
    unittest.main()
