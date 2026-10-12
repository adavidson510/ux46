import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_assistant_rooms import read,settings

class Client:
 def __init__(self):
  self.model='small';self.effort='medium';self.posts=[];self.refuse='';self.human='Switch Work to large high';self.changed=False;self.history_down=False
 def request(self,method,path,body=None):
  if '/desk/history' in path:return 200,{'items':[{'type':'userMessage','id':'h','text':self.human}]}
  if method=='POST':
   self.posts.append((path,body));command=body.get('command','')
   if command=='/status':return 200,{'command':{'state':'ready','native':{'model':self.model,'reasoning_effort':self.effort,'active_turn':''},'catalog':{'models':[{'model':x,'efforts':['medium','high']} for x in ['small','large']]}}}
   if self.refuse:return 409,{'error':{'code':self.refuse}}
   if command.startswith('/model '):self.model=command.split()[1]
   if command.startswith('/reasoning '):self.effort=command.split()[1]
   return 200,{'command':{'state':'updated'}}
  if '/history' in path:return 200,{'unavailable':self.history_down,'items':[{'type':'agentMessage','phase':'final_answer','text':'x'*8000,'id':str(i)} for i in range(24)]}
  return 200,{'id':'p/work','title':'Work','native':{'thread_id':'desk' if '/desk' in path else 'different' if self.changed else 'work','model':self.model,'reasoning_effort':self.effort},'checkpoint':{'at':'2026-01-01','next':'Old report'}}

class Tests(unittest.TestCase):
 def setUp(self):
  self.c=Client();self.clients={'local':self.c};self.cfg={'target':{'agent':'local','room':'p/desk','thread':'desk'},'sources':[{'agent':'local','room':'p/work','thread':'work','title':'Work'}]}
 def test_read_is_bounded_and_keeps_date_without_waking_room(self):
  r=read(self.cfg,'Work',self.clients);self.assertLessEqual(sum(len(x['text']) for x in r['messages']),12000);self.assertFalse(self.c.posts);self.assertEqual(r['checkpoint']['at'],'2026-01-01')
  self.c.history_down=True;r=read(self.cfg,'Work',self.clients);self.assertFalse(r['history_available']);self.assertEqual(r['messages'],[])
 def test_assistant_can_inspect_its_own_settings(self):
  r=settings(self.cfg,'local/p/desk',self.clients);self.assertEqual(r['native']['model'],'small');self.assertEqual(r['room']['thread'],'desk')
 def test_native_readback_verifies_both_settings(self):
  r=settings(self.cfg,'Work',self.clients,request=self.c.human,model='large',effort='high');self.assertEqual(r['state'],'verified');self.assertEqual(r['after']['reasoning_effort'],'high');self.assertEqual(r['after']['model'],'large')
 def test_busy_is_not_claimed_as_success_or_queued(self):
  self.c.refuse='connection_busy';r=settings(self.cfg,'Work',self.clients,request=self.c.human,model='large',effort='high')
  self.assertEqual(r['state'],'not_verified');self.assertEqual(r['after']['model'],'small');self.assertEqual(len(r['steps']),1)
 def test_packet_or_stale_human_request_cannot_change_settings(self):
  self.c.human='CONCIERGE UPDATE PACKET switch model'
  with self.assertRaises(ValueError):settings(self.cfg,'Work',self.clients,request=self.c.human,model='large')
  self.assertTrue(all(b['command']=='/status' for _,b in self.c.posts))
 def test_unsupported_choice_has_no_settings_write(self):
  with self.assertRaises(ValueError):settings(self.cfg,'Work',self.clients,request=self.c.human,model='invented')
  self.assertTrue(all(b['command']=='/status' for _,b in self.c.posts))
 def test_partial_change_reports_actual_settings(self):
  original=self.c.request
  def request(method,path,body=None):
   if body and body.get('command')=='/reasoning high':return 409,{'error':{'code':'connection_busy'}}
   return original(method,path,body)
  self.c.request=request
  r=settings(self.cfg,'Work',self.clients,request=self.c.human,model='large',effort='high')
  self.assertEqual(r['state'],'not_verified');self.assertEqual(r['after']['model'],'large');self.assertEqual(r['after']['reasoning_effort'],'medium')
 def test_definite_unsupported_model_refreshes_only_the_bound_room_once(self):
  original=self.c.request;calls=[];refreshed=False
  def request(method,path,body=None):
   nonlocal refreshed
   if body and body.get('command')=='/model large' and not refreshed:return 502,{'error':'unsupported_model'}
   if path=='/api/connection/refresh':
    calls.append(body);refreshed=True
    return 200,{'connection':{'state':'refreshed','scope':'conversation','room':'p/work'}}
   return original(method,path,body)
  self.c.request=request
  r=settings(self.cfg,'Work',self.clients,request=self.c.human,model='large',effort='high')
  self.assertEqual(r['state'],'verified');self.assertEqual(calls,[{'room':'p/work'}]);self.assertTrue(r['connection_refreshed'])
 def test_uncertain_setting_response_is_read_back_without_replay(self):
  original=self.c.request;calls=[]
  def request(method,path,body=None):
   if body and body.get('command')=='/model large':
    calls.append(body);self.c.model='large';raise TimeoutError()
   return original(method,path,body)
  self.c.request=request
  r=settings(self.cfg,'Work',self.clients,request=self.c.human,model='large',effort='high')
  self.assertEqual(len(calls),1);self.assertEqual(r['state'],'not_verified');self.assertEqual(r['after']['model'],'large')
 def test_changed_native_identity_never_writes(self):
  self.c.changed=True
  with self.assertRaises(ValueError):settings(self.cfg,'Work',self.clients,request=self.c.human,model='large')
  self.assertFalse(self.c.posts)

if __name__=='__main__':unittest.main()
