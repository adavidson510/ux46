import base64
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_recordings import RecordingStore
from constellation_store import Conflict, Unavailable

class RecordingsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=RecordingStore(self.tmp.name);self.ident='recording-test-00001'
  self.store.action({'action':'start','id':self.ident,'title':'Meeting','mime':'audio/webm'})
 def tearDown(self):
  self.store.executor.shutdown();self.tmp.cleanup()
 def chunk(self,seq,raw=b'audio'):
  return self.store.action({'action':'chunk','id':self.ident,'seq':seq,'data':base64.b64encode(raw).decode()})
 def test_retry_order_and_reopen_preserve_audio(self):
  self.chunk(0);self.chunk(0)
  with self.assertRaises(Conflict):self.chunk(0,b'changed')
  with self.assertRaises(Conflict):self.chunk(2)
  with self.assertRaises(Conflict):self.store.action({'action':'finish','id':self.ident,'count':2})
  self.chunk(1,b'more')
  self.store.action({'action':'finish','id':self.ident,'count':2,'duration':12,'interrupted':True})
  other=RecordingStore(self.tmp.name)
  row,audio=other.audio(self.ident);other.executor.shutdown()
  self.assertEqual(audio,b'audiomore');self.assertTrue(row['interrupted']);self.assertEqual(row['status'],'saved')
  with self.assertRaises(Conflict):self.chunk(2)
 def test_trash_restore_and_no_implicit_transcription(self):
  self.chunk(0);row=self.store.action({'action':'finish','id':self.ident,'count':1})
  self.assertEqual(row['status'],'saved');self.assertEqual(row['transcript'],'')
  trashed=self.store.action({'action':'trash','id':self.ident,'version':row['version']})
  with self.assertRaises(Conflict):self.store.action({'action':'rename','id':self.ident,'title':'Other','version':row['version']})
  restored=self.store.action({'action':'restore','id':self.ident,'version':trashed['version']})
  self.assertFalse(restored['deleted'])
  with self.assertRaises(Unavailable):self.store.action({'action':'transcribe','id':self.ident})
 def test_metadata_is_searchable_without_creating_native_sessions(self):
  import json
  import session_vault as vault
  registry={'recordings_roots':[str(self.store.index)]}
  row=self.store.view({'id':self.ident})
  self.store.action({'action':'metadata','id':self.ident,'version':row['version'],
   'metadata':{'tags':'supplier warranty','location':'Kitchen','summary':'Discussed replacement parts.'}})
  found=vault.recording_records(registry,[])
  self.assertEqual(len(found),1);self.assertEqual(found[0].metadata['kind'],'recording')
  self.assertIn('replacement parts',found[0].body)
  self.assertEqual(vault.records([found[0].project])[0],[])
  self.assertEqual(len(self.store.view({'query':'warranty'})['recordings']),1)
  row=self.store.view({'id':self.ident})
  self.store.action({'action':'trash','id':self.ident,'version':row['version']})
  self.assertEqual(vault.recording_records(registry,[]),[])

 def test_rejects_paths_and_non_audio(self):
  with self.assertRaises(ValueError):self.store.action({'action':'start','id':'../../etc/recording','title':'x','mime':'audio/webm'})
  with self.assertRaises(ValueError):self.store.action({'action':'start','id':'recording-test-00002','title':'x','mime':'text/html'})

if __name__=='__main__':unittest.main()
