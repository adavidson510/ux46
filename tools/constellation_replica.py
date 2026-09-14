"""Copy immutable central snapshots over an existing admin route; no model calls."""
import argparse,hashlib,json,os,shlex,subprocess,sys
from pathlib import Path

MAX_BYTES=256*1024*1024

def replicate(host,source,destination):
 if not host or host.startswith('-') or any(c.isspace() for c in host):raise ValueError('Invalid host')
 path=Path(source)
 if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute snapshot directory required')
 root=Path(destination).expanduser()
 if root.is_symlink():raise ValueError('Replica directory cannot be a symlink')
 root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
 code="from pathlib import Path; import json; p=Path("+repr(source)+"); files=list(p.iterdir()); assert all(x.is_file() and not x.is_symlink() for x in files); print(json.dumps({'bytes':sum(x.stat().st_size for x in files),'files':len(files)}))"
 result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',host,'sudo -n python3 -'],input=code,text=True,capture_output=True,timeout=20)
 if result.returncode:raise ValueError('Snapshot metadata unavailable; no copy attempted')
 metadata=json.loads(result.stdout)
 if metadata['bytes']>MAX_BYTES or metadata['files']>90:raise ValueError('Replica pilot bound exceeded; review retention')
 before={p.name for p in root.iterdir()}
 result=subprocess.run(['rsync','-rt','--ignore-existing','--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=',
  '--rsync-path=sudo -n rsync','-e','ssh -o BatchMode=yes -o ConnectTimeout=8',
  host+':'+shlex.quote(source.rstrip('/')+'/'),str(root)+'/'],capture_output=True,timeout=60)
 if result.returncode:raise ValueError('Snapshot copy failed; existing copies preserved')
 verified=0
 for receipt in root.glob('*.receipt.json'):
  data=json.loads(receipt.read_text());generation=data['generation']
  if Path(generation).name!=generation:raise ValueError('Invalid generation')
  for suffix,key in (('.sqlite3','backup'),('.jsonl','export')):
   file=root/(generation+suffix)
   if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=data[key]['sha256']:
    raise ValueError('Replica checksum mismatch; preserved for diagnosis')
  verified+=1
 return {'verified_generations':verified,'new_files':len({p.name for p in root.iterdir()}-before),'source_bytes':metadata['bytes'],'model_calls':0}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',required=True);p.add_argument('--source',required=True);p.add_argument('--destination',required=True);args=p.parse_args()
 try:print(json.dumps(replicate(args.host,args.source,args.destination)))
 except Exception as exc:
  print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else 'Replica unavailable'}));return 1
 return 0
if __name__=='__main__':sys.exit(main())
