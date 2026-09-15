"""Exercise the front server's Claude proxy and no-provider mode over HTTP."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from ux46_local import initialize


class ModeTests(unittest.TestCase):
    @contextmanager
    def server(self,root,agent):
        config=initialize(root)
        config.update(agent=agent,agent_label='My helper',cli=str(root/'nonexistent-cli'))
        (root/'config.json').write_text(json.dumps(config))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        proc=subprocess.Popen([sys.executable,'tools/ux46','run','--port',str(port)],cwd=ROOT,
            env=dict(os.environ,HOME=str(root),UX46_HOME=str(root),CODEX_HOME=str(root/'codex')),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        base=f'http://127.0.0.1:{port}'
        def request(path,body=None,csrf=None):
            headers={'Content-Type':'application/json','Origin':base}
            if csrf:headers['X-Atlas-CSRF']=csrf
            with urlopen(Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers),timeout=5) as response:
                return json.load(response)
        try:
            for _ in range(150):
                if proc.poll() is not None:self.fail(proc.communicate()[1][-2500:])
                try:boot=request('/api/bootstrap');break
                except OSError:time.sleep(.04)
            else:self.fail('Server did not start')
            self.assertFalse(boot['runtime_started'])
            yield request,boot
        finally:
            if proc.poll() is None:proc.send_signal(signal.SIGINT)
            try:out,err=proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.communicate();self.fail('Server did not stop')
            self.assertEqual(proc.returncode,0,err[-2500:])

    def test_claude_create_via_proxy_survives_server_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.server(root,'claude') as (request,boot):
                agents=request('/api/agents')['agents']
                self.assertEqual(agents[0]['runtime'],'claude-code')
                self.assertTrue(request('/api/session-options')['available'])
                with self.assertRaises(HTTPError) as exc:
                    request('/api/sessions',{'title':'Fresh'})
                self.assertEqual(exc.exception.code,403)
                created=request('/api/sessions',{'title':'Fresh','client_id':'new_session_fixture_123'},boot['csrf'])
                self.assertEqual(created['state'],'created')
                native=created['session_id']
            with self.server(root,'claude') as (request,boot):
                rooms=request('/api/rooms')
                self.assertIn(native,json.dumps(rooms))
                self.assertFalse(request('/api/bootstrap')['runtime_started'])

    def test_skip_has_no_agent_and_refuses_native_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.server(Path(tmp),'none') as (request,boot):
                self.assertEqual(request('/api/agents')['agents'],[])
                self.assertFalse(request('/api/session-options')['available'])
                self.assertIsInstance(request('/api/constellation/health'),dict)
                with self.assertRaises(HTTPError) as exc:
                    request('/api/sessions',{'title':'Fresh'},boot['csrf'])
                self.assertEqual(exc.exception.code,409)

if __name__=='__main__':unittest.main()
