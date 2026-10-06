"""Gateway additions do not restart or bypass the existing console boundary."""
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import ux46_access_gateway as gate
import ux46_live_agents as live
from test_ux46_access_gateway import GateCase, HOST, USER, ORIGIN

class API(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def run(self):
        n=int(self.headers.get('Content-Length') or 0)
        body=self.rfile.read(n) if n else b''
        self.server.seen.append((self.command,self.path,dict(self.headers),body))
        if self.path=='/api/bootstrap': x={'csrf':self.server.token,'capabilities':{'read':True,'send':True},'voice':{'enabled':False}}
        elif self.path=='/api/agents':x={'agents':[{'id':'local','label':'Local'}],'default':'local','errors':[]}
        else:x={'received':True,'room':{'id':'fixture/room'}}
        raw=json.dumps(x).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    do_GET=do_POST=run

def api(token):
    s=ThreadingHTTPServer(('127.0.0.1',0),API);s.token=token;s.seen=[]
    threading.Thread(target=s.serve_forever,daemon=True).start();return s

@unittest.skipUnless(gate.scrypt_available(),'scrypt required')
class LiveCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        GateCase.setUpClass.__func__(cls)
        cls.old_console=cls.console;cls.console=api('outer-csrf');cls.adapter=api('cp-csrf')
        cls.server.console=gate.Upstream('http://127.0.0.1:'+str(cls.console.server_address[1]))
        cls.config=Path(cls.tmp.name)/'agents.json';cls.write_config('cp')
        cls.server.live_agents=live.LiveAgents(cls.config)
    @classmethod
    def write_config(cls,ident):
        cls.config.write_text(json.dumps({'schema_version':1,'agents':[{'id':ident,'label':ident.upper(),'runtime':'claude','node':'fixture','transport':'loopback','remote_port':cls.adapter.server_address[1]}]}))
    @classmethod
    def tearDownClass(cls):
        cls.adapter.shutdown();cls.old_console.shutdown();GateCase.tearDownClass.__func__(cls)
    public_brand=False
    ask=GateCase.ask
    def setUp(self):self.console.seen.clear();self.adapter.seen.clear();self.write_config('cp')
    def test_merges_live_agent_without_replacing_assistant(self):
        status,_,body=self.ask(path='/api/agents');self.assertEqual(status,200)
        self.assertEqual([a['id'] for a in json.loads(body)['agents']],['local','cp'])
        self.write_config('next');_,_,body=self.ask(path='/api/agents')
        self.assertEqual([a['id'] for a in json.loads(body)['agents']],['local','next'])
    def test_outer_auth_still_required(self):
        self.assertEqual(self.ask(path='/api/agents/cp/api/bootstrap',password=None)[0],401)
        self.assertFalse(self.adapter.seen)
    def test_csrf_and_origin_cannot_be_bypassed(self):
        path='/api/agents/cp/api/room/fixture/room/submit'
        for headers in [{},{'Origin':ORIGIN},{'Origin':'https://evil.example','X-Atlas-CSRF':'outer-csrf'}]:
            self.assertEqual(self.ask('POST',path,body=b'{}',headers=headers)[0],403)
        self.assertFalse(self.adapter.seen)
    def test_native_token_is_private_and_mutation_forwards_once(self):
        _,_,body=self.ask(path='/api/agents/cp/api/bootstrap');self.assertNotIn(b'cp-csrf',body)
        status,_,_=self.ask('POST','/api/agents/cp/api/room/fixture/room/submit',body=b'{"text":"hello"}',headers={'Origin':ORIGIN,'X-Atlas-CSRF':'outer-csrf'})
        self.assertEqual(status,200)
        posts=[x for x in self.adapter.seen if x[0]=='POST'];self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][2].get('X-Atlas-CSRF'),'cp-csrf');self.assertNotIn('Authorization',posts[0][2]);self.assertNotIn('Cookie',posts[0][2])
    def test_arbitrary_route_refused(self):
        self.assertEqual(self.ask(path='/api/agents/cp/api/test-thread')[0],400)
        self.assertFalse(self.adapter.seen)
    def test_relayed_agent_bytes_cannot_be_framed_or_run(self):
        # S19: agent answers are served on the gateway origin with the
        # gateway's own CSP and frame refusal, whatever the remote sent.
        self.assertEqual(live.CONSOLE_CSP, gate.CONSOLE_CSP)
        status,headers,_=self.ask(path='/api/agents/cp/api/bootstrap')
        self.assertEqual(status,200)
        self.assertEqual(headers.get('X-Frame-Options'),'DENY')
        self.assertEqual(headers.get('Content-Security-Policy'),gate.CONSOLE_CSP)

class _StubHandler:
    """Just enough of a request handler to drive LiveAgents.handle directly."""
    def __init__(self,path,command,headers):
        import email.message,io
        self.path=path;self.command=command;self.headers=email.message.Message()
        for k,v in headers.items():self.headers[k]=v
        self.rfile=io.BytesIO(b'{"leftover":"bytes"}');self.wfile=io.BytesIO()
        self.close_connection=False;self.status=None
    def send_response(self,status):self.status=status
    def _emit_login(self):pass
    def send_header(self,*_):pass
    def end_headers(self):pass

class LengthFraming(unittest.TestCase):
    def test_negative_or_malformed_content_length_is_refused_before_reading(self):
        with TemporaryDirectory() as tmp:
            config=Path(tmp)/'agents.json'
            config.write_text(json.dumps({'schema_version':1,'agents':[{'id':'cp','label':'CP','runtime':'claude','node':'fixture','transport':'loopback','remote_port':9}]}))
            agents=live.LiveAgents(config)
            proxied=[]
            registry=agents.read();registry.proxy=lambda *a,**k:proxied.append(a)
            for value in ('-1','-20','1e3',' x'):
                handler=_StubHandler('/api/agents/cp/api/workspace','GET',{'Content-Length':value})
                self.assertTrue(agents.handle(handler))
                self.assertEqual(handler.status,400,value)
                self.assertTrue(handler.close_connection)
                self.assertEqual(handler.rfile.tell(),0,value)
            self.assertEqual(proxied,[])

if __name__=='__main__':unittest.main()
