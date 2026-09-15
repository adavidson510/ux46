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
DENIED_NAMES={'.env','auth.json','credentials.json','google_token.json','registry.json','agents.json','config.json'}
# Reviewed documentation artwork is allowed by exact path, not an entire upload folder.
DOC_ART={'docs/assets/ux46-workspace-2.png'}
DENIED_DIRS={'sessions','artifacts','state','credentials','.ux46','node_modules','__pycache__'}
PATTERNS=[re.compile(x) for x in (
    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{40,}',
    r'AKIA[A-Z0-9]{16}', r'sk-[A-Za-z0-9_-]{30,}',
    r'https?://[^/\s:@]+:[^/\s@]+@',
)]

def check():
    git=(ROOT/'.git').exists()
    if git:
        paths=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=ROOT).decode().split('\0')
    else:
        paths=[str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&{'__pycache__','.git'}]
    bad=[];count=0
    for name in filter(None,paths):
        p=Path(name);count+=1
        if set(p.parts)&DENIED_DIRS or p.name in DENIED_NAMES or p.suffix in {'.db','.sqlite','.sqlite3','.pem','.key','.log'} or name.endswith('.origins.json'):
            bad.append((name,'private/runtime path'));continue
        source=ROOT/p
        if source.is_symlink():bad.append((name,'symlink'));continue
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT) if git else source.read_bytes()
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:
            if not ((p.parts[:3]==('app','console','brand') and p.suffix=='.png') or name in DOC_ART):
                bad.append((name,'unexpected binary'))
            continue
        if any(rx.search(text) for rx in PATTERNS):bad.append((name,'credential pattern'))
    for name,reason in bad:print(name+': '+reason)
    print(f'Checked {count} source files; {len(bad)} publication issues')
    return bool(bad) or count==0

if __name__=='__main__':raise SystemExit(check())
