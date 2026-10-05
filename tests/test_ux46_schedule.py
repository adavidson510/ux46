import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_schedule import ScheduleStore
from constellation_store import Conflict

class Reminders(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ScheduleStore(self.temp.name)
        self.store.register(dict(id='review',kind='reminder',title='Review',counter='constellation.applied_uses',baseline=1,uses=10,deadline=2000,timezone='America/Los_Angeles',owner='Local'))
    def test_whichever_first_survives_restart_and_counter_loss(self):
        self.assertEqual(self.store.tick(uses=10,now=1000)['due'],0)
        self.assertEqual(self.store.tick(uses=11,now=1001)['due'],1)
        reopened=ScheduleStore(self.temp.name)
        self.assertEqual(reopened.tick(uses=2,now=1002)['due'],1)
        self.assertEqual(reopened.view(1002)['items'][0]['due_reason'],'uses')
    def test_date_still_surfaces_with_down_checker(self):
        self.assertEqual(self.store.view(1999)['due'],0)
        view=self.store.view(2000)
        self.assertEqual(view['due'],1);self.assertTrue(view['checker_stale'])
        self.assertEqual(self.store.tick(now=2000,error='offline')['items'][0]['due_reason'],'date')
    def test_acknowledgement_is_explicit_and_revision_checked(self):
        due=self.store.tick(uses=11,now=1000)['items'][0]
        with self.assertRaises(Conflict):self.store.action(dict(id='review',revision=1,action='complete'))
        self.store.action(dict(id='review',revision=due['revision'],action='complete'))
        self.assertEqual(self.store.tick(uses=12,now=3000)['due'],0)
    def test_jobs_cannot_be_executed_by_actions(self):
        self.store.register(dict(id='job',kind='job',title='Email',owner='Local'))
        with self.assertRaises(ValueError):self.store.action(dict(id='job',revision=1,action='complete'))
        with self.assertRaises(ValueError):self.store.action(dict(id='job',revision=1,action='execute'))

class Malformed(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ScheduleStore(self.temp.name)
        self.good=dict(id='good',kind='reminder',title='Review',counter='constellation.applied_uses',baseline=1,uses=10,deadline=2000)
        self.store.register(self.good)
    def test_register_refuses_a_reminder_without_its_shape(self):
        for broken in (dict(id='r1',kind='reminder'),dict(self.good,id='r2',deadline='soon'),dict(self.good,id='r3',uses=0),
                       dict(self.good,id='r4',baseline=-1),dict({k:v for k,v in self.good.items() if k!='baseline'},id='r8'),dict(self.good,id='r5',title=''),dict(self.good,id='r6',uses=True),
                       dict(self.good,id='r7',deadline=float('nan')),dict(self.good,id=''),dict(self.good,kind='other'),['not','a','task']):
            with self.subTest(broken):
                with self.assertRaises(ValueError):self.store.register(broken)
        self.assertEqual([t['id'] for t in self.store.view(0)['items']],['good'])
        self.store.register(dict(id='dated',kind='reminder',title='Date only',uses=10,deadline=3000))
    def test_a_malformed_stored_record_is_skipped_and_reported(self):
        # Rows written before validation existed, or edited on disk, must not break the schedule.
        with self.store.db() as db:
            db.execute('INSERT INTO tasks VALUES (?,?)',('legacy',json.dumps({'id':'legacy','kind':'reminder','state':'scheduled','revision':1,'created_at':1})))
            db.execute('INSERT INTO tasks VALUES (?,?)',('garbled','{not json'))
        view=self.store.view(2500)
        self.assertEqual([t['id'] for t in view['items']],['good'])
        self.assertEqual(view['due'],1)
        self.assertEqual({i['id'] for i in view['invalid']},{'legacy','garbled'})
        ticked=self.store.tick(uses=50,now=1000)
        self.assertEqual(ticked['due'],1);self.assertEqual(len(ticked['invalid']),2)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT body FROM tasks WHERE id='garbled'").fetchone()[0],'{not json')

class Collection(unittest.TestCase):
    def test_desktops_only_safe_deduplicated_rooms(self):
        from ux46_usage_collect import desktop_rooms
        state={'sharedLayout':{'tabs':[{'agent':'local','room':'a/b'},{'room':'../../etc'}]},'desktops':[{'tabs':[{'agent':'local','room':'a/b'}]}],
            'liveDesktops':[{'layout':{'tabs':[{'agent':'agent4','room':'c/d'}]}}]}
        self.assertEqual(desktop_rooms(state),[('agent4','c/d'),('local','a/b')])

if __name__=='__main__':unittest.main()
