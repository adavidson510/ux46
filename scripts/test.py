"""Run test modules in isolated processes, keeping runtime fixtures bounded."""
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
failed=[]
requested={Path(name).name for name in sys.argv[1:]}
modules=sorted((root/'tests').glob('test_*.py'))
unknown=requested-{p.name for p in modules}
if unknown:raise SystemExit('Unknown test modules: '+', '.join(sorted(unknown)))
for p in modules:
    if requested and p.name not in requested:continue
    command=([sys.executable,'-m','pytest','-q',str(p)] if 'import pytest' in p.read_text() else [sys.executable,'-m','unittest','discover','-s','tests','-p',p.name])
    result=subprocess.run(command,cwd=root,capture_output=True,text=True)
    print(p.name, 'PASS' if result.returncode==0 else 'FAIL',flush=True)
    if result.returncode:
        failed.append(p.name)
        print(result.stdout[-3000:]+result.stderr[-5000:],flush=True)
print(f'{len(failed)} failed modules')
raise SystemExit(bool(failed))
