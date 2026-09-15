"""Offline installer tests: checked archive, hostile paths, private local ownership."""
import hashlib
import io
import json
import os
import pty
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def attempt(self,root,*,hostile=False,bad_checksum=False,args=None,terminal=False):
        fixtures=root/'fixtures';fixtures.mkdir(exist_ok=True)
        archive=fixtures/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            code=b'import json,os,sys; from pathlib import Path; p=Path(os.environ["INSTALL_TEST_CALLS"]); p.open("a").write(json.dumps(sys.argv[1:])+"\\n"); print(json.dumps({"agent":"none","instructions":"docs/ai-install.md"}))\n'
            entry=tarfile.TarInfo('../escaped' if hostile else 'tools/ux46');entry.size=len(code)
            tar.addfile(entry,io.BytesIO(code))
        sha='0'*64 if bad_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
        script=re.sub(r"archive_sha='[^']+'","archive_sha='"+sha+"'",(ROOT/'install.sh').read_text())
        (fixtures/'install.sh').write_text(script)
        # Only curl is stubbed; run the real shell, extractor and generated launcher.
        curl=fixtures/'curl'
        curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\nif [ "$1" = -o ]; then cp "$FIXTURE_ARCHIVE" "$2"; exit; fi\nshift\ndone\nexit 2\n')
        curl.chmod(0o700)
        source=root/"my ' copy $(touch SHOULD_NOT_EXIST)"
        state=root/'private data';binaries=root/'local bin'
        env=dict(os.environ,HOME=str(root),PATH=str(fixtures)+os.pathsep+os.environ['PATH'],
                 INSTALL_TEST_CALLS=str(root/'calls.jsonl'),FIXTURE_ARCHIVE=str(archive),UX46_INSTALL_DIR=str(source),UX46_HOME=str(state),UX46_BIN_DIR=str(binaries))
        arguments=args if args is not None else ['--agent','none','--no-start','--no-python-download']
        # Feed the script through stdin, as curl | sh does.
        command=['sh','-s','--',*arguments]
        if terminal:
            master,slave=pty.openpty()
            output=[]
            def drain():
                try:
                    while True:
                        chunk=os.read(master,4096)
                        if not chunk:break
                        output.append(chunk)
                except OSError:pass
            reader=threading.Thread(target=drain,daemon=True);reader.start()
            try:
                result=subprocess.run(command,input=script,cwd=root,env=env,stdout=slave,stderr=slave,text=True,timeout=30)
                os.close(slave);slave=None
                reader.join(timeout=5)
                result.stdout=b''.join(output).decode(errors='replace');result.stderr=''
            finally:
                if slave is not None:os.close(slave)
                os.close(master)
        else:
            result=subprocess.run(command,input=script,cwd=root,env=env,capture_output=True,text=True,errors='replace',timeout=30)
        return result,source,state,binaries

    def test_checked_install_handles_literal_paths_and_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);result,source,state,binaries=self.attempt(root)
            self.assertEqual(result.returncode,0,result.stderr)
            launcher=binaries/'ux46'
            self.assertEqual(launcher.stat().st_mode & 0o777,0o700)
            run=subprocess.run([str(launcher),'doctor'],cwd=root,env=dict(os.environ,INSTALL_TEST_CALLS=str(root/'calls.jsonl')),capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout)['agent'],'none')
            self.assertFalse((root/'SHOULD_NOT_EXIST').exists())
            custom=source/'mine.txt';custom.write_text('my customization')
            again,*_=self.attempt(root)
            self.assertNotEqual(again.returncode,0)
            self.assertEqual(custom.read_text(),'my customization')

    def test_invalid_archive_cannot_escape_or_install(self):
        for options in ({'hostile':True},{'bad_checksum':True}):
            with self.subTest(options=options),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);result,source,state,binaries=self.attempt(root,**options)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(source.exists())
                self.assertFalse((root/'escaped').exists())
                self.assertFalse((binaries/'ux46').exists())

    def test_same_command_with_captured_output_hands_control_to_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);result,*_=self.attempt(root,args=[])
            self.assertEqual(result.returncode,0,result.stderr)
            calls=[json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][0],'setup')
            self.assertIn('AI next step: read the guide',result.stdout)
            self.assertIn('connection.json',result.stdout)
            self.assertIn('do not reinstall',result.stdout)

    def test_terminal_starts_workspace_and_no_start_overrides(self):
        for extra,expected in [([],True),(['--no-start'],False)]:
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);result,*_=self.attempt(root,args=['--agent','none',*extra],terminal=True)
                self.assertEqual(result.returncode,0,result.stdout)
                calls=[json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
                self.assertEqual(any(c[0]=='run' for c in calls),expected)

if __name__=='__main__':unittest.main()
