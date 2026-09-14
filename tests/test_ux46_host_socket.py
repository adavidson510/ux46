import http.client
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import stat
import tempfile
import threading
import pytest
from ux46_host_socket import private_server_class


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'private adapter')
    def log_message(self, *_args):
        pass


def test_private_socket_serves_http_and_cleans_up():
    with tempfile.TemporaryDirectory(prefix='ux46-socket-') as tmp:
        path = Path(tmp) / 'a.sock'
        server = private_server_class(path)(('127.0.0.1', 8878), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert server.address_family == socket.AF_UNIX
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(path))
                client.sendall(b'GET / HTTP/1.0\r\nHost: 127.0.0.1:8878\r\n\r\n')
                response = http.client.HTTPResponse(client)
                response.begin()
                assert response.read() == b'private adapter'
            with pytest.raises(ValueError, match='already serving'):
                private_server_class(path)
        finally:
            server.shutdown()
            server.server_close()
        assert not path.exists()


def test_refuses_shared_directory_and_non_socket_replacement():
    with tempfile.TemporaryDirectory(prefix='ux46-socket-') as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        with pytest.raises(ValueError, match='0700'):
            private_server_class(root / 'a.sock')
        root.chmod(0o700)
        target = root / 'a.sock'
        target.write_text('keep this file')
        with pytest.raises(ValueError, match='non-socket'):
            private_server_class(target)
        assert target.read_text() == 'keep this file'
