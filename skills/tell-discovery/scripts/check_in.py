#!/usr/bin/env python3
"""Use the deployed boards CLI, without embedding a stale release path."""
import json
import os
from pathlib import Path
import plistlib
import sys

args = sys.argv[1:]
if not args or args[0] not in {"browse", "contribute", "finish"}:
    raise SystemExit("Usage: check_in.py browse|contribute|finish [arguments]")
root = os.environ.get("TELL_BOARDS_ROOT")
if not root:
    try:
        plist = Path.home() / "Library/LaunchAgents/ai.workspace.tell-boards-v1.plist"
        root = plistlib.loads(plist.read_bytes()).get("WorkingDirectory")
    except (OSError, ValueError):
        pass
if not root or not (Path(root) / "boards/cli.py").is_file():
    print(json.dumps({"status": "skip", "reason": "boards_not_configured"}))
    raise SystemExit(0)
os.chdir(root)
state = os.environ.get("TELL_BOARDS_STATE", str(Path.home() / ".local/state/tell-boards-v1"))
os.execv(sys.executable, [sys.executable, "-m", "boards.cli", "--state-dir", state, *args])
