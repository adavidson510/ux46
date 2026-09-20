"""Doctor diagnostics must inspect only, honor configured CLIs, and redact output."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import ux46_doctor as doctor
from ux46_local import initialize


class DoctorTests(unittest.TestCase):
    def test_missing_configuration_does_not_create_any_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'missing'
            result=doctor.inspect(root)
            self.assertEqual(result['configuration'],'invalid')
            self.assertFalse(root.exists())

    def test_configured_absolute_cli_is_checked_even_off_path_and_output_redacted(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout='codex-cli 1.2.3 private-identity@example.test' if args[-1]=='--version' else 'app-server usage')
        with patch.object(doctor.shutil,'which',side_effect=lambda value: '/fixture/chosen-cli' if value=='/fixture/chosen-cli' else None):
            result=doctor.runtime_check({'agent':'codex','cli':'/fixture/chosen-cli'},run)
        self.assertEqual(result['state'],'available');self.assertEqual(result['version'],'1.2.3')
        self.assertEqual([args[0] for args in calls],['/fixture/chosen-cli']*2)
        self.assertNotIn('private-identity',json.dumps(result))
        self.assertFalse(any('exec' in args for args in calls))

    def test_claude_requires_lsof_and_valid_native_inventory(self):
        def run(args,**kwargs):
            return SimpleNamespace(returncode=0,stdout='1.2.3' if args[-1]=='--version' else 'not json')
        with patch.object(doctor.shutil,'which',return_value='/fixture/cli'):
            result=doctor.runtime_check({'agent':'claude','cli':'/fixture/cli'},run)
        self.assertEqual(result['state'],'incompatible')
        self.assertEqual(result['capability'],'prerequisite_missing')

    def test_only_gets_and_no_configuration_changes(self):
        calls=[]
        class Client:
            def request(self,method,path,body=None):
                calls.append((method,path))
                if path.endswith('/bootstrap'):return 200,{'recovery':{'admission':True}}
                return 200,{'native':{'active_turn':None},'native_terminal':{'kind':'usage_limit','message':'private history'},
                    'account_status':{'state':'available','checked_at':time.time()-500},'draft':{'body':'private words'}}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);initialize(root)
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(doctor,'runtime_check',return_value={'state':'available'}):
                result=doctor.inspect(root,'local','fixture/room',client_factory=lambda *args:Client())
            after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
        self.assertTrue(all(method=='GET' for method,_ in calls))
        self.assertEqual(result['agents'][0]['quota'],'unknown')
        self.assertTrue(result['agents'][0]['room']['historical_failure'])
        self.assertNotIn('private words',json.dumps(result));self.assertNotIn('private history',json.dumps(result))

    def test_cli_alias_and_inspect_recovery_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'missing'
            env=dict(os.environ,UX46_HOME=str(root))
            reports=[]
            for spelling in ['doctor','--doctor']:
                done=subprocess.run([sys.executable,str(ROOT/'tools/ux46'),spelling,'--check','--json'],env=env,capture_output=True,text=True)
                self.assertEqual(done.returncode,1)
                report=json.loads(done.stdout);report.pop('checked_at');reports.append(report)
            self.assertEqual(*reports);self.assertFalse(root.exists())
            rejected=subprocess.run([sys.executable,str(ROOT/'tools/ux46'),'--doctor','--check','--recover-all'],env=env,capture_output=True,text=True)
            self.assertEqual(rejected.returncode,2);self.assertFalse(root.exists())


if __name__=='__main__':unittest.main()
