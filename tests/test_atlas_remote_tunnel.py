"""The SSH forward's local end and what ssh is allowed to tell the browser.

No real ssh runs here. A stand-in ``ssh`` executable either serves a tiny
console over the Unix socket named in its ``-L`` argument (as OpenSSH's
StreamLocal forward would), or never comes up, or fails with a chatty stderr.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_remote as remote  # noqa: E402

# A stand-in for ``ssh -N -L <socket>:<target> host``: it listens on the local
# socket and answers like a console that saw the request's Host header.
SERVING_SSH = textwrap.dedent('''\
    #!{python}
    import json, os, socketserver, sys
    from http.server import BaseHTTPRequestHandler
    local = sys.argv[sys.argv.index("-L") + 1].split(":", 1)[0]
    for n in range(int(os.environ.get("FAKE_SSH_FLOOD", "0"))):
        sys.stderr.write("debug1: chatter %d svc-agent@10.20.30.40 %s\\n" % (n, "x" * 200))
    sys.stderr.flush()
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            raw = json.dumps({{"csrf": "remote-token", "capabilities": {{}},
                               "host_seen": self.headers.get("Host")}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    class S(socketserver.UnixStreamServer):
        def get_request(self):
            conn, _ = self.socket.accept()
            return conn, ("local", 0)
    S(local, H).serve_forever()
''')


class SquattingHandler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        SquattingHandler.seen.append(self.path)
        raw = b'{"csrf": "attacker", "capabilities": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TunnelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ux46-tun-", dir="/tmp"))
        self.addCleanup(self._cleanup)
        self.runtime = self.tmp / "run"
        self.runtime.mkdir(mode=0o700)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_ssh(self, body: str) -> str:
        path = self.tmp / "ssh"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def tunnel(self, ssh: str, **extra) -> remote.SshTunnel:
        tunnel = remote.SshTunnel(host="studio", user="agent2", remote_port=8878,
                                  ssh_binary=ssh, runtime_root=str(self.runtime), **extra)
        self.addCleanup(tunnel.close)
        return tunnel


class UnixSocketForwardTests(TunnelTestCase):
    def test_forward_is_a_unix_socket_in_a_private_directory(self):
        tunnel = self.tunnel("ssh")
        path = str(tunnel._private_dir() / tunnel.SOCKET_NAME)
        argv = tunnel._argv(path)
        self.assertIn(f"{path}:127.0.0.1:8878", argv)
        self.assertIn("StreamLocalBindUnlink=yes", argv)
        self.assertFalse(any(arg.startswith("127.0.0.1:") and arg.count(":") == 2 for arg in argv))
        info = os.lstat(Path(path).parent)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_uid, os.getuid())

    def test_requests_reach_the_console_over_the_socket_with_its_host_header(self):
        ssh = self.fake_ssh(SERVING_SSH.format(python=sys.executable))
        tunnel = self.tunnel(ssh)
        console = remote.RemoteConsole(tunnel, 8878)
        boot = console.bootstrap()
        self.assertEqual(boot["host_seen"], "127.0.0.1:8878")
        self.assertNotIn("csrf", boot)
        self.assertTrue(tunnel._alive())
        sock_path = tunnel._socket
        self.assertTrue(stat.S_ISSOCK(os.lstat(sock_path).st_mode))
        tunnel.close()
        self.assertFalse(os.path.exists(sock_path))

    def test_a_squatter_on_the_configured_local_port_is_never_the_tunnel(self):
        squatter = HTTPServer(("127.0.0.1", 0), SquattingHandler)
        threading.Thread(target=squatter.serve_forever, daemon=True).start()
        self.addCleanup(squatter.server_close)
        self.addCleanup(squatter.shutdown)
        SquattingHandler.seen = []
        # An ssh still authenticating: it never opens its listener.
        ssh = self.fake_ssh("#!/bin/sh\nsleep 30\n")
        tunnel = self.tunnel(ssh, local_port=squatter.server_address[1])
        console = remote.RemoteConsole(tunnel, 8878)
        with mock.patch.object(remote, "TUNNEL_READY_TIMEOUT_S", 0.6):
            with self.assertRaises(remote.RemoteError) as caught:
                console.bootstrap()
        self.assertEqual(caught.exception.code, "tunnel_failed")
        self.assertEqual(SquattingHandler.seen, [])

    def test_a_directory_that_stopped_being_private_is_refused(self):
        tunnel = self.tunnel("ssh")
        directory = tunnel._private_dir()
        directory.chmod(0o755)
        with self.assertRaises(remote.RemoteError) as caught:
            tunnel.socket_path()
        self.assertEqual(caught.exception.code, "tunnel_failed")

    def test_a_non_socket_at_the_forward_path_is_not_ready(self):
        tunnel = self.tunnel("ssh")
        path = tunnel._private_dir() / tunnel.SOCKET_NAME
        path.write_text("not a socket")
        self.assertFalse(tunnel._owned_socket(str(path)))


LEAKY_SSH = ("#!/bin/sh\n"
             "echo 'svc-agent@10.20.30.40: Permission denied (publickey). "
             "key /home/owner/.ssh/id_studio_ed25519' >&2\nexit 255\n")


class SshDiagnosticsStayServerSideTests(TunnelTestCase):
    def registry(self, ssh: str) -> remote.AgentRegistry:
        runtime = str(self.runtime)

        def transport(definition):
            return remote.SshTunnel(host="studio", user="svc-agent", remote_port=8877,
                                    ssh_binary=ssh, runtime_root=runtime)

        config = self.tmp / "agents.json"
        config.write_text(json.dumps({"schema_version": 1, "agents": [
            {"id": "studio", "remote_port": 8877}]}))
        registry = remote.AgentRegistry(config_path=str(config), transport_factory=transport)
        self.addCleanup(registry.close)
        return registry

    def test_ssh_stderr_is_logged_but_never_published(self):
        registry = self.registry(self.fake_ssh(LEAKY_SSH))
        with self.assertLogs("ux46.remote", level="WARNING") as logs:
            listing = registry.listing()
        published = json.dumps(listing)
        for secret in ("10.20.30.40", "svc-agent", "id_studio_ed25519", "Permission denied"):
            self.assertNotIn(secret, published)
        studio = next(a for a in listing["agents"] if a["id"] == "studio")
        self.assertEqual(studio["availability"]["detail"],
                         remote.TRANSPORT_MESSAGES["tunnel_failed"])
        self.assertIn("id_studio_ed25519", "\n".join(logs.output))

    def test_proxy_errors_carry_only_the_fixed_message(self):
        registry = self.registry(self.fake_ssh(LEAKY_SSH))
        with self.assertLogs("ux46.remote", level="WARNING"):
            with self.assertRaises(remote.RemoteError) as caught:
                registry.proxy(registry.get("studio"), "GET", "/api/rooms", "",
                               headers={}, body=None)
        self.assertEqual(str(caught.exception), remote.TRANSPORT_MESSAGES["tunnel_failed"])
        self.assertIsNone(caught.exception.detail)

    def test_an_unreachable_console_does_not_echo_socket_errors(self):
        tunnel = remote.DirectPort(1)
        console = remote.RemoteConsole(tunnel, 8878)
        with self.assertLogs("ux46.remote", level="WARNING"):
            with self.assertRaises(remote.RemoteError) as caught:
                console.bootstrap()
        self.assertEqual(caught.exception.code, "agent_unreachable")
        self.assertEqual(str(caught.exception), remote.TRANSPORT_MESSAGES["agent_unreachable"])

    def test_a_chatty_ssh_cannot_block_on_a_full_stderr_pipe(self):
        ssh = self.fake_ssh(SERVING_SSH.format(python=sys.executable))
        tunnel = self.tunnel(ssh)
        console = remote.RemoteConsole(tunnel, 8878)
        # ~1.2 MB of stderr before the listener opens: far beyond a pipe buffer.
        with mock.patch.dict(os.environ, {"FAKE_SSH_FLOOD": "5000"}):
            boot = console.bootstrap()
        self.assertEqual(boot["host_seen"], "127.0.0.1:8878")
        tail = tunnel._proc._ux46_stderr_tail
        self.assertLessEqual(len(tail), remote.SSH_STDERR_TAIL_LINES)
        self.assertTrue(all(len(line) <= remote.SSH_STDERR_LINE_CHARS for line in tail))


if __name__ == "__main__":
    unittest.main()
