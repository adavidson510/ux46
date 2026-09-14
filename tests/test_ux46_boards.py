"""Board isolation, narrow content, conflicting edits and authenticated writes."""
import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ux46_boards import BoardStore, Conflict
from ux46_room import change
from atlas_desktops import DesktopStore
from ux46_board_gateway import BoardAPI
import ux46_access_gateway as gate
from test_ux46_access_gateway import GateCase, ORIGIN
from test_ux46_live_agents import api

BOARD = {'title':'Demo-pet','reporter':'Local','sections':[
    {'title':'Now','items':[{'label':'Glucose chart shortcuts','state':'doing'}]},
    {'title':'Reliability','items':[{'label':'Recovery test','value':'43 seconds',
     'detail':'Reported 9 Sep; separate offsite recovery remains unverified','state':'done'}]}]}

class Store(unittest.TestCase):
    def test_chart_and_image_survive_save_but_urls_and_nonfinite_values_do_not(self):
        with TemporaryDirectory() as tmp:
            store=BoardStore(Path(tmp)/'boards.db')
            board=copy.deepcopy(BOARD)
            board['sections'][0]['chart']={'type':'line','labels':['Mon','Tue'],'values':[12,18],'unit':'visitors'}
            board['sections'][0]['image']={'file_id':'a'*64,'file_agent':'local','alt':'Our logo'}
            saved=store.save('local','project/room',0,board)
            self.assertEqual(saved['board']['sections'][0]['chart']['values'],[12,18])
            self.assertEqual(saved['board']['sections'][0]['image']['alt'],'Our logo')
            board['sections'][0]['chart']['values'][0]=float('nan')
            with self.assertRaises(ValueError):store.save('local','project/room',1,board)
            board['sections'][0]['chart']['values'][0]=12
            board['sections'][0]['image']['file_id']='https://example.com/tracker'
            with self.assertRaises(ValueError):store.save('local','project/room',1,board)

    def test_room_icon_updates_preserve_notes_desktops_and_other_room_marks(self):
        with TemporaryDirectory() as tmp:
            store=DesktopStore(Path(tmp)/'desktop.db')
            state=store.read()['state'];state['notes']=[{'id':'one','text':'keep me'}]
            state['sessionMarks']=[{'agent':'cp','room':'project/other','kind':'initials','value':'CP'}]
            store.save(0,state)
            change(store,'local','project/room',1,'brand','ux46')
            saved=store.read()['state']
            self.assertEqual(saved['notes'],state['notes'])
            self.assertEqual(saved['sessionMarks'][0],state['sessionMarks'][0])
            self.assertEqual(saved['sessionMarks'][1]['value'],'ux46')
            change(store,'local','project/room',2,'label','UX46')
            self.assertEqual(len(store.read()['state']['sessionMarks']),2)

    def test_two_devices_conflict_and_agent_rooms_are_separate(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'boards.db'; laptop=BoardStore(path); phone=BoardStore(path)
            self.assertIsNone(phone.read('local','demo-pet/v3')['board'])
            saved=laptop.save('local','demo-pet/v3',0,BOARD)
            self.assertEqual(phone.read('local','demo-pet/v3'),saved)
            with self.assertRaises(Conflict):phone.save('local','demo-pet/v3',0,BOARD)
            self.assertIsNone(phone.read('cp','demo-pet/v3')['board'])
            self.assertIsNone(phone.read('local','demo-pet/other')['board'])
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            board=copy.deepcopy(BOARD);board['title']='Edited'
            phone.save('local','demo-pet/v3',1,board)
            with phone.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM board_revisions').fetchone()[0],2)

    def test_narrow_content_limits_and_invalid_states_do_not_write(self):
        with TemporaryDirectory() as tmp:
            store=BoardStore(Path(tmp)/'boards.db')
            for mutate in [lambda b:b.update(title='x'*81),
                           lambda b:b['sections'][0]['items'][0].update(label='a\nparagraph'),
                           lambda b:b['sections'][0]['items'][0].update(state='live'),
                           lambda b:b['sections'][0].update(items=[{'label':'x'}]*25)]:
                board=copy.deepcopy(BOARD);mutate(board)
                with self.assertRaises(ValueError):store.save('local','project/room',0,board)
            self.assertEqual(store.read('local','project/room')['version'],0)

@unittest.skipUnless(gate.scrypt_available(),'scrypt required')
class Gateway(unittest.TestCase):
    public_brand=False
    ask=GateCase.ask
    @classmethod
    def setUpClass(cls):
        GateCase.setUpClass.__func__(cls)
        cls.old_console=cls.console;cls.console=api('board-csrf')
        cls.server.console=gate.Upstream('http://127.0.0.1:'+str(cls.console.server_address[1]))
        cls.server.boards=BoardAPI(Path(cls.tmp.name)/'boards.db')
    @classmethod
    def tearDownClass(cls):
        cls.old_console.shutdown();cls.old_console.server_close()
        GateCase.tearDownClass.__func__(cls)
    def test_password_identity_origin_and_csrf_required(self):
        path='/api/boards/cp/weird-gallery/start-here'
        self.assertEqual(self.ask(path=path,password=None)[0],401)
        self.assertEqual(self.ask(path=path,identity='stranger')[0],403)
        body=json.dumps({'base_version':0,'board':BOARD}).encode()
        for headers in [{},{'Origin':ORIGIN,'X-Atlas-CSRF':'wrong'},
                        {'Origin':'https://evil.example','X-Atlas-CSRF':'board-csrf'}]:
            self.assertEqual(self.ask('PUT',path,body=body,headers=headers)[0],403)
        self.assertEqual(self.server.boards.store.read('cp','weird-gallery/start-here')['version'],0)
    def test_saved_roundtrip_and_stale_edit_retained(self):
        path='/api/boards/local/demo-pet/v3'
        headers={'Origin':ORIGIN,'X-Atlas-CSRF':'board-csrf','Content-Type':'application/json'}
        body=json.dumps({'base_version':0,'board':BOARD}).encode()
        status,_,raw=self.ask('PUT',path,body=body,headers=headers)
        self.assertEqual(status,200,raw)
        self.assertEqual(json.loads(raw)['version'],1)
        self.assertEqual(json.loads(self.ask(path=path)[2])['board']['title'],'Demo-pet')
        status,_,raw=self.ask('PUT',path,body=body,headers=headers)
        self.assertEqual(status,409)
        self.assertEqual(json.loads(raw)['detail']['version'],1)
        self.assertFalse(any(r[0]!='GET' for r in self.console.seen))

if __name__=='__main__':unittest.main()
