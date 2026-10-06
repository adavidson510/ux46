"""Fail on runtime data, credentials or unexpected binary files in a release tree.

This is a deterministic publication guard, not a complete secret detector.
Review the staged diff too. In Git check the staged tree; otherwise check the
source allowlist so downloaded ZIP copies can run the same check.
"""
from pathlib import Path
import argparse
import os
import re
import stat
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
DENIED_NAMES={'.env','auth.json','credentials.json','google_token.json','registry.json','agents.json','config.json',
              '.netrc','.npmrc','.pypirc','connection.json','installation.json','private-markers.txt'}
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

def load_markers(path=None):
    """Optional local literals, never echoed or copied into the repository."""
    explicit = path or os.environ.get('UX46_PRIVATE_MARKERS')
    path = Path(explicit).expanduser() if explicit else Path.home()/'.ux46/private-markers.txt'
    if not path.exists() and not path.is_symlink():
        if explicit: raise ValueError('Configured private marker file is missing')
        return []
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private marker file must be a regular owner-only file (chmod 600)')
    if info.st_size > 65536: raise ValueError('Private marker file is too large')
    return [line.strip().casefold() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def private_text(text, markers):
    folded = text.casefold()
    return any(marker in folded for marker in markers)


def check(markers=None, message_file=None, commits=None):
    markers = load_markers() if markers is None else markers
    git=(ROOT/'.git').exists()
    if git:
        paths=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=ROOT).decode().split('\0')
    else:
        paths=[str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&{'__pycache__','.git'}]
    bad=[];count=0
    for name in filter(None,paths):
        p=Path(name);count+=1
        if private_text(name, markers):
            bad.append(('[redacted path]','private marker in filename'));continue
        if denied_path(name):
            bad.append((name,'private/runtime path'));continue
        source=ROOT/p
        if source.is_symlink():bad.append((name,'symlink'));continue
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT) if git else source.read_bytes()
        # Also inspect embedded textual metadata in allowed image assets. This
        # cannot read pixels; public images still require a visual review.
        if private_text(data.decode('utf-8', errors='ignore'), markers):
            bad.append((name,'private marker'))
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:
            if not ((p.parts[:3]==('app','console','brand') and p.suffix=='.png') or name in DOC_ART):
                bad.append((name,'unexpected binary'))
            continue
        if credential_text(text):bad.append((name,'credential pattern'))
    messages=[]
    if message_file:
        messages.append(Path(message_file).read_text())
    if commits:
        if not git: raise ValueError('Commit checking requires a Git checkout')
        messages.extend(subprocess.check_output(
            ['git','log','--format=%B','-z',commits,'--'],cwd=ROOT).decode().split('\0'))
    for index, message in enumerate(messages):
        if credential_text(message) or private_text(message, markers):
            bad.append((f'commit message {index+1}','credential or private marker'))
    for name,reason in bad:print(name+': '+reason)
    print(f'Checked {count} source files; {len(bad)} publication issues')
    return bool(bad) or count==0

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markers', help='Owner-only private literal list; defaults to ~/.ux46/private-markers.txt')
    parser.add_argument('--message-file', help='Check a proposed commit message as well as staged source')
    parser.add_argument('--commits', help='Check messages in a Git revision or range as well as staged source')
    args=parser.parse_args()
    try:
        return check(load_markers(args.markers), args.message_file, args.commits)
    except (ValueError, OSError, subprocess.CalledProcessError):
        print('Publication check could not complete. Check marker-file permissions and Git inputs; no private values were printed.')
        return 1


if __name__=='__main__':raise SystemExit(main())
