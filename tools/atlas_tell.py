"""Small allowlisted proxy to the separately versioned local Tell service."""
import http.client
import json
import os
import re
import socket
from pathlib import Path

MAX_RESPONSE = 2 * 1024 * 1024
READ = re.compile(r"/(?:summary|activity|boards/(?:daily-review|working-better|bigger-picture|invention-watch|activity))$")
WRITE = re.compile(r"/posts/[A-Za-z0-9_-]{1,120}/(?:seen|replies)$")


class TellConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def request(method, path, body=None, socket_path=None):
    allowed = ((method == "GET" and READ.fullmatch(path))
               or (method == "POST" and WRITE.fullmatch(path))
               or (method == "PATCH" and path == "/settings"))
    if not allowed:
        return 404, {"error":"not_found","message":"No such Tell endpoint"}
    if body and len(body) > 65536:
        return 413, {"error":"too_large","message":"Tell request is too large"}
    conn = TellConnection(socket_path or os.environ.get("UX46_TELL_SOCKET")
                          or Path.home()/".local/state/tell-boards-v1/boards.sock")
    try:
        conn.request(method,path,body=body,headers={"Content-Type":"application/json","Connection":"close"})
        response = conn.getresponse()
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("Tell response exceeded the display limit")
        payload = json.loads(data)
        if response.status >= 400:
            payload.setdefault("message",str(payload.get("error","Tell request failed")))
        return response.status,payload
    except (OSError, ValueError, http.client.HTTPException):
        return 503, {"error":"tell_unavailable","message":"Tell is unavailable; your console is still connected"}
    finally:
        conn.close()
