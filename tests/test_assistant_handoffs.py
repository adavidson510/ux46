import sys,json,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_assistant_handoffs import Handoffs

class Client:
 def __init__(self):self.posts=[];self.history=[];self.human='Ask Work to check the mobile layout';self.thread='work-thread';self.fail=False;self.active=''
 def request(self,method,path,body=None):
  if method=='POST':
   self.posts.append(body)
   if self.fail:raise TimeoutError('sensitive transport details')
   return 200,{'submission':{'status':'accepted','native_turn_id':'turn-1','mode':'steer'}}
  if '/submissions/' in path:return 200,{'submission':{'status':'accepted','native_turn_id':'turn-1'}}
  if '/desk/history' in path:return 200,{'items':[{'type':'userMessage','id':'human-1','text':self.human}]}
  if '/history' in path:return 200,{'items':self.history,'complete':True}
  return 200,{'native':{'thread_id':'desk-thread' if '/desk' in path else self.thread,'active_turn':self.active}}

class HandoffTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Handoffs(self.tmp.name);self.c=Client();self.clients={'local':self.c}
  self.cfg={'name':'Keel','target':{'agent':'local','room':'p/desk','thread':'desk-thread'},'sources':[{'agent':'local','room':'p/work','thread':'work-thread','title':'Work'}]}
 def ask(self):return self.store.ask(self.cfg,'Work','Ask Work to check the mobile layout','Check the mobile layout',self.clients)
 def test_once_and_only_the_exact_turn_counts_as_reply(self):
  r=self.ask();self.assertEqual(r['state'],'sent');self.assertEqual(self.ask()['id'],r['id']);self.assertEqual(len(self.c.posts),1)
  self.c.history=[{'id':'old','turn_id':'another-turn','type':'agentMessage','phase':'final_answer','text':'Wrong answer'}];self.store.track(self.clients)
  self.assertEqual(self.store.view()[0]['state'],'sent')
  self.c.history=[{'id':'answer','turn_id':'turn-1','type':'agentMessage','phase':'final_answer','text':'Layout checked'}];self.c.active='turn-1';self.store.track(self.clients)
  self.assertEqual(self.store.view()[0]['state'],'sent')
  self.c.active='';self.store.track(self.clients);self.assertEqual(self.store.view()[0]['reply'],'Layout checked')
 def test_packets_and_stale_requests_cannot_dispatch(self):
  self.c.human='CONCIERGE UPDATE PACKET ask Work to do something'
  with self.assertRaises(ValueError):self.ask()
  with self.assertRaises(ValueError):self.store.ask(self.cfg,'Work',self.c.human,'Do something',self.clients)
  self.assertFalse(self.c.posts)
 def test_unknown_send_is_read_back_without_replay(self):
  self.c.fail=True;self.assertEqual(self.ask()['state'],'unknown');self.ask();self.store.track(self.clients)
  self.assertEqual(len(self.c.posts),1);self.assertEqual(self.store.view()[0]['state'],'sent')
 def test_target_change_never_sends(self):
  self.c.thread='different';self.assertEqual(self.ask()['state'],'failed');self.assertFalse(self.c.posts)
 def test_changed_retry_wording_is_not_a_second_request(self):
  self.ask()
  with self.assertRaises(ValueError):self.store.ask(self.cfg,'Work',self.c.human,'Different instruction',self.clients)
  self.assertEqual(len(self.c.posts),1)

if __name__=='__main__':unittest.main()
