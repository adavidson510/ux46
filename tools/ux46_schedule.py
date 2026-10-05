"""Persisted reminders and observed jobs. No arbitrary commands or model turns."""
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from constellation_store import Conflict


def _number(value):
    return type(value) in (int, float) and value == value and abs(value) != float('inf')


def task_problem(task, *, stored=False):
    """Return why a task record is unusable, or None.

    register() refuses a bad shape up front; view() and tick() use the same
    check to skip and report a stored record (written before this check, or
    edited on disk) instead of letting one bad row break the whole schedule.
    """
    if not isinstance(task, dict): return 'not an object'
    tid = task.get('id')
    if not isinstance(tid, str) or not tid or len(tid) > 100: return 'invalid id'
    if task.get('kind') not in ('reminder', 'job'): return 'unknown kind'
    title = task.get('title')
    if title is not None and (not isinstance(title, str) or len(title) > 200): return 'invalid title'
    if task['kind'] == 'reminder':
        if not isinstance(title, str) or not title.strip(): return 'reminder needs a title'
        if not _number(task.get('deadline')): return 'reminder needs a numeric deadline'
        if type(task.get('uses')) is not int or task['uses'] < 1: return 'reminder needs uses >= 1'
        # A counted reminder measures progress from its baseline; a date-only one may omit it.
        if (task.get('counter') is not None or 'baseline' in task) and (
                type(task.get('baseline')) is not int or task['baseline'] < 0): return 'reminder needs baseline >= 0'
        if 'progress' in task and not _number(task['progress']): return 'invalid progress'
        if 'counter' in task and not isinstance(task['counter'], str): return 'invalid counter'
    if stored:
        if type(task.get('revision')) is not int or not _number(task.get('created_at')): return 'invalid revision'
        if not isinstance(task.get('state'), str): return 'invalid state'
    return None


class ScheduleStore:
    def __init__(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'schedule.sqlite3'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, body TEXT NOT NULL);''')
        os.chmod(self.path, 0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    def register(self, task):
        problem = task_problem(task)
        if problem: raise ValueError('Invalid task: ' + problem)
        with self.db() as db:
            if db.execute('SELECT 1 FROM tasks WHERE id=?', (task['id'],)).fetchone():
                raise Conflict('Task already registered')
            if db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] >= 100:
                raise ValueError('Review the task list before adding more')
            task = dict(task, revision=1, created_at=time.time(), state='scheduled')
            db.execute('INSERT INTO tasks VALUES (?,?)', (task['id'], json.dumps(task)))
        return task

    def action(self, args):
        if args['action'] not in ('complete', 'cancel', 'reopen'):
            raise ValueError('Unknown action')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (args['id'],)).fetchone()
            if not row: raise ValueError('Task unavailable')
            task = json.loads(row[0])
            if task['kind'] != 'reminder': raise ValueError('Recurring jobs are managed by their owner')
            if args['revision'] != task['revision']: raise Conflict('Task changed; refresh before saving')
            task.update(state={'complete':'complete', 'cancel':'cancelled', 'reopen':'scheduled'}[args['action']],
                        revision=task['revision']+1, changed_at=time.time())
            if args['action'] == 'reopen': task.pop('due_at', None)
            db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(task), task['id']))
        return task

    def tick(self, *, uses=None, probes=None, now=None, error=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for tid, raw in db.execute('SELECT id,body FROM tasks').fetchall():
                task = _load(raw)
                if task_problem(task, stored=True): continue  # Left untouched; view() reports it.
                if task['kind'] == 'reminder' and task['state'] in ('scheduled', 'due'):
                    if uses is not None and task.get('counter') == 'constellation.applied_uses':
                        # High-water mark: a later feedback revision must not undo a due reminder.
                        task['progress'] = max(task.get('progress', 0), max(0, uses-task['baseline']))
                        task['counter_checked_at'] = now
                    task['counter_error'] = error
                    if task['state'] != 'due' and (now >= task['deadline'] or task.get('progress', 0) >= task['uses']):
                        task.update(state='due', due_at=now, due_reason='uses' if task.get('progress',0)>=task['uses'] else 'date')
                elif task['kind'] == 'job' and probes and tid in probes:
                    task.update(probes[tid], checked_at=now)
                if json.dumps(task) != raw:
                    task['revision'] += 1
                    db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(task), tid))
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('tick', json.dumps({'at':now,'error':error})))
        return self.view(now)

    def view(self, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            rows = db.execute('SELECT id,body FROM tasks').fetchall()
            row = db.execute("SELECT body FROM metadata WHERE key='tick'").fetchone()
        tick = _load(row[0]) if row else None
        if not isinstance(tick, dict) or not _number(tick.get('at')): tick = {}
        tasks, invalid = [], []
        for tid, raw in rows:
            task = _load(raw); problem = task_problem(task, stored=True)
            if problem: invalid.append({'id': tid, 'problem': problem})
            else: tasks.append(task)
        for task in tasks:
            if task['kind'] == 'reminder' and task['state'] == 'scheduled' and now >= task['deadline']:
                task.update(state='due', due_reason='date')  # Time deadlines still surface if checker is down.
            if task['kind'] == 'job':
                task['stale'] = not _number(task.get('checked_at')) or now-task['checked_at'] > 900
        return {'items':sorted(tasks,key=lambda t:(t['state']!='due',t['kind']!='reminder',t['created_at'])),
                'due':sum(t['state']=='due' for t in tasks), 'invalid':invalid, 'checked_at':tick.get('at'),
                'checker_stale':bool(tasks) and (not tick.get('at') or now-tick['at']>900), 'model_calls':0,
                'coverage':'Registered UX46 tasks only; operating-system and other agents’ schedules are not inventoried.'}


def _load(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def observe(directory, client_config, replica_directory=None):
    from constellation_relay import Client
    from ux46_email import EmailStore
    now = time.time(); probes = {}; uses = None; problem = None
    try:
        response = Client(json.loads(Path(client_config).read_text())).request('health', {})
        if response.get('offline'): raise ValueError('Fresh learning count unavailable')
        health = response['result']
        uses = health.get('applied_uses')
        if type(uses) is not int: raise ValueError('Learning counter unavailable')
        backup = health.get('last_backup') or {}
        probes['constellation-backup'] = {'state':'observed' if backup.get('verified') else 'unknown',
            'last_success':backup.get('at'), 'observation':'Last verified snapshot; unchanged data may skip a new snapshot.'}
    except Exception:
        problem = 'Learning counter unavailable; the date reminder remains active.'
        probes['constellation-backup'] = {'state':'unknown', 'observation':'Server snapshot status unavailable.'}
    try:
        accounts = EmailStore(Path(directory)/'email.sqlite3').view()['accounts']
        healthy = bool(accounts) and all(a['status']=='healthy' and not a['stale'] for a in accounts)
        probes['email-check'] = {'state':'observed' if healthy else 'needs-check',
            'last_success':min((a.get('last_success',0) for a in accounts),default=None),
            'observation':str(len(accounts))+' connected accounts · '+('recent checks healthy' if healthy else 'coverage needs a check')}
    except Exception:
        probes['email-check'] = {'state':'unknown','observation':'Email check receipts unavailable.'}
    if replica_directory:
        try:
            root=Path(replica_directory)
            receipts=list(root.glob('*.receipt.json'))
            if len(receipts)>30: raise ValueError('Bound exceeded')
            # A replica's presence is evidence of a copy, never evidence that its timer is running.
            latest=max((p.stat().st_mtime for p in receipts),default=None)
            probes['constellation-replica']={'state':'observed' if latest else 'unknown','last_success':latest,
                'observation':str(len(receipts))+' local snapshot receipts · timer execution is not inferred from files.'}
        except Exception:
            probes['constellation-replica']={'state':'unknown','observation':'Local snapshot receipts unavailable.'}
    probes['task-check']={'state':'observed','last_success':now,'observation':'Reminder conditions checked without model calls.'}
    try:
        usage=json.loads((Path(directory)/'usage-collection.json').read_text())
        probes['usage-collection']={'state':'observed' if now-usage['at']<900 else 'needs-check','last_success':usage['at'],
            'observation':str(sum('data' in r for r in usage['responses']))+' of '+str(len(usage['responses']))+' agents reporting usage; missing readers remain visible.'}
    except (OSError,ValueError,KeyError):
        probes['usage-collection']={'state':'unknown','observation':'Usage collection has not reported yet.'}
    return ScheduleStore(directory).tick(uses=uses, probes=probes, now=now, error=problem)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',required=True)
    sub=p.add_subparsers(dest='operation',required=True)
    register=sub.add_parser('register');register.add_argument('--file',required=True)
    sub.add_parser('view')
    tick=sub.add_parser('tick');tick.add_argument('--client-config',required=True);tick.add_argument('--replica-directory')
    args=p.parse_args();store=ScheduleStore(args.directory)
    if args.operation=='register':result=store.register(json.loads(Path(args.file).read_text()))
    elif args.operation=='view':result=store.view()
    else:result=observe(args.directory,args.client_config,args.replica_directory)
    print(json.dumps(result))

if __name__=='__main__':main()
