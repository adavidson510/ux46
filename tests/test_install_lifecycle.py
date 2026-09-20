"""Offline checked install through a real independent launcher and no-provider UI."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]


class InstalledJourney(unittest.TestCase):
    def test_install_edit_broken_entry_undo_and_reopen(self):
        with tempfile.TemporaryDirectory(prefix='ux46-install-journey-') as temporary:
            base=Path(temporary).resolve();archive=base/'source.tar.gz';fake=base/'transport';fake.mkdir()
            with tarfile.open(archive,'w:gz') as tar:
                for directory in ('tools','app','docs','skills'):
                    for path in (ROOT/directory).rglob('*'):
                        if path.is_file() and '__pycache__' not in path.parts:
                            tar.add(path,arcname=str(path.relative_to(ROOT)),recursive=False)
                for name in ('README.md','AGENTS.md','SECURITY.md'):tar.add(ROOT/name,arcname=name)
            checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
            script=re.sub(r"archive_sha='[^']+'","archive_sha='"+checksum+"'",(ROOT/'install.sh').read_text())
            curl=fake/'curl';curl.write_text('#!/bin/sh\nwhile [ "$1" != -o ]; do shift; done\ncp "$FIXTURE_SOURCE" "$2"\n');curl.chmod(0o700)
            source=base/'editable';root=base/'private';binaries=base/'bin'
            env=dict(os.environ,HOME=str(base),PATH=str(fake)+os.pathsep+os.environ['PATH'],FIXTURE_SOURCE=str(archive),UX46_INSTALL_DIR=str(source),UX46_HOME=str(root),UX46_BIN_DIR=str(binaries))
            result=subprocess.run(['sh','-s','--','--agent','none','--no-start','--no-python-download'],input=script,env=env,capture_output=True,text=True,timeout=40)
            self.assertEqual(result.returncode,0,result.stderr)
            manifest=json.loads((root/'installation.json').read_text())
            self.assertEqual(manifest['archive_sha256'],checksum)
            self.assertTrue(Path(manifest['control']).is_relative_to(base))
            config=json.loads((root/'config.json').read_text())
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));config['port']=sock.getsockname()[1]
            (root/'config.json').write_text(json.dumps(config))
            def command(*args):
                done=subprocess.run([str(binaries/'ux46'),*args,'--json'],env=env,capture_output=True,text=True,timeout=40)
                self.assertEqual(done.returncode,0,done.stderr+done.stdout)
                return json.loads(done.stdout)
            def get(path):
                with urlopen(f'http://127.0.0.1:{config["port"]}'+path,timeout=3) as response:return response.read()
            try:
                self.assertEqual(command('start')['state'],'ready')
                self.assertFalse(json.loads(get('/api/bootstrap'))['runtime_started'])
                point=command('customize')['recovery_point']
                original=get('/styles.css')
                with (source/'app/console/styles.css').open('a') as out:out.write('\n:root { --lav: #ff00aa !important; }\n')
                self.assertIn(b'--lav: #ff00aa',get('/styles.css'))
                self.assertTrue(command('source-status')['update_conflicts'])
                self.assertEqual(command('stop')['state'],'stopped')
                # The repair commands execute the copied controller, not this broken file.
                (source/'tools/ux46').write_text('broken entry !!!')
                (root/'saved-draft.txt').write_text('keep my words')
                self.assertEqual(command('undo','--point',point)['state'],'restored')
                self.assertEqual(command('start')['state'],'ready')
                self.assertEqual(get('/styles.css'),original)
                self.assertEqual((root/'saved-draft.txt').read_text(),'keep my words')
                self.assertFalse((source/'sessions').exists())
                self.assertTrue(command('source-status')['clean'])
                again=subprocess.run(['sh','-s','--','--agent','none','--no-start','--no-python-download'],input=script,env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(again.returncode,0,again.stderr)
                self.assertEqual((root/'saved-draft.txt').read_text(),'keep my words')
            finally:command('stop')


if __name__=='__main__':unittest.main()
