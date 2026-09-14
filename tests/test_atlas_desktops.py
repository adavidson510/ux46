import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from atlas_desktops import DesktopStore, Conflict

class DesktopsTest(unittest.TestCase):
    def test_shared_versioned_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = DesktopStore(Path(tmp)/'layouts.sqlite3')
            phone = DesktopStore(Path(tmp)/'layouts.sqlite3')
            state = first.read()['state']
            state['aliases'] = [{'agent':'local','room':'atlas/ux','label':'UX46'}]
            state['desktops'] = [{'id':'one','name':'Work','tabs':[{'agent':'local','room':'atlas/ux'}], 'active':{'agent':'local','room':'atlas/ux'},'customizations':{'future':{'density':2}}}]
            state['future_extension'] = {'preserved':True}
            first.save(0, state)
            self.assertEqual(phone.read(), {'version':1,'state':state})
            with self.assertRaises(Conflict) as caught:
                phone.save(0, state)
            self.assertEqual(caught.exception.current, first.read())
            state['desktops'][0]['name']='Gym'
            phone.save(1,state)
            self.assertEqual(first.read()['state'],state)
            self.assertEqual(first.path.stat().st_mode & 0o777,0o600)

    def test_invalid_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DesktopStore(Path(tmp)/'layouts.sqlite3')
            original = store.read()
            for key,value in [('schema_version',2),('aliases',[{'agent':'../escape','room':'x/y','label':'x'}]),('desktops',[{'id':'one','name':'bad','tabs':[], 'active':{'agent':'local','room':'x/y'}}]),('extra','x'*61000)]:
                state=copy.deepcopy(original['state']);state[key]=value
                with self.assertRaises(ValueError): store.save(0,state)
                self.assertEqual(store.read(),original)
            with self.assertRaises(ValueError): store.save(True,original['state'])

    def test_named_live_desktops_are_isolated_and_preserve_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            laptop = DesktopStore(Path(tmp)/'layouts.sqlite3')
            phone = DesktopStore(Path(tmp)/'layouts.sqlite3')
            state = laptop.read()['state']
            state['desktops'] = [{'id':'snapshot','name':'Before travel','tabs':[
                {'agent':'local','room':'atlas/ux'}], 'active':{'agent':'local','room':'atlas/ux'}}]
            state['notes'] = [{'id':'n1','text':'keep the draft with its room','done':False}]
            state['navprefs'] = {'local\u0000atlas/ux': {'group':'active'}}
            state['sharedLayout'] = {'version':1, 'tabs':[{'agent':'local','room':'atlas/ux'}],
                'active':{'agent':'local','room':'atlas/ux'}, 'customizations':{'future':True}}
            # Migration keeps the anonymous layout as default, then adds a
            # named live space. A second client receives both exactly.
            state['liveDesktops'] = [
                {'id':'default','name':'Shared desktop','layout':state['sharedLayout']},
                {'id':'project','name':'Project Atlas','layout':{'version':1,
                    'tabs':[{'agent':'local','room':'atlas/ux'}, {'agent':'agent2','room':'atlas/ship'}],
                    'active':{'agent':'agent2','room':'atlas/ship'}, 'customizations':{}}},
            ]
            laptop.save(0, state)
            joined = phone.read()
            self.assertEqual(joined['state']['desktops'][0]['name'], 'Before travel')
            self.assertEqual(joined['state']['liveDesktops'][1]['layout']['tabs'][1]['room'], 'atlas/ship')
            changed = copy.deepcopy(joined['state'])
            changed['liveDesktops'][1]['layout']['tabs'].reverse()
            phone.save(joined['version'], changed)
            current = laptop.read()['state']
            self.assertEqual(current['liveDesktops'][0]['layout']['tabs'][0]['room'], 'atlas/ux')
            self.assertEqual(current['desktops'][0]['name'], 'Before travel')
            self.assertEqual(current['notes'][0]['text'], 'keep the draft with its room')
            self.assertEqual(current['navprefs']['local\u0000atlas/ux']['group'], 'active')

class DesktopApiTest(unittest.TestCase):
    def test_http_cas_and_auth(self):
        from test_atlas_console import ConsoleHarness
        h = ConsoleHarness()
        try:
            status, first = h.call('GET', '/api/desktop-state')
            self.assertEqual(status, 200)
            body = {'base_version':first['version'], 'state':first['state']}
            self.assertEqual(h.call('PUT','/api/desktop-state',body,csrf=False)[0],403)
            self.assertEqual(h.call('PUT','/api/desktop-state',body)[0],200)
            status, conflict = h.call('PUT','/api/desktop-state',body)
            self.assertEqual(status,409)
            self.assertEqual(conflict['detail']['version'],1)
            self.assertFalse(h.service.runtime_started)
        finally: h.close()

if __name__=='__main__': unittest.main()
