"""Focused boundary test for the separately activated Tell connection."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import atlas_tell
from atlas_console import ConsoleServer
from ux46_tell_gateway import GatewayService, GatewayHandler

class ProxyBoundaryTest(unittest.TestCase):
    def test_allowlist_missing_socket_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"missing.sock"
            self.assertEqual(atlas_tell.request("GET","/../../other",socket_path=path)[0],404)
            self.assertEqual(atlas_tell.request("POST","/summary",b"{}",path)[0],404)
            self.assertEqual(atlas_tell.request("GET","/summary",socket_path=path)[0],503)
            service=GatewayService(0,"https://example.test","owner@example.test","http://127.0.0.1:1")
            with ConsoleServer(("127.0.0.1",0),GatewayHandler,service) as gateway:
                thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start()
                url="http://127.0.0.1:"+str(gateway.server_port)
                for headers in ({}, {"Host":"example.test"},
                                {"Host":"example.test","Tailscale-User-Login":"someone@example.test"}):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(urllib.request.Request(url+"/api/tell/summary",headers=headers))
                    self.assertEqual(ctx.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(urllib.request.Request(url+"/api/tell/settings",
                        headers={"Host":"example.test","Tailscale-User-Login":"owner@example.test",
                                 "Origin":"https://evil.test","Content-Type":"application/json"},
                        data=b'{"schedule_enabled":false}',method="PATCH"))
                self.assertEqual(ctx.exception.code,403)
                gateway.shutdown();thread.join()

if __name__ == "__main__":
    unittest.main()
