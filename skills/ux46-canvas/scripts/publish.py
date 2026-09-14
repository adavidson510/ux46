#!/usr/bin/env python3
"""Publish a prepared Canvas only when its content changed. No model calls."""
import argparse
import json
from pathlib import Path
import sys

from board import BoardStore, Conflict, DEFAULT_STORE, validate


def publish(store, agent, room, base_version, board):
    clean = validate(board)
    current = store.read(agent, room)
    if current['version'] != base_version:
        raise Conflict(current)
    previous = current['board']
    if previous and validate(previous) == clean:
        return {'changed': False, 'version': current['version']}
    saved = store.save(agent, room, base_version, clean)
    return {'changed': True, 'version': saved['version']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=DEFAULT_STORE)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--room', required=True)
    parser.add_argument('--base-version', type=int, required=True)
    parser.add_argument('--file', required=True, help='Prepared Canvas JSON, or - for stdin')
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(24001) if args.file == '-' else Path(args.file).read_text()
        if len(raw.encode()) > 24000:
            raise ValueError('Board input exceeds 24 KB')
        result = publish(BoardStore(args.store), args.agent, args.room,
                         args.base_version, json.loads(raw))
        print(json.dumps(result))
        return 0
    except Conflict:
        print(json.dumps({'error': 'board_conflict', 'message': 'Canvas changed; read and reconcile before publishing. Nothing replaced.'}))
        return 2
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
