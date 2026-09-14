"""Optional activation bridge for Tell without restarting active UX46 sessions.

Only Tell routes are exposed. The existing console supplies authentication
semantics and current CSRF; all native traffic stays on its original listener.
Remove these three private proxy mounts to undo the bridge after disabling Tell.
"""
import argparse
import json
import sys
import urllib.request
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atlas_console import AuthBoundary, ConsoleHandler, ConsoleServer, ApiError
import atlas_tell


class GatewayService:
    def __init__(self, port, origin, user, upstream):
        self.auth = AuthBoundary(port, origin, user)
        self.upstream = upstream
        self.config = SimpleNamespace(quiet=True)
        self.files = SimpleNamespace(max_upload_bytes=0)

    @property
    def csrf_token(self):
        req = urllib.request.Request(self.upstream + "/api/bootstrap", headers={
            "Host": self.auth.public_host,
            "Tailscale-User-Login": self.auth.public_user})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)["csrf"]
        except Exception:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "console_unavailable",
                           "The console is unavailable; the change was not sent")


class GatewayHandler(ConsoleHandler):
    def _handle(self, method):
        # Some path-prefix proxies strip the mounting prefix.
        if self.path.startswith(("/summary", "/activity", "/boards/", "/posts/", "/settings")):
            self.path = "/api/tell" + self.path
        return super()._handle(method)

    def _api(self, method, path, query, decision):
        if not path.startswith("/api/tell/") or query:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "This listener serves Tell only")
        status, payload = atlas_tell.request(method,path.removeprefix("/api/tell"),
                                             getattr(self,"_raw_body",b"") or None)
        return self._json(status,payload)

    def _agent_api(self, *args):
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "This listener serves Tell only")

    def _serve_static(self, path):
        if path not in {"/tell.js", "/tell.css"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "This listener serves Tell only")
        return super()._serve_static(path)


def main():
    p=argparse.ArgumentParser(description="Private UX46 Tell activation bridge")
    p.add_argument("--port",type=int,default=8879)
    p.add_argument("--public-origin",required=True)
    p.add_argument("--public-user",required=True)
    p.add_argument("--upstream",default="http://127.0.0.1:8877")
    a=p.parse_args()
    service=GatewayService(a.port,a.public_origin,a.public_user,a.upstream)
    with ConsoleServer(("127.0.0.1",a.port),GatewayHandler,service) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
