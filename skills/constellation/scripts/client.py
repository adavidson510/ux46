#!/usr/bin/env python3
"""Portable Constellation client: Python standard library, no model/provider SDK."""
from __future__ import annotations
import argparse
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class UnixConnection(http.client.HTTPConnection):
    def __init__(self,path):super().__init__('localhost',timeout=15);self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout);self.sock.connect(self.path)


class Refused(Exception):
    def __init__(self, message, status=None):
        super().__init__(message);self.status=status


class Client:
    def __init__(self,config,credential=None):
        self.config=config
        self.credential=credential
        root=Path(config['state_dir']).expanduser()
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        scope=hashlib.sha256(json.dumps({k:config.get(k) for k in ('principal','url','socket')},sort_keys=True).encode()).hexdigest()[:20]
        self.db_path=root/(scope+'.sqlite3')
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,at REAL,expires REAL,body TEXT);
                CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY,at REAL,body TEXT);
                CREATE TABLE IF NOT EXISTS acknowledged(key TEXT PRIMARY KEY,at REAL,body TEXT);''')
        os.chmod(self.db_path,0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.db_path,timeout=5)
        try:
            with db:yield db
        finally:db.close()

    def request(self,operation,args):
        raw=json.dumps({'operation':operation,'args':args},ensure_ascii=False).encode()
        if len(raw)>16000:raise Refused('Request exceeds 16 KB')
        if self.config.get('socket'):
            conn=UnixConnection(str(Path(self.config['socket']).expanduser()))
            try:
                conn.request('POST','/v1/call',body=raw,headers={'Content-Type':'application/json'})
                response=conn.getresponse();body=response.read(100001);status=response.status
            finally:conn.close()
        else:
            url=self.config['url'].rstrip('/')
            parsed=urlsplit(url)
            if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise Refused('An exact HTTPS service origin is required')
            token=self.credential or os.environ.get(self.config.get('token_env','CONSTELLATION_TOKEN'),'')
            if not token:raise Refused('Configured credential is unavailable; no fallback identity')
            request=Request(url+'/v1/call',data=raw,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            try:
                with build_opener(NoRedirect).open(request,timeout=15) as response:
                    body=response.read(100001);status=response.status
            except HTTPError as exc:
                status=exc.code;body=exc.read(100001)
        if len(body)>100000:raise Refused('Oversized service reply')
        if status==503:raise OSError('Service temporarily unavailable')
        if status!=200:
            if status in (401,403):
                with self.db() as db:db.execute('DELETE FROM cache')
            raise Refused('Service refused request ('+str(status)+'); correct it before retrying',status)
        answer=json.loads(body)
        if answer.get('principal')!=self.config['principal']:
            raise Refused('Service principal mismatch')
        return answer

    def call(self,operation,args):
        cache_key=hashlib.sha256(json.dumps([operation,args],sort_keys=True).encode()).hexdigest()
        try:
            answer=self.request(operation,args);result=answer['result']
            if operation in ('lookup','brief') and all(r.get('classification')=='private' for r in result.get('items',[])):
                with self.db() as db:
                    ttl=min(300,max(0,int(answer.get('cache_ttl_seconds',0))))
                    db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',(cache_key,time.time(),time.time()+ttl,json.dumps(result)))
                    db.execute('DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY at DESC LIMIT 20)')
            elif operation in ('lookup','brief'):
                # A fresh confidential result must not leave an older private copy of this query usable offline.
                with self.db() as db:db.execute('DELETE FROM cache WHERE key=?',(cache_key,))
            return result
        except (OSError,URLError,TimeoutError,http.client.HTTPException):
            if operation in ('lookup','brief'):
                with self.db() as db:
                    row=db.execute('SELECT at,body FROM cache WHERE key=? AND expires>?',(cache_key,time.time())).fetchone()
                if row:
                    value=json.loads(row[1]);value.update(offline=True,cached_at=row[0],cache_age_seconds=round(time.time()-row[0]))
                    return value
            if operation in ('capture','feedback'):
                key=args.get('key')
                if not isinstance(key,str) or not key:raise Refused('Offline write requires an idempotency key')
                if operation=='capture' and args.get('record',{}).get('classification')=='confidential':
                    raise Refused('Confidential capture is online-only')
                if operation=='feedback':
                    with self.db() as db:
                        cached=[json.loads(row[0]) for row in db.execute('SELECT body FROM cache WHERE expires>?',(time.time(),))]
                    if not any(r.get('id')==args.get('id') and r.get('revision')==args.get('revision') and r.get('classification')=='private'
                               for result in cached for r in result.get('items',[])):
                        raise Refused('Offline feedback requires a fresh private lesson in this client cache')
                body=json.dumps({'operation':operation,'args':args},sort_keys=True)
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT body FROM outbox WHERE key=?',(key,)).fetchone()
                    if row and row[0]!=body:raise Refused('Outbox key conflict')
                    if not row and db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]>=50:
                        raise Refused('Outbox full (50); reconnect before adding more')
                    db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?)',(key,time.time(),body))
                return {'accepted':False,'pending':True,'key':key,'delivery':'unknown until idempotent replay succeeds'}
            raise Refused('Service unavailable; no fresh scoped result')

    def flush(self):
        with self.db() as db:rows=db.execute('SELECT key,body FROM outbox ORDER BY at LIMIT 20').fetchall()
        accepted=0
        for key,body in rows:
            request=json.loads(body)
            result=self.request(request['operation'],request['args'])
            if not result['result'].get('accepted'):raise Refused('Write was not accepted')
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO acknowledged VALUES (?,?,?)',(key,time.time(),body))
                db.execute('DELETE FROM outbox WHERE key=?',(key,))
                db.execute('DELETE FROM acknowledged WHERE key NOT IN (SELECT key FROM acknowledged ORDER BY at DESC LIMIT 100)')
            accepted+=1
        with self.db() as db:pending=db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        return {'accepted':accepted,'pending':pending}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    installed_config=Path(__file__).resolve().parents[1]/'connection.json'
    p.add_argument('--config',default=os.environ.get('CONSTELLATION_CONFIG','') or (str(installed_config) if installed_config.is_file() else ''))
    p.add_argument('operation',choices=['brief','catalog','review','lookup','get','source','capture','feedback','changes','health','flush'])
    p.add_argument('--json-file',help='Arguments file, or - for stdin')
    p.add_argument('--query',default='')
    p.add_argument('--project',default='')
    p.add_argument('--id',help='Lesson ID for get or source')
    p.add_argument('--revision',type=int,help='Exact published lesson revision')
    p.add_argument('--source-id',help='Source ID from a brief or lesson')
    p.add_argument('--historical',action='store_true',help='Explicitly read a source from an older published revision')
    args=p.parse_args()
    if not args.config:p.error('Configure CONSTELLATION_CONFIG or --config; no implicit service/identity')
    try:
        client=Client(json.loads(Path(args.config).expanduser().read_text()))
        payload=json.loads(sys.stdin.read(16001) if args.json_file=='-' else Path(args.json_file).read_text()) if args.json_file else {}
        if args.operation in ('lookup','brief'):payload.update(query=args.query or payload.get('query',''),project=args.project or payload.get('project',''))
        if args.id:
            if args.operation=='get':
                payload.update(id=args.id)
                if args.revision is not None:payload['revision']=args.revision
            elif args.operation=='source':
                if not args.source_id or args.revision is None:p.error('source requires --id, --revision and --source-id')
                payload={'refs':[dict(id=args.id,revision=args.revision,source_id=args.source_id,**({'historical':True} if args.historical else {}))]}
            else:p.error('--id is supported by get and source')
        result=client.flush() if args.operation=='flush' else client.call(args.operation,payload)
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    except (Refused,ValueError,KeyError,OSError) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,Refused) else 'Client configuration, input or transport unavailable'}));return 1
    return 0


if __name__=='__main__':sys.exit(main())
