"""Recovery control uses isolated journals, fake adapters and fake managers."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import ux46_recovery as recovery
from ux46_local import initialize
from atlas_journal import Journal


class Agent:
    def __init__(self, *, busy=False, unknown=False, stopped=False):
        self.calls = []; self.busy = busy; self.unknown = unknown; self.stopped = stopped

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/api/bootstrap':
            return 200, {'remembered_owned': [{'room': 'fixture/alias', 'thread_id': 'thread-1'}, {'room': 'fixture/canonical', 'thread_id': 'thread-1'}]}
        if method == 'GET':
            return 200, {'native': {'thread_id': 'thread-1', 'active_turn': 'turn-1' if self.busy else None,
                'worker': {'thread_id': 'thread-1', 'room': 'fixture/canonical', 'running': not self.stopped}},
                'submissions': [], 'approvals': [], 'goal_status': {'state':'known', 'goal':None}}
        if self.unknown and body: raise OSError('Response lost after mutation')
        if path.endswith('/continue'):
            return 200, {'room': {'ownership': {'state': 'atlas_owned'}}}
        return 200, {'connection': {'state': 'refreshed'}}


class Manager:
    def __init__(self, fail=''): self.calls = []; self.fail = fail
    def stop(self, service):
        self.calls.append(('stop', service['id']))
        return 'stop_unknown' if service['id'] == self.fail else 'stopped'
    def start(self, service):
        self.calls.append(('start', service['id']))
        return 'ready'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        initialize(self.root); self.agent = Agent(); self.manager = Manager()
        self.coordinator = recovery.Coordinator(self.root, manager_factory=lambda *args: self.manager,
            client_factory=lambda *args: self.agent)
    def tearDown(self): self.temp.cleanup()

    def plan(self):
        plan = recovery.installation(self.root)
        plan['services'] = [{'id': 'console', 'depends_on': []}, {'id': 'front', 'depends_on': ['console']}]
        return plan

    def test_scoped_refresh_deduplicates_aliases_and_uses_canonical_owner(self):
        job = self.coordinator.execute('agent', 'local', 'scoped_fixture_1')
        self.assertEqual(job['state'], 'complete')
        writes = [call for call in self.agent.calls if call[0] == 'POST']
        self.assertEqual(writes, [('POST', '/api/connection/refresh', {'room': 'fixture/canonical'}), ('POST', '/api/connection/refresh', {})])
        self.assertEqual(self.manager.calls, [])

    def test_busy_result_is_deferred_without_a_retry_loop(self):
        self.agent.busy = True
        job = self.coordinator.execute('agent', 'local', 'scoped_busy_123')
        self.assertEqual(job['state'], 'partial')
        self.assertEqual(job['connections'][0]['state'], 'deferred')
        self.assertFalse(any(body for method, path, body in self.agent.calls if method == 'POST'))

    def test_unknown_refresh_and_duplicate_request_are_never_replayed(self):
        self.agent.unknown = True
        one = self.coordinator.execute('agent', 'local', 'unknown_fixture_1')
        calls = self.agent.calls[:]
        two = self.coordinator.execute('agent', 'local', 'unknown_fixture_1')
        self.assertEqual(one, two); self.assertEqual(calls, self.agent.calls)
        self.assertEqual(one['connections'][0]['state'], 'unknown')
        with self.assertRaises(ValueError): self.coordinator.execute('all', request_id='unknown_fixture_1')

    def test_full_restart_follows_dependencies_and_preserves_delivery(self):
        journal = Journal(self.root/'state/atlas-console.sqlite3')
        journal.enqueue('queued_fixture_1','fixture/canonical','thread-1','unsent words',[],0)
        journal.enqueue('unknown_fixture_2','fixture/canonical','thread-1','do not repeat',[],0)
        journal.queue_mark('unknown_fixture_2', 'uncertain')
        journal.settle('unknown_fixture_2', 'uncertain')
        journal.save_draft('fixture/canonical','keep this draft',0)
        self.agent.stopped = True
        with patch.object(recovery, 'installation', return_value=self.plan()):
            job = self.coordinator.execute('all', request_id='full_fixture_123')
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(self.manager.calls, [('stop','front'),('stop','console'),('start','console'),('start','front')])
        self.assertEqual(journal.queue_get('queued_fixture_1').status, 'editing')
        self.assertEqual(journal.queue_get('unknown_fixture_2').status, 'uncertain')
        self.assertEqual(journal.get('unknown_fixture_2').status, 'uncertain')
        self.assertEqual(journal.draft('fixture/canonical')['body'], 'keep this draft')
        self.assertEqual(journal.queue_due(time.time()+100), [])
        self.assertFalse(recovery.Gate(self.root).paused())
        self.assertFalse(any('/submit' in path or '/command' in path for _, path, _ in self.agent.calls))
        journal.close()

    def test_uncertain_stop_is_partial_and_is_not_repeated_or_started(self):
        self.manager.fail = 'console'
        with patch.object(recovery, 'installation', return_value=self.plan()):
            job = self.coordinator.execute('all', request_id='partial_fixture_1')
        self.assertEqual(job['state'], 'partial')
        self.assertFalse(any(action == 'start' for action, _ in self.manager.calls))
        calls = self.manager.calls[:]
        self.coordinator.execute('all', request_id='partial_fixture_1')
        self.assertEqual(self.manager.calls, calls)

    def test_dead_coordinator_receipt_is_interrupted_not_replayed(self):
        recovery.write_json(self.root/'control/recovery-old_fixture_id.json', {'id':'old_fixture_id','mode':'all','agent':None,'state':'running'})
        result = self.coordinator.execute('all', request_id='old_fixture_id')
        self.assertEqual(result['state'], 'partial'); self.assertEqual(self.manager.calls, [])

    def test_two_installations_have_separate_barriers_and_receipts(self):
        with tempfile.TemporaryDirectory() as second:
            other = recovery.Gate(second)
            gate = recovery.Gate(self.root); gate.pause('fixture_request', ['local'], time.monotonic()+1)
            with self.assertRaises(recovery.RecoveryBusy):
                with gate.admit(): pass
            with gate.admit('another-agent'): pass
            with other.admit(): pass
            gate.resume('fixture_request')
            self.assertFalse(other.paused())

    def test_pause_waits_for_an_existing_send_and_refuses_new_admission(self):
        gate = recovery.Gate(self.root); entered = threading.Event(); release = threading.Event()
        def sending():
            with gate.admit(): entered.set(); release.wait(3)
        worker = threading.Thread(target=sending); worker.start(); entered.wait(1)
        try:
            with self.assertRaises(recovery.RecoveryBusy): gate.pause('fixture_wait', ['local'], time.monotonic()+.05)
            self.assertTrue(gate.paused())
        finally:
            release.set(); worker.join(1); gate.resume('fixture_wait')

    def test_unknown_agent_and_unregistered_services_do_not_create_targets(self):
        with self.assertRaises(ValueError): self.coordinator.execute('agent', 'unregistered', 'bad_target_fixture')
        result = self.coordinator.execute('all', request_id='no_service_fixture')
        self.assertEqual(result['state'], 'partial'); self.assertEqual(self.manager.calls, [])
        self.assertEqual(result['services'][0]['state'], 'unsupported')

    def test_macos_process_identity_ignores_running_sleeping_state(self):
        from types import SimpleNamespace
        def result(state):
            return SimpleNamespace(returncode=0,stdout=f'{os.getuid()} Sun Sep 20 08:00:00 2026 {state} python fixture.py')
        with patch.object(recovery.sys,'platform','darwin'), patch.object(recovery.subprocess,'run',side_effect=[result('S'),result('R'),result('Z')]):
            sleeping = recovery.process_identity(123)
            self.assertEqual(sleeping,recovery.process_identity(123))
            self.assertIsNone(recovery.process_identity(123))

    def test_process_birth_change_is_never_signalled(self):
        service = {'manager':'process','pid':123,'identity':'old'}
        manager = recovery.ServiceManager(self.root, time.monotonic()+1)
        with patch.object(recovery,'process_identity',return_value='new'), patch.object(os,'kill') as kill:
            self.assertEqual(manager.stop(service),'ownership_changed')
            kill.assert_not_called()

    def test_duplicate_before_first_receipt_cannot_overwrite_running_request(self):
        entered = threading.Event(); release = threading.Event(); outcome = []
        original = recovery.installation
        def plan(root):
            entered.set(); release.wait(2); return original(root)
        with patch.object(recovery,'installation',side_effect=plan):
            worker = threading.Thread(target=lambda: outcome.append(self.coordinator.execute('agent','local','duplicate_early_1')))
            worker.start(); self.assertTrue(entered.wait(1))
            try:
                duplicate = self.coordinator.execute('agent','local','duplicate_early_1')
                self.assertEqual(duplicate['state'],'starting')
                self.assertIsNone(self.coordinator.receipt('duplicate_early_1'))
            finally: release.set(); worker.join(3)
        self.assertEqual(outcome[0]['state'],'complete')
        self.assertEqual(sum(path == '/api/connection/refresh' for method,path,_ in self.agent.calls if method == 'POST'),2)

    def test_overlapping_request_has_its_own_persisted_declined_receipt(self):
        recovery.write_json(self.root/'control/latest.json', {'id':'active_fixture_1'})
        recovery.write_json(self.root/'control/recovery-active_fixture_1.json', {'id':'active_fixture_1','mode':'all','agent':None,'state':'running'})
        with (self.root/'control/recovery.lock').open('a') as lock:
            recovery.fcntl.flock(lock, recovery.fcntl.LOCK_EX)
            job = self.coordinator.execute('agent','local','overlap_fixture_1')
            self.assertEqual(job['id'],'overlap_fixture_1')
            self.assertEqual(job['state'],'partial')
            self.assertEqual(job['active_request'],'active_fixture_1')
            self.assertEqual(self.coordinator.receipt(job['id']),job)
            self.assertEqual(self.coordinator.receipt()['id'],'active_fixture_1')
            with self.assertRaises(ValueError): self.coordinator.execute('all',request_id=job['id'])
        self.assertEqual(self.coordinator.execute('agent','local',job['id']),job)
        self.assertEqual(self.agent.calls,[])

    def test_goal_known_absent_reconnects_but_unknown_or_active_does_not(self):
        for goal, allowed in [({'state':'known','goal':None},True),
                              ({'state':'unknown','goal':None},False),
                              ({'state':'known','goal':{'status':'active'}},False)]:
            agent = Agent(stopped=True)
            original = agent.request
            def request(method,path,body=None):
                status, result = original(method,path,body)
                if path.startswith('/api/room/') and method == 'GET': result['goal_status'] = goal
                return status, result
            agent.request = request
            recovery.refresh_agent({'id':'local','runtime':'codex','endpoint':{'port':8877}},agent,reconnect=True)
            self.assertEqual(any(path.endswith('/continue') for _,path,_ in agent.calls),allowed)

    def test_remote_requires_preparation_before_any_refresh(self):
        plan = self.plan(); plan['agents'] = [{'id':'remote','runtime':'codex','endpoint':{'port':8878},'local':False}]
        plan['services'] = [{'id':'remote-service','agents':['remote'],'depends_on':[]}]
        with patch.object(recovery,'installation',return_value=plan):
            job = self.coordinator.execute('all',request_id='remote_missing_1')
        self.assertEqual(job['state'],'partial')
        self.assertEqual(self.manager.calls,[])
        self.assertFalse(any(method == 'POST' for method,_,_ in self.agent.calls))
        self.assertEqual(job['services'][0]['state'],'admission_unavailable')

    def test_remote_preparation_is_finished_even_when_refresh_is_unknown(self):
        plan = self.plan(); plan['agents'] = [{'id':'remote','runtime':'codex','endpoint':{'port':8878},'local':False}]
        original = self.agent.request
        def request(method,path,body=None):
            if path in {'/api/recovery/prepare','/api/recovery/finish'}:
                self.agent.calls.append((method,path,body)); return 200, {'prepared':True,'resumed':True,'held_messages':2}
            status, result = original(method,path,body)
            if path == '/api/bootstrap': result['recovery'] = {'external_preparation':True}
            return status, result
        self.agent.request = request; self.agent.unknown = True
        with patch.object(recovery,'installation',return_value=plan):
            job = self.coordinator.execute('agent','remote','remote_unknown_1')
        self.assertEqual(job['state'],'partial'); self.assertEqual(job['held_messages'],2)
        writes = [path for method,path,_ in self.agent.calls if method == 'POST']
        self.assertEqual(writes[0],'/api/recovery/prepare'); self.assertEqual(writes[-1],'/api/recovery/finish')
        self.assertEqual(self.manager.calls,[])

    def test_prepare_barrier_rejects_other_requests_and_wrong_finish(self):
        status, _ = recovery.api(self.root,'POST','/api/recovery/prepare',{'request_id':'prepared_fixture_1'})
        self.assertEqual(status,200)
        with self.assertRaises(recovery.RecoveryBusy):
            recovery.api(self.root,'POST','/api/recovery/prepare',{'request_id':'prepared_fixture_2'})
        recovery.api(self.root,'POST','/api/recovery/finish',{'request_id':'prepared_fixture_2'})
        self.assertTrue(recovery.Gate(self.root).paused())
        recovery.api(self.root,'POST','/api/recovery/finish',{'request_id':'prepared_fixture_1'})
        self.assertFalse(recovery.Gate(self.root).paused())

    def test_dependency_cycle_is_refused_before_action(self):
        definition = self.root/'unit.service'; definition.write_text('[Service]\nExecStart=/bin/true\n')
        recovery.write_json(self.root/'recovery.json',{'schema_version':1,'services':[
            {'id':'one','manager':'systemd','target':'one.service','definition':str(definition),'depends_on':['two'],'agents':[]},
            {'id':'two','manager':'systemd','target':'two.service','definition':str(definition),'depends_on':['one'],'agents':[]}], 'agents':[]})
        with self.assertRaises(ValueError): self.coordinator.execute('all',request_id='cycle_fixture_1')
        self.assertEqual(self.manager.calls,[])


if __name__ == '__main__': unittest.main()
