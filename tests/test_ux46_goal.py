import sys,threading,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_goal import GoalStatus
from atlas_native import NativeError

class Server:
    def __init__(self):
        self.calls=[];self.response={'goal':None};self.error=None;self.gate=None
    def request(self,method,params,timeout=30):
        self.calls.append((method,params,timeout))
        response=self.response
        if self.gate:self.gate.wait(2)
        if self.error:raise self.error
        return response

class GoalTests(unittest.TestCase):
    def settle(self,status,tid='t'):
        for _ in range(200):
            with status._lock: pending=status._entry(tid)['pending']
            if not pending:return
            time.sleep(.005)
        self.fail('goal read did not settle')

    def test_known_none_and_goal_cached_without_model_or_attach(self):
        s=Server();g=GoalStatus(s)
        self.assertEqual(g.read('t')['state'],'unknown');self.settle(g)
        self.assertEqual(g.read('t')['state'],'known');self.assertIsNone(g.read('t')['goal'])
        for _ in range(50):g.read('t')
        self.assertEqual(s.calls,[('thread/goal/get',{'threadId':'t'},3)])
        s.response={'goal':{'status':'active','objective':'Ship useful work'}}
        g.invalidate('t');g.read('t');self.settle(g)
        self.assertEqual(g.read('t')['goal'],s.response['goal'])

    def test_failure_and_malformed_never_mean_no_goal(self):
        for response,error,state in [({},None,'unknown'),({'goal':42},None,'unknown'),({},NativeError('private error'),'unknown'),({},NativeError('unknown method',detail={'code':-32601}),'unsupported')]:
            s=Server();s.response=response;s.error=error;g=GoalStatus(s)
            g.read('t');self.settle(g)
            self.assertEqual(g.read('t')['state'],state)
            self.assertNotIn('private',str(g.read('t')))

    def test_new_explicit_observation_wins_over_inflight_response(self):
        s=Server();s.gate=threading.Event();g=GoalStatus(s)
        g.read('t');g.observe('t',{'status':'paused','objective':'Keep me'})
        s.gate.set();self.settle(g)
        self.assertEqual(g.read('t')['goal']['status'],'paused')
        self.assertEqual(len(s.calls),1)

    def test_slow_reader_single_flight_and_thread_isolation(self):
        s=Server();s.gate=threading.Event();g=GoalStatus(s)
        g.observe('other',{'status':'blocked'})
        for _ in range(20): self.assertEqual(g.read('t')['state'],'unknown')
        self.assertEqual(g.read('other')['goal']['status'],'blocked')
        s.gate.set();self.settle(g)
        self.assertEqual(len(s.calls),1)

class GoalResumeTests(unittest.TestCase):
    def test_already_active_running_goal_is_read_not_mutated(self):
        from atlas_native import NativeSessions
        from unittest.mock import Mock
        server=Server();server.response={'goal':{'status':'active','objective':'Keep working'}}
        sessions=NativeSessions(server);sessions.require_control=Mock()
        sessions.note_turn('t','running-turn',True)
        result=sessions.set_goal_status('t','active')
        self.assertEqual(result['status'],'active')
        self.assertEqual([x[0] for x in server.calls],['thread/goal/get'])
        self.assertEqual(sessions.active_turn('t'),'running-turn')

    def test_running_turn_with_paused_goal_keeps_existing_admission(self):
        from atlas_native import NativeSessions
        from unittest.mock import Mock
        server=Server();server.response={'goal':{'status':'paused'}}
        sessions=NativeSessions(server);sessions.require_control=Mock()
        sessions.note_turn('t','running-turn',True)
        with self.assertRaises(NativeError):sessions.set_goal_status('t','active')
        self.assertEqual([x[0] for x in server.calls],['thread/goal/get'])

class GoalRoomTests(unittest.TestCase):
    def test_room_poll_observes_goal_without_taking_writer_or_sending(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_atlas_session_workers import WorkerHarness, ROOM_A
        h=WorkerHarness()
        try:
            for _ in range(40):
                status,room=h.call('GET','/api/room/'+ROOM_A)
                self.assertEqual(status,200)
                if room.get('goal_status',{}).get('state')=='known':break
                time.sleep(.025)
            self.assertEqual(room['goal_status']['state'],'known')
            self.assertEqual(room['goal_status']['goal']['status'],'usageLimited')
            self.assertFalse(h.service.workers.attached())
            self.assertEqual(h.service.journal.recent(),[])
        finally:h.close()

if __name__=='__main__':unittest.main()
