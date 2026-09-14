import json,socket,tempfile,threading,unittest
from pathlib import Path
from test_constellation_email import ROOT,lesson,Store,Principal
from constellation_relay import Client,Refused,Handler,Server
from ux46_workspace_api import WorkspaceAPI

class RelayTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.path=self.root/'relay.sock'
  self.server=Server(str(self.path),Handler)
  threading.Thread(target=self.server.serve_forever,daemon=True).start()
  self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
  self.config={'principal':'peer','socket':str(self.path),'state_dir':str(self.root/'cache')}
  self.client=Client(self.config)
 def test_fixed_upstream_principal_and_error_preservation(self):
  class Upstream:
   def request(self,operation,args):
    if operation=='capture':raise Refused('stale',409)
    return {'principal':'peer','result':{'ok':True},'cache_ttl_seconds':300}
  self.server.client=Upstream()
  self.assertTrue(self.client.request('health',{})['result']['ok'])
  with self.assertRaises(Refused) as error:self.client.request('capture',{})
  self.assertEqual(error.exception.status,409)
  forged=Client(dict(self.config,principal='other'))
  with self.assertRaisesRegex(Refused,'principal mismatch'):forged.request('health',{})
 def test_gateway_reads_remote_without_local_write_fallback(self):
  config=self.root/'client.json';config.write_text(json.dumps(self.config))
  api=WorkspaceAPI(self.root/'workspace',config)
  class Upstream:
   def request(self,operation,args):
    if operation=='lookup':return {'principal':'peer','result':{'items':[{'id':'central'}]}}
    raise OSError('offline')
  self.server.client=Upstream()
  self.assertEqual(api.dispatch('/api/constellation/lookup',{})['items'][0]['id'],'central')
  with self.assertRaises(OSError):api.dispatch('/api/constellation/capture',lesson())
  self.assertIsNone(api.constellation)
  self.assertFalse((self.root/'workspace/constellation.sqlite3').exists())

class MigrationBackupTests(unittest.TestCase):
 def test_imported_maintenance_receipt_does_not_skip_new_destination(self):
  from constellation_admin import maintain
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);db=root/'knowledge.sqlite3'
   Store(db).capture(Principal('owner',('*',),True),lesson())
   self.assertTrue(maintain(db,root/'old-host')['changed'])
   result=maintain(db,root/'new-host')
   self.assertTrue(result['changed'])
   self.assertTrue((root/'new-host'/(result['generation']+'.jsonl')).is_file())
   self.assertFalse(maintain(db,root/'new-host')['changed'])

class LearningTransportTests(unittest.TestCase):
 def test_brief_is_scoped_cached_bounded_and_confidential_replacement_removes_cache(self):
  from unittest.mock import patch
  from ux46_workspace_api import constellation_call
  with tempfile.TemporaryDirectory() as directory:
   store=Store(Path(directory)/'store.db');owner=Principal('peer',('*',),True)
   record=lesson();store.capture(owner,record)
   client=Client({'principal':'peer','socket':str(Path(directory)/'not-running.sock'),'state_dir':str(Path(directory)/'cache')})
   def online(operation,args):return {'principal':'peer','result':constellation_call(store,owner,operation,args),'cache_ttl_seconds':300}
   args={'query':record['record']['claim'],'budget_bytes':3000}
   with patch.object(client,'request',side_effect=online):
    result=client.call('brief',args);self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),3000)
   with patch.object(client,'request',side_effect=OSError('offline')):
    self.assertTrue(client.call('brief',args)['offline'])
    with self.assertRaises(Refused):client.call('source',{'refs':[]})
   current=store.get(owner,record['record']['id']);current['classification']='confidential'
   store.capture(owner,{'key':'confidential','base_revision':1,'record':current})
   with patch.object(client,'request',side_effect=online):
    self.assertEqual(client.call('brief',args)['items'][0]['classification'],'confidential')
   with patch.object(client,'request',side_effect=OSError('offline')):
    with self.assertRaises(Refused):client.call('brief',args)
