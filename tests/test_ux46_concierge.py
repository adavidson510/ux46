import json,sys,tempfile,unittest,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_concierge import Concierge
from constellation_store import Conflict

class Client:
 supported=True
 def __init__(self):self.rows=[];self.active=None;self.sent=[];self.fail=False;self.target_thread='dst';self.unavailable=False
 def request(self,method,path,body=None):
  if method=='POST':
   self.sent.append(body)
   if self.fail:raise TimeoutError()
   return 200,{'submission':{'status':'accepted','meaning':'accepted by the native runtime — not a completed answer'}}
  if '/history' in path:return 200,{'items':self.rows,'complete':True,'unavailable':self.unavailable}
  if '/pending' in path:return 200,{'pending':[]}
  dst='/desk' in path
  return 200,{'title':'Concierge' if dst else 'Project','native':{'thread_id':self.target_thread if dst else 'src','active_turn':self.active if not dst else None}}

def message(id,text='A change',phase='final_answer',kind='agentMessage'):return {'id':id,'text':text,'type':kind,'phase':phase}

class ConciergeTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Concierge(self.tmp.name);self.c=Client()
  self.s.change({'action':'configure','base_revision':0,'target':{'agent':'local','room':'p/desk'},'sources':[{'agent':'local','room':'p/work'}]},lambda _:self.c)
  self.cfg=self.s.settings();self.cfg['enabled']=True;self.s.save(self.cfg)
 def test_new_completed_changes_only_no_model_polling(self):
  self.c.rows=[message('old')];self.s.tick();self.s.tick();self.assertFalse(self.c.sent)
  self.c.active='turn';self.c.rows=[message('new','Still writing'),message('tool','shell',kind='commandExecution'),message('old')]
  self.s.tick();self.assertFalse(self.c.sent)
  self.c.active=None;self.c.rows[0]['text']='Completed';self.s.tick();self.assertEqual(len(self.c.sent),1)
  self.assertIn('Completed',self.c.sent[0]['body']);self.assertNotIn('shell',self.c.sent[0]['body'])
  self.s.tick();self.assertEqual(len(self.c.sent),1)
 def test_unknown_delivery_pauses_and_never_replays(self):
  self.s.tick();self.c.rows=[message('new')];self.c.fail=True;self.s.tick();self.assertFalse(self.s.settings()['enabled'])
  self.assertEqual(self.s.view()['attempts'][0]['state'],'unknown');self.s.tick();self.assertEqual(len(self.c.sent),1)
 def test_changed_target_and_unavailable_source_do_not_dispatch(self):
  self.s.tick();self.c.rows=[message('new')];self.c.unavailable=True;self.s.tick();self.assertFalse(self.c.sent)
  self.assertIn('Could not',self.s.view()['sources'][0]['error'])
  self.c.unavailable=False;self.c.target_thread='other'
  with self.assertRaises(ValueError):self.s.tick()
  self.assertFalse(self.c.sent)
 def test_focus_cas_pause_and_packet_bound(self):
  self.s.tick();self.s.change({'action':'focus','base_revision':1,'focus':'AT details'})
  with self.assertRaises(Conflict):self.s.change({'action':'pause','base_revision':1})
  self.c.rows=[message(str(i),'x'*9000) for i in range(20)];self.s.tick();self.assertLess(len(self.c.sent[0]['body']),16000)
  self.assertIn('AT details',self.c.sent[0]['body'])
  self.s.change({'action':'pause','base_revision':2});self.c.rows=[message('later')];self.s.tick();self.assertEqual(len(self.c.sent),1)
 def test_first_failed_read_does_not_turn_history_into_new_updates(self):
  self.c.unavailable=True;self.s.tick();self.c.unavailable=False;self.c.rows=[message('old')];self.s.tick();self.assertFalse(self.c.sent)
 def test_quiet_mode_skips_commentary_and_spaces_summaries(self):
  self.s.tick();self.c.rows=[message('comment','Progress',phase='commentary')];self.s.tick();self.assertFalse(self.c.sent)
  self.c.rows.insert(0,message('final','Done'));self.s.tick();self.assertEqual(len(self.c.sent),1)
  self.s.save_attempt({'id':'clock','at':time.time()-120,'state':'accepted'})
  with self.s.db() as c:c.execute('DELETE FROM attempts WHERE id!=?',('clock',))
  self.c.rows.insert(0,message('next','Next finished'));self.s.tick();self.assertEqual(len(self.c.sent),1)
  self.s.change({'action':'focus','base_revision':1,'update_mode':'live'});self.s.tick();self.assertEqual(len(self.c.sent),2)
 def test_requested_reply_returns_once_with_priority_and_provenance(self):
  self.s.tick();cfg=self.s.settings()
  self.s.save_attempt({'id':'earlier','at':time.time()-120,'state':'accepted'})
  self.s.handoffs.save({'id':'ask-one','at':time.time()-200,'state':'answered','source':cfg['target'],'destination':cfg['sources'][0], 'request':'Check layout','reply':'Layout passed','reply_id':'r'})
  self.c.rows=[message('r','Layout passed')];self.s.tick();self.assertEqual(len(self.c.sent),1)
  self.assertIn('Reply to your request: Check layout',self.c.sent[0]['body']);self.assertEqual(self.c.sent[0]['body'].count('Layout passed'),1)
  self.assertTrue(self.s.handoffs.view()[0]['returned_at']);self.s.tick();self.assertEqual(len(self.c.sent),1)
 def test_same_native_source_alias_rejected(self):
  self.c.target_thread='src'
  with self.assertRaises(ValueError):self.s.change({'action':'configure','base_revision':1,'target':{'agent':'local','room':'p/desk'},'sources':[{'agent':'local','room':'p/work'}]},lambda _:self.c)

if __name__=='__main__':unittest.main()
