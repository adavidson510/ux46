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
    def attempt(self,root,*,hostile=False,bad_checksum=False,args=None,terminal=False,corrupt_download=False,fail_setup=False,no_python=False):
        fixtures=root/'fixtures';fixtures.mkdir(exist_ok=True)
        archive=fixtures/'source.tar.gz'
        if not archive.exists():
            self.make_archive(archive,hostile)

        sha='0'*64 if bad_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
        script=re.sub(r"archive_sha='[^']+'","archive_sha='"+sha+"'",(ROOT/'install.sh').read_text())
        (fixtures/'install.sh').write_text(script)
        # Only curl is stubbed; run the real shell, extractor and generated launcher.
        curl=fixtures/'curl'
        curl.write_text('#!/bin/sh\nif [ -n "$CORRUPT_DOWNLOAD" ]; then while [ "$1" != -o ]; do shift; done; printf broken > "$2"; exit; fi\nwhile [ "$#" -gt 0 ]; do\nif [ "$1" = -o ]; then cp "$FIXTURE_ARCHIVE" "$2"; exit; fi\nshift\ndone\nexit 2\n')
        curl.chmod(0o700)
        if no_python:
            for name in ('python3.13','python3.12','python3.11','python3.10','python3'):
                shim=fixtures/name;shim.write_text('#!/bin/sh\nexit 1\n');shim.chmod(0o700)
            uv_install=fixtures/'uv-install.sh'
            uv_install.write_text('''#!/bin/sh
mkdir -p "$UV_UNMANAGED_INSTALL"
printf installed >> "$INSTALL_TEST_UV_CALLS"
cat > "$UV_UNMANAGED_INSTALL/uv" <<'FAKEUV'
#!/bin/sh
if [ "$2" = find ]; then printf '%s\\n' "$FIXTURE_PYTHON"; else mkdir -p "$UV_PYTHON_INSTALL_DIR"; fi
FAKEUV
chmod 700 "$UV_UNMANAGED_INSTALL/uv"
''')
            original=curl.read_text()
            curl.write_text(original.replace('#!/bin/sh\n', '#!/bin/sh\ncase "$*" in *astral.sh*) while [ "$1" != -o ]; do shift; done; cp "$FIXTURE_UV_INSTALL" "$2"; exit;; esac\n',1))
        source=root/"my ' copy $(touch SHOULD_NOT_EXIST)"
        state=root/'private data';binaries=root/'local bin'
        env=dict(os.environ,HOME=str(root),PATH=str(fixtures)+os.pathsep+os.environ['PATH'],
                 INSTALL_TEST_CALLS=str(root/'calls.jsonl'),INSTALL_TEST_UV_CALLS=str(root/'uv-calls'),FIXTURE_PYTHON=sys.executable,FIXTURE_UV_INSTALL=str(fixtures/'uv-install.sh'),INSTALL_FAIL_SETUP='1' if fail_setup else '',CORRUPT_DOWNLOAD='1' if corrupt_download else '',FIXTURE_ARCHIVE=str(archive),UX46_INSTALL_DIR=str(source),UX46_HOME=str(state),UX46_BIN_DIR=str(binaries))
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

    def make_archive(self,archive,hostile):
        with tarfile.open(archive,'w:gz') as tar:
            code=b'import json,os,sys; from pathlib import Path; p=Path(os.environ["INSTALL_TEST_CALLS"]); p.open("a").write(json.dumps(sys.argv[1:])+"\\n"); sys.exit(3) if os.environ.get("INSTALL_FAIL_SETUP") else None; print(json.dumps({"agent":"none","instructions":"docs/ai-install.md"}))\n'
            entry=tarfile.TarInfo('../escaped' if hostile else 'tools/ux46');entry.size=len(code)
            tar.addfile(entry,io.BytesIO(code))

    def test_checked_install_handles_literal_paths_and_preserves_completed_copy(self):
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
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(custom.read_text(),'my customization')
            self.assertEqual(json.loads((state/'installation.json').read_text())['source'],str(source.resolve()))

    def test_failed_download_resumes_without_final_private_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first,source,state,binaries=self.attempt(root,corrupt_download=True)
            self.assertNotEqual(first.returncode,0);self.assertFalse(state.exists());self.assertFalse(source.exists())
            transaction=Path(str(state)+'.install')
            identity=json.loads((transaction/'install.json').read_text())['id']
            again,*_=self.attempt(root)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(json.loads((state/'installation.json').read_text())['id'],identity)

    def test_private_python_bootstrap_then_bad_download_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=['--agent','none','--no-start']
            first,source,state,binaries=self.attempt(root,corrupt_download=True,no_python=True,args=args)
            self.assertNotEqual(first.returncode,0)
            self.assertFalse(state.exists());self.assertFalse(source.exists())
            self.assertTrue((Path(str(state)+'.install')/'runtime/uv/uv').exists())
            again,*_=self.attempt(root,no_python=True,args=args)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual((root/'uv-calls').read_text(),'installed')
            receipt=json.loads((Path(str(state)+'.install')/'install.json').read_text())
            self.assertEqual(receipt['phase'],'complete');self.assertTrue(receipt['owned']['runtime'])

    def test_failed_setup_resumes_owned_paths_and_keeps_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first,source,state,binaries=self.attempt(root,fail_setup=True)
            self.assertNotEqual(first.returncode,0)
            (state/'keep-me.txt').write_text('private data')
            again,*_=self.attempt(root)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual((state/'keep-me.txt').read_text(),'private data')
            self.assertEqual(json.loads((Path(str(state)+'.install')/'install.json').read_text())['phase'],'complete')

    def test_existing_unrelated_source_is_never_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first,source,state,binaries=self.attempt(root,corrupt_download=True)
            source.mkdir();(source/'mine.txt').write_text('unrelated')
            again,*_=self.attempt(root)
            self.assertNotEqual(again.returncode,0)
            self.assertEqual((source/'mine.txt').read_text(),'unrelated')
            self.assertFalse((source/'.ux46-install-id').exists())

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
