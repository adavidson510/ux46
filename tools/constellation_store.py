"""Bounded, portable shared learning. No model calls or transcript ingestion."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_RECORDS = 100
MAX_RECORD_BYTES = 12000
MAX_REPLY_BYTES = 18000
KINDS = {'principle', 'preference', 'decision', 'method', 'failure', 'component', 'usecase'}
STATES = {'proposed', 'supported', 'contradicted', 'superseded', 'retired'}
EVIDENCE = {'explicit-direction', 'observed', 'inference', 'proposal'}
ID = re.compile(r'^[A-Za-z0-9._-]{1,96}$')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def text(value, limit, name, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError('Invalid ' + name)
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('Invalid identifier')
    return value


class Conflict(ValueError):
    pass


class Unavailable(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    name: str
    projects: tuple[str, ...]
    write: bool = False

    def allows(self, record):
        return '*' in self.projects or set(record['projects']).issubset(self.projects)


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, revision INTEGER, body TEXT);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, id TEXT UNIQUE, body TEXT);
                CREATE TABLE IF NOT EXISTS requests(principal TEXT, key TEXT, hash TEXT, receipt TEXT,
                    PRIMARY KEY(principal,key));
                CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, revision INTEGER, body TEXT);
                CREATE TABLE IF NOT EXISTS measurements(id TEXT PRIMARY KEY, principal TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('started', str(time.time())))
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, principal, key):
        row = db.execute('SELECT body FROM records WHERE id=?', (identifier(key),)).fetchone()
        if not row or not principal.allows(json.loads(row['body'])):
            raise Unavailable('Record unavailable in this scope')
        return json.loads(row['body'])

    def _get_revision(self, db, principal, key, revision=None):
        current=self._get(db,principal,key)
        if revision is not None and (type(revision) is not int or revision<1):raise ValueError('Invalid revision')
        if revision is None or revision==current['revision']:return current
        for row in db.execute('SELECT body FROM events ORDER BY seq DESC'):
            record=json.loads(row[0]).get('record')
            if record and record['id']==key and record['revision']==revision:
                if not principal.allows(record):raise Unavailable('Revision unavailable in this scope')
                record['current_revision']=current['revision']
                if current['classification']=='confidential':record['classification']='confidential'
                return record
        raise Unavailable('Revision unavailable')

    def get(self, principal, key, revision=None):
        with self.db() as db:
            record = self._get_revision(db, principal, key, revision)
            brief = self._visible(db, principal, record)
            for source in brief['sources']:source.pop('excerpt',None)
            import constellation_learning as learning
            feedback=[json.loads(row[0]) for row in db.execute('SELECT body FROM feedback')]
            feedback=[f for f in feedback if principal.allows(f) and f['lesson_id']==record['id']]
            brief['outcome']=learning.outcomes(record,feedback)
            brief['warnings']=learning.warnings(record,brief['outcome'],[],time.time())
            return brief

    def _visible(self, db, principal, record):
        record = json.loads(json.dumps(record))
        visible = []
        for link in record.get('links', []):
            try:
                target = self._get(db, principal, link['target'])
                visible.append(dict(link, title=target['claim'], revision=target['revision']))
            except Unavailable:
                pass
        record['links'] = visible
        return record

    def _event(self, db, event):
        key = uuid.uuid4().hex
        event = dict(event, event_id=key, at=time.time())
        db.execute('INSERT INTO events(id,body) VALUES (?,?)', (key, encoded(event).decode()))
        return key

    def _request(self, db, principal, key, payload):
        identifier(key)
        row = db.execute('SELECT hash,receipt FROM requests WHERE principal=? AND key=?',
                         (principal.name, key)).fetchone()
        if row:
            if row['hash'] != digest(payload):
                raise Conflict('Idempotency key already names different content')
            return json.loads(row['receipt'])

    def _receipt(self, db, principal, key, payload, result):
        # Stop the experiment for review instead of growing an unbounded journal.
        if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]>=2000:
            raise Conflict('Pilot write budget reached; export and evaluate before expanding')
        db.execute('INSERT INTO requests VALUES (?,?,?,?)',
                   (principal.name, key, digest(payload), encoded(result).decode()))
        return result

    def capture(self, principal, payload):
        if not principal.write:
            raise PermissionError('Read-only principal')
        key = identifier(payload['key'])
        raw = payload['record']
        if not isinstance(raw, dict) or len(encoded(payload)) > MAX_RECORD_BYTES:
            raise ValueError('Keep one capture under 12 KB')
        rid = identifier(raw['id'])
        projects = raw.get('projects')
        if not isinstance(projects, list) or not 1 <= len(projects) <= 8:
            raise ValueError('Provide one to eight project scopes')
        projects = sorted(set(identifier(p) for p in projects))
        if not principal.allows({'projects': projects}):
            raise PermissionError('Project scope not granted')
        kind, state, evidence = raw.get('kind'), raw.get('state', 'proposed'), raw.get('evidence')
        if kind not in KINDS or state not in STATES or evidence not in EVIDENCE:
            raise ValueError('Invalid kind, state or evidence')
        classification = raw.get('classification', 'private')
        if classification not in ('private', 'confidential'):
            raise ValueError('Invalid classification')
        sources = raw.get('sources', [])
        if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
            raise ValueError('Provide one to three source references')
        clean_sources = []
        for source in sources:
            excerpt = text(source.get('excerpt', ''), 1800, 'excerpt', True)
            ref = {k: text(source.get(k), n, k) for k,n in
                   [('id',96), ('room',192), ('revision',128), ('locator',500)]}
            if ref['room'].split('/')[0] not in projects:
                raise ValueError('Source project must be included in record scope')
            ref.update(excerpt=excerpt, sha256=hashlib.sha256(excerpt.encode()).hexdigest() if excerpt else None)
            clean_sources.append(ref)
        terms = raw.get('terms', [])
        if not isinstance(terms, list) or len(terms) > 20:
            raise ValueError('At most twenty terms or aliases')
        links = raw.get('links', [])
        if not isinstance(links, list) or len(links) > 5:
            raise ValueError('At most five outgoing connections')
        clean_links = []
        for link in links:
            if link.get('type') not in ('may-reuse', 'implements', 'informs', 'contradicts', 'supersedes', 'example-of') or link.get('state') not in STATES:
                raise ValueError('Invalid connection type/state')
            clean_links.append({'target':identifier(link['target']), 'type':link['type'],
                                'state':link['state'], 'reason':text(link['reason'],400,'connection reason')})
        record = dict(id=rid, schema=1, projects=projects, kind=kind, state=state,
                      evidence=evidence, classification=classification,
                      claim=text(raw.get('claim'),500,'claim'),
                      rationale=text(raw.get('rationale'),900,'rationale'),
                      applies=text(raw.get('applies'),500,'applicability'),
                      limits=text(raw.get('limits'),500,'limits'),
                      terms=[text(t,60,'term') for t in terms], sources=clean_sources, links=clean_links)
        origin=raw.get('origin','unspecified')
        if origin not in ('human-direction','agent-discovery','joint-discovery','inference','unspecified'):
            raise ValueError('Invalid origin')
        learning=raw.get('learning',{})
        if not isinstance(learning,dict) or set(learning)-{'trigger','action','check'}:raise ValueError('Invalid learning fields')
        learning={k:text(v,500,'learning '+k) for k,v in learning.items()}
        subjects=raw.get('subjects',[])
        if not isinstance(subjects,list) or len(subjects)>8:raise ValueError('At most eight subjects')
        subjects=[text(v,80,'subject') for v in subjects]
        review_after=raw.get('review_after')
        if review_after is not None and (type(review_after) not in (int,float) or not 0<review_after<1e11):
            raise ValueError('Invalid review date')
        record.update(origin=origin,learning=learning,subjects=subjects,review_after=review_after)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = self._request(db, principal, key, payload)
            if receipt:
                return receipt
            row = db.execute('SELECT body FROM records WHERE id=?',(rid,)).fetchone()
            old = json.loads(row['body']) if row else None
            if old and (not principal.allows(old) or old['owner'] != principal.name):
                raise PermissionError('Only the record owner may revise it; propose a separate correction')
            if type(payload.get('base_revision')) is not int or payload['base_revision'] != (old['revision'] if old else 0):
                raise Conflict('Stale revision; reread before changing')
            if not old and db.execute('SELECT COUNT(*) FROM records').fetchone()[0] >= MAX_RECORDS:
                raise Conflict('Pilot record cap reached; evaluate before expanding')
            for link in clean_links:
                target = self._get(db, principal, link['target'])
                if not set(target['projects']).issubset(projects):
                    raise ValueError('Connection provenance must share all target project scopes')
            record.update(owner=principal.name, revision=(old['revision'] if old else 0)+1,
                          created_at=old['created_at'] if old else time.time(), updated_at=time.time())
            db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (rid,record['revision'],encoded(record).decode()))
            event = self._event(db, {'type':'record','record':record,'projects':projects})
            return self._receipt(db,principal,key,payload,{'accepted':True,'id':rid,'revision':record['revision'],'event_id':event})

    def lookup(self, principal, query='', project='', kinds=None, limit=5):
        start = time.monotonic()
        query = text(query,500,'query',True)
        limit = max(1,min(5,int(limit)))
        import constellation_learning as learning
        results = []
        with self.db() as db:
            records=[json.loads(row[0]) for row in db.execute('SELECT body FROM records')]
            records=[r for r in records if principal.allows(r)]
            feedback=[json.loads(row[0]) for row in db.execute('SELECT body FROM feedback')]
            feedback=[f for f in feedback if principal.allows(f) and f['lesson_id'] in {r['id'] for r in records}]
            eligible=[r for r in records if (not project or project in r['projects']) and (not kinds or r['kind'] in kinds)]
            for score,item,matched,_ in learning.rank(eligible,feedback,query):
                brief = self._visible(db,principal,item)
                brief['sources'] = [{k:v for k,v in source.items() if k!='excerpt'} for source in item['sources']]
                outcome=learning.outcomes(item,feedback)
                brief.update(match_terms=matched, score=score, outcome=outcome, warnings=learning.warnings(item,outcome,records,time.time()))
                results.append(brief)
            results = results[:limit]
            while len(encoded(results)) > MAX_REPLY_BYTES:
                results.pop()
            mid = uuid.uuid4().hex
            elapsed = round((time.monotonic()-start)*1000,2)
            m={'id':mid,'at':time.time(),'query_bytes':len(query.encode()),'returned_bytes':len(encoded(results)),
               'operation':'lookup','results':len(results),'lessons':[{'id':r['id'],'revision':r['revision']} for r in results],'elapsed_ms':elapsed,'model_calls':0,'tokens':None}
            db.execute('INSERT INTO measurements VALUES (?,?,?)',(mid,principal.name,encoded(m).decode()))
            # Bounded operational telemetry, not another transcript archive.
            db.execute('DELETE FROM measurements WHERE rowid NOT IN (SELECT rowid FROM measurements ORDER BY rowid DESC LIMIT 500)')
            return {'items':results,'measurement':m,'coverage':'captured records only','offline':False}

    def learning_view(self, principal, operation, args):
        import constellation_learning as learning
        with self.db() as db:
            records=[json.loads(row[0]) for row in db.execute('SELECT body FROM records')]
            records=[r for r in records if principal.allows(r)]
            feedback=[json.loads(row[0]) for row in db.execute('SELECT body FROM feedback')]
            feedback=[f for f in feedback if principal.allows(f) and f['lesson_id'] in {r['id'] for r in records}]
            if operation=='brief':
                start=time.monotonic()
                result=learning.brief(records,feedback,**{k:args[k] for k in ('query','project','limit','budget_bytes','include_flagged') if k in args})
                # Query text stays out of telemetry; returned IDs/bytes show use without archiving task context.
                m={'id':uuid.uuid4().hex,'at':time.time(),'operation':'brief','query_bytes':len(args['query'].encode()),
                   'returned_bytes':len(encoded(result)),'results':len(result['items']),'lessons':[{'id':r['id'],'revision':r['revision']} for r in result['items']],
                   'elapsed_ms':round((time.monotonic()-start)*1000,2),'model_calls':0,'tokens':None}
                db.execute('INSERT INTO measurements VALUES (?,?,?)',(m['id'],principal.name,encoded(m).decode()))
                db.execute('DELETE FROM measurements WHERE rowid NOT IN (SELECT rowid FROM measurements ORDER BY rowid DESC LIMIT 500)')
                return result
            if operation=='catalog':return learning.catalog(records,**{k:args[k] for k in ('after','limit','subject','project') if k in args})
            if operation=='review':
                observations=[dict(json.loads(row[1]),principal=row[0]) for row in db.execute('SELECT principal,body FROM measurements')]
                return learning.review(records,feedback,observations)
            raise ValueError('Unknown learning view')

    def source(self, principal, refs):
        if not isinstance(refs,list) or not 1 <= len(refs) <= 2:
            raise ValueError('Request one or two exact source excerpts')
        out=[]
        with self.db() as db:
            for ref in refs:
                record=self._get_revision(db,principal,ref['id'],ref.get('revision') if ref.get('historical') is True else None)
                if record['revision'] != ref.get('revision'):
                    raise Conflict('Lesson revision changed; reread before loading evidence')
                source=next((s for s in record['sources'] if s['id']==ref.get('source_id')),None)
                if not source:
                    raise Unavailable('Source unavailable')
                out.append(dict(source,available=bool(source['excerpt']),classification=record['classification']))
        return {'sources':out}

    def feedback(self, principal, payload):
        if not principal.write:
            raise PermissionError('Read-only principal')
        key=identifier(payload['key'])
        if payload.get('verdict') not in ('interest','used','helped','failed','not-applicable','unknown'):
            raise ValueError('Invalid feedback verdict')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt=self._request(db,principal,key,payload)
            if receipt:return receipt
            record=self._get_revision(db,principal,payload['id'],payload.get('revision'))
            if record['revision']!=payload.get('revision'):
                raise Conflict('Feedback must name the applied revision')
            use=identifier(payload['use_id'])
            fid=hashlib.sha256((principal.name+'\0'+use+'\0'+record['id']).encode()).hexdigest()
            row=db.execute('SELECT revision FROM feedback WHERE id=?',(fid,)).fetchone()
            rev=row['revision'] if row else 0
            if payload.get('base_revision',0)!=rev:raise Conflict('Feedback already exists; revise it instead of voting again')
            m=payload.get('measurements',{})
            if not isinstance(m,dict) or set(m)-{'tokens','elapsed_ms','capture_tokens','rework_minutes'}:
                raise ValueError('Invalid measurements')
            if any(type(v) not in (int,float) or not 0<=v<=1e12 for v in m.values()):
                raise ValueError('Measurements must be observed nonnegative numbers')
            body={'id':fid,'revision':rev+1,'lesson_id':record['id'],'lesson_revision':record['revision'],
                  'principal':principal.name,'use_id':use,'verdict':payload['verdict'],
                  'reason':text(payload['reason'],600,'reason'),'evidence':text(payload.get('evidence',''),600,'evidence',True),
                  'measurements':m,'projects':record['projects'],'at':time.time(),
                  'suggestion':text(payload.get('suggestion',''),500,'suggestion',True)}
            db.execute('INSERT OR REPLACE INTO feedback VALUES (?,?,?)',(fid,rev+1,encoded(body).decode()))
            event=self._event(db,{'type':'feedback','feedback':body,'projects':record['projects']})
            return self._receipt(db,principal,key,payload,{'accepted':True,'id':fid,'revision':rev+1,'event_id':event})

    def changes(self, principal, cursor='', limit=20):
        limit=max(1,min(20,int(limit)))
        with self.db() as db:
            seq=0
            if cursor:
                row=db.execute('SELECT seq,body FROM events WHERE id=?',(identifier(cursor),)).fetchone()
                if not row or not principal.allows(json.loads(row['body'])):raise ValueError('Invalid scope cursor')
                seq=row['seq']
            out=[]
            for row in db.execute('SELECT body FROM events WHERE seq>? ORDER BY seq',(seq,)):
                event=json.loads(row['body'])
                if principal.allows(event):out.append(event)
                if len(out)>limit:break
            more=len(out)>limit
            out=out[:limit]
            # Brief changed records: source bodies require explicit retrieval.
            for event in out:
                if 'record' in event:
                    event['record']=self._visible(db,principal,event['record'])
                    for src in event['record']['sources']:src.pop('excerpt',None)
            while out and len(encoded(out))>MAX_REPLY_BYTES:
                out.pop();more=True
            return {'items':out,'cursor':out[-1]['event_id'] if out else cursor,'more':more,'coverage':'authorized changes only'}

    def health(self, principal):
        with self.db() as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT body FROM records')]
            records=[r for r in records if principal.allows(r)]
            feedback=[json.loads(r[0]) for r in db.execute('SELECT body FROM feedback')]
            feedback=[f for f in feedback if principal.allows(f) and f['lesson_id'] in {r['id'] for r in records}]
            metrics=[json.loads(r[0]) for r in db.execute('SELECT body FROM measurements WHERE principal=?',(principal.name,))]
            started=float(db.execute("SELECT value FROM metadata WHERE key='started'").fetchone()[0])
            backup=db.execute("SELECT value FROM metadata WHERE key='backup'").fetchone()
            adoption=[]
            if '*' in principal.projects:
                people=sorted({r['owner'] for r in records}|{f['principal'] for f in feedback}|{x[0] for x in db.execute('SELECT DISTINCT principal FROM measurements')})
            else:people=[principal.name]
            for person in people:
                observations=[json.loads(x[0]) for x in db.execute('SELECT body FROM measurements WHERE principal=?',(person,))]
                owned=[r for r in records if r['owner']==person]
                adoption.append({'principal':person,'records':len(owned),'agent_discoveries':sum(r.get('origin') in ('agent-discovery','joint-discovery') for r in owned),
                    'briefs_in_window':sum(m.get('operation')=='brief' for m in observations),
                    'empty_briefs_in_window':sum(m.get('operation')=='brief' and not m['results'] for m in observations),
                    'reported_applications':len({f['use_id'] for f in feedback if f['principal']==person and f['verdict'] in ('used','helped','failed')})})
            return {'ok':True,'adoption':adoption,'adoption_window':'bounded latest 500 retrieval observations; installation is separate', 'records':len(records),'feedback':len(feedback),
                    'applied_outcomes':sum(f['verdict'] in ('helped','failed') for f in feedback),
                    'applied_uses':len({(f['principal'],f['use_id']) for f in feedback if f['verdict'] in ('helped','failed')}),
                    'feedback_items':feedback[-20:], 'retrievals':len(metrics),
                    'retrieval_bytes':sum(m['returned_bytes'] for m in metrics),
                    'model_calls':0,'token_savings':None,'pilot_cap':MAX_RECORDS,
                    'review_due':time.time()-started>=14*86400 or len(records)>=MAX_RECORDS or len(feedback)>=10,
                    'last_backup':json.loads(backup[0]) if backup and '*' in principal.projects else None}

    def export(self, path):
        """Owner maintenance only; all revisions and receipts, no credential material."""
        with self.db() as db:
            tables={t:[dict(r) for r in db.execute('SELECT * FROM '+t)] for t in
                    ('records','events','requests','feedback','measurements','metadata')}
        with Path(path).open('xb') as out:
            os.chmod(path,0o600)
            for table,rows in tables.items():
                for row in rows:out.write(encoded({'schema':1,'table':table,'row':row})+b'\n')
        return {'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),'bytes':Path(path).stat().st_size}

    def backup(self, destination):
        dest=Path(destination)
        if dest.exists():raise Conflict('Backup destination already exists')
        dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        target=sqlite3.connect(dest)
        try:
            with self.db() as db:db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity failed')
        finally:target.close()
        os.chmod(dest,0o600)
        receipt={'at':time.time(),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),
                 'bytes':dest.stat().st_size,'verified':True}
        with self.db() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('backup',encoded(receipt).decode()))
        return receipt
