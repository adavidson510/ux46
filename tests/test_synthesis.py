import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_synthesis import synthesize, shape, STRING, PreparationError

class SynthesisTests(unittest.TestCase):
    def test_missing_launcher_runtime_is_not_reported_as_login_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli=Path(tmp)/'codex'
            cli.write_text('#!/bin/sh\necho "env: node: No such file or directory" >&2\nexit 127\n')
            cli.chmod(0o700)
            with self.assertRaises(PreparationError) as caught:
                synthesize('Summarize',{},shape({'summary':STRING}),tmp,str(cli))
            self.assertEqual(caught.exception.code,'writer_unavailable')
            self.assertIn('background job environment',str(caught.exception))

    def test_missing_executable_and_login_failure_have_distinct_safe_errors(self):
        with patch('ux46_synthesis.subprocess.run',side_effect=FileNotFoundError('private path')):
            with self.assertRaises(PreparationError) as caught:synthesize('',{}, {},'/unused','missing')
        self.assertEqual(caught.exception.code,'writer_unavailable')
        self.assertNotIn('private path',str(caught.exception))
        with patch('ux46_synthesis.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','Not logged in')):
            with self.assertRaises(PreparationError) as caught:synthesize('',{}, {},'/unused','missing')
        self.assertEqual(caught.exception.code,'login_required')

if __name__=='__main__':unittest.main()
