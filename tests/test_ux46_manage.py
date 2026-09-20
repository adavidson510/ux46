"""Installed source recovery uses disposable files, never an owner's workspace."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import ux46_manage as manage
from ux46_local import initialize


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve()
        self.root=self.base/'private';self.source=self.base/'source';self.source.mkdir()
        (self.source/'app').mkdir();(self.source/'app/style.css').write_text('body { color: white; }')
        self.control=self.base/'installer-control';self.control.mkdir()
        for name in ('ux46_manage.py','ux46_recovery.py'):shutil.copy2(ROOT/'tools'/name,self.control/name)
        initialize(self.root)
        self.manifest={'schema_version':1,'id':'fixture123','release':'fixture-v1','source':str(self.source),
            'state':str(self.root),'control':str(self.control),'files':manage.inventory(self.source)}
        manage.save(self.root/'installation.json',self.manifest)
    def tearDown(self):self.temp.cleanup()

    def test_snapshot_changes_and_independent_undo_keep_private_state_and_local_history(self):
        prepared=manage.prepare_customization(self.root,'customfixture123')
        point=prepared['recovery_point']
        self.assertEqual(prepared['source'],str(self.source))
        self.assertFalse((self.source/'sessions').exists())
        self.assertTrue((self.root/'projects/ux46-workspace/project.json').exists())
        (self.source/'app/style.css').write_text('body { color: violet; }')
        (self.source/'new-feature.txt').write_text('new file')
        (self.source/'.git').mkdir();(self.source/'.git/keep').write_text('local history')
        (self.root/'draft.txt').write_text('private unsent words')
        status=manage.source_status(self.root)
        self.assertTrue(status['update_conflicts']);self.assertEqual(status['changed'],['app/style.css'])
        # Run the copied independent controller, even with the editable entry broken.
        (self.source/'tools').mkdir();(self.source/'tools/ux46').write_text('broken syntax !!!')
        env=dict(os.environ,UX46_HOME=str(self.root))
        done=subprocess.run([sys.executable,str(self.control/'ux46_manage.py'),'undo','--point',point,'--json'],env=env,capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stderr);receipt=json.loads(done.stdout)
        self.assertEqual((self.source/'app/style.css').read_text(),'body { color: white; }')
        self.assertFalse((self.source/'new-feature.txt').exists())
        self.assertEqual((Path(receipt['previous_source'])/'new-feature.txt').read_text(),'new file')
        self.assertEqual((self.source/'.git/keep').read_text(),'local history')
        self.assertEqual((self.root/'draft.txt').read_text(),'private unsent words')
        self.assertTrue(manage.source_status(self.root)['clean'])

    def test_interrupted_undo_resumes_exact_transaction(self):
        point=manage.checkpoint(self.root,self.source)
        (self.source/'app/style.css').write_text('a changed source')
        original=os.rename
        calls=[]
        def rename(source,destination):
            calls.append((source,destination))
            if Path(destination)==self.source:raise OSError('simulated interruption after old source moved')
            return original(source,destination)
        with patch.object(manage.os,'rename',side_effect=rename),self.assertRaises(OSError):manage.restore(self.root,point['id'])
        self.assertFalse(self.source.exists())
        result=manage.restore(self.root,point['id'])
        self.assertEqual(result['state'],'restored')
        self.assertEqual((self.source/'app/style.css').read_text(),'body { color: white; }')

    def test_symlink_and_tampered_snapshot_are_refused(self):
        outside=self.base/'outside';outside.write_text('private')
        (self.source/'link').symlink_to(outside)
        with self.assertRaises(ValueError):manage.checkpoint(self.root,self.source)
        (self.source/'link').unlink();point=manage.checkpoint(self.root,self.source)
        (self.root/'recovery-points'/point['id']/'source/app/style.css').write_text('changed snapshot')
        with self.assertRaises(ValueError):manage.restore(self.root,point['id'])
        self.assertEqual(outside.read_text(),'private')

    def test_active_registered_process_prevents_source_undo(self):
        point=manage.checkpoint(self.root,self.source)
        manage.save(self.root/'control/console.json',{'pid':os.getpid()})
        with self.assertRaisesRegex(ValueError,'Stop this installation first'):manage.restore(self.root,point['id'])

    def test_duplicate_customization_does_not_replace_recovery_point(self):
        one=manage.prepare_customization(self.root,'samefixture123')
        (self.source/'app/style.css').write_text('new work')
        two=manage.prepare_customization(self.root,'samefixture123')
        self.assertEqual(one,two)
        registry=manage.read(self.root/'registry.json')
        self.assertEqual(len(registry['projects']),1)
        self.assertEqual((self.root/'recovery-points/samefixture123/source/app/style.css').read_text(),'body { color: white; }')

    def test_real_no_provider_start_stop_reopen_preserves_private_data(self):
        # Real source modules, an isolated installation and a free loopback port.
        shutil.copytree(ROOT/'tools',self.source/'tools',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'app',self.source/'app',dirs_exist_ok=True)
        config=manage.read(self.root/'config.json');config['agent']='none'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));config['port']=sock.getsockname()[1]
        manage.save(self.root/'config.json',config)
        (self.root/'draft.txt').write_text('unsent')
        try:
            first=manage.lifecycle(self.root,'start');self.assertEqual(first['state'],'ready')
            pid=manage.read(self.root/'control/console.json')['pid']
            self.assertEqual(manage.lifecycle(self.root,'start')['state'],'ready')
            self.assertEqual(manage.read(self.root/'control/console.json')['pid'],pid)
            self.assertEqual(manage.lifecycle(self.root,'stop')['state'],'stopped')
            self.assertEqual(manage.lifecycle(self.root,'start')['state'],'ready')
            self.assertEqual((self.root/'draft.txt').read_text(),'unsent')
        finally:manage.lifecycle(self.root,'stop')


if __name__=='__main__':unittest.main()
