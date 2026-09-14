#!/usr/bin/env python3
"""Small deterministic Notes commands for UX46's shared owner store."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import uuid
from atlas_desktops import DesktopStore, Conflict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=Path.home()/'.local/state/atlas-console-v1/desktops.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add'); add.add_argument('text')
    sub.add_parser('list').add_argument('--all', action='store_true')
    sub.add_parser('check').add_argument('id')
    sub.add_parser('uncheck').add_argument('id')
    args = parser.parse_args()
    if not args.store.is_file(): parser.error('Owner store does not exist; select the UX46 host store with --store.')
    store = DesktopStore(args.store)
    if args.command == 'list':
        for note in store.read()['state'].get('notes', []):
            if args.all or not note.get('done'):
                print(f"[{'x' if note.get('done') else ' '}] {note['text']} ({note['id']})")
        return
    text = getattr(args, 'text', '').strip()
    if args.command == 'add' and (not text or len(text) > 1000): parser.error('Use 1–1000 characters for a note.')
    note_id = uuid.uuid4().hex
    for attempt in range(3):
        envelope = store.read(); state = envelope['state']
        notes = state.setdefault('notes', [])
        now = datetime.now(timezone.utc).isoformat()
        if args.command == 'add':
            notes.append({'id': note_id, 'text': text, 'done': False, 'created_at': now})
        else:
            matches = [n for n in notes if n.get('id') == args.id]
            if len(matches) != 1: parser.error('Use the exact note ID from list; no note changed.')
            matches[0].update(done=args.command == 'check', updated_at=now)
        try:
            store.save(envelope['version'], state)
            print('Note saved.' if args.command == 'add' else 'Note updated.')
            return
        except Conflict:
            if attempt == 2: parser.error('The workspace is changing; try again. Nothing was overwritten.')


if __name__ == '__main__':
    main()
