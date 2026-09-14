#!/usr/bin/env python3
"""Owner-only deterministic maintenance and portable restore; never runs a model."""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from constellation_store import Store, Conflict, MAX_RECORDS, encoded

TABLES={'records':('id','revision','body'),'events':('seq','id','body'),
        'requests':('principal','key','hash','receipt'),'feedback':('id','revision','body'),
        'measurements':('id','principal','body'),'metadata':('key','value')}


def restore(export,destination,sha256):
    path=Path(export)
    if path.stat().st_size>32*1024*1024:raise ValueError('Export exceeds pilot restore bound')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha256:raise ValueError('Export checksum mismatch')
    if Path(destination).exists():raise Conflict('Restore requires a new destination')
    rows=[json.loads(line) for line in raw.splitlines()]
    if sum(r.get('table')=='records' for r in rows)>MAX_RECORDS:raise ValueError('Pilot record cap exceeded')
    for row in rows:
        if row.get('schema')!=1 or row.get('table') not in TABLES or set(row.get('row',{}))!=set(TABLES[row['table']]):
            raise ValueError('Unsupported export schema')
        if row['table']=='records':
            record=json.loads(row['row']['body'])
            if not record.get('projects') or not record.get('owner') or record.get('schema')!=1:raise ValueError('Invalid portable record')
            for source in record.get('sources',[]):
                if source.get('excerpt') and hashlib.sha256(source['excerpt'].encode()).hexdigest()!=source.get('sha256'):
                    raise ValueError('Source checksum mismatch')
    store=Store(destination)
    with store.db() as db:
        for table in TABLES:db.execute('DELETE FROM '+table)
        for row in rows:
            table=row['table'];columns=TABLES[table]
            db.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                       [row['row'][key] for key in columns])
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored database failed integrity check')
    return {'restored':True,'sha256':sha256,'records':sum(r['table']=='records' for r in rows)}


def maintain(database,directory):
    store=Store(database)
    root=Path(directory).expanduser()
    with store.db() as db:
        rows=[tuple(r) for r in db.execute('SELECT id,revision,body FROM records ORDER BY id')]
        rows.extend(tuple(r) for r in db.execute('SELECT id,revision,body FROM feedback ORDER BY id'))
        fingerprint=hashlib.sha256(encoded(rows)).hexdigest()
        old=db.execute("SELECT value FROM metadata WHERE key='maintenance' ").fetchone()
        if old:
            previous=json.loads(old[0]);generation=previous.get('generation','')
            intact=bool(generation and Path(generation).name==generation)
            for suffix,key in (('.sqlite3','backup'),('.jsonl','export')):
                path=root/(generation+suffix)
                intact=intact and path.is_file() and not path.is_symlink()
                if intact:intact=hashlib.sha256(path.read_bytes()).hexdigest()==previous.get(key,{}).get('sha256')
            if previous['fingerprint']==fingerprint and intact:return {'changed':False,'model_calls':0}
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    generation=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+fingerprint[:8]
    backup=root/(generation+'.sqlite3');export=root/(generation+'.jsonl')
    receipt={'generation':generation,'fingerprint':fingerprint,'backup':store.backup(backup),
             'export':store.export(export),'changed':True,'model_calls':0,'source_locators':'not probed'}
    with (root/(generation+'.receipt.json')).open('x') as out:
        os.chmod(out.name,0o600);json.dump(receipt,out,indent=2)
    with store.db() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('maintenance',encoded(receipt).decode()))
    return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['maintain','restore'])
    p.add_argument('--db',required=True);p.add_argument('--directory');p.add_argument('--export');p.add_argument('--sha256')
    args=p.parse_args()
    if args.operation=='maintain':
        if not args.directory:p.error('--directory required')
        result=maintain(args.db,args.directory)
    else:
        if not args.export or not args.sha256:p.error('--export and --sha256 required')
        result=restore(args.export,args.db,args.sha256)
    print(json.dumps(result))


if __name__=='__main__':main()
