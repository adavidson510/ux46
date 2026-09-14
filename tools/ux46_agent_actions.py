"""User-requested, bounded session reconnection; no model turns or service restarts."""
import json
import re
import sqlite3
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

ROOM = re.compile(r'[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,96}')
AGENT = re.compile(r'[a-z][a-z0-9-]{0,31}')
ACTIVE = {'discovering', 'refreshing', 'waiting'}


class AgentClient:
    """Forward the authenticated caller through existing configured adapters only."""
    def __init__(self, handler, agent):
        if not isinstance(agent,str) or not AGENT.fullmatch(agent): raise ValueError('Invalid agent')
        self.server=handler.server;self.agent=agent
        self.headers={'Host':handler.headers.get('Host',''),self.server.auth.identity_header:handler.headers.get(self.server.auth.identity_header,'')}
        self.origin=self.server.auth.origin_for(self.headers['Host'])
        self.extra=None;self.prefix=''
        status, listed=self._main('GET','/api/agents')
        if status!=200:raise OSError('Agent list unavailable')
        found=next((a for a in listed.get('agents',[]) if a.get('id')==agent),None)
        if found:self.prefix='' if found.get('kind')=='local' else '/api/agents/'+agent
        else:
            extra=getattr(self.server,'live_agents',None);registry=extra.read() if extra else None
            selected=registry.agents.get(agent) if registry else None
            if selected and not selected.is_local:
                self.extra=(registry,selected);found=selected.as_json()
        if not found:raise ValueError('Unknown agent')
        self.supported=found.get('runtime')=='codex'

    def _main(self, method, path, body=None):
        headers=dict(self.headers,Accept='application/json')
        if body is not None:
            status,boot=self._main('GET','/api/bootstrap')
            if status!=200 or not boot.get('csrf'):raise OSError('Console unavailable')
            headers.update({'Content-Type':'application/json','Origin':self.origin,'X-Atlas-CSRF':boot['csrf']})
        upstream=self.server.console;c=HTTPConnection(upstream.host,upstream.port,timeout=75 if method=='POST' else 15)
        try:
            c.request(method,path,json.dumps(body) if body is not None else None,headers)
            r=c.getresponse();raw=r.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise OSError('Response too large')
            return r.status,json.loads(raw)
        finally:c.close()

    def request(self, method, path, body=None):
        if self.extra:
            registry,agent=self.extra
            r=registry.proxy(agent,method,path,'',headers={'Content-Type':'application/json'},body=json.dumps(body).encode() if body is not None else None)
            return r.status,json.loads(r.body)
        return self._main(method,self.prefix+path,body)


class RefreshJobs:
    def __init__(self,path,interval=10):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.interval=interval;self.stopping=threading.Event()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, agent TEXT, at REAL, payload TEXT)')
            for id,raw in db.execute('SELECT id,payload FROM jobs').fetchall():
                job=json.loads(raw)
                if job['state'] in ACTIVE:
                    job.update(state='interrupted',message='Gateway restarted. Check sessions before requesting another refresh.')
                    db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(job),id))
        self.path.chmod(0o600)

    def _save(self,job):
        with self.lock,sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)',(job['id'],job['agent'],job['at'],json.dumps(job)))
            db.execute('DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY at DESC LIMIT 100)')

    def view(self,agent):
        if not isinstance(agent,str) or not AGENT.fullmatch(agent):raise ValueError('Invalid agent')
        with self.lock,sqlite3.connect(self.path) as db:
            row=db.execute('SELECT payload FROM jobs WHERE agent=? ORDER BY at DESC LIMIT 1',(agent,)).fetchone()
        return {'job':json.loads(row[0]) if row else None}

    def start(self,agent,client_id,client):
        if not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',client_id):raise ValueError('Invalid request id')
        if not client.supported:return {'supported':False,'message':'This runtime does not expose safe session-wide login refresh yet.'}
        with self.lock:
            with sqlite3.connect(self.path) as db:row=db.execute('SELECT agent,payload FROM jobs WHERE id=?',(client_id,)).fetchone()
            if row:
                if row[0]!=agent:raise ValueError('Request belongs to another agent')
                return {'job':json.loads(row[1])}
            previous=self.view(agent)['job']
            if previous and previous['state'] in ACTIVE:return {'job':previous}
            job={'id':client_id,'agent':agent,'at':time.time(),'state':'discovering','items':[],'message':'Checking connected sessions…'}
            self._save(job)
            threading.Thread(target=self._run,args=(job,client),daemon=True,name='ux46-refresh-'+agent).start()
            return {'job':json.loads(json.dumps(job))}

    def _run(self,job,client):
        try:
            status,boot=client.request('GET','/api/bootstrap')
            rows=boot.get('remembered_owned')
            if status!=200 or not isinstance(rows,list) or len(rows)>200:raise ValueError('Session inventory unavailable')
            seen={}
            for row in rows:
                room=row.get('room','');tid=row.get('thread_id','')
                if not isinstance(room,str) or not ROOM.fullmatch(room) or not isinstance(tid,str) or not tid:continue
                if tid in seen:
                    seen[tid]['aliases'].append(room);continue
                item={'room':room,'thread':tid,'aliases':[room],'state':'waiting'}
                seen[tid]=item;job['items'].append(item)
            # Refresh the read connection too; it otherwise retains the previous login.
            job['items'].append({'room':'','thread':'','state':'waiting','title':'Session list connection'})
            while not self.stopping.is_set():
                pending=[i for i in job['items'] if i['state']=='waiting']
                if not pending:break
                if time.time()-job['at']>86400:
                    for i in pending:i.update(state='expired',detail='Still busy after 24 hours. Request refresh again when ready.')
                    break
                job.update(state='refreshing',message='Reconnecting idle sessions…');self._save(job)
                for item in pending:
                    if not item['room'] and any(i['room'] and i['state']=='waiting' for i in job['items']):continue
                    self._step(job,item,client);self._save(job)
                if any(i['state']=='waiting' for i in job['items']):
                    job.update(state='waiting',message='Waiting for active turns or pending input to finish.');self._save(job)
                    self.stopping.wait(self.interval)
            bad=any(i['state'] in ('failed','unknown','expired') for i in job['items'])
            job.update(state='interrupted' if self.stopping.is_set() else 'partial' if bad else 'complete',message='Review session results.' if bad else 'Connections refreshed. No prompts resent.')
        except Exception:
            job.update(state='failed',message='Could not verify this agent’s sessions. Nothing was automatically retried.')
        self._save(job)

    def _step(self,job,item,client):
        mutating=False
        try:
            room=item['room']
            if room:
                status,detail=client.request('GET','/api/room/'+room)
                if status!=200:item.update(state='failed',detail='Could not read this session.');return
                native=detail.get('native') or {};worker=native.get('worker')
                if detail.get('ownership',{}).get('state')=='connecting':return
                if not worker or not worker.get('running'):item.update(state='skipped',detail='Not connected; its next connection uses the current login.');return
                if worker.get('thread_id')!=item['thread'] or worker.get('room') not in item.get('aliases',[room]):item.update(state='skipped',detail='Session ownership changed.');return
                room=worker['room'];item['room']=room
                if native.get('active_turn') or detail.get('approvals') or any(s.get('state') in ('pending','queued','in_flight','uncertain') for s in detail.get('submissions',[])):return
                if worker.get('started_at',0)>job['at']:item.update(state='refreshed',detail='Already connected since this request.');return
            mutating=True
            status,result=client.request('POST','/api/connection/refresh',{'room':room} if room else {})
            if status==409 and result.get('error')=='connection_busy':return
            connection=result.get('connection') or result
            if status==200 and connection.get('state')=='refreshed':item.update(state='refreshed',detail='Using the current saved login.')
            elif status>=500:item.update(state='unknown',detail='Refresh outcome unknown. Not retried; inspect this session.')
            else:item.update(state='failed',detail='Refresh refused. Inspect this session’s connection.')
        except Exception:
            item.update(state='unknown' if mutating else 'failed',detail='Connection did not answer. No refresh was repeated.')
