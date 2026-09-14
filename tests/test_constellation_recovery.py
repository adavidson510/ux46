"""Crash recovery for the local pilot, without touching a real service."""
import http.client
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from constellation_server import prepare_unix_path
from constellation_store import Store,Principal
from test_constellation_email import lesson

class UnixConnection(http.client.HTTPConnection):
    def __init__(self,path):super().__init__('localhost',timeout=3);self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(3);self.sock.connect(str(self.path))

class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cs-',dir='/tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'service.sock'
    def test_preserves_plain_files_and_existing_listeners(self):
        self.path.write_text('keep')
        with self.assertRaises(RuntimeError):prepare_unix_path(self.path)
        self.assertEqual(self.path.read_text(),'keep');self.path.unlink()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as live:
            live.bind(str(self.path));os.chmod(self.path,0o700);live.listen(1)
            before=self.path.stat().st_ino
            with self.assertRaises(RuntimeError):prepare_unix_path(self.path)
            self.assertEqual(self.path.stat().st_ino,before)
    def test_listener_lease_excludes_competing_start(self):
        held=prepare_unix_path(self.path)
        try:
            with self.assertRaises(BlockingIOError):prepare_unix_path(self.path)
        finally:held.close()
    def test_abnormal_stop_restarts_and_retains_knowledge(self):
        db=self.root/'knowledge.sqlite3';Store(db).capture(Principal('local-workspace',('*',),True),lesson())
        def start():
            proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'tools/constellation_server.py'),
                                   '--db',str(db),'--socket',str(self.path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            if not select.select([proc.stdout],[],[],5)[0]:self.fail('Service did not start before deadline')
            self.assertIn(b'Constellation ready',proc.stdout.readline())
            return proc
        first=start();first.kill();first.communicate(timeout=5)
        self.assertTrue(self.path.exists())
        second=start()
        try:
            conn=UnixConnection(self.path)
            try:
                conn.request('POST','/v1/call',json.dumps({'operation':'lookup','args':{'query':'attention'}}))
                response=conn.getresponse();body=json.loads(response.read())
                self.assertEqual(response.status,200);self.assertEqual(body['result']['items'][0]['id'],'method')
            finally:conn.close()
        finally:
            second.send_signal(signal.SIGTERM);second.communicate(timeout=5)
        self.assertEqual(second.returncode,0);self.assertFalse(self.path.exists())

if __name__=='__main__':unittest.main()
