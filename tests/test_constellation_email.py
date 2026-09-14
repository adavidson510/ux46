import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from constellation_store import Store, Principal, Conflict, Unavailable
from ux46_email import EmailStore
from ux46_email_check import check_account


def lesson(key='method',projects=None):
    return {'key':key+'-event','base_revision':0,'record':{'id':key,'projects':projects or ['demo-platform'],
        'kind':'method','claim':'Use deterministic projections for cheap attention status',
        'rationale':'An existing status response has the information already.',
        'applies':'Status displays','limits':'A status projection does not reason about a new design',
        'state':'supported','evidence':'observed','terms':['attention','signals','constellation'],
        'sources':[{'id':'src','room':(projects or ['demo-platform'])[0]+'/room',
                    'revision':'sha256:abc','locator':'section:status','excerpt':'Observed no extra model call.'}]}}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'knowledge.db')
        self.owner=Principal('local',('*',),True)
        self.peer=Principal('pane',('demo-platform',),True)

    def test_review_counter_counts_distinct_applied_uses(self):
        for name in ('first','second','interest','not-applicable'):
            self.store.capture(self.owner,lesson(name))
            self.store.feedback(self.owner,{'key':'feedback-'+name,'id':name,'revision':1,'use_id':'shared-use' if name in ('first','second') else name,
                'verdict':'helped' if name in ('first','second') else name,'reason':'Observed example'})
        health=self.store.health(self.owner)
        self.assertEqual(health['applied_uses'],1)
        self.assertEqual(health['applied_outcomes'],2)

    def test_idempotency_revision_owner_and_scope(self):
        p=lesson();r=self.store.capture(self.owner,p)
        self.assertEqual(r,self.store.capture(self.owner,p))
        changed=json.loads(json.dumps(p));changed['record']['claim']='Changed'
        with self.assertRaises(Conflict):self.store.capture(self.owner,changed)
        changed['key']='another'
        with self.assertRaises(Conflict):self.store.capture(self.owner,changed)
        changed['base_revision']=1
        with self.assertRaises(PermissionError):self.store.capture(self.peer,changed)
        self.store.capture(self.owner,changed)
        self.assertEqual(self.store.get(self.peer,'method')['revision'],2)
        with self.assertRaises(Conflict):self.store.source(self.peer,[{'id':'method','revision':1,'source_id':'src'}])

    def test_no_cross_scope_results_sources_links_or_counts(self):
        self.store.capture(self.owner,lesson('private',['secret']))
        p=lesson('mixed',['demo-platform','secret']);p['record']['links']=[{'target':'private','type':'informs','state':'proposed','reason':'Private connection'}]
        self.store.capture(self.owner,p)
        self.store.capture(self.owner,lesson('visible'))
        self.assertEqual([x['id'] for x in self.store.lookup(self.peer,'attention')['items']],['visible'])
        self.assertEqual(self.store.health(self.peer)['records'],1)
        self.assertEqual(len(self.store.changes(self.peer)['items']),1)
        with self.assertRaises(Unavailable):self.store.get(self.peer,'private')
        with self.assertRaises(Unavailable):self.store.source(self.peer,[{'id':'private','revision':1,'source_id':'src'}])

    def test_recall_is_bounded_and_exact_evidence(self):
        for i in range(8):self.store.capture(self.owner,lesson('r'+str(i)))
        result=self.store.lookup(self.peer,'attention',limit=999)
        self.assertEqual(len(result['items']),5)
        self.assertTrue(all('excerpt' not in s for r in result['items'] for s in r['sources']))
        self.assertLessEqual(result['measurement']['returned_bytes'],18000)
        self.assertNotIn('excerpt',self.store.get(self.peer,'r1')['sources'][0])
        self.assertIsNone(result['measurement']['tokens'])
        src=self.store.source(self.peer,[{'id':'r1','revision':1,'source_id':'src'}])['sources'][0]
        self.assertEqual(src['sha256'],hashlib.sha256(src['excerpt'].encode()).hexdigest())
        with self.assertRaises(ValueError):self.store.source(self.peer,[{}]*3)

    def test_feedback_is_per_application_and_interest_is_not_outcome(self):
        self.store.capture(self.owner,lesson())
        p={'key':'feedback','id':'method','revision':1,'use_id':'task-1','verdict':'interest','reason':'Worth trying'}
        receipt=self.store.feedback(self.peer,p)
        self.assertEqual(receipt,self.store.feedback(self.peer,p))
        self.assertEqual(self.store.health(self.peer)['applied_outcomes'],0)
        with self.assertRaises(Conflict):self.store.feedback(self.peer,dict(p,key='duplicate'))
        self.store.feedback(self.peer,dict(p,key='outcome',base_revision=1,verdict='helped',reason='Reduced the repeated status read'))
        self.assertEqual(self.store.health(self.peer)['applied_outcomes'],1)

    def test_pilot_cap_and_retirement(self):
        with patch('constellation_store.MAX_RECORDS',1):
            p=lesson();self.store.capture(self.owner,p)
            with self.assertRaises(Conflict):self.store.capture(self.owner,lesson('next'))
            p['key']='retire';p['base_revision']=1;p['record']['state']='retired'
            self.store.capture(self.owner,p)
            self.assertFalse(self.store.lookup(self.peer,'attention')['items'])

    def test_backup_can_be_reopened_with_history_and_receipts(self):
        p=lesson();self.store.capture(self.owner,p)
        path=self.root/'backup.db';receipt=self.store.backup(path)
        self.assertTrue(receipt['verified'])
        recovered=Store(path)
        self.assertEqual(recovered.get(self.peer,'method')['claim'],p['record']['claim'])
        self.assertEqual(recovered.capture(self.owner,p),self.store.capture(self.owner,p))
        self.assertEqual(len(recovered.changes(self.peer)['items']),1)
        self.store.export(self.root/'export.jsonl')
        from constellation_admin import restore,maintain
        exported=self.root/'export.jsonl'
        restore(exported,self.root/'portable.db',hashlib.sha256(exported.read_bytes()).hexdigest())
        self.assertEqual(Store(self.root/'portable.db').get(self.peer,'method')['revision'],1)
        self.assertTrue(maintain(self.store.path,self.root/'snapshots')['changed'])
        self.assertFalse(maintain(self.store.path,self.root/'snapshots')['changed'])


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=EmailStore(Path(self.temp.name)/'email.db')
        for aid in ('first','second'):self.store.account(aid,email=aid+'@example.com',status='healthy',last_success=time.time(),coverage_complete=True)

    def msg(self,**kw):
        return dict({'id':'a1','thread':'t1','stamp':time.time(),'sender':'Client <client@example.com>',
            'recipient':'legal@example.com','subject':'Contract','snippet':'Please review',
            'labels':['INBOX','UX46/Needs You']},**kw)

    def test_account_thread_dedupe_review_and_new_message(self):
        msg=self.msg();self.store.upsert('first',msg);self.store.upsert('first',msg);self.store.upsert('second',msg)
        v=self.store.view();self.assertEqual(v['counts']['needs'],2);self.assertEqual(v['counts']['received'],2)
        item=self.store.view('first')['items'][0]
        self.assertIn('authuser=first%40example.com',item['gmail_url'])
        p={k:item[k] for k in ('account','thread','revision')};p.update(action='reviewed',base_version=0)
        self.store.attention(p)
        self.assertEqual(self.store.view('first')['counts']['needs'],0)
        self.store.upsert('first',self.msg(id='a2',stamp=time.time()+1))
        self.assertEqual(self.store.view('first')['counts']['needs'],1)
        with self.assertRaises(Conflict):self.store.attention(p)

    def test_legal_visibility_wins_over_quiet_and_warning_survives(self):
        for rid,need in [('quiet','quiet'),('visible','read')]:
            self.store.save_rule({'base_revision':0,'rule':{'id':rid,'field':'recipient','value':'legal@example.com',
                'account':'first','need':need,'category':'legal','project':'orbit'}})
        self.store.upsert('first',self.msg(labels=['SPAM'],warnings=['Spam warning']))
        item=self.store.view('first')['items'][0]
        self.assertTrue(item['required']);self.assertEqual(item['need'],'read');self.assertEqual(item['warnings'],['Spam warning'])

    def test_historical_legal_labels_and_spam_do_not_flood_attention(self):
        self.store.upsert('first',self.msg(subject='Celebrate your pets',labels=['UX46/Taxes & Legal','UX46/Newsletters & Marketing']))
        self.store.upsert('second',self.msg(id='spam',labels=['SPAM'],warnings=['Spam warning']))
        view=self.store.view()
        self.assertEqual(view['counts']['needs'],0)
        self.assertEqual(len(self.store.view(lane='all')['items']),2)
        self.assertEqual(view['mix']['newsletters'],1)

    def test_missing_or_failed_coverage_never_looks_all_clear(self):
        self.store.account('first',status='error',coverage_complete=False)
        self.assertFalse(self.store.view()['coverage_complete'])
        self.assertEqual(self.store.view()['counts']['filed'],0)

    def test_graphics_count_received_mail_and_keep_unmapped(self):
        self.store.save_rule({'base_revision':0,'rule':{'id':'orbit','field':'recipient','value':'legal@example.com','need':'read','category':'legal','project':'Orbit'}})
        self.store.upsert('first',self.msg())
        self.store.upsert('second',self.msg(id='b',recipient='unknown@example.com',subject='Subscription renewal',labels=[]))
        self.store.upsert('second',self.msg(id='sent',labels=['SENT']))
        view=self.store.view(lane='all')
        self.assertEqual(view['account_mix'],{'first':1,'second':1})
        self.assertEqual(view['project_mix'],{'Orbit':1,'Unmapped':1})
        self.assertEqual(view['subscriptions'],[{'sender_domain':'example.com','messages':1}])
        self.assertTrue(all(i['project']=='Orbit' for i in self.store.view(project='Orbit',lane='all')['items']))

    def test_rule_revision_prevents_lost_edits(self):
        p={'base_revision':0,'rule':{'id':'legal','field':'recipient','value':'legal@example.com','need':'read','category':'legal'}}
        self.store.save_rule(p)
        with self.assertRaises(Conflict):self.store.save_rule(p)


class GmailFixture:
    def __init__(self):self.fail=False;self.history_calls=0
    def get(self,resource,**q):
        if resource=='profile':return {'emailAddress':'first@example.com','historyId':'100'}
        if resource=='labels':return {'labels':[]}
        if resource=='messages':return {'messages':[{'id':'a1'},{'id':'a2'}]}
        if resource=='history':self.history_calls+=1;return {'historyId':'101','history':[]}
        if resource.startswith('messages/'):
            if self.fail:raise OSError('temporary')
            return {'id':resource.split('/')[1],'threadId':'t1','internalDate':str(int(time.time()*1000)),
                    'labelIds':['INBOX'],'payload':{'headers':[{'name':'Subject','value':'Example'}]}}
        raise AssertionError(resource)


class SyncTests(unittest.TestCase):
    setUp = MailTests.setUp
    def test_cursor_survives_partial_budget_and_failure(self):
        fake=GmailFixture()
        one=check_account(self.store,'first','first@example.com',fake,budget=1)
        self.assertEqual(one['status'],'syncing')
        self.assertEqual(self.store.sync_state('first')['cursor'],'100')
        fake.fail=True
        with self.assertRaises(OSError):check_account(self.store,'first','first@example.com',fake)
        self.assertEqual(self.store.sync_state('first')['pending'],['a2'])
        fake.fail=False
        final=check_account(self.store,'first','first@example.com',fake)
        self.assertEqual(final['status'],'healthy')
        self.assertEqual(self.store.sync_state('first')['cursor'],'101')
        self.assertEqual(self.store.view('first')['counts']['received'],2)
        check_account(self.store,'first','first@example.com',fake)
        self.assertEqual(self.store.view('first')['counts']['received'],2)

    def test_wrong_gmail_account_is_refused(self):
        with self.assertRaises(ValueError):check_account(self.store,'first','wrong@example.com',GmailFixture())


class ClientTests(unittest.TestCase):
    def test_outbox_replay_and_expiring_scoped_cache(self):
        spec=importlib.util.spec_from_file_location('constellation_client',ROOT/'skills/constellation/scripts/client.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            client=module.Client({'state_dir':temp,'principal':'test','socket':'/not-running'})
            with patch.object(client,'request',side_effect=OSError()):
                self.assertTrue(client.call('capture',lesson())['pending'])
                self.assertTrue(client.call('capture',lesson())['pending'])
            with patch.object(client,'request',return_value={'result':{'accepted':True}}):
                self.assertEqual(client.flush(),{'accepted':1,'pending':0})
            with patch.object(client,'request',return_value={'result':{'items':[]},'cache_ttl_seconds':1}):
                client.call('lookup',{})
            with patch.object(client,'request',side_effect=OSError()):
                self.assertTrue(client.call('lookup',{})['offline'])
                with client.db() as db:db.execute('UPDATE cache SET expires=0')
                with self.assertRaises(module.Refused):client.call('lookup',{})


if __name__=='__main__':unittest.main()
