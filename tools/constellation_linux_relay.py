"""Decrypt a system-managed credential to memory, then run the relay unprivileged."""
import argparse,os,pwd,signal,subprocess,sys
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--user',required=True);p.add_argument('--credential',required=True)
 p.add_argument('--config',required=True);p.add_argument('--socket',required=True)
 args=p.parse_args()
 if os.geteuid()!=0:raise ValueError('Service launcher requires root')
 user=pwd.getpwnam(args.user)
 if user.pw_uid==0:raise ValueError('Relay must run as an unprivileged user')
 credential=Path(args.credential)
 if credential.is_symlink() or credential.stat().st_uid!=0 or credential.stat().st_mode&0o077:
  raise ValueError('Credential ciphertext must be root-owned and private')
 result=subprocess.run(['/usr/bin/systemd-creds','decrypt','--name=constellation-client',str(credential),'-'],capture_output=True,timeout=15)
 if result.returncode or not 32<=len(result.stdout.strip())<=4096:raise ValueError('Credential unavailable')
 relay=Path(__file__).with_name('constellation_relay.py')
 process=subprocess.Popen(['/usr/bin/python3',str(relay),'--config',args.config,'--socket',args.socket],
  stdin=subprocess.PIPE,user=user.pw_uid,group=user.pw_gid,extra_groups=[],
  env={'HOME':user.pw_dir,'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
 def stop(*_):process.terminate()
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 process.stdin.write(result.stdout);process.stdin.close();del result
 return process.wait()
if __name__=='__main__':
 try:sys.exit(main())
 except Exception:print('Constellation connector could not start; credential value suppressed',file=sys.stderr);sys.exit(1)
