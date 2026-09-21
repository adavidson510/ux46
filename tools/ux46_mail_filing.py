"""Reversible mailbox filing with per-message snapshots. Never deletes mail."""
import json
import fcntl
import re
import time
import uuid
from constellation_store import Conflict, encoded

LABELS={'legal':'Taxes & Legal','receipts':'Receipts','subscriptions':'Bills & Finance','customers':'Customers & Leads',
        'security':'Security Alerts','infrastructure':'AI & Infrastructure','newsletters':'Newsletters & Marketing','other':'Other'}

class MailFiling:
    def __init__(self,assistant):
        self.a=assistant;self.providers={}
        with assistant.mail.db() as db:db.execute('CREATE TABLE IF NOT EXISTS mail_filing(id TEXT PRIMARY KEY,body TEXT)')

    def history(self):
        with self.a.mail.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT body FROM mail_filing ORDER BY id DESC LIMIT 50')]

    def unsettled(self):
        with self.a.mail.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT body FROM mail_filing') if json.loads(r[0])['state'] in ('applying','unknown','undoing')]

    def apply(self,*args,**kwargs):
        with open(self.a.root/'email-filing.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._apply(*args,**kwargs)

    def _apply(self,account,thread,category,handled=False,read=False,expected_revision=None,expected_messages=None):
        if category not in LABELS:raise ValueError('Unknown category')
        if account not in self.providers:self.providers[account]=self.a.provider(account)
        provider=self.providers[account];snapshot=provider.mailbox_snapshot(thread) if hasattr(provider,'mailbox_snapshot') else provider.thread(thread)
        if expected_revision and snapshot['revision']!=expected_revision:
            return {'state':'changed','message':'New mail arrived; kept in inbox'}
        messages=[m for m in snapshot['messages'] if 'SENT' not in m['labels'] and 'TRASH' not in m['labels'] and 'SPAM' not in m['labels']]
        if expected_messages is not None and {m['id'] for m in messages}!=set(expected_messages):
            return {'state':'changed','message':'New incoming mail arrived; kept in inbox'}
        if not messages:return {'state':'nothing-to-file'}
        if not snapshot['complete']:raise ValueError('Thread too large or incomplete to file safely')
        ident=str(int(time.time()*1000))+'-'+uuid.uuid4().hex[:8]
        # An unconfirmed earlier mutation is not retried merely because it is old.
        if any(x['account']==account and x['thread']==thread and x['state'] in ('applying','unknown','undoing') for x in self.unsettled()):
            raise Conflict('An earlier mailbox change is uncertain. Check Gmail before another change.')
        label=provider.label('UX46/'+LABELS[category]);add=[label];remove=[]
        if handled:remove.append('INBOX')
        if read:remove.append('UNREAD')
        changes=[{'id':m['id'],'before':sorted(m['labels']),'after':sorted((set(m['labels'])|set(add))-set(remove))} for m in messages]
        changes=[c for c in changes if c['before']!=c['after']]
        if not changes:return {'state':'unchanged'}
        record={'id':ident,'account':account,'thread':thread,'category':category,'handled':handled,'marked_read':read,
                'at':time.time(),'state':'applying','changes':changes}
        self.a.put('mail_filing',ident,record)
        try:provider.modify([c['id'] for c in changes],add,remove);record['state']='applied'
        except Exception:record['state']='unknown'
        self.a.put('mail_filing',ident,record);return record

    def undo(self,ident):
        with open(self.a.root/'email-filing.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._undo(ident)

    def _undo(self,ident):
        record=self.a.get('mail_filing',ident)
        if not record or record['state']!='applied':raise ValueError('Only a confirmed mailbox change can be undone')
        provider=self.a.provider(record['account'])
        for c in record['changes']:
            actual=provider.get('messages/'+c['id'],format='minimal')
            if sorted(actual.get('labelIds',[]))!=c['after']:raise Conflict('This message changed in Gmail; automatic undo would overwrite those changes')
        record['state']='undoing';self.a.put('mail_filing',ident,record)
        try:
            for c in record['changes']:
                provider.modify([c['id']],list(set(c['before'])-set(c['after'])),list(set(c['after'])-set(c['before'])))
            record['state']='undone'
        except Exception:record['state']='unknown'
        self.a.put('mail_filing',ident,record);return record

    def tick(self):
        policy=self.a.get('mail_assistant','filing-policy') or {}
        if not policy.get('enabled'):return {'state':'paused'}
        view=self.a.mail.view(lane='all',limit=1000);changed=[]
        for item in view['items']:
            if len(changed)>=20:break
            if 'INBOX' not in item['labels'] or item['warnings']:continue
            quiet=not item['need'] and item['category'] in ('newsletters','receipts')
            if re.search(r'action required|reply required|please respond|please confirm|approval needed|payment failed|past due',item['subject']+' '+item['snippet'],re.I):quiet=False
            handled=quiet and policy.get('archive_routine',False)
            expected='UX46/'+LABELS[item['category']]
            if expected in item['labels'] and not handled:continue
            try:r=self.apply(item['account'],item['thread'],item['category'],handled,handled and policy.get('mark_read',False))
            except (ValueError,OSError,Conflict):
                changed.append({'state':'needs-review','account':item['account'],'thread':item['thread']});continue
            if r['state'] not in ('unchanged','nothing-to-file'):changed.append({'id':r['id'],'state':r['state']})
        result={'state':'checked','at':time.time(),'changes':changed};self.a.put('mail_assistant','filing-last',result);return result
