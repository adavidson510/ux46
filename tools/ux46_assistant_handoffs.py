"""Explicit human requests between configured rooms, with exact reply receipts.

This is a local owner helper, not an autonomous router. Automatic packets cannot
originate requests. One human message + destination identifies one delivery;
uncertain sends are never repeated. Native agents retain their normal controls.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit, quote
from ux46_agent_actions import AgentClient


def rooms(clients, query='', limit=20):
    """Search available adapters, independently of the monitoring selection."""
    ids=getattr(clients,'agent_ids',list(clients))
    def one(agent):
        try:
            code,result=clients[agent].request('GET','/api/rooms?complete=1&limit='+str(limit)+'&query='+quote(query,safe=''))
            if code!=200:raise OSError()
            rows=[{'agent':agent,'room':r['id'],'title':r.get('title',r['id']),
                'project':r.get('project_name',''),'runtime':r.get('runtime',''),
                'key':agent+'/'+r['id']} for r in result.get('rooms',[]) if r.get('runtime')]
            return rows,[],bool(result.get('truncated'))
        except Exception:return [],[agent],False
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(one,ids))
    found=[r for rows,_,_ in results for r in rows]
    return {'rooms':found[:limit],'unavailable_agents':[a for _,errors,_ in results for a in errors],
            'truncated':len(found)>limit or any(t for _,_,t in results)}


def resolve(cfg, room, clients):
    if room.count('/')==2:
        agent,project,session=room.split('/');dst={'agent':agent,'room':project+'/'+session}
    else:
        matches=[r for r in cfg['sources'] if room==r['room'] or room.casefold()==r['title'].casefold()]
        if not matches:
            found=rooms(clients,room.split('/')[0] if '/' in room else room,100)
            matches=[r for r in found['rooms'] if room==r['room'] or room.casefold()==r['title'].casefold()]
        if len(matches)!=1:raise ValueError('Search rooms and choose its returned key; this name is ambiguous or unavailable')
        dst=dict(matches[0])
    code,detail=clients[dst['agent']].request('GET','/api/room/'+dst['room']+'?inspect=1')
    tid=detail.get('native',{}).get('thread_id')
    if code!=200 or not tid:raise ValueError('This room has no available native conversation')
    if dst.get('thread') and dst['thread']!=tid:raise ValueError('Destination changed; search rooms again')
    dst.update(thread=tid,title=detail.get('title') or dst.get('title') or dst['room'])
    if dst['agent']==cfg['target']['agent'] and tid==cfg['target']['thread']:raise ValueError('Answer in this assistant conversation directly')
    return dst


class Handoffs:
    def __init__(self, directory):
        self.root = Path(directory); self.path = self.root / 'assistant-handoffs.sqlite3'
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, body TEXT)')
        self.path.chmod(0o600)

    def db(self): return sqlite3.connect(self.path, timeout=10)
    def view(self):
        with self.db() as c: rows = [json.loads(r[0]) for r in c.execute('SELECT body FROM requests')]
        return sorted(rows, key=lambda r:r['at'], reverse=True)[:100]
    def save(self, item):
        with self.db() as c: c.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (item['id'], json.dumps(item)))

    def ask(self, cfg, room, request, body, clients, readback=False):
        dst = resolve(cfg,room,clients); src = cfg['target']
        if not request.strip() or not body.strip() or len(body)>6000: raise ValueError('A short human request is required')
        sender = clients[src['agent']]; receiver = clients[dst['agent']]
        code, detail = sender.request('GET','/api/room/'+src['room']+'?inspect=1')
        if code!=200 or detail.get('native',{}).get('thread_id')!=src['thread']: raise ValueError('Assistant identity changed')
        code, history = sender.request('GET','/api/room/'+src['room']+'/history?limit=100&direction=desc')
        if code!=200 or history.get('unavailable'): raise ValueError('Cannot verify the human request')
        human = next((x for x in history.get('items',[]) if x.get('type')=='userMessage'),None)
        # The newest input must be this human request, not an update packet or
        # an old request retrieved from memory. This also bounds accidental loops.
        if not human or human.get('text','').strip()!=request.strip() or request.lstrip().startswith('CONCIERGE UPDATE PACKET'):
            raise ValueError('Only the current human message can send a handoff')
        ident = 'ask-' + hashlib.sha256((src['thread']+'\0'+str(human['id'])+'\0'+dst['agent']+'\0'+dst['thread']).encode()).hexdigest()[:40]
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT body FROM requests WHERE id=?',(ident,)).fetchone()
            if old:
                old=json.loads(old[0])
                if old['request']!=body: raise ValueError('This request was already sent with different wording; inspect its receipt')
                return old
            item={'id':ident,'at':time.time(),'source':src,'destination':dst,'request':body,'state':'checking','readback':bool(readback)}
            c.execute('INSERT INTO requests VALUES (?,?)',(ident,json.dumps(item)))
        dispatched=False
        try:
            code, d = receiver.request('GET','/api/room/'+dst['room']+'?inspect=1')
            if code!=200 or d.get('native',{}).get('thread_id')!=dst['thread']: raise ValueError('Destination changed or unavailable')
            # The normal native submit path steers active work, starts idle work,
            # and preserves native approvals. It never interrupts a running turn.
            item['state']='sending';self.save(item);dispatched=True
            message=('Human request relayed through '+cfg.get('name','Assistant')+'.\n'
                     'Answer in this room; the assistant will bring your reply back. Keep your existing work and action boundaries.\n\n'+body)
            code, result = receiver.request('POST','/api/room/'+dst['room']+'/submit',
                {'client_id':ident,'thread_id':dst['thread'],'body':message})
            submission=result.get('submission',{})
            if submission.get('status')=='accepted':
                item.update(state='sent',turn=submission.get('native_turn_id',''),mode=submission.get('mode',''))
            elif submission.get('status')=='failed':item['state']='failed'
            else:item['state']='unknown'
        except Exception:
            item['state']='unknown' if dispatched else 'failed'
        self.save(item);return item

    def track(self, clients):
        """Read receipts and exact native turn replies; no destination writes."""
        for item in self.view():
            if item['state'] not in ('sent','sending','unknown'): continue
            dst=item['destination']
            try:client=clients[dst['agent']]
            except (KeyError,ValueError,OSError):continue
            try:
                if not item.get('turn'):
                    code,r=client.request('GET','/api/submissions/'+item['id'])
                    receipt=r.get('submission',{})
                    if code!=200:continue
                    if receipt.get('status')=='failed':item['state']='failed';self.save(item);continue
                    if receipt.get('status')!='accepted':continue
                    item.update(state='sent',turn=receipt.get('native_turn_id',''))
                    self.save(item)
                if not item.get('turn'):continue
                code,d=client.request('GET','/api/room/'+dst['room']+'?inspect=1')
                if code!=200 or d.get('native',{}).get('thread_id')!=dst['thread']:continue
                cursor='';reply=None
                for _ in range(4):
                    code,page=client.request('GET','/api/room/'+dst['room']+'/history?limit=40&direction=desc'+('&cursor='+quote(cursor,safe='') if cursor else ''))
                    if code!=200 or page.get('unavailable'):break
                    reply=next((x for x in page.get('items',[]) if x.get('turn_id')==item['turn'] and x.get('type')=='agentMessage' and x.get('phase')=='final_answer'),None)
                    if reply or page.get('complete'):break
                    cursor=page.get('next_cursor')
                    if not cursor:break
                if reply and d.get('native',{}).get('active_turn')!=item['turn']:
                    text=reply.get('text','');limit=40000 if item.get('readback') else 8000
                    item.update(state='answered',reply=text[:limit],reply_complete=len(text)<=limit,reply_id=reply['id'],answered_at=time.time())
                    self.save(item)
            except Exception:continue


def local_clients(directory, cfg):
    """Reuse the installation's owner-authenticated loopback adapter boundary."""
    config=json.loads((Path(directory)/'assistant-host.json').read_text())
    base=urlsplit(config['console'])
    if base.scheme!='http' or base.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('Use the existing local console')
    origin=config['origin'];host=urlsplit(origin).netloc
    server=SimpleNamespace(console=SimpleNamespace(host=base.hostname,port=base.port),
        auth=SimpleNamespace(identity_header=config.get('identity_header','Tailscale-User-Login'),origin_for=lambda _:origin))
    if config.get('additional_agents'):
        from ux46_live_agents import LiveAgents
        server.live_agents=LiveAgents(config['additional_agents'])
    handler=SimpleNamespace(server=server,headers={'Host':host,server.auth.identity_header:config['user']})
    class Clients(dict):
        def __missing__(self,agent):
            self[agent]=AgentClient(handler,agent);return self[agent]
    clients=Clients();first=clients[cfg['target']['agent']]
    code,listing=first._main('GET','/api/agents')
    if code!=200:raise ValueError('Agent inventory unavailable')
    ids={a['id'] for a in listing.get('agents',[])}
    if getattr(server,'live_agents',None):
        registry=server.live_agents.read()
        if registry:ids.update(a.id for a in registry.agents.values() if not a.is_local)
    clients.agent_ids=sorted(ids)
    return clients


def main():
    from ux46_concierge import Concierge
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True)
    p.add_argument('action',choices=['rooms','ask','status']);p.add_argument('--room');p.add_argument('--request');p.add_argument('--text')
    p.add_argument('--query',default='');p.add_argument('--readback',action='store_true')
    a=p.parse_args();store=Handoffs(a.directory);cfg=Concierge(a.directory).settings()
    try:
        if a.action=='rooms':result=rooms(local_clients(a.directory,cfg),a.query)
        elif a.action=='ask':
            if not a.room or not a.request or not a.text:raise ValueError('Use --room, --request (exact human wording), and --text')
            result=store.ask(cfg,a.room,a.request,a.text,local_clients(a.directory,cfg),a.readback)
        else:
            store.track(local_clients(a.directory,cfg));result={'requests':store.view()[:10]}
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else 'Handoff unavailable; inspect status before trying again'}));return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
