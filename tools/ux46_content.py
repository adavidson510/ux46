"""Room-owned editable content. Saved revisions are independent of native sessions."""
import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid
from constellation_store import Conflict, Unavailable
from ux46_agent_actions import AGENT, ROOM


class ContentStore:
    def __init__(self, directory):
        self.path = Path(directory)/'content.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS content(id TEXT PRIMARY KEY, agent TEXT, room TEXT, kind TEXT, revision INTEGER);
                CREATE TABLE IF NOT EXISTS content_revisions(id TEXT, revision INTEGER, title TEXT, payload TEXT, at REAL, reporter TEXT,
                PRIMARY KEY(id,revision));''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally:c.close()

    def scope(self,a):
        if not isinstance(a.get('agent'),str) or not AGENT.fullmatch(a['agent']):raise ValueError('Invalid agent')
        if not isinstance(a.get('room'),str) or not ROOM.fullmatch(a['room']):raise ValueError('Invalid room')
        return a['agent'],a['room']

    def owned(self,c,a):
        agent,room=self.scope(a)
        row=c.execute('SELECT * FROM content WHERE id=? AND agent=? AND room=?',(a.get('id'),agent,room)).fetchone()
        if not row:raise Unavailable('Content is not in this room')
        return dict(row)

    def read(self,a):
        with self.db() as c:
            if not a.get('id'):
                agent,room=self.scope(a)
                return {'items':[dict(r) for r in c.execute('''SELECT x.*,r.title,r.at FROM content x JOIN content_revisions r
                    ON x.id=r.id AND x.revision=r.revision WHERE x.agent=? AND x.room=? ORDER BY r.at DESC''',(agent,room))]}
            item=self.owned(c,a)
            revision=int(a.get('revision') or item['revision'])
            r=c.execute('SELECT * FROM content_revisions WHERE id=? AND revision=?',(item['id'],revision)).fetchone()
            if not r:raise Unavailable('Revision unavailable')
            return {**item,'revision':revision,'latest_revision':item['revision'],'title':r['title'],'payload':json.loads(r['payload']),
                    'at':r['at'],'reporter':r['reporter'],'history':[dict(h) for h in c.execute('SELECT revision,at,reporter FROM content_revisions WHERE id=? ORDER BY revision DESC',(item['id'],))]}

    def action(self,a):
        agent,room=self.scope(a);op=a['action']
        if op not in ('create','save'):raise ValueError('Unknown content action')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if op=='create':
                ident=a.get('id') or uuid.uuid4().hex
                if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',ident):raise ValueError('Invalid content identity')
                if c.execute('SELECT 1 FROM content WHERE id=?',(ident,)).fetchone():raise Conflict('Content already exists')
                kind=a['kind'];revision=1
            else:
                held=self.owned(c,a);ident=held['id'];kind=held['kind'];revision=held['revision']+1
                if int(a.get('base_revision',0))!=held['revision']:raise Conflict('A newer revision was saved. Your text is kept; compare before saving.')
            title=a.get('title');payload=a.get('payload')
            if not isinstance(title,str) or not title.strip() or len(title)>160:raise ValueError('Title required')
            if not isinstance(payload,dict):raise ValueError('Content required')
            if kind=='markdown':
                text=payload.get('text')
                if not isinstance(text,str) or len(text)>50000:raise ValueError('Markdown must be under 50000 characters')
                payload={'text':text}
            elif kind=='chart':
                chart=payload.get('chart',{})
                if not isinstance(chart,dict):raise ValueError('Invalid chart')
                labels=chart.get('labels',[]);values=chart.get('values',[])
                if chart.get('type') not in ('line','bar') or not isinstance(labels,list) or not isinstance(values,list) or not 1<=len(labels)<=24 or len(labels)!=len(values):raise ValueError('Invalid chart')
                if any(not isinstance(v,(float,int)) or isinstance(v,bool) or not math.isfinite(v) for v in values):raise ValueError('Invalid chart values')
                if any(not isinstance(l,str) or len(l)>40 for l in labels):raise ValueError('Invalid chart labels')
                unit=chart.get('unit','');source=payload.get('source','')
                if not isinstance(unit,str) or len(unit)>30 or not isinstance(source,str) or not source.strip() or len(source)>300:raise ValueError('Chart needs a source label')
                payload={'chart':{'type':chart['type'],'labels':labels,'values':values,'unit':unit},'source':source,'snapshot':True}
            else:raise ValueError('Unsupported content kind')
            if op=='create':c.execute('INSERT INTO content VALUES(?,?,?,?,?)',(ident,agent,room,kind,revision))
            else:c.execute('UPDATE content SET revision=? WHERE id=?',(revision,ident))
            c.execute('INSERT INTO content_revisions VALUES(?,?,?,?,?,?)',(ident,revision,title.strip(),json.dumps(payload),time.time(),str(a.get('reporter','User'))[:80]))
            # Baseline plus latest 50 revisions; accidental saves remain recoverable.
            c.execute('DELETE FROM content_revisions WHERE id=? AND revision>1 AND revision<?',(ident,max(2,revision-49)))
        # A concurrent save after commit must not change this write's receipt.
        return self.read({'agent':agent,'room':room,'id':ident,'revision':revision})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True);p.add_argument('--agent',required=True);p.add_argument('--room',required=True)
    p.add_argument('--id');p.add_argument('--revision',type=int);p.add_argument('--file',help='JSON create/save action; scope comes from CLI arguments')
    a=p.parse_args();store=ContentStore(a.directory);args={'agent':a.agent,'room':a.room,'id':a.id,'revision':a.revision}
    if a.file:
        payload=json.loads(Path(a.file).read_text());payload.update(agent=a.agent,room=a.room)
        if a.id:payload['id']=a.id
        result=store.action(payload)
    else:result=store.read(args)
    print(json.dumps(result))

if __name__=='__main__':main()
