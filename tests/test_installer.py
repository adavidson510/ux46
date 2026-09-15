"""Offline installer tests: checked archive, hostile paths, private local ownership."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def attempt(self,root,*,hostile=False,bad_checksum=False):
        fixtures=root/'fixtures';fixtures.mkdir(exist_ok=True)
        archive=fixtures/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            code=b'import json; print(json.dumps({"agent":"none","instructions":"docs/ai-install.md"}))\n'
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
                 FIXTURE_ARCHIVE=str(archive),UX46_INSTALL_DIR=str(source),UX46_HOME=str(state),UX46_BIN_DIR=str(binaries))
        result=subprocess.run(['sh',str(fixtures/'install.sh'),'--agent','none','--no-start','--no-python-download'],
                              cwd=root,env=env,capture_output=True,text=True,errors='replace',timeout=30)
        return result,source,state,binaries

    def test_checked_install_handles_literal_paths_and_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);result,source,state,binaries=self.attempt(root)
            self.assertEqual(result.returncode,0,result.stderr)
            launcher=binaries/'ux46'
            self.assertEqual(launcher.stat().st_mode & 0o777,0o700)
            run=subprocess.run([str(launcher),'doctor'],cwd=root,capture_output=True,text=True)
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

if __name__=='__main__':unittest.main()
