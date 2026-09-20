"""Installation-owned recovery, usable even when the console cannot answer.

The CLI process (or its detached UI-launched copy) is the coordinator. It never
runs inside a service it stops. Requests select modes/registered IDs, not OS
commands. Receipts and the admission barrier survive a console restart.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
ID = re.compile(r'[a-z][a-z0-9_-]{0,31}')
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,64}')
ROOM = re.compile(r'[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}')
TERMINAL = {'complete', 'partial', 'failed'}


def read_json(path, default=None):
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Invalid installation record')
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, separators=(',', ':')); out.write('\n')
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class RecoveryBusy(RuntimeError):
    pass


class Gate:
    """A short-lived shared lock spans admission through native acceptance.

    Pause is written first to refuse new admissions, then an exclusive lock
    waits for existing sends to settle. The queued-message hold is persistent;
    removing the barrier never releases held input into a restarted writer.
    """
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else None
        self.directory = self.root / 'control' if self.root else None

    def paused(self, agent='local'):
        if not self.directory: return False
        data = read_json(self.directory / 'admission.json', {})
        return bool(data.get('paused') and data.get('expires_at', 0) > time.time() and (agent in data.get('agents', []) or '*' in data.get('agents', [])))

    @contextlib.contextmanager
    def admit(self, agent='local', recovery_id=None):
        if not self.directory:
            yield; return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / 'admission.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_SH)
            current = read_json(self.directory/'admission.json', {})
            restoring = recovery_id and current.get('request_id') == recovery_id
            if self.paused(agent) and not restoring:
                raise RecoveryBusy('Recovery is holding new input. Your words have not been sent.')
            yield

    def pause(self, request_id, agents, deadline):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory/'pause.lock').open('a') as intent:
            os.chmod(intent.name, 0o600); fcntl.flock(intent, fcntl.LOCK_EX)
            current = read_json(self.directory/'admission.json', {})
            if current.get('paused') and current.get('expires_at', 0) > time.time() and current.get('request_id') != request_id:
                raise RecoveryBusy('Another recovery is holding this adapter')
            write_json(self.directory / 'admission.json', {'paused': True, 'request_id': request_id, 'agents': agents, 'expires_at': time.time()+max(0, deadline-time.monotonic())+10})
        with (self.directory / 'admission.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); return
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise RecoveryBusy('An input operation has not settled; no service was stopped.')
                    time.sleep(.05)

    def resume(self, request_id):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory/'pause.lock').open('a') as intent:
            os.chmod(intent.name, 0o600); fcntl.flock(intent, fcntl.LOCK_EX)
            current = read_json(self.directory / 'admission.json', {})
            if current.get('request_id') == request_id:
                write_json(self.directory / 'admission.json', {'paused': False, 'request_id': request_id, 'agents': []})


def process_identity(pid):
    """Compare process birth and owner, never just an old PID or process name."""
    if type(pid) is not int or pid < 1: return None
    try:
        if sys.platform.startswith('linux'):
            stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z': return None
            uid = Path(f'/proc/{pid}').stat().st_uid
            if uid != os.getuid(): return None
            raw = f'{pid}:{uid}:{stat[19]}'
        else:
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=,lstart=,stat=,command='], capture_output=True, text=True, timeout=2)
            if result.returncode or not result.stdout.strip(): return None
            raw = result.stdout.strip()
            if len(raw.split()) < 8 or raw.split()[0] != str(os.getuid()) or 'Z' in raw.split()[6]: return None
            # STAT changes from sleeping to running during an ordinary request.
            # It is useful for rejecting zombies, never as a birth identity.
            fields = raw.split()
            raw = f'{pid}:' + ' '.join(fields[:6] + fields[7:])
        return hashlib.sha256(raw.encode()).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError): return None


def register_console(root, port):
    root = Path(root).resolve()
    path = root / 'control' / 'console.json'
    previous = read_json(path, {})
    if previous.get('pid') != os.getpid() and previous.get('identity') and process_identity(previous.get('pid')) == previous['identity']:
        raise RecoveryBusy('This installation already has a registered console.')
    record = {'id': 'console', 'manager': 'process', 'root': str(root), 'source': str(SOURCE),
              'python': sys.executable, 'pid': os.getpid(), 'identity': process_identity(os.getpid()),
              'port': port, 'at': time.time()}
    if not record['identity']: raise RecoveryBusy('Cannot verify the console process identity.')
    write_json(path, record)
    return record


def hold_queue(path):
    """Only never-dispatched input is held. Accepted/uncertain records stay intact."""
    if not path.exists(): return 0
    with sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=3) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queued_messages'").fetchone()
        if not exists: return 0
        cursor = db.execute("UPDATE queued_messages SET status='editing', queued_reason='Held after recovery; edit and send when ready', version=version+1, updated_at=? WHERE status IN ('pending','editing')", (time.time(),))
        return cursor.rowcount


class Client:
    def __init__(self, endpoint, deadline=None):
        self.port = endpoint['port']; self.headers = dict(endpoint.get('headers', {}))
        self.deadline = deadline or time.monotonic() + 15

    def request(self, method, path, body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('Recovery deadline reached')
        headers = {**self.headers, 'Accept': 'application/json'}
        if getattr(self, 'recovery_id', None): headers['X-UX46-Recovery-ID'] = self.recovery_id
        if body is not None:
            status, boot = self.request('GET', '/api/bootstrap')
            if status != 200 or not boot.get('csrf'): raise OSError('Connection unavailable')
            headers.update({'Content-Type': 'application/json', 'Origin': boot.get('public_origin') or f'http://127.0.0.1:{self.port}', 'X-Atlas-CSRF': boot['csrf']})
            remaining = self.deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('Recovery deadline reached before mutation')
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=max(.1, min(remaining, 12)))
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse(); raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024: raise ValueError('Response too large')
            payload = json.loads(raw)
            if not isinstance(payload, dict): raise ValueError('Invalid adapter response')
            return response.status, payload
        finally: conn.close()


def endpoint(value):
    if not isinstance(value, dict) or set(value) - {'port', 'headers'}: raise ValueError('Invalid recovery endpoint')
    if type(value.get('port')) is not int or not 1 <= value['port'] <= 65535: raise ValueError('Invalid recovery port')
    headers = value.get('headers', {})
    if not isinstance(headers, dict) or any(k not in {'Host', 'Tailscale-User-Login'} or not isinstance(v, str) or '\r' in v or '\n' in v for k, v in headers.items()): raise ValueError('Invalid endpoint identity mapping')
    return value


def installation(root):
    root = Path(root).resolve()
    config = read_json(root / 'config.json')
    if not isinstance(config, dict) or config.get('schema_version') != 1 or config.get('agent') not in {'codex', 'claude', 'none'}: raise ValueError('Run ux46 setup to repair this installation configuration')
    if not isinstance(config.get('cli', ''), str) or config.get('execution_policy', 'preserve') not in {'preserve','workspace-write','full-access'}: raise ValueError('Invalid runtime configuration')
    record = read_json(root / 'control' / 'console.json', {})
    if not isinstance(record, dict): raise ValueError('Invalid console ownership record')
    port = record.get('port', config.get('port', 8877))
    local = {'id': 'local', 'runtime': config['agent'], 'endpoint': endpoint({'port': port}), 'local': True}
    agents = [] if config['agent'] == 'none' else [local]
    listed = read_json(root / 'agents.json', {'agents': []})
    if not isinstance(listed, dict) or not isinstance(listed.get('agents'), list): raise ValueError('Invalid configured agent list')
    for row in listed['agents']:
        if not isinstance(row, dict): raise ValueError('Invalid agent entry')
        identity = row.get('id')
        if not isinstance(identity, str) or not ID.fullmatch(identity) or identity == 'local' or any(a['id'] == identity for a in agents): raise ValueError('Invalid or duplicate agent ID')
        target = {'id': identity, 'runtime': row.get('runtime', ''), 'local': False}
        if row.get('transport') == 'loopback': target['endpoint'] = endpoint({'port': row.get('remote_port')})
        else: target['unsupported'] = 'This remote transport has no configured recovery capability'
        agents.append(target)
    services = []
    if record:
        if record.get('root') != str(root) or record.get('source') != str(SOURCE) or record.get('manager') != 'process' or record.get('id') != 'console': raise ValueError('Console ownership does not match this installation')
        if not Path(record.get('python', '')).is_absolute(): raise ValueError('Invalid console executable')
        services.append({**record, 'depends_on': [], 'agents': ['local'] if agents else [], 'endpoint': local['endpoint']})
    mapping = read_json(root / 'recovery.json', {'schema_version': 1, 'services': [], 'agents': []})
    if not isinstance(mapping, dict) or set(mapping) - {'schema_version', 'services', 'agents'} or mapping.get('schema_version') != 1: raise ValueError('Invalid private recovery mapping')
    if any(not isinstance(mapping.get(key, []), list) for key in ('services','agents')): raise ValueError('Recovery mappings must be lists')
    for row in mapping.get('services', []):
        if not isinstance(row, dict): raise ValueError('Invalid service entry')
        if set(row) - {'id', 'manager', 'target', 'definition', 'depends_on', 'endpoint', 'agents'}: raise ValueError('Unknown service mapping field')
        if not isinstance(row.get('agents'), list) or any(not isinstance(a, str) or not ID.fullmatch(a) for a in row['agents']): raise ValueError('Map each service to its owned agents, or explicitly use an empty list')
        if not isinstance(row.get('depends_on', []), list) or any(not isinstance(a, str) or not ID.fullmatch(a) for a in row.get('depends_on', [])): raise ValueError('Invalid service dependencies')
        identity = row.get('id')
        if not isinstance(identity, str) or not ID.fullmatch(identity) or any(s['id'] == identity for s in services): raise ValueError('Invalid or duplicate service ID')
        if row.get('manager') not in {'launchd', 'systemd'}: raise ValueError('Unsupported service manager')
        if not isinstance(row.get('target'), str) or not re.fullmatch(r'[A-Za-z0-9._/@-]{1,160}', row['target']) or row['target'].startswith('-'): raise ValueError('Invalid service target')
        definition = Path(row.get('definition', ''))
        if not definition.is_absolute() or not definition.is_file() or definition.is_symlink() or definition.stat().st_uid != os.getuid(): raise ValueError('Service definition must be an owner-owned regular file')
        services.append({**row, 'definition_sha256': hashlib.sha256(definition.read_bytes()).hexdigest(), 'endpoint': endpoint(row['endpoint']) if row.get('endpoint') else None})
    for row in mapping.get('agents', []):
        if not isinstance(row, dict): raise ValueError('Invalid recovery agent entry')
        if set(row) - {'id', 'runtime', 'endpoint', 'unsupported'} or not isinstance(row.get('id'), str) or not ID.fullmatch(row['id']) or row['id'] == 'local': raise ValueError('Invalid private agent mapping')
        # Explicit private mappings can supplement or replace a host's transport,
        # never widen from HTTP input or a discovered process name.
        agents = [a for a in agents if a['id'] != row['id']]
        agents.append({**row, 'endpoint': endpoint(row['endpoint']) if row.get('endpoint') else None, 'local': False})
    if len(services) > 16 or len(agents) > 16: raise ValueError('Too many configured recovery targets')
    if any(set(s.get('agents', [])) - {a['id'] for a in agents} for s in services): raise ValueError('Service names an unconfigured agent')
    ordered, todo = [], services[:]
    while todo:
        ready = [s for s in todo if set(s.get('depends_on', [])) <= {s['id'] for s in ordered}]
        if not ready: raise ValueError('Service dependencies are missing or cyclic')
        for row in ready: ordered.append(row); todo.remove(row)
    return {'root': root, 'config': config, 'agents': agents, 'services': ordered, 'console': local['endpoint']}


class ServiceManager:
    def __init__(self, root, deadline): self.root, self.deadline = root, deadline

    def command(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=max(.1, min(3, self.deadline-time.monotonic())), check=False)

    def inspect(self, service):
        if service['manager'] == 'process':
            found = process_identity(service.get('pid'))
            return {'supported': True, 'running': found == service.get('identity') and bool(found), 'replaced': bool(found and found != service.get('identity'))}
        path = Path(service['definition'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != service['definition_sha256']: raise ValueError('Service definition changed during recovery')
        target = service['target']
        if service['manager'] == 'launchd':
            if sys.platform != 'darwin' or not target.startswith(f'gui/{os.getuid()}/'): return {'supported': False, 'running': False}
            import plistlib
            plist = plistlib.loads(path.read_bytes())
            if plist.get('Label') != target.split('/')[-1]: raise ValueError('Service label differs from its definition')
            result = self.command(['/bin/launchctl', 'print', target])
            if result.returncode: return {'supported': False, 'running': False}
            # Verify the loaded job's definition too; a matching label alone
            # must not claim an unrelated program registered under that label.
            loaded = re.search(r'^\s*path = (.+)$', result.stdout, re.M)
            if not loaded or Path(loaded[1]).resolve() != path.resolve(): raise ValueError('Loaded service belongs to another definition')
            match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
            return {'supported': True, 'running': bool(match), 'pid': int(match[1]) if match else None}
        if not sys.platform.startswith('linux') or '/' in target or not target.endswith('.service'): return {'supported': False, 'running': False}
        result = self.command(['systemctl', '--user', 'show', target, '--property=FragmentPath,MainPID,LoadState'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or values.get('LoadState') != 'loaded': return {'supported': False, 'running': False}
        if Path(values.get('FragmentPath', '')).resolve() != path.resolve(): raise ValueError('Loaded unit belongs to another definition')
        pid = int(values.get('MainPID', 0))
        return {'supported': True, 'running': bool(pid), 'pid': pid or None}

    def stop(self, service):
        if time.monotonic() >= self.deadline: return 'expired'
        before = self.inspect(service)
        if not before['supported']: return 'unsupported'
        if before.get('replaced'): return 'ownership_changed'
        if not before['running']: return 'stopped'
        kind = service['manager']
        if kind == 'process':
            # Inspect immediately before signalling exactly the registered birth.
            if process_identity(service['pid']) != service['identity']: return 'ownership_changed'
            os.kill(service['pid'], signal.SIGINT)
        else:
            args = (['/bin/launchctl', 'kill', 'SIGINT', service['target']] if kind == 'launchd' else
                    ['systemctl', '--user', 'kill', '--signal=SIGINT', '--kill-whom=main', service['target']])
            if self.command(args).returncode: return 'stop_refused'
        limit = min(self.deadline, time.monotonic() + 30)
        while time.monotonic() < limit:
            after = self.inspect(service)
            if not after['running'] or after.get('pid') != before.get('pid'): return 'stopped'
            time.sleep(.1)
        return 'stop_unknown'

    def start(self, service):
        if time.monotonic() >= self.deadline: return 'expired'
        before = self.inspect(service)
        if not before['supported']: return 'unsupported'
        if before.get('replaced'): return 'ownership_changed'
        child = None
        if service['manager'] == 'process':
            if before['running']: return 'already_running'
            env = dict(os.environ, UX46_HOME=str(self.root))
            child = subprocess.Popen([service['python'], str(SOURCE/'tools/ux46'), 'run', '--port', str(service['port'])], env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        elif not before['running']:
            args = (['/bin/launchctl', 'kickstart', service['target']] if service['manager'] == 'launchd' else ['systemctl', '--user', 'start', service['target']])
            if self.command(args).returncode: return 'start_failed'
        if not service.get('endpoint'): return 'started_unverified'
        limit = min(self.deadline, time.monotonic()+20)
        while time.monotonic() < limit:
            try:
                status, boot = Client(service['endpoint'], limit).request('GET', '/api/bootstrap')
                if status == 200:
                    if service['manager'] == 'process':
                        record = read_json(self.root/'control/console.json', {})
                        if child.poll() is not None: return 'start_failed'
                        if record.get('pid') != child.pid or record.get('identity') != process_identity(child.pid):
                            time.sleep(.1); continue
                    elif not self.inspect(service).get('running'):
                        time.sleep(.1); continue
                    return 'ready'
            except (OSError, ValueError, http.client.HTTPException): pass
            time.sleep(.1)
        return 'start_unverified'


def refresh_agent(agent, client, *, reconnect=False, saved=None):
    if agent.get('unsupported') or not agent.get('endpoint') or agent['runtime'] != 'codex':
        return [{'agent': agent['id'], 'state': 'unsupported', 'message': 'This adapter has no verified owned-connection recovery capability.'}]
    results = []
    status, boot = client.request('GET', '/api/bootstrap')
    rows = saved if saved is not None else boot.get('remembered_owned')
    if status != 200 or not isinstance(rows, list) or len(rows) > 200: raise ValueError('Owned-session inventory unavailable')
    grouped = {}
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid owned-session row')
        room, thread = row.get('room'), row.get('thread_id')
        if not isinstance(room, str) or not ROOM.fullmatch(room) or not isinstance(thread, str) or not thread: raise ValueError('Invalid owned-session identity')
        grouped.setdefault(thread, []).append(room)
    for thread, aliases in grouped.items():
        room = aliases[0]
        result = {'agent': agent['id'], 'room': room, 'state': 'deferred'}; results.append(result)
        mutating = False
        try:
            status, detail = client.request('GET', '/api/room/'+room+'?inspect=1')
            if status != 200 or (detail.get('native') or {}).get('thread_id') != thread:
                result['state'] = 'ownership_changed'; continue
            native = detail.get('native') or {}; worker = native.get('worker') or {}
            if native.get('active_turn') or detail.get('approvals') or any(s.get('status', s.get('state')) in {'pending','queued','in_flight','uncertain'} for s in detail.get('submissions', [])):
                continue
            if worker.get('running'):
                if worker.get('thread_id') != thread or worker.get('room') not in aliases: result['state'] = 'ownership_changed'; continue
                room = worker['room']; result['room'] = room
                path, body = '/api/connection/refresh', {'room': room}
            elif reconnect:
                goal = detail.get('goal_status') or {}
                no_goal = goal.get('state') == 'known' and goal.get('goal') is None
                if not no_goal and goal.get('state') != 'unsupported' and (goal.get('goal') or {}).get('status') not in {'completed','complete','paused','blocked'}:
                    result['state'] = 'deferred'; result['message'] = 'Inspect the native goal before reconnecting; recovery does not restart goals.'; continue
                path, body = '/api/room/'+room+'/continue', {}
            else: result['state'] = 'not_connected'; continue
            mutating = True
            status, answer = client.request('POST', path, body)
            verified = (answer.get('connection') or {}).get('state') == 'refreshed' if path == '/api/connection/refresh' else (answer.get('room') or {}).get('ownership', {}).get('state') == 'atlas_owned'
            result['state'] = ('refreshed' if status == 200 and verified else 'deferred' if status == 409 and answer.get('error') in {'connection_busy', 'refresh_safety_unknown'} else 'unknown' if status >= 500 else 'failed')
        except (OSError, ValueError, http.client.HTTPException): result['state'] = 'unknown' if mutating else 'failed'
    try:
        status, answer = client.request('POST', '/api/connection/refresh', {})
        state = 'refreshed' if status == 200 and (answer.get('connection') or answer).get('state') == 'refreshed' else 'unknown' if status >= 500 else 'deferred' if status == 409 else 'failed'
    except (OSError, ValueError, http.client.HTTPException): state = 'unknown'
    results.append({'agent': agent['id'], 'connection': 'reader', 'state': state})
    return results


class Coordinator:
    def __init__(self, root, *, manager_factory=ServiceManager, client_factory=Client, budget=120):
        self.root = Path(root).resolve(); self.directory = self.root/'control'
        self.manager_factory, self.client_factory, self.budget = manager_factory, client_factory, budget

    def receipt(self, request_id=None):
        if request_id is not None and not REQUEST.fullmatch(request_id): raise ValueError('Invalid recovery request ID')
        identity = request_id or read_json(self.directory/'latest.json', {}).get('id')
        return read_json(self.directory/f'recovery-{identity}.json') if identity else None

    def execute(self, mode, agent=None, request_id=None):
        if mode not in {'agent', 'all'} or (mode == 'agent' and (not isinstance(agent, str) or not ID.fullmatch(agent))) or (mode == 'all' and agent is not None): raise ValueError('Choose an exact agent or full recovery')
        request_id = request_id or uuid.uuid4().hex
        if not REQUEST.fullmatch(request_id): raise ValueError('Invalid recovery request ID')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize duplicates before the global lock: a second copy of the
        # same request must not write a declined receipt during the tiny window
        # before the first copy has persisted its running receipt.
        with (self.directory/f'request-{request_id}.lock').open('a') as request_lock:
            os.chmod(request_lock.name, 0o600)
            try: fcntl.flock(request_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                previous = self.receipt(request_id)
                if previous:
                    if previous.get('mode') != mode or previous.get('agent') != agent: raise ValueError('Request ID belongs to another recovery')
                    return previous
                return {'id':request_id,'mode':mode,'agent':agent,'state':'starting',
                        'message':'This request is already starting. Read its receipt; nothing was repeated.'}
            with (self.directory/'recovery.lock').open('a') as lock:
                os.chmod(lock.name, 0o600)
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    previous = self.receipt(request_id)
                    if previous:
                        if previous.get('mode') != mode or previous.get('agent') != agent: raise ValueError('Request ID belongs to another recovery')
                        return previous
                    active = self.receipt()
                    declined = {'id':request_id, 'mode':mode, 'agent':agent, 'state':'partial',
                                'at':time.time(), 'finished_at':time.time(), 'services':[], 'connections':[],
                                'held_messages':0, 'active_request':active.get('id') if active else None,
                                'message':'Another recovery is running. Nothing changed for this request.'}
                    write_json(self.directory/f'recovery-{request_id}.json', declined)
                    return declined
                previous = self.receipt(request_id)
                if previous:
                    if previous.get('mode') != mode or previous.get('agent') != agent: raise ValueError('Request ID belongs to another recovery')
                    # A nonterminal receipt with a free operation lock proves its
                    # coordinator is gone. Report interruption; never replay stops.
                    if previous['state'] not in TERMINAL:
                        previous.update(state='partial', message='Coordinator stopped. Inspect the recorded results; no operation was replayed.')
                        write_json(self.directory/f'recovery-{request_id}.json', previous)
                    return previous
                plan = installation(self.root)
                selected = plan['agents'] if mode == 'all' else [a for a in plan['agents'] if a['id'] == agent]
                if mode == 'agent' and not selected: raise ValueError('Agent is not configured in this installation')
                deadline = time.monotonic()+self.budget
                job = {'id': request_id, 'mode': mode, 'agent': agent, 'state': 'running', 'at': time.time(),
                       'services': [], 'connections': [], 'held_messages': 0, 'message': 'Recovery in progress. No input will be replayed.'}
                path = self.directory/f'recovery-{request_id}.json'
                def save(): write_json(path, job)
                save(); write_json(self.directory/'latest.json', {'id': request_id})
                gate = Gate(self.root)
                prepared = []; unavailable = set(); source_lock = None
                def client(target):
                    value = self.client_factory(target.get('endpoint') or {'port': 1}, deadline)
                    value.recovery_id = request_id
                    return value
                try:
                    source_lock = (self.directory/'source.lock').open('a')
                    os.chmod(source_lock.name,0o600)
                    try: fcntl.flock(source_lock,fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError: raise RecoveryBusy('Source undo is in progress; no service was changed')
                    gate.pause(request_id, ['*'] if mode == 'all' else [agent], deadline)
                    if mode == 'all' or agent == 'local':
                        job['held_messages'] = hold_queue(self.root/'state/atlas-console.sqlite3') + hold_queue(self.root/'claude/claude-console.sqlite3')
                    for target in selected:
                        if target.get('local'): continue
                        try:
                            probe = client(target)
                            status, boot = probe.request('GET', '/api/bootstrap') if target.get('endpoint') else (503,{})
                            if status != 200 or not boot.get('recovery', {}).get('external_preparation'): raise ValueError('Adapter lacks recovery admission')
                            status, result = probe.request('POST', '/api/recovery/prepare', {'request_id': request_id})
                            if status != 200 or not result.get('prepared'): raise ValueError('Adapter did not hold input')
                            prepared.append(target)
                            job['held_messages'] += result.get('held_messages', 0)
                        except (OSError, ValueError, http.client.HTTPException):
                            unavailable.add(target['id']); job['connections'].append({'agent': target['id'], 'state':'unsupported', 'message':'This adapter could not hold input for recovery; no connection was replaced.'})
                    if mode == 'all':
                        manager = self.manager_factory(self.root, deadline)
                        saved = {}
                        for target in selected:
                            if not target.get('endpoint'): continue
                            try:
                                status, boot = client(target).request('GET', '/api/bootstrap')
                                if status == 200: saved[target['id']] = boot.get('remembered_owned')
                            except (OSError, ValueError, http.client.HTTPException): pass
                        if not plan['services']:
                            job['services'].append({'id': 'installation', 'state': 'unsupported', 'message': 'No installation-owned services are registered.'})
                        stopped = set()
                        for service in reversed(plan['services']):
                            if set(service.get('agents', [])) & unavailable:
                                job['services'].append({'id':service['id'],'state':'admission_unavailable'}); save(); continue
                            try: outcome = manager.stop(service)
                            except (OSError, ValueError, subprocess.SubprocessError): outcome = 'stop_unknown'
                            job['services'].append({'id': service['id'], 'state': outcome}); save()
                            if outcome == 'stopped': stopped.add(service['id'])
                        ready = set()
                        for service in plan['services']:
                            row = next(r for r in job['services'] if r['id'] == service['id'])
                            if service['id'] not in stopped: continue
                            if not set(service.get('depends_on', [])) <= ready: row['state'] = 'dependency_failed'; save(); continue
                            try: row['state'] = manager.start(service)
                            except (OSError, ValueError, subprocess.SubprocessError): row['state'] = 'start_unknown'
                            if row['state'] == 'ready': ready.add(service['id'])
                            save()
                        for target in selected:
                            if target['id'] in unavailable: continue
                            if not target.get('local') and not any(target['id'] in service.get('agents', []) for service in plan['services']):
                                job['services'].append({'id':target['id'],'state':'unsupported','message':'No explicit service ownership mapping; only owned connections were refreshed.'})
                            try: job['connections'].extend(refresh_agent(target, client(target), reconnect=True, saved=saved.get(target['id'])))
                            except (OSError, ValueError, http.client.HTTPException): job['connections'].append({'agent': target['id'], 'state': 'failed'})
                    else:
                        target = selected[0]
                        if target['id'] not in unavailable: job['connections'].extend(refresh_agent(target, client(target)))
                    good = {'ready', 'refreshed', 'not_connected'}
                    bad = any(row['state'] not in good for row in job['services']+job['connections'])
                    message = ('Review deferred or unsupported targets; full recovery can interrupt owned busy work.' if mode == 'agent' else 'Review the unresolved recovery results before choosing a new action.')
                    job.update(state='partial' if bad else 'complete', message=message if bad else 'Recovery finished. Conversations and drafts are kept; no input was replayed.')
                except (OSError, ValueError, RecoveryBusy, sqlite3.Error, subprocess.SubprocessError, http.client.HTTPException):
                    job.update(state='partial', message='Recovery could not verify every step. No uncertain operation was repeated.')
                finally:
                    for target in prepared:
                        try:
                            status, result = client(target).request('POST', '/api/recovery/finish', {'request_id':request_id})
                            if status != 200: raise ValueError('Admission could not be reopened')
                        except (OSError, ValueError, http.client.HTTPException):
                            job.update(state='partial', message='Recovery ended, but this adapter’s input hold could not be checked. Queued messages remain held.')
                            job['connections'].append({'agent':target['id'],'state':'admission_unknown'})
                    if source_lock is not None: source_lock.close()
                    gate.resume(request_id); job['finished_at'] = time.time(); save()
                return job


def launch(root, mode, agent, request_id):
    """Start the same CLI coordinator outside this requesting service's lifetime."""
    if mode not in {'agent','all'} or not isinstance(request_id,str) or not REQUEST.fullmatch(request_id): raise ValueError('Invalid recovery request')
    if (mode == 'all' and agent is not None) or (mode == 'agent' and (not isinstance(agent,str) or not ID.fullmatch(agent))): raise ValueError('Choose an exact agent or full recovery')
    root = Path(root).resolve(); coordinator = Coordinator(root)
    previous = coordinator.receipt(request_id)
    if previous:
        if previous.get('mode') != mode or previous.get('agent') != agent: raise ValueError('Request ID belongs to another recovery')
        if previous.get('state') in TERMINAL: return previous
    plan = installation(root)
    if mode == 'agent' and not any(a['id'] == agent for a in plan['agents']): raise ValueError('Unknown configured agent')
    command = [sys.executable, str(SOURCE/'tools/ux46'), 'doctor', '--json', '--request-id', request_id]
    command += ['--recover-all'] if mode == 'all' else ['--agent',agent,'--recover']
    subprocess.Popen(command, env=dict(os.environ,UX46_HOME=str(root)), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    # Creation of the persisted receipt precedes any interruption. A missing
    # receipt after this short window stays explicitly unconfirmed in the UI.
    deadline = time.monotonic()+.3
    while time.monotonic() < deadline:
        receipt = coordinator.receipt(request_id)
        if receipt: return receipt
        time.sleep(.01)
    return {'id':request_id,'mode':mode,'agent':agent,'state':'starting','at':time.time(),
            'message':'Recovery requested. Check its receipt; do not repeat uncertain operations.'}


def api(root, method, path, body=None, request_id=None):
    if not root: return 503, {'error':'recovery_unsupported','message':'This host has no installation recovery mapping. Use its configured native launcher.'}
    if method == 'POST' and path in {'/api/recovery/prepare','/api/recovery/finish'}:
        if not isinstance(body,dict) or set(body) != {'request_id'} or not isinstance(body['request_id'],str) or not REQUEST.fullmatch(body['request_id']): raise ValueError('Invalid recovery identity')
        gate = Gate(root)
        if path.endswith('/finish'):
            gate.resume(body['request_id']); return 200, {'resumed':True}
        gate.pause(body['request_id'], ['local'], time.monotonic()+120)
        count = hold_queue(Path(root)/'state/atlas-console.sqlite3') + hold_queue(Path(root)/'claude/claude-console.sqlite3')
        return 200, {'prepared':True,'held_messages':count}
    if method == 'GET' and path == '/api/recovery/status':
        return 200, {'supported':True,'job':Coordinator(root).receipt(request_id)}
    if method != 'POST' or path != '/api/recovery/start': return 405, {'error':'bad_recovery_method'}
    if not isinstance(body,dict) or set(body)-{'mode','agent','request_id'}: raise ValueError('Only mode, configured agent and request ID are accepted')
    return 202, {'job':launch(root,body.get('mode'),body.get('agent'),body.get('request_id'))}
