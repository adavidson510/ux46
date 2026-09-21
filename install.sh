#!/bin/sh
# UX46 installer. No sudo, GitHub account, repository remote or provider login.
# AI assistants: use this same command, then follow the AI installation guide:
# https://github.com/adavidson510/ux46/blob/main/docs/ai-install.md
# Captured output installs without prompting or starting a foreground server.
# Release values are set by the maintainer after packaging the tested source.
set -eu
main() {
  release='v0.2.0-alpha.11'
  archive_sha='bf589377788eff536b0aa1e3abc50bb1199089bb4af57a67896f7a96e69e64e1'
  agent=''; start=auto; bootstrap_python=1; interactive=0
  if [ -t 1 ]; then interactive=1; fi
  printf '%s\n' 'UX46: install here, or give this same curl command to your AI.' \
    'AI installation guide: https://github.com/adavidson510/ux46/blob/main/docs/ai-install.md'
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --agent) agent=${2:?Choose codex, claude, none or auto}; shift 2;;
      --no-start) start=0; shift;;
      --start) start=1; shift;;
      --no-python-download) bootstrap_python=0; shift;;
      --help) printf '%s\n' 'Usage: sh install.sh [--agent codex|claude|none|auto] [--start|--no-start] [--no-python-download]' 'Terminal output opens the workspace; captured output returns an AI setup handoff.' 'UX46_INSTALL_DIR, UX46_HOME and UX46_BIN_DIR customize user-owned locations.'; return;;
      *) printf 'Unknown option: %s\n' "$1" >&2; exit 2;;
    esac
  done
  case "$(uname -s)" in Darwin|Linux) ;; *) echo 'Use macOS or Linux (including WSL). Native Windows is not packaged yet.' >&2; exit 2;; esac
  case "$agent" in ''|codex|claude|none|auto) ;; *) echo 'Choose codex, claude, none or auto.' >&2; exit 2;; esac
  install_dir=${UX46_INSTALL_DIR:-"$HOME/ux46"}
  state_dir=${UX46_HOME:-"$HOME/.ux46"}
  bin_dir=${UX46_BIN_DIR:-"$HOME/.local/bin"}
  for directory in "$install_dir" "$state_dir" "$bin_dir"; do
    case "$directory" in /*) ;; *) echo 'Install locations must be absolute paths.' >&2; exit 2;; esac
  done
  umask 077
  staging=$(mktemp -d "${TMPDIR:-/tmp}/ux46-install.XXXXXXXX")
  transaction="${state_dir}.install"
  locked=0
  cleanup() {
    if [ "$locked" -eq 1 ]; then rm -f "$transaction/active/pid"; rmdir "$transaction/active" 2>/dev/null || :; fi
    rm -rf "$staging"
  }
  trap cleanup EXIT HUP INT TERM
  printf '%s\n' "$release" "$archive_sha" "$install_dir" "$state_dir" "$bin_dir" > "$staging/intent"
  if [ -e "$transaction" ] || [ -L "$transaction" ]; then
    if [ -L "$transaction" ] || [ -L "$transaction/intent" ] || ! cmp -s "$staging/intent" "$transaction/intent"; then
      echo "An unrelated or different installation transaction exists at $transaction. Nothing was replaced." >&2; exit 2
    fi
  else
    for path in "$install_dir" "$state_dir" "$bin_dir/ux46"; do
      if [ -e "$path" ] || [ -L "$path" ]; then echo "Existing path preserved: $path. Choose new install locations or use its existing launcher." >&2; exit 2; fi
    done
    mkdir -p "$(dirname "$transaction")"
    mkdir "$transaction"
    cp "$staging/intent" "$transaction/intent"
  fi
  if ! mkdir "$transaction/active" 2>/dev/null; then
    # A crash may leave this tiny lock directory. Only the exact known dead
    # installer lock is removed; never the transaction or its created paths.
    old_pid=$(cat "$transaction/active/pid" 2>/dev/null || :)
    case "$old_pid" in ''|*[!0-9]*) echo 'Another installer lock needs inspection.' >&2; exit 2;; esac
    if kill -0 "$old_pid" 2>/dev/null; then echo 'This installation is already running.' >&2; exit 2; fi
    if [ -L "$transaction/active" ] || [ -L "$transaction/active/pid" ]; then echo 'Invalid installer lock.' >&2; exit 2; fi
    rm "$transaction/active/pid"
    rmdir "$transaction/active"
    mkdir "$transaction/active"
  fi
  printf '%s\n' "$$" > "$transaction/active/pid"
  locked=1
  python=''
  if [ -f "$transaction/python" ] && [ ! -L "$transaction/python" ]; then
    saved_python=$(cat "$transaction/python")
    if "$saved_python" -c 'import sys; sys.exit(sys.version_info < (3,10))' >/dev/null 2>&1; then python=$saved_python; fi
  fi
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if [ -z "$python" ] && command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,10))' >/dev/null 2>&1; then
      python=$(command -v "$candidate"); break
    fi
  done
  if [ -z "$python" ]; then
    if [ "$bootstrap_python" -eq 0 ]; then echo 'Python 3.10+ is required. Install it or allow the private Python download.' >&2; exit 2; fi
    printf '%s\n' runtime-preparing > "$transaction/phase"
    echo 'Installing a private Python 3.12 with Astral uv. System Python and shell profiles stay unchanged.'
    curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/0.10.12/install.sh -o "$staging/uv-install.sh"
    UV_UNMANAGED_INSTALL="$transaction/runtime/uv" sh "$staging/uv-install.sh"
    UV_PYTHON_INSTALL_DIR="$transaction/runtime/python" UV_PYTHON_BIN_DIR="$transaction/runtime/bin" "$transaction/runtime/uv/uv" python install 3.12
    python=$(UV_PYTHON_INSTALL_DIR="$transaction/runtime/python" "$transaction/runtime/uv/uv" python find --managed-python 3.12)
  fi
  printf '%s\n' "$python" > "$transaction/python"
  if [ ! -f "$transaction/source.tar.gz" ]; then
    printf '%s\n' downloading > "$transaction/phase"
    echo "Downloading UX46 ${release}..."
    curl --proto '=https' --tlsv1.2 -fsSL "https://github.com/adavidson510/ux46/releases/download/$release/ux46-source.tar.gz" -o "$transaction/download.pending"
    mv "$transaction/download.pending" "$transaction/source.tar.gz"
  fi
  if [ -z "$agent" ]; then
    codex_found=0; claude_found=0
    command -v codex >/dev/null 2>&1 && codex_found=1
    command -v claude >/dev/null 2>&1 && claude_found=1
    if [ "$codex_found$claude_found" = '10' ]; then agent=codex
    elif [ "$codex_found$claude_found" = '01' ]; then agent=claude
    elif [ "$interactive" -eq 1 ] && ( : </dev/tty ) 2>/dev/null; then
      printf '\nChoose your agent: 1) Codex  2) Claude  3) Skip / connect my own later\nChoice [3]: ' >/dev/tty
      read -r choice </dev/tty
      case "$choice" in 1) agent=codex;; 2) agent=claude;; *) agent=none;; esac
    else agent=none; fi
  fi
  "$python" - "$transaction" "$archive_sha" "$release" "$install_dir" "$bin_dir" "$state_dir" "$agent" <<'PYINSTALL'
import hashlib,json,os,shlex,sys,tarfile,tempfile,shutil,subprocess,uuid
from pathlib import Path,PurePosixPath
transaction,expected,release,destination,binaries,state,agent=sys.argv[1:]
locations=[Path(p) for p in (transaction,destination,binaries,state)]
if any(p.is_symlink() for p in locations):raise SystemExit('Install locations cannot be symbolic links.')
transaction=Path(transaction).resolve();target=Path(destination).resolve();bindir=Path(binaries).resolve();state=Path(state).resolve()
if target==state or target in state.parents or state in target.parents:raise SystemExit('Source and private data must use separate directories.')
if transaction==target or target in transaction.parents or transaction in target.parents:raise SystemExit('Source and installer control must use separate directories.')
for path in (Path(destination),Path(state),Path(binaries),transaction):
    if path.is_symlink():raise SystemExit('Install locations cannot be symbolic links.')
if transaction.stat().st_uid!=os.getuid():raise SystemExit('Installer transaction belongs to another user.')

def save(path,value):
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd,'w') as out:json.dump(value,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)

journal=transaction/'install.json'
job=json.loads(journal.read_text()) if journal.exists() else {'schema_version':1,'id':uuid.uuid4().hex,'phase':'downloaded','source':str(target),'state':str(state),'binaries':str(bindir),'release':release,'archive_sha256':expected,'owned':{'transaction':str(transaction)}}
if any(job.get(k)!=v for k,v in {'source':str(target),'state':str(state),'binaries':str(bindir),'release':release,'archive_sha256':expected}.items()):raise SystemExit('Installation transaction identity changed.')
save(journal,job)
archive=transaction/'source.tar.gz'
if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
    # Only this transaction's failed download is discarded; installed paths stay.
    archive.unlink();raise SystemExit('Archive checksum failed. Rerun the same installer to download it again.')

if not job.get('files'):
    with tarfile.open(archive) as tar:
        members=tar.getmembers();names=set()
        if len(members)>2000 or sum(m.size for m in members)>30*1024*1024:raise SystemExit('Unexpected archive size')
        for m in members:
            parts=PurePosixPath(m.name)
            if parts.is_absolute() or '..' in parts.parts or not (m.isfile() or m.isdir()) or m.name in names:raise SystemExit('Unsafe archive entry')
            names.add(m.name)
        if not any(m.name=='tools/ux46' and m.isfile() for m in members):raise SystemExit('Incomplete UX46 release')
        unpack=transaction/'unpacked'
        if not unpack.exists():
            with tempfile.TemporaryDirectory(prefix='.unpack-',dir=transaction) as temporary:
                if hasattr(tarfile,'data_filter'):tar.extractall(temporary,members=members,filter='data')
                else:tar.extractall(temporary,members=members)
                os.rename(temporary,unpack)
        job['files']={str(p.relative_to(unpack)):hashlib.sha256(p.read_bytes()).hexdigest() for p in unpack.rglob('*') if p.is_file()}
        (unpack/'.ux46-install-id').write_text(job['id'])
        job['phase']='verified';save(journal,job)

# Exclusive ownership markers allow resuming the tiny gap after an atomic
# move and before the next receipt. A matching directory name proves nothing.
marker=target/'.ux46-install-id'
if target.exists():
    if not marker.is_file() or marker.is_symlink() or marker.read_text()!=job['id']:raise SystemExit('Existing source is not owned by this transaction.')
else:
    target.parent.mkdir(parents=True,exist_ok=True)
    # mkdir reserves the name without replacing even an empty existing folder.
    target.mkdir(mode=0o700)
    marker.write_text(job['id'])
    job['owned']['source']=str(target);save(journal,job)
if job['phase'] in {'verified','source-copying'}:
    job['phase']='source-copying';save(journal,job)
    for name,digest in job['files'].items():
        path=target/name
        if any(p.is_symlink() for p in [path,*path.parents] if p!=target.parent):raise SystemExit('A source path became a symbolic link; it was preserved.')
        if path.exists():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise SystemExit('An installed source file changed before setup finished. It was preserved.')
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            fd,temporary=tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as out:out.write((transaction/'unpacked'/name).read_bytes());out.flush();os.fsync(out.fileno())
                os.chmod(temporary,(transaction/'unpacked'/name).stat().st_mode & 0o777)
                os.link(temporary,path)  # exclusive, complete file; no partial target on interruption
            finally:os.unlink(temporary)
    job['phase']='source-ready';save(journal,job)
if not state.exists():
    state.mkdir(parents=True,mode=0o700)
    (state/'.ux46-install-id').write_text(job['id'])
if state.is_symlink() or not (state/'.ux46-install-id').is_file() or (state/'.ux46-install-id').read_text()!=job['id']:raise SystemExit('Existing private data is not owned by this transaction.')
job['owned']['state']=str(state);job['owned']['runtime']=str(transaction/'runtime') if (transaction/'runtime').exists() else None
control=transaction/'control';control.mkdir(exist_ok=True,mode=0o700)
for name in ('ux46_manage.py','ux46_recovery.py'):
    baseline=transaction/'unpacked/tools'/name
    if baseline.exists():
        digest=job['files']['tools/'+name]
        if hashlib.sha256(baseline.read_bytes()).hexdigest()!=digest:raise SystemExit('Repair controller baseline failed verification.')
        if not (control/name).exists():shutil.copy2(baseline,control/name)
        if (control/name).is_symlink() or hashlib.sha256((control/name).read_bytes()).hexdigest()!=digest:raise SystemExit('Independent repair controller changed; it was not overwritten.')
manifest={'schema_version':1,'id':job['id'],'release':release,'archive_sha256':expected,'source':str(target),'state':str(state),'control':str(control),'python':sys.executable,'launcher':str(bindir/'ux46'),'files':job['files']}
if not (state/'installation.json').exists():save(state/'installation.json',manifest)
else:
    present=json.loads((state/'installation.json').read_text())
    if present.get('id')!=job['id'] or present.get('source')!=str(target) or present.get('state')!=str(state):raise SystemExit('Installed-base manifest belongs to another transaction.')
    manifest=present
entry=control/'ux46_manage.py' if (control/'ux46_manage.py').exists() else target/'tools/ux46'
launcher_text='#!/bin/sh\nexport UX46_HOME='+shlex.quote(str(state))+'\nexec '+shlex.quote(manifest['python'])+' '+shlex.quote(str(entry))+' "$@"\n'
bindir.mkdir(parents=True,exist_ok=True);launcher=bindir/'ux46'
if launcher.exists() or launcher.is_symlink():
    if launcher.is_symlink() or launcher.read_text()!=launcher_text:raise SystemExit('Existing launcher was preserved; it is not this transaction’s launcher.')
else:
    with launcher.open('x') as out:out.write(launcher_text)
    launcher.chmod(0o700)
job['owned']['launcher']=str(launcher);save(journal,job)
if job['phase']!='complete':
    job['phase']='setup';save(journal,job)
    result=subprocess.run([str(launcher),'setup','--agent',agent,'--json'],capture_output=True,text=True)
    if result.returncode:raise SystemExit('Setup did not finish. Installed source and data were kept; rerun this command or run ux46 setup. '+result.stderr[:300])
    setup=json.loads(result.stdout);save(transaction/'setup.json',setup)
    job['phase']='complete';save(journal,job)
print('\nUX46 is installed. Existing source and private data are preserved on reruns.')
connection=state/'connection.json'
setup=json.loads((connection if connection.exists() else transaction/'setup.json').read_text())
if setup.get('agent')=='none':print('No local agent selected. Connect one later with ux46 setup or ux46 connect.')
elif not setup.get('cli_available'):print('Install your selected native CLI and sign in there: '+str(setup.get('provider_setup','see docs/ai-install.md')))
elif setup.get('readiness',{}).get('command',{}).get('state') not in {None,'available'}:print('The selected native command needs attention. Run ux46 doctor --check for its current prerequisites.')
print('AI installation guide: '+str(target/'docs/ai-install.md'))
print('AI connection record: '+str(state/'connection.json'))
print('AI next step: read the guide, configure your actual runtime, verify the connection, then open the workspace for your human.')
print('Installation is complete: continue at "Configure your connection"; do not reinstall to change settings.')
PYINSTALL
  if [ -f "$transaction/control/ux46_manage.py" ]; then
    printf '\nOpen any time: %s/ux46 open\n' "$bin_dir"
  else
    printf '\nStart any time: %s/ux46 run --open\n' "$bin_dir"
  fi
  printf 'Editable source: %s\nPrivate data: %s\n' "$install_dir" "$state_dir"
  if [ "$start" = auto ]; then start=$interactive; fi
  if [ "$start" -eq 1 ]; then
    if [ -f "$transaction/control/ux46_manage.py" ]; then "$bin_dir/ux46" open; else "$bin_dir/ux46" run --open; fi
  fi
}
main "$@"
