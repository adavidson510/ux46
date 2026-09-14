"""Run a UX46 host adapter on an account-private Unix socket.

An SSH local forward connects to this socket as its owning host account.
Other unprivileged host accounts cannot use the adapter's native login merely
by connecting to a loopback TCP port. This runner exposes no TCP listener.
"""
from __future__ import annotations

import argparse
import http.server
import os
from pathlib import Path
import runpy
import socket
import socketserver
import stat
import sys


def private_server_class(socket_path: Path):
    if not socket_path.is_absolute() or len(str(socket_path).encode()) > 100:
        raise ValueError("Choose an absolute Unix socket path shorter than 101 bytes")
    parent = socket_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if parent.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Socket directory must be owned by this account with mode 0700")
    if socket_path.exists() or socket_path.is_symlink():
        info = socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Refusing to replace a non-socket or another account's socket")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(socket_path))
            except ConnectionRefusedError:
                socket_path.unlink()
            else:
                raise ValueError("This adapter socket is already serving")

    class PrivateHTTPServer(http.server.ThreadingHTTPServer):
        address_family = socket.AF_UNIX

        def __init__(self, server_address, handler, bind_and_activate=True):
            if not isinstance(server_address, tuple) or server_address[0] not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("Host adapter must retain a loopback-only configuration")
            self._socket_identity = None
            super().__init__(str(socket_path), handler, bind_and_activate)

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            os.chmod(socket_path, 0o600)
            self._socket_identity = socket_path.stat().st_ino
            self.server_name, self.server_port = "localhost", 0

        def get_request(self):
            connection, _address = super().get_request()
            # Admission is the OS permission check on the account-private
            # socket; existing HTTP handlers then apply normal Host/CSRF rules.
            return connection, ("127.0.0.1", 0)

        def server_close(self):
            super().server_close()
            try:
                if self._socket_identity is not None and socket_path.lstat().st_ino == self._socket_identity:
                    socket_path.unlink()
            except FileNotFoundError:
                pass

    return PrivateHTTPServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--module", required=True, choices=("atlas_console", "atlas_rivet"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    http.server.ThreadingHTTPServer = private_server_class(args.socket)
    forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    sys.argv = [args.module, *forwarded]
    runpy.run_module(args.module, run_name="__main__")


if __name__ == "__main__":
    main()
