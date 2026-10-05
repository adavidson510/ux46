"""Fail on runtime data, credentials or unexpected binary files in a release tree.

This is a deterministic publication guard, not a complete secret detector.
Review the staged diff too. In Git check the staged tree; otherwise check the
source allowlist so downloaded ZIP copies can run the same check.
"""
from pathlib import Path
import re
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
DENIED_NAMES={'.env','auth.json','credentials.json','google_token.json','registry.json','agents.json','config.json',
              '.netrc','.npmrc','.pypirc','connection.json','installation.json'}
# Example environment files are documentation; any other .env.* is treated as real.
ENV_TEMPLATES={'.env.example','.env.sample','.env.template'}
DENIED_PREFIXES=('id_rsa','id_ed25519','id_ecdsa','id_dsa')
DENIED_SUFFIXES={'.db','.sqlite','.sqlite3','.pem','.key','.log','.p12','.pfx'}
DOC_ART={'docs/assets/ux46-workspace-2.png'}
DENIED_DIRS={'sessions','artifacts','state','credentials','.ux46','node_modules','__pycache__'}
PATTERNS=[re.compile(x) for x in (
    r'-----BEGIN (?:(?:ENCRYPTED|DSA|PGP|OPENSSH|EC|RSA) )?PRIVATE KEY(?: BLOCK)?-----',
    r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{40,}',
    r'AKIA[A-Z0-9]{16}', r'sk-[A-Za-z0-9_-]{30,}',
    r'\b[sr]k_live_[0-9A-Za-z]{16,}',                 # Stripe live secret / restricted keys
    r'\bxox[abposr]-[0-9A-Za-z-]{10,}',               # Slack tokens
    r'\bAIza[0-9A-Za-z_-]{35}',                       # Google API keys
    r'\btskey-[A-Za-z0-9]+-[A-Za-z0-9_-]{10,}',       # Tailscale auth/API/client keys
    r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.', # JSON Web Tokens
    r'https?://[^/\s:@]+:[^/\s@]+@',
)]


def denied_path(name):
    p=Path(name)
    return bool(set(p.parts)&DENIED_DIRS or p.name in DENIED_NAMES or p.suffix in DENIED_SUFFIXES
        or name.endswith('.origins.json') or p.name.startswith(DENIED_PREFIXES)
        or (p.name.startswith('.env.') and p.name not in ENV_TEMPLATES))


def credential_text(text):
    return any(rx.search(text) for rx in PATTERNS)

def check():
    git=(ROOT/'.git').exists()
    if git:
        paths=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=ROOT).decode().split('\0')
    else:
        paths=[str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&{'__pycache__','.git'}]
    bad=[];count=0
    for name in filter(None,paths):
        p=Path(name);count+=1
        if denied_path(name):
            bad.append((name,'private/runtime path'));continue
        source=ROOT/p
        if source.is_symlink():bad.append((name,'symlink'));continue
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT) if git else source.read_bytes()
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:
            if not ((p.parts[:3]==('app','console','brand') and p.suffix=='.png') or name in DOC_ART):
                bad.append((name,'unexpected binary'))
            continue
        if credential_text(text):bad.append((name,'credential pattern'))
    for name,reason in bad:print(name+': '+reason)
    print(f'Checked {count} source files; {len(bad)} publication issues')
    return bool(bad) or count==0

if __name__=='__main__':raise SystemExit(check())
