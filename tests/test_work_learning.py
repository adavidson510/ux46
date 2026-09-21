import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_work import WorkStore
from ux46_work_checkpoint import packet
from constellation_store import Conflict

class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'work.db';self.s=WorkStore(self.path)
        self.source={'id':'idea1','kind':'idea','title':'Larger controls','summary':'Try larger controls on the phone','sources':[]}
        self.item=self.s.observe(self.source)
    def route(self):return self.s.action({'action':'route','source':'idea1','agent':'local','room':'app/timer','reason':'Phone controls are small'})
    def test_dismiss_survives_source_revision_and_restore(self):
        self.s.action({'action':'dismiss','id':'idea1','base_version':1});self.s.observe({**self.source,'summary':'New evidence'})
        self.assertEqual(WorkStore(self.path).view()['sources'],[])
        item=self.s.view(lane='dismissed')['sources'][0]
        self.s.action({'action':'restore','id':'idea1','base_version':item['version']});self.assertEqual(len(self.s.view()['sources']),1)
    def test_material_revision_reopens_review_not_a_comment_or_repeated_read(self):
        r=self.route();s=self.s.observe(self.source)
        self.assertEqual(s['version'],1)
        self.s.action({'action':'assess','id':r['id'],'base_version':r['version'],'source_digest':s['digest'],'verdict':'test','reason':'Try it once'})
        self.assertFalse(self.s.view()['routes'][0]['needs_review'])
        self.s.observe({**self.source,'summary':'Changed evidence'});self.assertTrue(self.s.view()['routes'][0]['needs_review'])
        with self.assertRaises(Conflict):self.s.action({'action':'assess','id':r['id'],'base_version':2,'source_digest':s['digest'],'verdict':'test','reason':'Old read'})
    def test_room_packet_deduplicates_and_is_bound_to_configured_reporter(self):
        self.route();p=self.path.parent/'config.json';p.write_text(json.dumps({'store':str(self.path),'reporters':{'local':'local'}}))
        with patch.dict(os.environ,{'UX46_WORK_REVIEW_CONFIG':str(p)}):
            self.assertIsNone(packet('app/timer','other'));self.assertIsNone(packet('other/room','local'))
            self.assertEqual(len(packet('app/timer','local')['items']),1);self.assertIsNone(packet('app/timer','local'))
            self.s.observe({**self.source,'summary':'New useful evidence'});self.assertEqual(len(packet('app/timer','local')['items']),1)
    def test_outcome_requires_chosen_experiment_and_result_is_room_scoped(self):
        with self.assertRaises(ValueError):self.s.action({'action':'outcome','id':'missing','base_version':0,'verdict':'helped','reason':'A vote','evidence':'none'})
        e=self.s.action({'action':'experiment','source':'idea1','agent':'local','room':'app/timer','hypothesis':'Easier to tap','baseline':'Missed twice','check':'Try one handed','stop':'After one use'})
        r=self.s.action({'action':'result','agent':'local','room':'app/timer','experiment':e['id'],'title':'Timer','url':'https://example.com/timer','checked':'Narrow layout','unchecked':'Phone use','evidence':'fixture receipt','base_version':0})
        self.assertEqual(self.s.view('local','other/room')['results'],[])
        self.assertEqual(self.s.view('local','app/timer')['results'][0]['id'],r['id'])
        self.s.action({'action':'outcome','id':e['id'],'base_version':e['version'],'verdict':'unknown','reason':'No phone use yet','evidence':'Layout test only'})
        self.assertEqual(self.s.view()['metrics']['helped'],0)
    def test_plain_explanation_keeps_source_and_expires_when_evidence_changes(self):
        r=self.route();digest=self.item['digest']
        explained=self.s.action({'action':'explain','id':'idea1','base_version':1,'source_digest':digest,
            'title':'Make phone buttons easier to tap','idea':'Larger controls may reduce missed taps.',
            'why':'Small controls are hard to use one handed.','next':'Try one timer screen.','editor':'Room agent'})
        self.assertEqual(explained['digest'],digest)
        self.assertEqual(explained['summary'],self.source['summary'])
        self.assertEqual(explained['explanation']['source_digest'],digest)
        changed=self.s.observe({**self.source,'summary':'A user reports a different cause'})
        self.assertNotEqual(changed['explanation']['source_digest'],changed['digest'])
        with self.assertRaises(Conflict):
            self.s.action({'action':'explain','id':'idea1','base_version':changed['version'],'source_digest':digest})
    def test_chosen_room_takes_precedence_over_automatic_source_room(self):
        self.s.action({'action':'route','source':'idea1','agent':'local','room':'app/old','reason':'Cited source','automatic':True})
        chosen=self.route()
        self.assertEqual(self.s.view()['routes'][0]['id'],chosen['id'])
        latest=self.s.action({'action':'route','source':'idea1','agent':'local','room':'app/old','reason':'Discuss here now'})
        self.assertEqual(self.s.view()['routes'][0]['id'],latest['id'])
    def test_result_refuses_active_content_urls_and_cross_room_experiment(self):
        with self.assertRaises(ValueError):self.s.action({'action':'result','agent':'local','room':'app/timer','url':'javascript:alert(1)'})

if __name__=='__main__':unittest.main()
