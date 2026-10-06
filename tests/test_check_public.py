"""Publication guard: credential formats and private file names are refused.

Every sample token is synthetic and assembled at runtime, so this file itself
never contains a literal the guard would flag.
"""
from pathlib import Path
import importlib.util
import contextlib
import io
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('check_public',ROOT/'scripts/check_public.py')
check_public=importlib.util.module_from_spec(spec);spec.loader.exec_module(check_public)


def join(*parts):return ''.join(parts)


class CredentialPatternTests(unittest.TestCase):
    def test_common_secret_formats_are_flagged(self):
        filler='Ab1'*12
        samples={
            'stripe secret':join('sk','_live_',filler),
            'stripe restricted':join('rk','_live_',filler),
            'slack bot':join('xo','xb-','1234567890-',filler),
            'slack user':join('xo','xp-','1234567890-',filler),
            'google api':join('AI','za',('Sy'+'x'*40)[:35]),
            'tailscale':join('ts','key-auth-','kAbCdEf1CNTRL-',filler),
            'jwt':join('ey','JhbGciOiJIUzI1NiJ9','.','ey','JzdWIiOiIxMjM0NTY3ODkwIn0','.','c2lnbmF0dXJl'),
            'rsa key':join('-----BEGIN ','RSA PRIVATE KEY-----'),
            'plain key':join('-----BEGIN ','PRIVATE KEY-----'),
            'encrypted key':join('-----BEGIN ','ENCRYPTED PRIVATE KEY-----'),
            'dsa key':join('-----BEGIN ','DSA PRIVATE KEY-----'),
            'openssh key':join('-----BEGIN ','OPENSSH PRIVATE KEY-----'),
            'pgp key':join('-----BEGIN ','PGP PRIVATE KEY BLOCK-----'),
            'github':join('gh','p_',filler),
            'url password':join('https:','//user',':hunter2','@example.invalid/'),
        }
        for label,token in samples.items():
            with self.subTest(label):
                self.assertTrue(check_public.credential_text('value = "'+token+'"\n'),token)

    def test_ordinary_text_is_not_flagged(self):
        for text in ('sk_test_ example','xoxo- hugs','tskey-','eyJ alone','-----BEGIN PUBLIC KEY-----',
                     'BEGIN CERTIFICATE','https://example.com/path@v1','AIza short'):
            with self.subTest(text):
                self.assertFalse(check_public.credential_text(text))


class PrivatePathTests(unittest.TestCase):
    def test_sensitive_file_names_are_refused(self):
        for name in ('.env','.env.local','config/.env.production','.netrc','home/.npmrc','id_rsa','keys/id_rsa.pub',
                     'id_ed25519','id_ed25519_work','cert.p12','client.pfx','state/x.json','connection.json',
                     'deploy/installation.json','credentials.json','data.sqlite3','server.key'):
            with self.subTest(name):self.assertTrue(check_public.denied_path(name))

    def test_ordinary_source_names_are_allowed(self):
        for name in ('.env.example','docs/install.md','tools/ux46','tests/test_installer.py','app/console/app.js',
                     'package.json','environment.md'):
            with self.subTest(name):self.assertFalse(check_public.denied_path(name))


class RepositoryTests(unittest.TestCase):
    def test_repository_passes_its_own_guard(self):
        import contextlib,io
        with contextlib.redirect_stdout(io.StringIO()) as out:failed=check_public.check()
        self.assertFalse(failed,out.getvalue())


class PrivateMarkerTests(unittest.TestCase):
    def test_list_is_optional_but_explicit_missing_or_public_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'markers.txt'
            with self.assertRaises(ValueError):check_public.load_markers(str(path))
            path.write_text('# private literals\n\nSynthetic Contact\n')
            path.chmod(0o644)
            with self.assertRaises(ValueError):check_public.load_markers(str(path))
            path.chmod(0o600)
            self.assertEqual(check_public.load_markers(str(path)),['synthetic contact'])
            link=Path(tmp)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):check_public.load_markers(str(link))

    def test_staged_tree_and_commit_messages_not_clean_worktree_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def git(*args):return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL)
            git('init');git('config','user.name','Example');git('config','user.email','example@example.com')
            source=root/'example.txt';source.write_text('Synthetic Contact')
            git('add','example.txt');source.write_text('Clean working copy')
            with patch.object(check_public,'ROOT',root),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertTrue(check_public.check(markers=['synthetic contact']))
            self.assertNotIn('Synthetic Contact',out.getvalue())
            git('add','example.txt')
            message=root/'message';message.write_text('Mention Synthetic Contact')
            with patch.object(check_public,'ROOT',root),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertTrue(check_public.check(markers=['synthetic contact'],message_file=message))
            self.assertNotIn('Synthetic Contact',out.getvalue())
            git('commit','-m','Mention Synthetic Contact')
            with patch.object(check_public,'ROOT',root),contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(check_public.check(markers=['synthetic contact'],commits='HEAD'))
                self.assertFalse(check_public.check(markers=[]))

    def test_paths_and_image_metadata_are_checked_without_echoing_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'Synthetic Contact.txt').write_text('example')
            image=root/'app/console/brand/icon.png';image.parent.mkdir(parents=True)
            image.write_bytes(b'\x89PNG\x00Synthetic Contact')
            with patch.object(check_public,'ROOT',root),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertTrue(check_public.check(markers=['synthetic contact']))
            self.assertNotIn('Synthetic Contact',out.getvalue())
            self.assertIn('[redacted path]',out.getvalue())

    def test_private_list_cannot_be_published(self):
        self.assertTrue(check_public.denied_path('private-markers.txt'))


if __name__=='__main__':unittest.main()
