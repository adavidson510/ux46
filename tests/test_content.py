import tempfile
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from ux46_content import ContentStore
from constellation_store import Conflict, Unavailable

class ContentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=ContentStore(self.temp.name)
  self.a={'agent':'keel','room':'example/article','id':'article-draft-01'}
 def create(self):return self.s.action({**self.a,'action':'create','kind':'markdown','title':'Draft 2','payload':{'text':'Original baseline'}})
 def test_two_editors_conflict_and_baseline_survives_reopen(self):
  self.create();self.s.action({**self.a,'action':'save','base_revision':1,'title':'Edited','payload':{'text':'Human edit'}})
  with self.assertRaises(Conflict):self.s.action({**self.a,'action':'save','base_revision':1,'title':'Stale','payload':{'text':'Lost edit'}})
  reopened=ContentStore(self.temp.name)
  self.assertEqual(reopened.read(self.a)['payload']['text'],'Human edit')
  self.assertEqual(reopened.read({**self.a,'revision':1})['payload']['text'],'Original baseline')
  with self.assertRaises(Conflict):self.create()
 def test_other_room_or_agent_cannot_substitute_content_id(self):
  self.create()
  for field,value in [('room','other/article'),('agent','aster')]:
   foreign={**self.a,field:value}
   with self.assertRaises(Unavailable):self.s.read(foreign)
   with self.assertRaises(Unavailable):self.s.action({**foreign,'action':'save','base_revision':1,'title':'Wrong','payload':{'text':'no'}})
 def test_chart_snapshot_preserves_labels_units_and_source(self):
  row=self.s.action({**self.a,'action':'create','kind':'chart','title':'Fixture comparison','payload':{'source':'Synthetic fixture, no market data','chart':{'type':'line','labels':['Mon','Tue'],'values':[1,2],'unit':'points'}}})
  self.assertEqual(row['payload']['chart']['unit'],'points');self.assertTrue(row['payload']['snapshot'])
  self.assertEqual(row['payload']['chart']['labels'],['Mon','Tue'])
 def test_html_is_stored_as_text_and_history_is_bounded_with_baseline(self):
  self.create()
  for r in range(1,55):self.s.action({**self.a,'action':'save','base_revision':r,'title':'Draft','payload':{'text':'<script>unsafe()</script>'}})
  d=self.s.read(self.a);self.assertLessEqual(len(d['history']),51);self.assertEqual(d['history'][-1]['revision'],1)

if __name__=='__main__':unittest.main()
