"""Fresh installation and real loopback HTTP, without native runtime calls."""
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
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from ux46_local import initialize

class StandaloneTests(unittest.TestCase):
    def test_init_preserves_customization_and_starts_empty(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);initialize(p)
            config=json.loads((p/'config.json').read_text());config['agent_label']='My agent'
            (p/'config.json').write_text(json.dumps(config))
            self.assertEqual(initialize(p)['agent_label'],'My agent')
            self.assertEqual(json.loads((p/'registry.json').read_text())['projects'],[])
            self.assertEqual(json.loads((p/'agents.json').read_text())['agents'],[])

    def test_fresh_http_and_module_boundaries(self):
        with tempfile.TemporaryDirectory() as t:
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            root=Path(t);env=dict(os.environ,UX46_HOME=t,CODEX_HOME=str(root/'empty-codex'))
            proc=subprocess.Popen([sys.executable,'tools/ux46','run','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            base=f'http://127.0.0.1:{port}'
            def get(path):
                with urlopen(base+path,timeout=2) as r:return r.read()
            try:
                for _ in range(100):
                    if proc.poll() is not None:self.fail(proc.communicate()[1])
                    try:boot=json.loads(get('/api/bootstrap'));break
                    except OSError:time.sleep(.05)
                else:self.fail('Server failed to become ready')
                self.assertFalse(boot['runtime_started'])
                self.assertEqual(boot['vault_errors'],[])
                self.assertIn(b'Constellation',get('/'))
                self.assertTrue(get('/brand/ux46-icon-32-1.png').startswith(b'\x89PNG'))
                modules=json.loads(get('/api/modules'));self.assertTrue(modules['constellation']);self.assertFalse(modules['email'])
                agents=json.loads(get('/api/agents'))
                self.assertEqual(len(agents['agents']),1)
                self.assertEqual(agents['agents'][0]['id'],'local')
                for path in ['/api/email/view','/api/tell/summary']:
                    with self.assertRaises(HTTPError) as exc:get(path)
                    self.assertEqual(exc.exception.code,404)
                self.assertIsInstance(json.loads(get('/api/constellation/health')),dict)
                with self.assertRaises(HTTPError) as exc:get('/api/constellation/capture')
                self.assertEqual(exc.exception.code,405)
                req=Request(base+'/api/constellation/capture',data=b'{}',headers={'Content-Type':'application/json'})
                with self.assertRaises(HTTPError) as exc:urlopen(req)
                self.assertEqual(exc.exception.code,403)
                self.assertFalse((root/'workspace/email.sqlite3').exists())
                self.assertFalse(json.loads(get('/api/bootstrap'))['runtime_started'])
            finally:
                if proc.poll() is None:proc.send_signal(signal.SIGINT)
                try:out,err=proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();out,err=proc.communicate();self.fail('Server failed to stop')
                self.assertEqual(proc.returncode,0,err)

if __name__=='__main__':unittest.main()
