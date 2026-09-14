import sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_agent_actions import RefreshJobs
from ux46_agent_actions import AgentClient
from types import SimpleNamespace
from unittest.mock import patch

class TransportTest(unittest.TestCase):
    def test_configured_local_and_remote_targets(self):
        handler=SimpleNamespace(headers={'Host':'fixture.local','Identity':'fixture'},server=SimpleNamespace(auth=SimpleNamespace(identity_header='Identity',origin_for=lambda host:'https://'+host)))
        agents={'agents':[{'id':'local','kind':'local','runtime':'codex'},{'id':'agent2','kind':'remote','runtime':'codex'}]}
        for agent,prefix in [('local',''),('agent2','/api/agents/agent2')]:
            with patch.object(AgentClient,'_main',return_value=(200,agents)) as main:
                client=AgentClient(handler,agent);client.request('POST','/api/connection/refresh',{'room':'p/r'})
                main.assert_called_with('POST',prefix+'/api/connection/refresh',{'room':'p/r'})
                self.assertTrue(client.supported)
        with patch.object(AgentClient,'_main',return_value=(200,agents)):
            with self.assertRaises(ValueError):AgentClient(handler,'unconfigured')

class Client:
    supported=True
    def __init__(self):self.busy=True;self.calls=[];self.timeout=False
    def request(self,method,path,body=None):
        self.calls.append((method,path,body))
        if path=='/api/bootstrap':return 200,{'remembered_owned':[{'room':'p/idle','thread_id':'a'},{'room':'p/busy','thread_id':'b'},{'room':'p/alias','thread_id':'b'},{'room':'p/gone','thread_id':'c'}]}
        if path.startswith('/api/room/'):
            room=path.removeprefix('/api/room/');tid={'p/idle':'a','p/busy':'b','p/gone':'c'}[room]
            return 200,{'native':{'worker':None if room=='p/gone' else {'running':True,'room':room,'thread_id':tid,'started_at':1},'active_turn':{'id':'turn'} if room=='p/busy' and self.busy else None}}
        if self.timeout:raise TimeoutError()
        return 200,{'connection':{'state':'refreshed'}}

class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.jobs=RefreshJobs(Path(self.temp.name)/'jobs.db',interval=.03);self.addCleanup(self.jobs.stopping.set)
    def wait(self,predicate):
        until=time.monotonic()+3
        while time.monotonic()<until:
            value=self.jobs.view('agent2')['job']
            if value and predicate(value):return value
            time.sleep(.01)
        self.fail('Job did not settle')
    def test_idle_busy_deduplicated_and_no_prompt_operations(self):
        client=Client();self.jobs.start('agent2','request-123',client)
        job=self.wait(lambda j:j['state']=='waiting')
        self.assertEqual([c[2] for c in client.calls if c[0]=='POST'],[{'room':'p/idle'}])
        self.assertEqual(self.jobs.start('agent2','request-456',client)['job']['id'],'request-123')
        client.busy=False;job=self.wait(lambda j:j['state']=='complete')
        self.assertEqual([c[2] for c in client.calls if c[0]=='POST'],[{'room':'p/idle'},{'room':'p/busy'},{}])
        self.assertEqual(len(job['items']),4)
        self.assertEqual(self.jobs.start('agent2','request-123',client)['job']['state'],'complete')
    def test_unknown_write_not_retried(self):
        client=Client();client.busy=False;client.timeout=True
        self.jobs.start('agent2','request-123',client);job=self.wait(lambda j:j['state']=='partial')
        self.assertEqual(sum(c[0]=='POST' and c[2]=={'room':'p/idle'} for c in client.calls),1)
        self.assertEqual(job['items'][0]['state'],'unknown')
    def test_restart_does_not_replay_pending(self):
        self.jobs._save({'id':'request-123','agent':'agent2','at':1,'state':'waiting','items':[]})
        reboot=RefreshJobs(self.jobs.path)
        self.assertEqual(reboot.view('agent2')['job']['state'],'interrupted')
    def test_request_id_cannot_cross_agents(self):
        self.jobs._save({'id':'request-123','agent':'local','at':1,'state':'complete','items':[]})
        with self.assertRaises(ValueError):self.jobs.start('agent2','request-123',Client())

if __name__=='__main__':unittest.main()
