"""Real HTTP boundaries: scope, revocation, browser origin and gateway login."""
import hashlib
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from test_constellation_email import Store,Principal,lesson
from constellation_server import Handler
from ux46_workspace_api import WorkspaceAPI
from test_ux46_access_gateway import GateCase,ORIGIN

class AgentBoundary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.grants=root/'grants.json'
        self.config={'principals':[{'name':'peer','projects':['demo-platform'],'write':True,'token_sha256':hashlib.sha256(b'fixture-only').hexdigest()}]}
        self.grants.write_text(json.dumps(self.config))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.local_principal=None;self.server.grants=self.grants;self.server.store=Store(root/'db')
        for key,project in [('visible','demo-platform'),('hidden','secret')]:
            self.server.store.capture(Principal('owner',('*',),True),lesson(key,[project]))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def request(self,operation,args=None,headers=None):
        conn=HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        try:
            conn.request('POST','/v1/call',json.dumps({'operation':operation,'args':args or {}}),headers or {'Authorization':'Bearer fixture-only'})
            r=conn.getresponse();return r.status,json.loads(r.read())
        finally:conn.close()
    def test_scope_revocation_and_no_browser_email(self):
        status,result=self.request('lookup');self.assertEqual(status,200)
        self.assertEqual([r['id'] for r in result['result']['items']],['visible'])
        self.assertEqual(self.request('source',{'refs':[{'id':'hidden','revision':1,'source_id':'src'}]})[0],404)
        self.assertEqual(self.request('email/view')[0],404)
        self.assertEqual(self.request('health',headers={'Authorization':'Bearer fixture-only','Origin':'https://example.com'})[0],403)
        self.config['principals'][0]['enabled']=False;self.grants.write_text(json.dumps(self.config))
        self.assertEqual(self.request('health')[0],403)

class HumanBoundary(GateCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server.workspace_api=WorkspaceAPI(Path(cls.tmp.name)/'workspace')
        cls.server.workspace_ui=Path(cls.tmp.name)/'ui';cls.server.workspace_ui.mkdir()
        for name in ('index.html','workspace.js','workspace.css','app.js','styles.css','tell.js','tell.css'):
            (cls.server.workspace_ui/name).write_text('fixture-ui')
    def test_workspace_requires_both_identity_and_login(self):
        self.assertEqual(self.ask(path='/api/email/view',password=None)[0],401)
        self.assertEqual(self.ask(path='/api/email/view',identity=None)[0],403)
        self.assertEqual(self.ask(path='/api/email/view')[0],200)
        self.assertEqual(self.ask(path='/workspace.js',password=None)[0],401)
        self.assertEqual(self.ask(path='/workspace.js')[2],b'fixture-ui')
    def test_mutations_require_origin_and_current_csrf(self):
        body=json.dumps({'base_revision':0,'rule':{'id':'test','field':'recipient','value':'legal@example.com','need':'read','category':'legal'}})
        self.assertEqual(self.ask('POST','/api/email/rule',body=body)[0],403)
        self.assertEqual(self.ask('POST','/api/email/rule',body=body,headers={'Origin':ORIGIN,'X-Atlas-CSRF':'wrong'})[0],403)
        headers={'Origin':ORIGIN,'X-Atlas-CSRF':'fixture-csrf'}
        self.assertEqual(self.ask('POST','/api/email/rule',body=body,headers=headers)[0],200)
        self.assertEqual(self.ask('POST','/api/email/rule',body=body,headers=headers)[0],409)
        self.assertEqual(self.ask(path='/api/email/rule')[0],400)

    def test_agent_refresh_requires_identity_origin_and_csrf_before_routing(self):
        body=json.dumps({'agent':'agent2','client_id':'boundary-request'})
        with patch('ux46_agent_actions.AgentClient') as client,patch.object(self.server.workspace_api.agent_jobs,'start',return_value={'job':None}) as start:
            for kwargs in ({'password':None},{'identity':None},{},{'headers':{'Origin':ORIGIN,'X-Atlas-CSRF':'wrong'}}):
                self.assertIn(self.ask('POST','/api/agent-actions/refresh',body=body,**kwargs)[0],(401,403))
            self.assertFalse(client.called);self.assertFalse(start.called)
            self.assertEqual(self.ask('POST','/api/agent-actions/refresh',body=body,headers={'Origin':ORIGIN,'X-Atlas-CSRF':'fixture-csrf'})[0],200)
            self.assertEqual(start.call_args.args[:2],('agent2','boundary-request'))
        self.assertEqual(self.ask(path='/tell.js')[2],b'fixture-ui')
        self.assertEqual(self.ask(path='/tell.js',password=None)[0],401)
    def test_schedule_auth_and_revision(self):
        self.assertEqual(self.ask(path='/api/schedule/view',password=None)[0],401)
        self.assertEqual(self.ask(path='/api/schedule/view')[0],200)
        self.server.workspace_api.schedule.register(dict(id='boundary',kind='reminder',title='Review',deadline=9999999999,uses=10,owner='User'))
        body=json.dumps({'id':'boundary','revision':1,'action':'complete'})
        self.assertEqual(self.ask('POST','/api/schedule/action',body=body)[0],403)
        headers={'Origin':ORIGIN,'X-Atlas-CSRF':'fixture-csrf'}
        self.assertEqual(self.ask('POST','/api/schedule/action',body=body,headers=headers)[0],200)
        self.assertEqual(self.ask('POST','/api/schedule/action',body=body,headers=headers)[0],409)

    def test_device_registry_requires_identity_origin_and_csrf(self):
        view='/api/desktop-devices/view?browser=boundary-browser'
        self.assertEqual(self.ask(path=view,password=None)[0],401)
        self.assertEqual(self.ask(path=view,identity=None)[0],403)
        body=json.dumps({'browser':'boundary-browser','window':'boundary-window','platform':'mac'})
        for route in ('check-in','rename','associate'):
            url='/api/desktop-devices/'+route
            self.assertEqual(self.ask('POST',url,body=body)[0],403)
            self.assertEqual(self.ask('POST',url,body=body,headers={'Origin':ORIGIN,'X-Atlas-CSRF':'wrong'})[0],403)
            self.assertEqual(self.ask(path=url)[0],400)
        headers={'Origin':ORIGIN,'X-Atlas-CSRF':'fixture-csrf'}
        self.assertEqual(self.ask('POST','/api/desktop-devices/check-in',body=body,headers=headers)[0],200)
        self.assertEqual(self.ask(path=view)[0],200)

    def test_assets_do_not_escape_release_and_keep_native_api(self):
        self.assertEqual(self.ask(path='/api/example')[0],200)
        self.assertTrue(self.console.seen)
        self.assertEqual(self.ask(path='/workspace.js',method='HEAD')[2],b'')
        self.assertEqual(self.ask(path='/workspace.js',method='POST',body='x')[0],405)
        self.assertNotEqual(self.ask(path='/../not-brand.txt')[2],b'this must never be served')

if __name__=='__main__':unittest.main()
