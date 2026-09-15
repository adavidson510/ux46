#!/bin/sh
# UX46 installer. No sudo, GitHub account, repository remote or provider login.
# AI assistants: use this same command, then follow the AI installation guide:
# https://github.com/adavidson510/ux46/blob/main/docs/ai-install.md
# Captured output installs without prompting or starting a foreground server.
# Release values are set by the maintainer after packaging the tested source.
set -eu
main() {
  release='v0.2.0-alpha.3'
  archive_sha='3b14da1b3c3946d609d24e94f75250e4fbe78b0eb5650dd5f6f37a9f85cae685'
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
  if [ -e "$state_dir" ] || [ -L "$state_dir" ]; then
    echo "Private data already exists at $state_dir. Use a new UX46_HOME for a fresh copy." >&2; exit 2
  fi
  # Refuse replacements before downloading or changing any existing install.
  if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
    echo "Your copy already exists at $install_dir. Nothing was replaced." >&2
    echo 'Use its tools/ux46 setup command, or choose a new UX46_INSTALL_DIR.' >&2; exit 2
  fi
  if [ -e "$bin_dir/ux46" ] || [ -L "$bin_dir/ux46" ]; then
    echo "A launcher already exists at $bin_dir/ux46. Choose a separate UX46_BIN_DIR." >&2; exit 2
  fi
  umask 077
  staging=$(mktemp -d "${TMPDIR:-/tmp}/ux46-install.XXXXXXXX")
  trap 'rm -rf "$staging"' EXIT HUP INT TERM
  python=''
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,10))' >/dev/null 2>&1; then
      python=$(command -v "$candidate"); break
    fi
  done
  if [ -z "$python" ]; then
    if [ "$bootstrap_python" -eq 0 ]; then echo 'Python 3.10+ is required. Install it or allow the private Python download.' >&2; exit 2; fi
    echo 'Installing a private Python 3.12 with Astral uv. System Python and shell profiles stay unchanged.'
    curl --proto '=https' --tlsv1.2 -fsSL https://astral.sh/uv/0.10.12/install.sh -o "$staging/uv-install.sh"
    UV_UNMANAGED_INSTALL="$state_dir/runtime/uv" sh "$staging/uv-install.sh"
    UV_PYTHON_INSTALL_DIR="$state_dir/runtime/python" UV_PYTHON_BIN_DIR="$state_dir/runtime/bin" "$state_dir/runtime/uv/uv" python install 3.12
    python=$(UV_PYTHON_INSTALL_DIR="$state_dir/runtime/python" "$state_dir/runtime/uv/uv" python find --managed-python 3.12)
  fi
  echo "Downloading UX46 ${release}..."
  curl --proto '=https' --tlsv1.2 -fsSL "https://github.com/adavidson510/ux46/releases/download/$release/ux46-source.tar.gz" -o "$staging/source.tar.gz"
  "$python" - "$staging/source.tar.gz" "$archive_sha" "$install_dir" "$bin_dir" "$state_dir" <<'PY'
import hashlib,os,shlex,sys,tarfile,tempfile,shutil
from pathlib import Path,PurePosixPath
archive,expected,destination,binaries,state=sys.argv[1:]
p=Path(archive)
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise SystemExit('Archive checksum failed. Nothing was installed.')
target=Path(destination).expanduser().resolve();bindir=Path(binaries).expanduser().resolve()
state=Path(state).expanduser().resolve()
if target==state or target in state.parents or state in target.parents:raise SystemExit('Source and private data must use separate directories.')
if target.exists() or target.is_symlink():raise SystemExit('Installation destination already exists.')
target.parent.mkdir(parents=True,exist_ok=True)
# Validate every member before extraction; archive paths cannot escape or link elsewhere.
with tarfile.open(p) as tar:
    members=tar.getmembers()
    if len(members)>2000 or sum(m.size for m in members)>30*1024*1024:raise SystemExit('Unexpected archive size')
    for m in members:
        parts=PurePosixPath(m.name)
        if parts.is_absolute() or '..' in parts.parts or not (m.isfile() or m.isdir()):raise SystemExit('Unsafe archive entry')
    with tempfile.TemporaryDirectory(prefix='.ux46-',dir=target.parent) as stage:
        if hasattr(tarfile,'data_filter'):tar.extractall(stage,members=members,filter='data')
        else:tar.extractall(stage,members=members)
        if not (Path(stage)/'tools/ux46').is_file():raise SystemExit('Incomplete UX46 release')
        # Refuse overwrite even if another process created the destination meanwhile.
        target.mkdir(mode=0o700)
        for child in Path(stage).iterdir():shutil.move(str(child),str(target/child.name))
bindir.mkdir(parents=True,exist_ok=True)
launcher=bindir/'ux46'
with launcher.open('x') as out:
    out.write('#!/bin/sh\nexport UX46_HOME='+shlex.quote(str(state))+'\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(target/'tools/ux46'))+' "$@"\n')
launcher.chmod(0o700)
PY
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
  "$bin_dir/ux46" setup --agent "$agent" --json > "$staging/setup.json"
  "$python" - "$staging/setup.json" "$state_dir" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
print('\nUX46 is installed. Your copy is yours to change.')
if r['agent']=='none':print('No local agent selected. You can connect one later with ux46 setup or ux46 connect.')
elif not r['cli_available']:print('Install '+r['agent']+' and sign in using its official guide: '+r['provider_setup'])
else:print('Using '+r['name']+'. If not already signed in, complete login in the provider CLI.')
print('AI setup guide: '+r['instructions'])
print('AI connection record: '+sys.argv[2]+'/connection.json')
print('AI next step: read the guide, configure your actual runtime, verify the connection, then start the workspace for your human.')
print('Installation is complete: continue at "Configure your connection"; do not reinstall.')
PY
  printf '\nStart any time: %s/ux46 run --open\n' "$bin_dir"
  printf 'Editable source: %s\nPrivate data: %s\n' "$install_dir" "$state_dir"
  if [ "$start" = auto ]; then start=$interactive; fi
  if [ "$start" -eq 1 ]; then
    "$bin_dir/ux46" run --open
  fi
}
main "$@"
