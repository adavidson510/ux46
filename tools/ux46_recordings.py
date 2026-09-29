"""Private recordings: durable ordered audio, optional local transcription, no agent calls."""
from datetime import datetime, timezone
from contextlib import contextmanager
import base64
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import threading

from constellation_store import Conflict, Unavailable

ID = re.compile(r'^[a-zA-Z0-9-]{16,64}$')
MIMES = {'audio/webm', 'audio/ogg', 'audio/mp4', 'audio/wav', 'audio/x-wav'}
MAX_BYTES = 128 * 1024 * 1024


class RecordingStore:
    def __init__(self, directory):
        self.root = Path(directory) / 'recordings'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'recordings.sqlite3'
        self.index_lock = threading.Lock()
        self.index = self.root / "index"
        self.index.mkdir(exist_ok=True, mode=0o700)
        self.config_path = Path(directory) / 'recording-transcription.json'
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-transcript')
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS recordings(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                mime TEXT NOT NULL, status TEXT NOT NULL, next_seq INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0,
                interrupted INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                transcript TEXT NOT NULL DEFAULT '', segments TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, metadata TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS chunks(id TEXT NOT NULL, seq INTEGER NOT NULL,
                digest TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(id,seq));''')
            if 'metadata' not in {r[1] for r in c.execute('PRAGMA table_info(recordings)')}:
                c.execute("ALTER TABLE recordings ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
            c.execute("UPDATE recordings SET status='saved', error='Transcription was interrupted; you can retry.' WHERE status='transcribing'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        try:
            with c: yield c
        finally: c.close()

    def ident(self, value):
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise ValueError('Invalid recording')
        return value

    def row(self, c, ident):
        row = c.execute('SELECT * FROM recordings WHERE id=?', (self.ident(ident),)).fetchone()
        if row is None:
            raise Unavailable('Recording not found')
        return dict(row)

    def config(self):
        try:
            d = json.loads(self.config_path.read_text())
            if Path(d['python']).is_file() and Path(d['model']).is_dir():
                return d
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def projects(self):
        try:
            path = Path(os.environ.get('ATLAS_REGISTRY', str(Path.home()/'.codex/projects/registry.json')))
            data = json.loads(path.read_text())
            return [{'id':p['id'], 'name':p.get('name',p['id'])} for p in data.get('projects',[]) if isinstance(p.get('id'),str)][:200]
        except (OSError, ValueError, KeyError): return []

    def export_index(self, ident):
        with self.index_lock: self._export_index(ident)

    def _export_index(self, ident):
        with self.db() as c: row = self.row(c, ident)
        target = self.index / (ident + '.md')
        if row['deleted']:
            target.unlink(missing_ok=True)
            return
        meta = json.loads(row['metadata'])
        fields = {'kind':'recording', 'project':meta.get('project') or 'unfiled-recordings',
                  'session':'recording-'+ident, 'recording_id':ident, 'title':row['title'],
                  'date':str(meta.get('recorded_at') or datetime.fromtimestamp(row['created'],timezone.utc).isoformat())[:10],
                  'updated':datetime.fromtimestamp(row['updated'],timezone.utc).date().isoformat(),
                  'status':'complete' if row['status']!='recording' else 'active',
                  'keywords':'recording, '+str(meta.get('tags','')), 'transcript_status':row['status']}
        body = ['# '+row['title'], '', meta.get('summary') or 'Recording metadata; no summary requested.', '',
                'Recording ID: '+ident, 'Audio and transcript owner: UX46 recordings store.',
                'Duration: '+str(row['duration'])+' seconds', 'Recorded: '+str(meta.get('recorded_at','unknown')),
                'Time zone: '+str(meta.get('timezone','unknown')), 'Device: '+str(meta.get('device','unknown')),
                'Location (user supplied): '+str(meta.get('location') or 'unknown'),
                'Participants (user supplied): '+str(meta.get('participants') or 'unknown'),
                'Summary provenance: '+str(meta.get('summary_origin') or 'none'),
                'Interrupted: '+str(bool(row['interrupted'])),
                'This source is a recording, not a native agent session. It has no runtime pointer.']
        text = '---\n'+'\n'.join(k+': '+json.dumps(str(v)) for k,v in fields.items())+'\n---\n\n'+'\n'.join(body)+'\n'
        temporary = target.with_suffix('.pending')
        temporary.write_text(text);os.chmod(temporary,0o600);temporary.replace(target)

    def view(self, args):
        with self.db() as c:
            if args.get('id'):
                row = self.row(c, args['id'])
                row['segments'] = json.loads(row['segments'])
                row['metadata'] = json.loads(row['metadata'])
                return row
            q = str(args.get('query', ''))[:200]
            rows = [dict(r) for r in c.execute('''SELECT id,title,created,updated,mime,status,next_seq,bytes,
                duration,interrupted,deleted,error,version,metadata FROM recordings
                WHERE (instr(lower(title),lower(?))>0 OR instr(lower(transcript),lower(?))>0 OR instr(lower(metadata),lower(?))>0)
                ORDER BY created DESC LIMIT 100''', (q, q, q))]
        for row in rows: row['metadata'] = json.loads(row['metadata'])
        if args.get('project'):
            rows = [r for r in rows if (r['metadata'].get('project') or 'unfiled') == args['project']]
        return {'recordings': rows, 'projects':self.projects(), 'transcription_available': bool(self.config()),
                'transcription_location': 'workspace host', 'max_bytes': MAX_BYTES}

    def action(self, args):
        result = self._action(args)
        if args.get('action') != 'chunk':
            try: self.export_index(args['id'])
            except OSError: result['index_error'] = 'Saved; Vault metadata index could not be refreshed.'
        return result

    def _action(self, args):
        op = args.get('action'); ident = self.ident(args.get('id'))
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if op == 'start':
                mime = str(args.get('mime', '')).split(';')[0]
                title = str(args.get('title', '')).strip()[:160]
                if mime not in MIMES or not title:
                    raise ValueError('Invalid audio type or title')
                held = c.execute('SELECT mime FROM recordings WHERE id=?', (ident,)).fetchone()
                if held and held['mime'] != mime:
                    raise Conflict('Recording identity changed')
                c.execute('INSERT OR IGNORE INTO recordings(id,title,created,updated,mime,status) VALUES(?,?,?,?,?,?)',
                          (ident, title, time.time(), time.time(), mime, 'recording'))
                if not held:
                    meta = {k:str(args.get('metadata',{}).get(k,''))[:300] for k in ('device','timezone','recorded_at')}
                    c.execute('UPDATE recordings SET metadata=? WHERE id=?', (json.dumps(meta),ident))
                return self.row(c, ident)
            row = self.row(c, ident)
            if op == 'chunk':
                seq = int(args['seq']); data = base64.b64decode(args['data'], validate=True)
                if not data or len(data) > 32768 or seq < 0:
                    raise ValueError('Invalid chunk')
                digest = hashlib.sha256(data).hexdigest()
                held = c.execute('SELECT digest FROM chunks WHERE id=? AND seq=?', (ident, seq)).fetchone()
                if held:
                    if held['digest'] != digest: raise Conflict('Chunk content changed')
                    return {'next_seq': row['next_seq']}
                if row['status'] != 'recording' or row['deleted'] or seq != row['next_seq']:
                    raise Conflict('Upload must continue from its saved position')
                if row['bytes'] + len(data) > MAX_BYTES:
                    raise ValueError('Recording reached the 128 MiB alpha limit')
                c.execute('INSERT INTO chunks VALUES(?,?,?,?)', (ident, seq, digest, data))
                c.execute('UPDATE recordings SET next_seq=next_seq+1,bytes=bytes+?,updated=? WHERE id=?', (len(data), time.time(), ident))
                return {'next_seq': seq + 1}
            if op == 'finish':
                if int(args['count']) != row['next_seq']:
                    raise Conflict('Audio is still uploading')
                if row['status'] == 'recording':
                    duration = float(args.get('duration', 0))
                    if not math.isfinite(duration) or not 0 <= duration <= 86400: raise ValueError('Invalid duration')
                    c.execute("UPDATE recordings SET status='saved',duration=?,interrupted=?,updated=? WHERE id=?",
                              (duration, bool(args.get('interrupted')), time.time(), ident))
                return self.row(c, ident)
            if op in ('rename', 'trash', 'restore', 'metadata'):
                if int(args.get('version', -1)) != row['version']:
                    raise Conflict('Recording changed; reopen before editing')
                title = str(args.get('title', row['title'])).strip()[:160]
                if not title: raise ValueError('A name is required')
                meta = json.loads(row['metadata'])
                if op == 'metadata':
                    incoming = args.get('metadata', {})
                    for key in ('project','tags','device','timezone','recorded_at','location','participants','summary','summary_origin'):
                        if key in incoming:
                            value = str(incoming[key]).strip()
                            if len(value) > (6000 if key=='summary' else 300): raise ValueError('Metadata too long')
                            meta[key] = value
                    if meta.get('project') and meta['project'] not in {p['id'] for p in self.projects()}:
                        raise ValueError('Choose a registered project')
                deleted = 1 if op == 'trash' else 0 if op == 'restore' else row['deleted']
                c.execute('UPDATE recordings SET title=?,deleted=?,metadata=?,version=version+1,updated=? WHERE id=?', (title, deleted, json.dumps(meta), time.time(), ident))
                return self.row(c, ident)
            if op == 'transcribe':
                if row['deleted'] or row['status'] == 'recording' or not row['bytes']:
                    raise ValueError('Save audio before transcribing')
                if row['status'] == 'transcribing': return row
                config = self.config()
                if not config: raise Unavailable('Local transcription is not configured on this host')
                # Only one job is admitted at a time, even if separate clients ask.
                if c.execute("SELECT 1 FROM recordings WHERE status='transcribing'").fetchone():
                    raise Conflict('Another recording is being transcribed; try again when it finishes')
                c.execute("UPDATE recordings SET status='transcribing',error='' WHERE id=?", (ident,))
                c.commit()
                self.executor.submit(self.transcribe, ident, config)
                return self.row(c, ident)
            raise ValueError('Unknown recording action')

    def audio(self, ident):
        with self.db() as c:
            row = self.row(c, ident)
            data = b''.join(r['data'] for r in c.execute('SELECT data FROM chunks WHERE id=? ORDER BY seq', (ident,)))
        return row, data

    def reply_audio(self, handler, ident):
        from atlas_voice import audio_response
        row, data = self.audio(ident)
        status, body, headers = audio_response(data, handler.headers.get('Range', ''))
        handler.send_response(status)
        if hasattr(handler, '_emit_login'): handler._emit_login()
        handler.send_header('Content-Type', row['mime'])
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in headers: handler.send_header(k, v)
        handler.end_headers()
        if handler.command != 'HEAD': handler.wfile.write(body)

    def transcribe(self, ident, config):
        try:
            _, data = self.audio(ident)
            with tempfile.TemporaryDirectory(dir=self.root, prefix='job-') as temp:
                audio = Path(temp) / 'audio'; output = Path(temp) / 'transcript.json'
                audio.write_bytes(data)
                env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
                result = subprocess.run([config['python'], str(Path(__file__).with_name('ux46_transcribe.py')),
                    str(audio), str(output), config['model']], env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=7200, check=False)
                if result.returncode != 0 or not output.is_file() or output.stat().st_size > 8*1024*1024:
                    raise ValueError('Transcription failed')
                transcript = json.loads(output.read_text())
            with self.db() as c:
                c.execute("UPDATE recordings SET status='ready',transcript=?,segments=?,error='',updated=? WHERE id=?",
                          (transcript['text'], json.dumps(transcript['segments']), time.time(), ident))
            self.export_index(ident)
        except Exception:
            # Do not put recorded words, paths or process output into logs/errors.
            with self.db() as c:
                c.execute("UPDATE recordings SET status='saved',error='Local transcription failed. Your audio is saved; you can retry.' WHERE id=?", (ident,))
