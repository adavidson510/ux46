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


if __name__ == "__main__":
    unittest.main()
