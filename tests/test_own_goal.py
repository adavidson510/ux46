import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from ux46_own_goal import Requests, calling_thread


class Client:
    def __init__(self):
        self.human = {'id': 'human-1', 'type': 'userMessage', 'text': 'Resume your saved goal'}
        self.goal = {'objective': 'Controlled test only', 'createdAt': 1, 'tokenBudget': 1000, 'tokensUsed': 20, 'status': 'blocked'}
        self.thread = 'own-thread'; self.active = 'request-turn'; self.approvals = []
        self.posts = []; self.mode = ''; self.ambiguous = False; self.connected = True
    def request(self, method, path, body=None):
        if path.startswith('/api/rooms'):
            rows = [{'id': 'example/own', 'runtime': 'codex'}]
            if self.ambiguous: rows.append({'id': 'example/alias', 'runtime': 'codex'})
            return 200, {'rooms': rows}
        if '/history?' in path: return 200, {'items': [dict(self.human)]}
        if body:
            self.posts.append(copy.deepcopy(body))
            if body['command'] == '/goal resume':
                if self.mode == 'timeout':
                    self.goal['status'] = 'active'; raise TimeoutError()
                if self.mode == 'busy': return 409, {'error': 'connection_busy'}
                if self.mode != 'unconfirmed': self.goal['status'] = 'active'
                return 200, {'command': {'state': 'updated' if self.mode != 'unconfirmed' else 'unconfirmed', 'native': {'goal': copy.deepcopy(self.goal)}}}
            return 200, {'command': {'state': 'ready', 'native': {'goal': copy.deepcopy(self.goal)}}}
        return 200, {'native': {'thread_id': self.thread, 'active_turn': self.active, 'worker': {'state': 'ready'} if self.connected else None}, 'approvals': self.approvals}
    @property
    def resumes(self): return [x for x in self.posts if x['command'] == '/goal resume']


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Requests(self.tmp.name); self.client = Client()
    def request(self): return self.store.request(self.client, 'own-thread', self.client.human['text'])
    def test_wait_then_resume_same_goal_once_with_native_readback(self):
        item = self.request()
        self.assertEqual(self.store.step(self.client, item)['state'], 'waiting')
        self.assertFalse(self.client.resumes)
        self.client.active = ''
        done = self.store.step(self.client, self.store.latest('own-thread'))
        self.assertEqual(done['state'], 'resumed'); self.assertEqual(done['goal']['status'], 'active')
        self.assertEqual(done['goal']['tokensUsed'], 20); self.assertEqual(done['goal']['tokenBudget'], 1000)
        self.assertEqual(len(self.client.resumes), 1)
        self.assertEqual(self.client.resumes[0]['thread_id'], 'own-thread')
        self.assertEqual(self.request()['state'], 'resumed')
        self.store.step(self.client, done); self.assertEqual(len(self.client.resumes), 1)
    def test_already_active_running_goal_needs_no_waiter_or_mutation(self):
        self.client.goal['status'] = 'active'
        self.assertEqual(self.request()['state'], 'resumed')
        self.assertFalse(self.client.resumes)

    def test_no_current_thread_or_other_thread_cannot_target_room(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): calling_thread()
        with self.assertRaises(ValueError): self.store.request(self.client, 'other-thread', self.client.human['text'])
        self.assertFalse(self.client.resumes)
    def test_duplicate_native_ownership_rejected(self):
        self.client.ambiguous = True
        with self.assertRaises(ValueError): self.request()
    def test_stale_quote_and_automatic_or_relay_messages_rejected(self):
        with self.assertRaises(ValueError): self.store.request(self.client, 'own-thread', 'different message')
        for text in ('CONCIERGE UPDATE PACKET resume', 'Request brief from Assistant resume', 'Request relayed by Assistant resume'):
            self.client.human['text'] = text
            with self.assertRaises(ValueError): self.request()
        self.assertFalse(self.client.resumes)
    def test_changed_message_goal_thread_or_connection_cancels_wait(self):
        for change in ('human', 'goal', 'thread', 'connection'):
            with self.subTest(change=change):
                self.client = Client(); self.client.human['id'] = change
                item = self.request(); self.client.active = ''
                if change == 'human': self.client.human.update(id='new-message', text='Do not resume')
                if change == 'goal': self.client.goal['createdAt'] = 2
                if change == 'thread': self.client.thread = 'new-thread'
                if change == 'connection': self.client.connected = False
                self.assertEqual(self.store.step(self.client, item)['state'], 'cancelled')
                self.assertFalse(self.client.resumes)
    def test_complete_missing_and_limits_never_reset(self):
        for state in ('complete', 'budget_limited', 'usageLimited', 'unknown'):
            self.client.goal['status'] = state
            with self.assertRaises(ValueError): self.request()
        self.client.goal = None
        with self.assertRaises(ValueError): self.request()
        self.assertFalse(self.client.resumes)
    def test_approval_waits_and_cancel_wins_over_stale_snapshot(self):
        item = self.request(); self.client.active = ''; self.client.approvals = [{'id': 'pending'}]
        self.assertEqual(self.store.step(self.client, item)['state'], 'waiting')
        self.client.approvals = []; self.store.cancel('own-thread')
        self.assertEqual(self.store.step(self.client, item)['state'], 'cancelled')
        self.assertFalse(self.client.resumes)
    def test_unknown_dispatch_never_replays_even_after_restart(self):
        item = self.request(); self.client.active = ''; self.client.mode = 'timeout'
        self.assertEqual(self.store.step(self.client, item)['state'], 'unconfirmed')
        self.store = Requests(self.tmp.name)
        self.assertEqual(self.request()['state'], 'unconfirmed'); self.assertEqual(len(self.client.resumes), 1)
    def test_native_refusal_is_not_reported_as_active(self):
        item = self.request(); self.client.active = ''; self.client.mode = 'unconfirmed'
        result = self.store.step(self.client, item)
        self.assertEqual(result['state'], 'unconfirmed'); self.assertEqual(result['goal']['status'], 'blocked')
    def test_definite_busy_can_wait_and_try_once_idle(self):
        item = self.request(); self.client.active = ''; self.client.mode = 'busy'
        item = self.store.step(self.client, item); self.assertEqual(item['state'], 'waiting')
        self.client.mode = ''
        self.assertEqual(self.store.step(self.client, item)['state'], 'resumed')
    def test_stopped_waiter_is_not_reported_as_still_waiting(self):
        item = self.request(); item['checked_at'] -= 91
        self.store.save(item, 'waiting')
        self.assertEqual(self.store.check_waiter(item)['state'], 'interrupted')
        self.assertFalse(self.client.resumes)

    def test_expired_and_newly_complete_never_resume(self):
        item = self.request(); item['at'] -= 1801; self.client.active = ''
        self.assertEqual(self.store.step(self.client, item)['state'], 'cancelled'); self.assertFalse(self.client.resumes)
        self.client.human['id'] = 'new'; item = self.request(); self.client.goal['status'] = 'complete'
        self.assertEqual(self.store.step(self.client, item)['state'], 'cancelled'); self.assertFalse(self.client.resumes)


class ConsoleJourney(unittest.TestCase):
    def test_controlled_goal_through_http_and_native_protocol(self):
        from test_atlas_session_workers import WorkerHarness, ROOM_A, ROOM_B
        from test_atlas_console import THREAD_ONE, THREAD_TWO
        h = WorkerHarness()
        try:
            self.assertEqual(h.attach(ROOM_A)[0], 200)
            self.assertEqual(h.attach(ROOM_B)[0], 200)
            worker = h.claim_lock(THREAD_ONE)
            other = h.claim_lock(THREAD_TWO)
            other_pids = tuple(other.server.pids)
            # Only the disposable protocol fixture receives this test state.
            worker.server.request('thread/goal/set', {'threadId': THREAD_ONE, 'status': 'blocked'})
            code, history = h.call('GET', '/api/room/' + ROOM_A + '/history?limit=100&direction=desc')
            human = next(x for x in history['items'] if x['type'] == 'userMessage')
            class HTTPClient:
                def request(self, method, path, body=None):
                    return h.call(method, path, body=body)
            client = HTTPClient(); store = Requests(h.tmp / 'own-goal-test')
            item = store.request(client, THREAD_ONE, human['text'])
            self.assertEqual(item['goal']['status'], 'blocked')
            worker.sessions.note_turn(THREAD_ONE, 'controlled-running-turn', True)
            self.assertEqual(store.step(client, item)['state'], 'waiting')
            worker.sessions.note_turn(THREAD_ONE, 'controlled-running-turn', False)
            result = store.step(client, store.latest(THREAD_ONE))
            self.assertEqual(result['state'], 'resumed', result)
            self.assertEqual(worker.sessions.goal(THREAD_ONE)['status'], 'active')
            self.assertIsNone(other.sessions.goal(THREAD_TWO))
            self.assertEqual(tuple(other.server.pids), other_pids)
            self.assertTrue(other.running)
        finally:
            h.close()


if __name__ == '__main__': unittest.main()
