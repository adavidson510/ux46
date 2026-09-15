"""Installation metadata is private, explicit and independent of provider auth."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from ux46_local import initialize
from ux46_setup import configure, connect


class SetupTests(unittest.TestCase):
    def test_fresh_project_default_preserves_an_existing_owner_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=initialize(root)
            self.assertEqual(config['execution_policy'],'workspace-write')
            config['execution_policy']='full-access'
            (root/'config.json').write_text(json.dumps(config))
            self.assertEqual(initialize(root)['execution_policy'],'full-access')

    def test_ambiguous_detection_does_not_rewrite_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=initialize(root)
            before=(root/'config.json').read_bytes()
            with patch('ux46_setup.detected',return_value={'codex':'/fake/codex','claude':'/fake/claude'}):
                with self.assertRaises(ValueError):configure(root,config)
            self.assertEqual(before,(root/'config.json').read_bytes())

    def test_no_cli_is_a_valid_install_and_private_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=initialize(root)
            with patch('ux46_setup.detected',return_value={'codex':None,'claude':None}):
                record=configure(root,config)
            self.assertEqual(record['agent'],'none')
            self.assertFalse(record['cli_available'])
            self.assertEqual(record,json.loads((root/'connection.json').read_text()))
            self.assertEqual((root/'connection.json').stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads((root/'registry.json').read_text())['projects'],[])
            self.assertEqual(record['environment']['UX46_HOME'],str(root))

    def test_explicit_cli_preserves_modules_and_customization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=initialize(root);config['port']=9988
            record=configure(root,config,agent='claude',name='My helper',cli=sys.executable)
            saved=json.loads((root/'config.json').read_text())
            self.assertEqual(saved['port'],9988)
            self.assertEqual(saved['modules'],config['modules'])
            self.assertTrue(record['cli_available'])
            self.assertEqual(record['name'],'My helper')

    def test_adapter_registration_does_not_claim_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);initialize(root)
            result=connect(root,identity='custom',name='Custom',runtime='custom',port=8890)
            self.assertFalse(result['verified'])
            item=json.loads((root/'agents.json').read_text())['agents'][0]
            self.assertEqual(item['transport'],'loopback')
            for identity,port in [('local',8890),('custom',8890),('bad',70000)]:
                with self.assertRaises(ValueError):connect(root,identity=identity,name='Custom',runtime='custom',port=port)

if __name__=='__main__':unittest.main()
