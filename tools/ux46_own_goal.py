"""Human-directed resume of the calling Codex room's existing goal.

Local owner helper, not a security sandbox. No room/thread selector. Native
permissions still apply. The detached waiter makes no model calls and never
interrupts, attaches, changes budgets, or replays an uncertain mutation.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

from ux46_assistant_handoffs import local_clients

WAITING = 'waiting'


def calling_thread():
    thread = os.environ.get('CODEX_THREAD_ID', '')
    if not thread or len(thread) > 100:
        raise ValueError('Run inside the owning Codex conversation; no current thread was provided')
    return thread


def checked(client, method, path, body=None):
    code, data = client.request(method, path, body)
    if code != 200:
        raise ValueError('The native conversation could not be checked')
    return data


def own_room(client, thread):
    found = checked(client, 'GET', '/api/rooms?complete=1&limit=100&query=' + quote(thread, safe=''))
    if found.get('truncated'):
        raise ValueError('Current conversation identity is ambiguous')
    matches = []
    for row in found.get('rooms', []):
        if row.get('runtime') != 'codex':
            continue
        detail = checked(client, 'GET', '/api/room/' + row['id'] + '?inspect=1')
        if (detail.get('native') or {}).get('thread_id') == thread:
            matches.append(row['id'])
    if len(matches) != 1:
        raise ValueError('The calling conversation must have exactly one available UX46 room')
    return matches[0]


def inspect(client, item):
    base = '/api/room/' + item['room']
    detail = checked(client, 'GET', base + '?inspect=1')
    native = detail.get('native') or {}
    if native.get('thread_id') != item['thread']:
        raise ValueError('This room now points to a different or unavailable conversation')
    if not native.get('worker') or detail.get('ownership', {}).get('held_by_room'):
        raise ValueError('The conversation is no longer connected here')
    history = checked(client, 'GET', base + '/history?limit=100&direction=desc')
    if history.get('unavailable') or history.get('source') == 'unavailable':
        raise ValueError('The human request could not be checked')
    human = next((x for x in history.get('items', []) if x.get('type') == 'userMessage'), None)
    if not human or not human.get('id'):
        raise ValueError('The current human message is unavailable')
    command = checked(client, 'POST', base + '/command',
                      {'command': '/goal', 'thread_id': item['thread']}).get('command', {})
    if command.get('state') != 'ready' or 'goal' not in command.get('native', {}):
        raise ValueError('Native goal status is unavailable')
    return detail, human, command['native']['goal']


def goal_key(goal):
    # Status and counters legitimately change while the requesting turn finishes.
    return {k: goal.get(k) for k in ('objective', 'createdAt', 'tokenBudget')}


class Requests:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'own-goal-resumes.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, thread TEXT, body TEXT)')
        self.path.chmod(0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def latest(self, thread):
        with self.db() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT body FROM requests WHERE thread=?', (thread,))]
        return max(rows, key=lambda x: x['at']) if rows else None

    def get(self, ident, thread):
        with self.db() as db:
            row = db.execute('SELECT body FROM requests WHERE id=? AND thread=?', (ident, thread)).fetchone()
        if not row:
            raise ValueError('No request belongs to this conversation')
        return json.loads(row[0])

    def save(self, item, expected):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM requests WHERE id=?', (item['id'],)).fetchone()
            if not row or json.loads(row[0])['state'] != expected:
                return False
            db.execute('UPDATE requests SET body=? WHERE id=?', (json.dumps(item), item['id']))
        return True

    def request(self, client, thread, request):
        room = own_room(client, thread)
        item = {'room': room, 'thread': thread}
        detail, human, goal = inspect(client, item)
        wording = human.get('text', '').strip()
        # A native userMessage can contain a machine relay. The first slice
        # accepts a direct human request only; a relay is not consent evidence.
        if (not request.strip() or wording != request.strip() or
                wording.startswith(('CONCIERGE UPDATE PACKET', 'Request brief from ', 'Request relayed by '))):
            raise ValueError('Use the current direct human request, quoted exactly; automatic packets and relays cannot resume a goal')
        ident = hashlib.sha256((thread + '\0' + human['id']).encode()).hexdigest()[:40]
        with self.db() as db:
            old = db.execute('SELECT body FROM requests WHERE id=?', (ident,)).fetchone()
        if old:
            return json.loads(old[0])
        if not isinstance(goal, dict) or goal.get('status') not in ('paused', 'blocked', 'active'):
            raise ValueError('Only an existing paused, blocked, or active goal can resume here; completed goals and usage/budget limits are unchanged')
        item.update(id=ident, at=time.time(), checked_at=time.time(), human_id=human['id'],
                    human_hash=hashlib.sha256(wording.encode()).hexdigest(),
                    goal_key=goal_key(goal), goal=goal, state=WAITING,
                    message='Resume requested. Waiting for this reply to finish; the goal has not been changed.')
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?)', (ident, thread, json.dumps(item)))
        return self.get(ident, thread)

    def step(self, client, item):
        if item['state'] != WAITING:
            return item
        try:
            detail, human, goal = inspect(client, item)
            item.update(checked_at=time.time(), goal=goal)
            if human['id'] != item['human_id'] or hashlib.sha256(human.get('text', '').strip().encode()).hexdigest() != item['human_hash']:
                raise ValueError('A newer human message replaced the resume request; nothing was resumed')
            if not isinstance(goal, dict) or goal_key(goal) != item['goal_key']:
                raise ValueError('The saved goal changed; nothing was resumed')
            if goal.get('status') not in ('paused', 'blocked', 'active'):
                raise ValueError('The goal completed or reached a limit; nothing was resumed')
            if time.time() - item['at'] > 1800:
                raise ValueError('Resume expired after 30 minutes; ask again when ready')
            if detail['native'].get('active_turn') or detail.get('approvals'):
                self.save(item, WAITING)
                return item
            item.update(state='dispatching', message='Applying your resume request; confirmation is pending.')
            if not self.save(item, WAITING):
                return self.get(item['id'], item['thread'])
            # Mark before mutation. A crash or lost response is NEVER retried.
            code, result = client.request('POST', '/api/room/' + item['room'] + '/command',
                {'command': '/goal resume', 'thread_id': item['thread'], 'client_id': 'own-goal-' + item['id']})
            command = result.get('command', {})
            error = result.get('error')
            error = error.get('code') if isinstance(error, dict) else error
            if error == 'connection_busy':
                item.update(state=WAITING, message='Resume requested. Waiting for this reply or approval to finish.')
            elif (code == 200 and command.get('state') == 'updated'
                  and (command.get('native', {}).get('goal') or {}).get('status') == 'active'
                  and goal_key(command['native']['goal']) == item['goal_key']):
                item.update(state='resumed', goal=command['native']['goal'],
                            message='Goal resumed; native status confirmed active at the recorded time. Later status may change.')
            else:
                item.update(state='unconfirmed', goal=command.get('native', {}).get('goal'),
                            message='Resume was not confirmed. Check native goal status; this request will not be replayed.')
            self.save(item, 'dispatching')
        except Exception as exc:
            prior = item['state']
            item.update(state='unconfirmed' if prior == 'dispatching' else 'cancelled',
                        message='Native outcome unknown; this request will not be replayed.' if prior == 'dispatching'
                        else str(exc) if isinstance(exc, ValueError) else 'Native checks unavailable; nothing was resumed.')
            self.save(item, prior)
        return self.get(item['id'], item['thread'])

    def cancel(self, thread):
        item = self.latest(thread)
        if item and item['state'] == WAITING:
            item.update(state='cancelled', message='Cancelled before running.')
            self.save(item, WAITING)
        return self.latest(thread)

    def check_waiter(self, item):
        # A detached process can disappear on logout/reboot. Never leave an old
        # receipt pretending there is still a worker, and never replay it.
        if not item or item['state'] != WAITING or time.time() - item['checked_at'] < 90:
            return item
        lock = self.root / ('own-goal-' + item['id'] + '.lock')
        with lock.open('a') as fd:
            lock.chmod(0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return item
            item.update(state='interrupted', message='The resume waiter stopped. Nothing will be retried; ask again or use Resume.')
            self.save(item, WAITING)
        return self.get(item['id'], item['thread'])


def client_for(directory):
    host = json.loads((Path(directory) / 'goal-host.json').read_text())
    agent = host['agent']
    return local_clients(directory, {'target': {'agent': agent}}, host_file='goal-host.json')[agent]


def run_waiter(store, client, thread, ident):
    lock = store.root / ('own-goal-' + ident + '.lock')
    with lock.open('a') as fd:
        lock.chmod(0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            item = store.get(ident, thread)
            if item['state'] != WAITING:
                return
            if store.step(client, item)['state'] != WAITING:
                return
            time.sleep(5)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', required=True)
    p.add_argument('action', choices=['resume', 'status', 'cancel'])
    p.add_argument('--request', default='', help='Exact current human wording, after interpreting it as explicit resume direction')
    p.add_argument('--worker', help=argparse.SUPPRESS)
    a = p.parse_args()
    try:
        thread = calling_thread(); store = Requests(a.directory)
        if a.worker:
            run_waiter(store, client_for(a.directory), thread, a.worker)
            return 0
        if a.action == 'cancel':
            item = store.cancel(thread)
        elif a.action == 'status':
            item = store.check_waiter(store.latest(thread))
            if item:
                try:
                    detail, _, goal = inspect(client_for(a.directory), item)
                    item = dict(item, current_goal=goal, active_turn=detail['native'].get('active_turn'), observed_at=time.time())
                except Exception:
                    item = dict(item, current_goal_unknown=True)
        else:
            item = store.request(client_for(a.directory), thread, a.request)
            if item['state'] == WAITING:
                try:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--directory', str(store.root.resolve()),
                        'resume', '--worker', item['id']], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                except OSError:
                    item.update(state='interrupted', message='Could not start the resume waiter; use Resume or ask again.')
                    store.save(item, WAITING)
        print(json.dumps({'request': item}, ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, ValueError) else 'Own-goal helper unavailable; check installation and status before retrying'})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
