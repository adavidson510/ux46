#!/usr/bin/env python3
"""Launch the Session Vault implementation from a Project Atlas checkout."""

from __future__ import annotations

import os
import sys
from pathlib import Path


skill_root = Path(__file__).resolve().parents[1]
checkout_root = skill_root.parents[1]
atlas_root = Path(os.environ.get("ATLAS_HOME", str(checkout_root))).expanduser()
implementation = atlas_root / "tools" / "session_vault.py"

if not implementation.is_file():
    print(
        f"Project Atlas Session Vault implementation not found: {implementation}",
        file=sys.stderr,
    )
    raise SystemExit(1)

os.execv(sys.executable, [sys.executable, str(implementation), *sys.argv[1:]])
