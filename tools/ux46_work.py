"""Owner-local dispositions, room reviews and experiments around referenced sources.

Tell keeps its posts; Constellation keeps lessons. This store owns human workflow
state, so the same controls also work when those optional services are absent.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from constellation_store import Store, Conflict, encoded, identifier, text
from ux46_boards import target


def line(value,limit=1000):return text(value,limit,'Text',True)
def url(value):
    value=line(value,1800)
    if not value:return ''
    if value.startswith('/') and not value.startswith('//'):return value
    p=urlsplit(value)
    if p.scheme not in ('https','http') or not p.netloc or p.username or p.password:raise ValueError('Use an http(s) result URL or a workspace path')
    return value

class WorkStore(Store):
    def __init__(self,path):
        super().__init__(path)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS work_sources(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS work_routes(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS work_experiments(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS work_results(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS work_rooms(id TEXT PRIMARY KEY,body TEXT);''')
    def rows(self,table):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT body FROM '+table)]
    def get(self,table,ident):
        with self.db() as db:row=db.execute('SELECT body FROM '+table+' WHERE id=?',(ident,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self,table,ident,body):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO '+table+' VALUES (?,?)',(ident,encoded(body).decode()))
    def mutate(self,table,ident,base,fn):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT body FROM '+table+' WHERE id=?',(ident,)).fetchone()
            old=json.loads(row[0]) if row else {'id':ident,'version':0}
            if old['version']!=base:raise Conflict('This item changed; refresh before saving')
            new=fn(old);new.update(id=ident,version=base+1,updated=time.time())
            db.execute('INSERT OR REPLACE INTO '+table+' VALUES (?,?)',(ident,encoded(new).decode()))
        return new
    def observe(self,source):
        ident=identifier(source['id']);kind=source['kind']
        if kind not in ('tell','constellation','idea'):raise ValueError('Unknown source kind')
        clean={k:line(source.get(k,''),limit) for k,limit in [('title',180),('summary',4000),('why',1000),('proposed_test',1000)]}
        refs=source.get('sources',[])
        if not isinstance(refs,list) or len(encoded(refs))>6000:raise ValueError('Source references too large')
        clean['lesson_id']=line(source.get('lesson_id',''),96)
        clean['board']=line(source.get('board',''),60)
        clean.update(kind=kind,sources=refs,locator=url(source.get('locator','')),source_revision=line(str(source.get('source_revision','')),120))
        digest=hashlib.sha256(encoded(clean)).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT body FROM work_sources WHERE id=?',(ident,)).fetchone()
            old=json.loads(row[0]) if row else {'id':ident,'version':0,'disposition':'active','interest':False,'created':time.time()}
            if old.get('digest')==digest:return old
            new={**old,**clean,'digest':digest,'version':old['version']+1,'updated':time.time()}
            db.execute('INSERT OR REPLACE INTO work_sources VALUES (?,?)',(ident,encoded(new).decode()))
        return new
    def refresh_sources(self,knowledge=None):
        # Deterministic reads of already-selected sources only; no model or new discovery.
        sources=self.rows('work_sources');boards={s.get('board') for s in sources if s['kind']=='tell' and s.get('board')}
        for board in sorted(boards):
            if board not in ('daily-review','working-better','bigger-picture','invention-watch'):continue
            try:
                from atlas_tell import request
                status,payload=request('GET','/boards/'+board)
                if status!=200:continue
                posts={p['id']:p for p in payload.get('posts',[])}
                for s in sources:
                    if s['kind']!='tell' or s.get('board')!=board:continue
                    post=posts.get(s['id'].removeprefix('tell-'))
                    if post:self.observe({**s,'title':post['title'],'summary':post.get('human_body') or post.get('body',''),'sources':post.get('sources',[])})
            except (OSError,ValueError,KeyError):continue
        if knowledge:
            for s in sources:
                if s['kind']!='constellation' or not s.get('lesson_id'):continue
                try:
                    r=knowledge(s['lesson_id'])
                    self.observe({**s,'title':r['claim'][:180],'summary':r.get('rationale',r['claim']),
                        'why':r.get('applies',''),'proposed_test':r.get('learning',{}).get('check',''),
                        'source_revision':str(r['revision']),'sources':r['sources']})
                except (ValueError,OSError,PermissionError):continue

    def view(self,agent='',room='',lane='active'):
        if agent or room:target(agent,room)
        sources=self.rows('work_sources');routes=[r for r in self.rows('work_routes') if not r.get('inactive')];experiments=self.rows('work_experiments')
        results=self.rows('work_results');rooms={r['id']:r for r in self.rows('work_rooms')}
        index={s['id']:s for s in sources}
        for route in routes:
            source=index.get(route['source']);context=rooms.get(route['agent']+':'+route['room'],{}).get('context','')
            route['needs_review']=bool(source and (route.get('assessed_digest')!=source['digest'] or route.get('assessed_context','')!=context))
            route['status']='Waiting for room review' if route['needs_review'] else {'applies':'Worth trying','not-applicable':'Not relevant here','covered':'Already covered','test':'Worth trying'}.get(route.get('assessment'),'Reviewed')
            route['title']=source['title'] if source else 'Source unavailable'
            if source:route['current_digest']=source['digest']
        if room:
            routes=[r for r in routes if r['agent']==agent and r['room']==room]
            ids={r['source'] for r in routes};sources=[s for s in sources if s['id'] in ids]
            experiments=[e for e in experiments if e['agent']==agent and e['room']==room]
            results=[r for r in results if r['agent']==agent and r['room']==room]
        if lane=='active':sources=[s for s in sources if s['disposition']!='dismissed' and s.get('snooze_until',0)<=time.time()]
        elif lane=='dismissed':sources=[s for s in sources if s['disposition']=='dismissed' or s.get('snooze_until',0)>time.time()]
        return {'sources':sorted(sources,key=lambda x:x['updated'],reverse=True)[:100],'routes':routes[:200],
          'experiments':sorted(experiments,key=lambda x:x['updated'],reverse=True)[:100],
          'results':sorted(results,key=lambda x:x['updated'],reverse=True)[:20],
          'metrics':{'interested':sum(s.get('interest',False) for s in index.values()),'routed':len(routes),
             'awaiting_review':sum(r['needs_review'] for r in routes),'tried':len(experiments),
             'helped':sum(e.get('outcome')=='helped' for e in experiments),'failed':sum(e.get('outcome')=='failed' for e in experiments),
             'unknown':sum(e.get('outcome')=='unknown' for e in experiments)},'coverage':'Reported outcomes; interest and delivery are not evidence of success.'}
    def action(self,args):
        action=args['action']
        if action=='observe':return self.observe(args['source'])
        if action in ('dismiss','restore','snooze','interest'):
            def change(old):
                if 'kind' not in old:raise ValueError('Missing source')
                if action in ('dismiss','restore'):old.update(disposition='dismissed' if action=='dismiss' else 'active',snooze_until=0)
                if action=='snooze':old['snooze_until']=time.time()+86400
                if action=='interest':old['interest']=True
                return old
            return self.mutate('work_sources',identifier(args['id']),args['base_version'],change)
        if action=='route':
            agent,room=target(args['agent'],args['room']);source=self.get('work_sources',args['source'])
            if not source:raise ValueError('Missing source')
            ident=hashlib.sha256(encoded([source['id'],agent,room])).hexdigest()[:32]
            existing=self.get('work_routes',ident)
            if existing and (not existing.get('inactive') or args.get('automatic')):return existing
            if existing:return self.mutate('work_routes',ident,existing['version'],lambda old:{**old,'inactive':False,'reason':text(args['reason'],1000,'Reason')})
            return self.mutate('work_routes',ident,0,lambda _:dict(source=source['id'],agent=agent,room=room,
                reason=text(args['reason'],1000,'Reason'),assessment='',assessed_digest='',assessed_context=''))
        if action=='unroute':
            return self.mutate('work_routes',args['id'],args['base_version'],lambda old:{**old,'inactive':True})
        if action=='context':
            agent,room=target(args['agent'],args['room']);ident=agent+':'+room
            old=self.get('work_rooms',ident) or {'version':0}
            value=line(args['context'],500)
            if old.get('context')==value:return old
            return self.mutate('work_rooms',ident,args.get('base_version',old['version']),lambda _:dict(context=value))
        if action=='assess':
            route=self.get('work_routes',args['id'])
            if not route:raise ValueError('Missing route')
            source=self.get('work_sources',route['source'])
            if args.get('source_digest')!=source['digest']:raise Conflict('The source changed; assess the latest revision')
            if args['verdict'] not in ('applies','not-applicable','covered','test'):raise ValueError('Invalid assessment')
            context=(self.get('work_rooms',route['agent']+':'+route['room']) or {}).get('context','')
            return self.mutate('work_routes',route['id'],args['base_version'],lambda old:{**old,'assessment':args['verdict'],
               'assessment_reason':text(args['reason'],1500,'Assessment reason'),'assessed_digest':source['digest'],
               'assessed_context':context,'assessed_at':time.time()})
        if action=='experiment':
            agent,room=target(args['agent'],args['room']);source=self.get('work_sources',args['source'])
            if not source:raise ValueError('Missing source')
            ident=identifier(args.get('id') or uuid.uuid4().hex)
            values={k:text(args[k],1200,k) for k in ('hypothesis','baseline','check','stop')}
            return self.mutate('work_experiments',ident,args.get('base_version',0),lambda old:{**old,**values,'agent':agent,'room':room,
               'source':source['id'],'source_digest':source['digest'],'source_revision':source['source_revision'],'owner':line(args.get('owner','Room owner'),100),
               'state':'chosen','outcome':'','result_id':''})
        if action=='outcome':
            if not self.get('work_experiments',args['id']):raise ValueError('Missing experiment')
            if args['verdict'] not in ('used','helped','failed','unknown'):raise ValueError('Invalid outcome')
            reason=text(args['reason'],1500,'Observed result');evidence=text(args['evidence'],1500,'Evidence or why unresolved')
            return self.mutate('work_experiments',args['id'],args['base_version'],lambda old:{**old,'state':'running' if args['verdict']=='used' else 'reviewed',
              'outcome':args['verdict'],'reason':reason,'evidence':evidence,'measurement':line(args.get('measurement',''),500)})
        if action=='result':
            agent,room=target(args['agent'],args['room']);ident=hashlib.sha256(encoded([agent,room])).hexdigest()[:32]
            values={k:line(args.get(k,''),n) for k,n in [('title',180),('summary',1500),('artifact_version',150),('changed',2000),('checked',2000),('unchecked',1200),('evidence',2000),('article',8000)]}
            values.update(agent=agent,room=room,url=url(args.get('url','')),changes_url=url(args.get('changes_url','')),
                reporter=line(args.get('reporter','Room owner'),100),experiment=args.get('experiment',''))
            exp=values['experiment']
            if exp:
                e=self.get('work_experiments',exp)
                if not e or (e['agent'],e['room'])!=(agent,room):raise ValueError('Experiment belongs to another room')
            return self.mutate('work_results',ident,args.get('base_version',0),lambda old:{**values,'previous':[{k:v for k,v in old.items() if k!='previous'}]+old.get('previous',[])[:4] if old['version'] else []})
        raise ValueError('Unknown work action')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--store',required=True);p.add_argument('--agent',default='');p.add_argument('--room',default='');p.add_argument('--file');args=p.parse_args()
    store=WorkStore(args.store)
    result=store.action(json.loads(Path(args.file).read_text())) if args.file else store.view(args.agent,args.room,lane='all')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
