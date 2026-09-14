#!/usr/bin/env python3
"""Deterministic, local-only catalog and installer for the UX46 skill basket.

The module intentionally has no network client, package resolver, or plugin
loader.  A consumer can discover short metadata cheaply, then read a named
allowlisted file only when it needs that skill's instructions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any


SKILLS: dict[str, dict[str, Any]] = {
    "session-vault": {"description": "Portable project memory and native session pointers.",
        "source": "skills/session-vault", "files": ("SKILL.md",)},
    "constellation": {
        "description": "Retrieve actionable experience, contribute discoveries and corrections, and review usefulness within or across projects.",
        "source": "skills/constellation",
        "files": ("SKILL.md", "scripts/client.py", "references/record-v1.md"),
    },
    "ux46-attention": {
        "description": "Report a human need or progress through UX46 Attention and Session Vault.",
        "source": "skills/ux46-attention",
        "files": ("SKILL.md",),
    },
    "ux46-canvas": {
        "description": "Publish a compact, version-checked Canvas beside a UX46 conversation.",
        "source": "skills/ux46-canvas",
        "files": ("SKILL.md", "references/ledger-projection-1.0.0.md",
                  "scripts/atlas_desktops.py", "scripts/atlas_files.py", "scripts/board.py",
                  "scripts/publish.py", "scripts/room.py", "scripts/upload.py"),
    },
    "ux46-efficiency": {
        "description": "Reduce repeated UX46 agent overhead with compact context and deterministic helpers.",
        "source": "skills/ux46-efficiency",
        "files": ("SKILL.md", "references/operating-guide-1.md"),
    },
    "ux46-maintainer": {
        "description": "Locate, change, and verify UX46 components and their data contracts.",
        "source": "skills/ux46-maintainer",
        "files": ("SKILL.md", "references/system-map-1.0.0.md"),
    },
    "tell-discovery": {
        "description": "Make one bounded, useful Tell-discovery check-in at a project checkpoint.",
        "source": "skills/tell-discovery",
        "files": ("SKILL.md", "scripts/check_in.py"),
    },
}

_VERSION = re.compile(r'^\s*version:\s*["\']?([^"\'\s]+)', re.MULTILINE)


def repository_root() -> Path:
    """Return the checked-out Project Atlas root without consulting $HOME."""
    return Path(__file__).resolve().parents[1]


def _skill_root(name: str, root: Path | None = None) -> Path:
    try:
        source = SKILLS[name]["source"]
    except KeyError as exc:
        raise ValueError(f"unknown UX46 skill: {name}") from exc
    return ((root or repository_root()) / source).resolve()


def _relative_files(name: str, root: Path | None = None) -> list[Path]:
    skill_root = _skill_root(name, root)
    files = [Path(path) for path in SKILLS[name]["files"]]
    if Path("SKILL.md") not in files:
        raise ValueError(f"{name} manifest must include SKILL.md")
    for relative in files:
        source = skill_root / relative
        if relative.is_absolute() or ".." in relative.parts or not source.is_file() or source.is_symlink():
            raise ValueError(f"{name} manifest has an unreadable file: {relative}")
    return sorted(files, key=lambda path: path.as_posix())


def _bundle_hash(skill_root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in files:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((skill_root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _version(skill_root: Path) -> str:
    match = _VERSION.search((skill_root / "SKILL.md").read_text(encoding="utf-8"))
    return match.group(1) if match else "unversioned"


def catalog(root: Path | None = None) -> list[dict[str, Any]]:
    """Return short deterministic metadata; no instruction body is returned."""
    entries: list[dict[str, Any]] = []
    for name in sorted(SKILLS):
        skill_root = _skill_root(name, root)
        files = _relative_files(name, root)
        entries.append({
            "name": name,
            "description": SKILLS[name]["description"],
            "version": _version(skill_root),
            "sha256": _bundle_hash(skill_root, files),
            "files": [path.as_posix() for path in files],
        })
    return entries


def discovery(root: Path | None = None) -> dict[str, str]:
    """Return the local catalog pointer without loading any skill contents."""
    repository = (root or repository_root()).resolve()
    return {
        "repository": str(repository),
        "catalog_cli": str(repository / "tools" / "ux46_skills.py"),
        "portable_source": "Use this repository's fixed skill manifest; do not download a skill bundle.",
    }


def read(name: str, path: str = "SKILL.md", root: Path | None = None) -> dict[str, Any]:
    """Read one allowlisted skill file on demand, with its catalog metadata."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must name one allowlisted relative skill file")
    skill_root = _skill_root(name, root)
    allowed = _relative_files(name, root)
    if relative not in allowed:
        raise ValueError(f"{path!r} is not an allowlisted file for {name}")
    entry = next(item for item in catalog(root) if item["name"] == name)
    return {"metadata": entry, "path": relative.as_posix(),
            "content": (skill_root / relative).read_text(encoding="utf-8")}


def install(name: str, target: Path, root: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Copy one fixed local bundle to an explicit existing local skill root.

    Only files in this module's static allowlist are copied.  The function does
    not download code, resolve dependencies, expand archives, or copy secrets.
    """
    if not target.is_absolute() or not target.is_dir():
        raise ValueError("target must be an existing absolute local directory")
    source = _skill_root(name, root)
    destination = target.resolve() / name
    files = _relative_files(name, root)
    changes = [relative.as_posix() for relative in files
               if not (destination / relative).is_file()
               or (destination / relative).read_bytes() != (source / relative).read_bytes()]
    if not dry_run:
        for relative in files:
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, output)
    return {
        "name": name,
        "target": str(destination),
        "changed": bool(changes),
        "changed_files": changes,
        "sha256": _bundle_hash(source, files),
        "dry_run": dry_run,
    }


def measure(before: Path, after: Path) -> dict[str, Any]:
    """Compare literal file size and word count without claiming token savings."""
    def stats(path: Path) -> dict[str, int | str]:
        text = path.read_text(encoding="utf-8")
        return {"path": str(path), "bytes": len(text.encode("utf-8")),
                "lines": len(text.splitlines()), "words": len(re.findall(r"\S+", text))}
    first, second = stats(before), stats(after)
    return {"before": first, "after": second,
            "delta": {key: int(second[key]) - int(first[key]) for key in ("bytes", "lines", "words")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="list short metadata without loading skill bodies")
    commands.add_parser("discover", help="print the local catalog pointer without loading skill bodies")
    show = commands.add_parser("show", help="read one named allowlisted file")
    show.add_argument("name", choices=sorted(SKILLS))
    show.add_argument("--path", default="SKILL.md")
    show.add_argument("--json", action="store_true")
    install_command = commands.add_parser("install", help="install one fixed local skill bundle")
    install_command.add_argument("name", choices=sorted(SKILLS))
    install_command.add_argument("--target", required=True, type=Path)
    install_command.add_argument("--dry-run", action="store_true")
    measure_command = commands.add_parser("measure", help="compare two local instruction artifacts")
    measure_command.add_argument("--before", required=True, type=Path)
    measure_command.add_argument("--after", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result: Any = catalog()
        elif args.command == "discover":
            result = discovery()
        elif args.command == "show":
            result = read(args.name, args.path)
            if not args.json:
                sys.stdout.write(result["content"])
                return 0
        elif args.command == "install":
            result = install(args.name, args.target, dry_run=args.dry_run)
        else:
            result = measure(args.before, args.after)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
