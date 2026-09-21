import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_mail_assistant import MailAssistant
from ux46_mail_filing import MailFiling
from constellation_store import Conflict

class FakeProvider:
    def __init__(self):
        self.email='owner@example.com';self.sent=[];self.revision='rev1';self.fail=False
        self.labels={'m1':['INBOX','UNREAD']};self.modifications=[]
    def thread(self,ident):
        return {'id':ident,'email':self.email,'complete':True,'revision':self.revision,'messages':[
            {'id':'m1','from':'person@example.net','reply_to':'person@example.net','to':self.email,'cc':'',
             'subject':'Question','message_id':'<one@example.net>','references':'','date':'Today','text':'Could you explain the plan?',
             'attachments':[],'labels':self.labels['m1'][:]}]}
    def send(self,d):
        self.sent.append(copy.deepcopy(d))
        if self.fail:raise TimeoutError()
        return {'id':'sent1'}
    def label(self,name):return 'label1'
    def modify(self,ids,add,remove):
        self.modifications.append((ids,add,remove))
        for ident in ids:self.labels[ident]=sorted((set(self.labels[ident])|set(add))-set(remove))
    def get(self,resource,**kw):return {'labelIds':self.labels[resource.split('/')[-1]]}

class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'email-assistant.json').write_text(json.dumps({'accounts':{'first':{'email':'owner@example.com','token_file':'fake'}}}))
        self.provider=FakeProvider()
        self.a=MailAssistant(self.root,lambda *args:self.provider,lambda *args:({'body':'Here is the plan.','note':'Review before sending.'},{'model_calls':1}))
        self.a.mail.account('first',email='owner@example.com',status='healthy',last_success=time.time(),coverage_complete=True)
    def draft(self):return self.a.prepare_draft('first','t1')
    def reviewed(self):
        d=self.draft();return self.a.save_draft({'id':d['id'],'base_revision':d['revision'],'to':d['to'],'cc':'','subject':d['subject'],'body':d['body']})
    def send(self,d):return self.a.send({'id':d['id'],'revision':d['revision'],'review_hash':d['review_hash']})
    def test_draft_is_local_and_requires_exact_saved_review(self):
        d=self.draft();self.assertEqual(self.provider.sent,[])
        with self.assertRaises(Conflict):self.a.send({'id':d['id'],'revision':1,'review_hash':'invented'})
        saved=self.a.save_draft({'id':d['id'],'base_revision':1,'to':d['to'],'cc':'','subject':d['subject'],'body':'Edited answer.'})
        self.assertEqual(saved['body'],'Edited answer.')
        with self.assertRaises(Conflict):self.a.save_draft({'id':d['id'],'base_revision':1})
        self.assertEqual(self.send(saved)['state'],'sent');self.send(saved);self.assertEqual(len(self.provider.sent),1)
    def test_simultaneous_send_is_once_and_lost_receipt_never_replays(self):
        d=self.reviewed();self.provider.fail=True
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.send(d),range(2)))
        self.assertEqual(len(self.provider.sent),1)
        fresh=MailAssistant(self.root,lambda *args:self.provider)
        self.assertEqual(fresh.send({'id':d['id']})['state'],'unknown');self.assertEqual(len(self.provider.sent),1)
    def test_changed_thread_blocks_send_and_keeps_edits(self):
        d=self.reviewed();self.provider.revision='rev2'
        with self.assertRaises(Conflict):self.send(d)
        self.assertEqual(self.provider.sent,[]);self.assertEqual(self.a.get('mail_drafts',d['id'])['body'],d['body'])
    def test_filing_and_undo_restore_labels_but_refuse_new_user_changes(self):
        filing=MailFiling(self.a);r=filing.apply('first','t1','receipts',True,True)
        self.assertEqual(self.provider.labels['m1'],['label1']);self.assertEqual(r['state'],'applied')
        filing.undo(r['id']);self.assertEqual(self.provider.labels['m1'],['INBOX','UNREAD'])
        r=filing.apply('first','t1','receipts',True,True);self.provider.labels['m1'].append('STARRED')
        with self.assertRaises(Conflict):filing.undo(r['id'])
    def test_attention_mail_is_labelled_but_kept_unread_in_inbox(self):
        self.a.mail.upsert('first',{'id':'m1','thread':'t1','stamp':time.time(),'subject':'Action required','sender':'person@example.net','recipient':self.provider.email,'snippet':'Please respond','labels':['INBOX','UNREAD']})
        self.a.put('mail_assistant','filing-policy',{'enabled':True,'archive_routine':True,'mark_read':True})
        MailFiling(self.a).tick();self.assertIn('INBOX',self.provider.labels['m1']);self.assertIn('UNREAD',self.provider.labels['m1'])
    def test_brief_is_email_only_and_empty_coverage_is_explicit(self):
        b=self.a.prepare_brief();self.assertEqual(b['items'],[]);self.assertEqual(b['usage']['model_calls'],0)
        self.assertIn('not proof',b['summary']);self.assertEqual(self.provider.sent,[])
    def test_full_thread_is_required_for_drafting(self):
        orig=self.provider.thread
        self.provider.thread=lambda ident:{**orig(ident),'complete':False}
        with self.assertRaises(ValueError):self.draft()

if __name__=='__main__':unittest.main()
