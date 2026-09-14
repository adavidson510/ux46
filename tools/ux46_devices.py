"""Owner-local browser associations and recent desktop presence, never hardware identity."""
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from constellation_store import Conflict

PLATFORMS = {'mac': ('Mac', 'computer'), 'windows': ('Windows PC', 'computer'),
             'iphone': ('iPhone', 'phone'), 'ipad': ('iPad', 'tablet'),
             'android': ('Android', 'phone'), 'linux': ('Linux PC', 'computer'),
             'chromeos': ('Chromebook', 'computer'), 'other': ('Device', 'computer')}
BROWSERS = {'chrome': 'Chrome', 'safari': 'Safari', 'edge': 'Edge', 'firefox': 'Firefox', 'other': 'Browser'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,96}', value):
        raise ValueError('Invalid device reference')
    return value


class DeviceStore:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT, kind TEXT, revision INTEGER);
                CREATE TABLE IF NOT EXISTS browsers(id TEXT PRIMARY KEY, device TEXT, label TEXT, seen REAL);
                CREATE TABLE IF NOT EXISTS windows(id TEXT, browser TEXT, desktop TEXT, local INTEGER, seen REAL,
                    PRIMARY KEY(id,browser));''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def view(self, browser):
        identifier(browser)
        with self.db() as db: return self._view(db, browser)

    def _view(self, db, browser):
        now = time.time(); bound = db.execute('SELECT device FROM browsers WHERE id=?', (browser,)).fetchone()
        devices = []
        for device in db.execute('SELECT * FROM devices ORDER BY name,id'):
            clients = [dict(r) for r in db.execute('SELECT id,label,seen FROM browsers WHERE device=?', (device['id'],))]
            windows = [dict(r) for r in db.execute('SELECT w.* FROM windows w JOIN browsers b ON w.browser=b.id WHERE b.device=? ORDER BY w.seen DESC', (device['id'],))]
            seen = max([r['seen'] for r in clients] or [0]); online = now-seen < 100
            recent = [r for r in windows if now-r['seen'] < 100] if online else windows[:1]
            desktops = sorted({(r['desktop'], bool(r['local'])) for r in recent})
            devices.append(dict(device, browsers=[{'id':r['id'],'label':r['label']} for r in clients],
                                last_seen=seen, online=online,
                                desktops=[{'id':id,'local':local} for id,local in desktops]))
        return {'current_device':bound['device'] if bound else None, 'devices':devices, 'at':now,
                'coverage':'Remembered browsers; online means a UX46 check-in within 100 seconds.'}

    def check_in(self, args):
        browser = identifier(args['browser']); window = identifier(args['window'])
        desktop = identifier(args.get('desktop', 'default')); local = args.get('local', False)
        if type(local) is not bool: raise ValueError('Invalid layout scope')
        platform = args.get('platform', 'other'); label = BROWSERS.get(args.get('browser_kind'), 'Browser')
        if platform not in PLATFORMS: raise ValueError('Invalid device type')
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            bound = db.execute('SELECT device FROM browsers WHERE id=?', (browser,)).fetchone()
            if not bound:
                if db.execute('SELECT count(*) FROM devices').fetchone()[0] >= 40: raise ValueError('Device list is full')
                name, kind = PLATFORMS[platform]; names = {r[0] for r in db.execute('SELECT name FROM devices')}
                base = name; number = 2
                while name in names: name = base+' '+str(number); number += 1
                device = uuid.uuid4().hex
                db.execute('INSERT INTO devices VALUES (?,?,?,1)', (device, name, kind))
                db.execute('INSERT INTO browsers VALUES (?,?,?,?)', (browser, device, label, now))
            else: db.execute('UPDATE browsers SET label=?,seen=? WHERE id=?', (label, now, browser))
            db.execute('INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?)', (window, browser, desktop, int(local), now))
            # Browser names persist; old page-presence records do not accumulate forever.
            db.execute('DELETE FROM windows WHERE rowid NOT IN (SELECT rowid FROM windows ORDER BY seen DESC LIMIT 256)')
            return self._view(db, browser)

    def rename(self, args):
        browser = identifier(args['browser']); device = identifier(args['device'])
        name = args.get('name', '')
        if not isinstance(name, str): raise ValueError('Invalid device name')
        name = name.strip()
        if not name or len(name)>80 or any(ord(c)<32 for c in name): raise ValueError('Use a short device name')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            bound = db.execute('SELECT device FROM browsers WHERE id=?', (browser,)).fetchone()
            row = db.execute('SELECT revision FROM devices WHERE id=?', (device,)).fetchone()
            if not row or not bound or bound['device']!=device: raise ValueError('Associate this browser first')
            if type(args.get('revision')) is not int or row['revision']!=args['revision']: raise Conflict('Device name changed; refresh before saving')
            db.execute('UPDATE devices SET name=?,revision=revision+1 WHERE id=?', (name, device))
            return self._view(db, browser)

    def associate(self, args):
        browser = identifier(args['browser']); target = identifier(args['device'])
        previous = identifier(args['previous'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            bound = db.execute('SELECT device FROM browsers WHERE id=?', (browser,)).fetchone()
            if not bound or bound['device']!=previous: raise Conflict('Browser association changed; refresh before saving')
            if not db.execute('SELECT 1 FROM devices WHERE id=?', (target,)).fetchone(): raise ValueError('Device unavailable')
            db.execute('UPDATE browsers SET device=? WHERE id=?', (target, browser))
            # Remove only an empty former association, not another browser's name or presence.
            db.execute('DELETE FROM devices WHERE id=? AND NOT EXISTS (SELECT 1 FROM browsers WHERE device=?)', (previous, previous))
            return self._view(db, browser)
