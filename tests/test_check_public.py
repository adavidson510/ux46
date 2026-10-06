"""Publication guard: credential formats and private file names are refused.

Every sample token is synthetic and assembled at runtime, so this file itself
never contains a literal the guard would flag.
"""
from pathlib import Path
import importlib.util
import unittest

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


if __name__=='__main__':unittest.main()
