import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ux46_account import login_identity, project_limits, AccountStatus
from test_atlas_session_workers import WorkerHarness, ROOM_A, ROOM_B
from test_atlas_console import THREAD_ONE, THREAD_TWO

class AccountTests(unittest.TestCase):
    def test_identity_changes_on_login_not_token_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'auth.json'
            def save(sub, account, refresh):
                jwt='a.'+base64.urlsafe_b64encode(json.dumps({'sub':sub}).encode()).decode().rstrip('=')+'.z'
                p.write_text(json.dumps({'auth_mode':'chatgpt','tokens':{'id_token':jwt,'account_id':account,'refresh_token':refresh}}))
            save('one','shared-workspace','secret1');a=login_identity(Path(d))
            save('one','shared-workspace','secret2');self.assertEqual(a,login_identity(Path(d)))
            save('two','shared-workspace','secret3');self.assertNotEqual(a,login_identity(Path(d)))
            self.assertNotIn('secret',a)

    def test_limits_recovery_and_unknown(self):
        def limits(percent, **extra):return {'rateLimits':{'primary':{'usedPercent':percent,'resetsAt':500},**extra}}
        self.assertEqual(project_limits(limits(100),100)['state'],'limited')
        self.assertEqual(project_limits(limits(10),100)['state'],'available')
        self.assertEqual(project_limits({},100)['state'],'unknown')
        self.assertEqual(project_limits(limits(float('nan')),100)['state'],'unknown')
        self.assertEqual(project_limits(limits(10, rateLimitReachedType='credits'),100)['state'],'limited')
        self.assertEqual(project_limits(limits(100,credits={'hasCredits':True}),100)['state'],'unknown')

    def test_metadata_only_and_failed_refresh_does_not_claim_available(self):
        class Server:
            fail=False
            def __init__(self):self.calls=[]
            def request(self, method, params, timeout):
                self.calls.append(method)
                if self.fail:raise RuntimeError('secret must stay private')
                return {'account':{'type':'chatgpt'}} if method=='account/read' else {'rateLimits':{'primary':{'usedPercent':20}}}
        server=Server();status=AccountStatus(server)
        def settle():
            for _ in range(100):
                if not status._pending:return
                time.sleep(.005)
            self.fail('status did not settle')
        status.read();settle();self.assertEqual(status.read()['state'],'available')
        self.assertEqual(server.calls,['account/read','account/rateLimits/read'])
        server.fail=True;status.invalidate(clear=True);self.assertEqual(status.read()['state'],'unknown');settle()
        self.assertEqual(status.read()['state'],'unknown')

    def test_idle_login_change_refreshes_only_owned_session_without_prompt(self):
        with patch('atlas_workers.login_identity',return_value='login-a'):
            h=WorkerHarness()
            try:
                h.attach(ROOM_A);h.attach(ROOM_B)
                a=h.service.workers.worker(THREAD_ONE);b=h.service.workers.worker(THREAD_TWO)
                with patch('atlas_workers.login_identity',return_value='login-b'):
                    room=h.service.require_room(ROOM_A)
                    result=h.service._account_recovery(room,a,None)
                    self.assertEqual(result['state'],'deferred')
                    for _ in range(200):
                        if not h.service._account_recoveries[THREAD_ONE]['pending']:break
                        time.sleep(.025)
                    self.assertEqual(h.service._account_recoveries[THREAD_ONE]['state'],'refreshed')
                    self.assertIsNot(h.service.workers.worker(THREAD_ONE),a)
                    self.assertIs(h.service.workers.worker(THREAD_TWO),b)
                    self.assertEqual(h.service.journal.recent(),[])
            finally:h.close()

    def test_busy_login_change_is_deferred_without_stopping(self):
        with patch('atlas_workers.login_identity',return_value='login-a'):
            h=WorkerHarness()
            try:
                h.attach(ROOM_A);a=h.service.workers.worker(THREAD_ONE)
                a.sessions.note_turn(THREAD_ONE,'real-work',True)
                with patch('atlas_workers.login_identity',return_value='login-b'):
                    result=h.service._account_recovery(h.service.require_room(ROOM_A),a,None)
                self.assertEqual(result['state'],'deferred')
                self.assertIs(h.service.workers.worker(THREAD_ONE),a)
                self.assertNotIn(THREAD_ONE,h.service._account_recoveries)
            finally:h.close()

if __name__=='__main__':unittest.main()
