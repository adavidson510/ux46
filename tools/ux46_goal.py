"""Read-only, bounded native goal observations. Never starts a model turn."""
from copy import deepcopy
import threading
import time


class GoalStatus:
    def __init__(self, server, ttl=15.0):
        self.server, self.ttl = server, ttl
        self._lock = threading.Lock()
        self._entries = {}

    def _entry(self, thread_id):
        return self._entries.setdefault(thread_id, {
            'result': {'state': 'unknown', 'goal': None, 'checked_at': None},
            'expires': 0.0, 'pending': False, 'generation': 0})

    def invalidate(self, thread_id):
        with self._lock:
            e = self._entry(thread_id)
            e['generation'] += 1
            e['expires'] = 0.0
            e['result'] = {'state': 'unknown', 'goal': None, 'checked_at': None}

    def observe(self, thread_id, goal):
        """An explicit native read wins over older in-flight background reads."""
        with self._lock:
            e = self._entry(thread_id)
            e['generation'] += 1
            e['result'] = {'state': 'known', 'goal': deepcopy(goal), 'checked_at': time.time()}
            e['expires'] = time.monotonic() + self.ttl

    def read(self, thread_id):
        with self._lock:
            e = self._entry(thread_id)
            if time.monotonic() >= e['expires'] and not e['pending']:
                e['pending'] = True
                threading.Thread(target=self._check, args=(thread_id,e['generation']),
                                 name='ux46-goal-status', daemon=True).start()
            result = deepcopy(e['result'])
        if result['state'] == 'known' and result['checked_at'] and time.time() - result['checked_at'] > self.ttl * 3:
            return {'state': 'unknown', 'goal': None, 'checked_at': result['checked_at']}
        return result

    def _check(self, thread_id, generation):
        result = {'state': 'unknown', 'goal': None, 'checked_at': time.time()}
        try:
            raw = self.server.request('thread/goal/get', {'threadId': thread_id}, timeout=3)
            if isinstance(raw, dict) and 'goal' in raw and (raw['goal'] is None or isinstance(raw['goal'], dict)):
                result.update(state='known', goal=deepcopy(raw['goal']))
        except Exception as error:
            detail = getattr(error, 'detail', None)
            if getattr(error, 'code', None) == -32601 or (isinstance(detail, dict) and detail.get('code') == -32601):
                result['state'] = 'unsupported'
        finally:
            with self._lock:
                e = self._entry(thread_id)
                if e['generation'] == generation:
                    e['result'] = result
                    e['expires'] = time.monotonic() + (300 if result['state'] == 'unsupported' else self.ttl)
                e['pending'] = False
