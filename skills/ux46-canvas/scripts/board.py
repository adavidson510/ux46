"""Small, versioned conversation boards. No native process or model calls."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys

DEFAULT_STORE = Path.home() / '.local/state/ux46/boards.sqlite3'
STATES = {'info', 'todo', 'doing', 'done', 'blocked'}


class Conflict(Exception):
    def __init__(self, current):
        self.current = current
        super().__init__('This board changed elsewhere. Your edit has not replaced it.')


def target(agent, room):
    if not isinstance(agent, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', agent):
        raise ValueError('Invalid agent reference')
    if not isinstance(room, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}', room):
        raise ValueError('Invalid conversation reference')
    return agent, room


def short(value, limit, name, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{name} needs {1 if required else 0}–{limit} characters')
    if any(ord(c) < 32 for c in value):
        raise ValueError(f'{name} must fit on one line')
    return value.strip()


def validate(board):
    if not isinstance(board, dict):
        raise ValueError('A board must contain a title and sections')
    clean = {'title': short(board.get('title'), 80, 'Title'),
             'reporter': short(board.get('reporter'), 80, 'Updated by'), 'sections': []}
    sections = board.get('sections')
    if not isinstance(sections, list) or len(sections) > 8:
        raise ValueError('Use up to eight sections')
    count = 0
    for section in sections:
        if not isinstance(section, dict): raise ValueError('Invalid section')
        out = {'title': short(section.get('title'), 60, 'Section name'), 'items': []}
        items = section.get('items')
        if not isinstance(items, list): raise ValueError('A section needs a list of items')
        count += len(items)
        if count > 24: raise ValueError('Keep the board to 24 items; put longer plans in project files')
        for item in items:
            if not isinstance(item, dict): raise ValueError('Invalid board item')
            row = {'label': short(item.get('label'), 120, 'Item name')}
            for key, limit in [('value', 80), ('detail', 300)]:
                if item.get(key): row[key] = short(item[key], limit, key.capitalize())
            state = item.get('state', 'info')
            if state not in STATES: raise ValueError('Unknown board item state')
            row['state'] = state
            out['items'].append(row)
        if section.get('chart') is not None:
            chart = section['chart']
            if not isinstance(chart, dict) or chart.get('type') not in ('line', 'bar'):
                raise ValueError('Use a line or bar chart')
            labels, values = chart.get('labels'), chart.get('values')
            if not isinstance(labels, list) or not isinstance(values, list) or not 1 <= len(labels) <= 24 or len(labels) != len(values):
                raise ValueError('A chart needs 1–24 matching labels and values')
            if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e15 for v in values):
                raise ValueError('Chart values must be finite numbers no larger than 10^15')
            out['chart'] = {'type': chart['type'], 'labels': [short(v,40,'Chart label') for v in labels], 'values': values}
            if chart.get('unit'): out['chart']['unit'] = short(chart['unit'],30,'Chart unit')
        if section.get('image') is not None:
            picture = section['image']
            if not isinstance(picture, dict) or not isinstance(picture.get('file_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', picture['file_id']):
                raise ValueError('Use an uploaded image ID, not a path or external URL')
            agent = picture.get('file_agent')
            target(agent, 'image/reference')
            out['image'] = {'file_id': picture['file_id'], 'file_agent': agent,
                            'alt': short(picture.get('alt'),160,'Image description')}
        clean['sections'].append(out)
    return clean


class BoardStore:
    def __init__(self, path=DEFAULT_STORE):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS boards (agent TEXT, room TEXT, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(agent,room))')
            db.execute('CREATE TABLE IF NOT EXISTS board_revisions (agent TEXT, room TEXT, version INTEGER, body TEXT NOT NULL, PRIMARY KEY(agent,room,version))')
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _read(self, db, agent, room):
        row = db.execute('SELECT version,body FROM boards WHERE agent=? AND room=?', (agent,room)).fetchone()
        return {'version': row[0], 'board': json.loads(row[1])} if row else {'version': 0, 'board': None}

    def read(self, agent, room):
        target(agent,room)
        with self.connect() as db: return self._read(db,agent,room)

    def save(self, agent, room, base_version, board):
        target(agent,room)
        if type(base_version) is not int or base_version < 0: raise ValueError('Read the board and supply its version first')
        board = validate(board)
        board['updated_at'] = datetime.now(timezone.utc).isoformat()
        body = json.dumps(board, ensure_ascii=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._read(db,agent,room)
            if current['version'] != base_version: raise Conflict(current)
            version = base_version + 1
            db.execute('INSERT OR REPLACE INTO boards VALUES(?,?,?,?)', (agent,room,version,body))
            db.execute('INSERT INTO board_revisions VALUES(?,?,?,?)', (agent,room,version,body))
            # Small reversible history, not a second transcript store.
            db.execute('DELETE FROM board_revisions WHERE agent=? AND room=? AND version<=?',(agent,room,version-20))
        return {'version': version, 'board': board}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=DEFAULT_STORE)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--room', required=True)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('get')
    write = subs.add_parser('set')
    write.add_argument('--base-version', type=int, required=True)
    write.add_argument('--file', required=True, help='JSON file or - for stdin')
    args = parser.parse_args()
    try:
        store = BoardStore(args.store)
        if args.command == 'get': result = store.read(args.agent,args.room)
        else:
            raw = sys.stdin.read(24001) if args.file == '-' else Path(args.file).read_text()
            if len(raw.encode()) > 24000: raise ValueError('Board input exceeds 24 KB')
            result = store.save(args.agent,args.room,args.base_version,json.loads(raw))
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Conflict as exc:
        print(json.dumps({'error':'board_conflict','message':str(exc),'detail':exc.current}))
        return 2
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
