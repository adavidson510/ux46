"""Register an explicitly selected local file in an existing UX46 adapter store."""
import argparse
import json
from pathlib import Path
from atlas_files import FileStore, FileStoreError, MAX_UPLOAD_BYTES
from room import reference


def upload_file(store_path, agent, room, file_path):
    reference(agent, room)
    store_path = Path(store_path).expanduser()
    if not (store_path / 'atlas-files.sqlite3').is_file():
        raise ValueError('Select the existing attachment store of the serving adapter; no store was created')
    source = Path(file_path).expanduser()
    if not source.is_file():
        raise ValueError('Select an existing local file')
    with source.open('rb') as stream:
        data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('File exceeds the 20 MiB upload limit')
    record = FileStore(store_path).upload(room, source.name, data, project=room.split('/')[0])
    return {'file_agent': agent, 'file_id': record['id'], **record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files-store', type=Path, required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--room', required=True)
    parser.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(upload_file(args.files_store, args.agent, args.room, args.file)))
        return 0
    except (ValueError, OSError, FileStoreError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
