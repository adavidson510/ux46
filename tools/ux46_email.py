"""Private deterministic email projection. Provider writes are deliberately absent."""
from __future__ import annotations
import hashlib
import json
import re
import time
from email.utils import getaddresses
from urllib.parse import quote
from constellation_store import Store, Conflict, encoded, identifier, text

CATEGORIES = ('legal','receipts','subscriptions','customers','security','infrastructure','newsletters','other')
PANE_CATEGORIES = {'UX46/Taxes & Legal':'legal','UX46/Receipts':'receipts',
    'UX46/Bills & Finance':'subscriptions','UX46/Customers & Leads':'customers',
    'UX46/Security Alerts':'security','UX46/AI & Infrastructure':'infrastructure',
    'UX46/Domains & Accounts':'infrastructure','UX46/Newsletters & Marketing':'newsletters'}

class CapacityError(ValueError):pass


class EmailStore(Store):
    def __init__(self,path):
        super().__init__(path)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mail_accounts(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS mail_messages(account TEXT,id TEXT,thread TEXT,stamp REAL,body TEXT,
                    PRIMARY KEY(account,id));
                CREATE INDEX IF NOT EXISTS mail_thread ON mail_messages(account,thread);
                CREATE TABLE IF NOT EXISTS mail_attention(account TEXT,thread TEXT,body TEXT,PRIMARY KEY(account,thread));
                CREATE TABLE IF NOT EXISTS mail_rules(id TEXT PRIMARY KEY,revision INTEGER,body TEXT);
                CREATE TABLE IF NOT EXISTS mail_sync(account TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS mail_receipts(id TEXT PRIMARY KEY,body TEXT);
            ''')

    def account(self, account, **updates):
        aid=identifier(account)
        with self.db() as db:
            row=db.execute('SELECT body FROM mail_accounts WHERE id=?',(aid,)).fetchone()
            body=json.loads(row[0]) if row else {'id':aid,'status':'not-synced','email':'','last_success':None}
            body.update(updates)
            db.execute('INSERT OR REPLACE INTO mail_accounts VALUES (?,?)',(aid,encoded(body).decode()))
        return body

    def sync_state(self,account,body=None):
        with self.db() as db:
            if body is not None:
                db.execute('INSERT OR REPLACE INTO mail_sync VALUES (?,?)',(account,encoded(body).decode()))
            row=db.execute('SELECT body FROM mail_sync WHERE account=?',(account,)).fetchone()
            return json.loads(row[0]) if row else {}

    def rules(self):
        with self.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT body FROM mail_rules ORDER BY id')]

    def save_rule(self,payload):
        r=payload['rule'];rid=identifier(r['id'])
        if r.get('field') not in ('recipient','sender') or r.get('need') not in ('read','reply','decide','quiet'):
            raise ValueError('Invalid rule match or attention choice')
        if r.get('category') not in CATEGORIES:raise ValueError('Invalid category')
        value=text(r.get('value'),254,'email address').casefold()
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',value):raise ValueError('Use an exact email address')
        account=text(r.get('account','*'),96,'account')
        if account!='*':identifier(account)
        clean={k:r[k] for k in ('field','need','category')}
        clean.update(id=rid,value=value,account=account,project=text(r.get('project',''),96,'project',True),
                     enabled=r.get('enabled',True) is True,source=text(r.get('source','User in UX46'),300,'rule source'))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision FROM mail_rules WHERE id=?',(rid,)).fetchone()
            rev=row[0] if row else 0
            if payload.get('base_revision')!=rev:raise Conflict('Rule changed; reload before saving')
            if not row and db.execute('SELECT COUNT(*) FROM mail_rules').fetchone()[0]>=100:raise ValueError('Rule limit reached')
            clean['revision']=rev+1
            db.execute('INSERT OR REPLACE INTO mail_rules VALUES (?,?,?)',(rid,rev+1,encoded(clean).decode()))
        return clean

    def upsert(self,account,message):
        """Idempotent Gmail message metadata; never store OAuth or remote HTML."""
        identifier(account)
        msg={k:message.get(k,'') for k in ('id','thread','subject','sender','recipient','delivered_to','snippet')}
        identifier(msg['id']);identifier(msg['thread'])
        for k in ('subject','sender','recipient','delivered_to','snippet'):
            msg[k]=str(msg[k])[:1800]
        msg['stamp']=float(message['stamp'])
        msg['labels']=[str(x)[:120] for x in message.get('labels',[])][:100]
        msg['warnings']=[str(x)[:160] for x in message.get('warnings',[])][:4]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM mail_messages WHERE account=? AND id=?',(account,msg['id'])).fetchone() and db.execute('SELECT COUNT(*) FROM mail_messages WHERE account=?',(account,)).fetchone()[0]>=5000:
                raise CapacityError('Email index capacity reached; review retention before expanding')
            db.execute('INSERT OR REPLACE INTO mail_messages VALUES (?,?,?,?,?)',
                       (account,msg['id'],msg['thread'],msg['stamp'],encoded(msg).decode()))

    def delete_message(self,account,mid):
        with self.db() as db:db.execute('DELETE FROM mail_messages WHERE account=? AND id=?',(account,mid))

    def classify(self,account,message,rules):
        labels=message['labels'];subject=message['subject'].casefold();words=(subject+' '+message['snippet']).casefold()
        category=next((v for k,v in PANE_CATEGORIES.items() if k in labels),'other')
        marketing=any(l in labels for l in ('UX46/Newsletters & Marketing','CATEGORY_PROMOTIONS','CATEGORY_SOCIAL'))
        if marketing:category='newsletters'
        need='';reason='Categorized by deterministic rules';project='';required=False
        if 'INBOX' in labels:
            if 'UX46/Reply Needed' in labels:need='reply';reason='Inbox reply label; current message still needs review'
            elif 'UX46/Action Needed' in labels:need='decide';reason='Inbox action label; current message still needs review'
            elif 'UX46/Needs You' in labels:need='read';reason='Inbox attention label; unresolved status is not yet confirmed'
        if not marketing and 'INBOX' in labels and re.search(r'payment failed|past due|action required|renewal failed',subject):
            need=need or 'decide';reason='Payment or account-action wording'
        if category=='other':
            for cat,pattern in [('legal',r'legal|contract|agreement|\b1099\b|\bw-9\b'),('receipts',r'receipt|order confirmation'),('subscriptions',r'subscription|renewal|invoice'),
                                ('security',r'security alert|suspicious|password changed'),('newsletters',r'newsletter|unsubscribe')]:
                if re.search(pattern,words):category=cat;break
        if category=='legal' and not need and not marketing and 'INBOX' in labels and re.search(r'\blegal\b|\bcontract\b|\bagreement\b|\b1099\b|\bw-9\b',subject):
            need='read';reason='Legal/tax subject in your inbox; confirm relevance'
        if category=='customers' and not need and not marketing and 'INBOX' in labels and re.search(r'\?|can you|could you|please review',words):
            need='reply';reason='Possible human reply requested; confirm relevance'
        addresses={'recipient':{x.casefold() for _,x in getaddresses([message['recipient'],message['delivered_to']])},
                   'sender':{x.casefold() for _,x in getaddresses([message['sender']])}}
        matched=[r for r in rules if r['enabled'] and r['account'] in ('*',account) and r['value'] in addresses[r['field']]]
        visible=[r for r in matched if r['need']!='quiet']
        chosen=visible or matched
        if chosen:
            rule=chosen[0];category=rule['category'];project=rule['project']
            need='' if rule['need']=='quiet' else rule['need'];required=bool(visible)
            reason='Your '+rule['field']+' rule: '+rule['value']
            if len({(r['need'],r['project'],r['category']) for r in chosen})>1:
                need=need or 'read';reason='Conflicting rules need your review';required=True
        if 'SPAM' in labels and not visible:
            need='';reason='Gmail spam; inspect in all mail if needed'
        if message['warnings']:
            reason += ' · Sender/spam warning';required=required or bool(need)
        if 'SENT' in labels or 'TRASH' in labels:
            need=''
        return dict(category=category,need=need,reason=reason,project=project,required=required)

    def view(self,account='',category='',lane='needs',limit=100,project=''):
        rules=self.rules()
        with self.db() as db:
            accounts=[json.loads(r[0]) for r in db.execute('SELECT body FROM mail_accounts ORDER BY id')]
            if account and account not in {a['id'] for a in accounts}:raise ValueError('Unknown account')
            selected=[a for a in accounts if not account or a['id']==account]
            addresses={a['id']:a['email'] for a in accounts}
            rows=db.execute('SELECT account,body FROM mail_messages'+(' WHERE account=?' if account else '')+' ORDER BY stamp DESC',
                            (account,) if account else ()).fetchall()
            acks={(r[0],r[1]):json.loads(r[2]) for r in db.execute('SELECT * FROM mail_attention')}
        cutoff=time.time()-30*86400
        mix={k:0 for k in CATEGORIES};threads={};received=0
        account_mix={a['id']:0 for a in selected};project_mix={};subscriptions={}
        for row in rows:
            msg=json.loads(row['body']);aid=row['account'];cl=self.classify(aid,msg,rules)
            if 'SENT' not in msg['labels'] and 'TRASH' not in msg['labels'] and msg['stamp']>=cutoff:
                mix[cl['category']]+=1;received+=1
                account_mix[aid]=account_mix.get(aid,0)+1
                org=cl['project'] or 'Unmapped'
                project_mix[org]=project_mix.get(org,0)+1
                if cl['category']=='subscriptions':
                    sender=next((addr.casefold() for _,addr in getaddresses([msg['sender']]) if '@' in addr),'Unknown sender')
                    domain=sender.rsplit('@',1)[-1]
                    subscriptions[domain]=subscriptions.get(domain,0)+1
            key=(aid,msg['thread'])
            if key not in threads:
                threads[key]=dict(msg,account=aid,account_email=addresses.get(aid,''),**cl)
        items=[];needs=0;reviewed=0;awaiting=0
        for key,item in threads.items():
            revision=hashlib.sha256(encoded([item['id'],item['need'],item['reason'],item['category'],item['warnings']])).hexdigest()
            item['revision']=revision
            ack=acks.get(key,{})
            current=ack.get('revision')==revision
            item['ack_revision']=ack.get('version',0)
            item['reviewed']=current and ack.get('action')=='reviewed'
            item['snoozed']=current and ack.get('action')=='snooze' and ack.get('until',0)>time.time()
            item['awaiting']=current and ack.get('action')=='awaiting'
            item['active']=bool(item['need']) and not item['reviewed'] and not item['snoozed'] and not item['awaiting']
            needs+=item['active'];reviewed+=item['reviewed'];awaiting+=item['awaiting']
            # Address-based account selection; no guessed /u/0 account index.
            item['gmail_url']='https://mail.google.com/mail/?authuser='+quote(item['account_email'],safe='')+'#all/'+quote(item['thread'],safe='')
            if category and item['category']!=category:continue
            if project and (item['project'] or 'Unmapped')!=project:continue
            if lane=='needs' and not item['active']:continue
            if lane=='awaiting' and not item['awaiting']:continue
            items.append(item)
        items.sort(key=lambda x:(not x['required'],not x['active'],-x['stamp'],x['account'],x['thread']))
        for a in accounts:
            a['stale']=not a['last_success'] or time.time()-a['last_success']>900
        coverage=all(a['status']=='healthy' and a.get('coverage_complete') and not a['stale'] for a in selected) and bool(selected)
        limit=max(1,min(1000,int(limit)))
        return {'accounts':accounts,'items':items[:limit],'more':len(items)>limit,'rules':rules,
                'counts':{'needs':needs,'reviewed':reviewed,'awaiting':awaiting,'received':received,
                          'categorized':received-mix['other'],'filed':0},'mix':mix,
                'account_mix':account_mix,'project_mix':project_mix,
                'subscriptions':sorted(({'sender_domain':k,'messages':v} for k,v in subscriptions.items()),key=lambda x:(-x['messages'],x['sender_domain']))[:8],
                'period':'Last 30 days of received mail in the indexed coverage',
                'coverage_complete':coverage,'checker':'Deterministic checks · no model calls',
                'provider_writes':False,'updated_at':time.time()}

    def attention(self,payload):
        aid=identifier(payload['account']);tid=identifier(payload['thread'])
        item=next((x for x in self.view(aid,lane='all',limit=1000)['items'] if x['thread']==tid),None)
        if not item or payload.get('revision')!=item['revision']:raise Conflict('Mail changed; review the latest message')
        action=payload['action']
        if action not in ('reviewed','snooze','awaiting','reopen'):raise ValueError('Invalid attention action')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body FROM mail_attention WHERE account=? AND thread=?',(aid,tid)).fetchone()
            old=json.loads(row[0]) if row else {'version':0}
            if payload.get('base_version')!=old['version']:raise Conflict('Attention changed in another window')
            ack={'version':old['version']+1,'revision':item['revision'],'action':action,'at':time.time(),
                 'until':time.time()+86400 if action=='snooze' else None}
            db.execute('INSERT OR REPLACE INTO mail_attention VALUES (?,?,?)',(aid,tid,encoded(ack).decode()))
            return ack
