#!/usr/bin/env python3
"""Project Atlas: search, organize, resume, and work across owned projects."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

if __package__ in (None, ""):  # executed as a script; the siblings are here
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap
import domains
import ownership


VERSION = "0.3.0"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STOP_WORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "did", "do",
    "file", "files", "find", "for", "from", "in", "is", "it", "me",
    "my", "of", "on", "or", "project", "projects", "that", "the", "this",
    "to", "was", "we", "what", "where", "which", "with",
}
IGNORED_DIR_NAMES = {
    ".git", ".svn", ".hg", ".cache", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "__pycache__", "node_modules", ".venv", "venv",
}
IGNORED_FILE_NAMES = {".DS_Store"}
# A project home is the project's own manifest, sessions, and documents. A
# checkout, a linked worktree, or a container holding them is a separate body
# of work with its own history; indexing it through the project home inflates
# the index with thousands of foreign files and reports them as project-owned.
# Such a tree becomes searchable by being declared as its own manifest source.
NESTED_REPO_CONTAINER_NAMES = {".worktrees", "worktrees"}
SENSITIVE_FILE_NAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", "credentials",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
}
SENSITIVE_FILE_SUFFIXES = {".key", ".mobileprovision", ".p12", ".pem", ".pfx"}
TEXT_SUFFIXES = {
    ".md", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".sh",
    ".sql", ".xml", ".csv", ".rtf",
}
PROTECTED_KINDS = {
    "active-worktree", "live-runtime", "runtime-state", "runtime-repository",
    "review-clone", "runtime-backup", "legal-record",
}


class AtlasError(RuntimeError):
    """A user-facing Atlas error."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: Path
    aliases: tuple[str, ...]
    manifest: dict

    @property
    def keywords(self) -> tuple[str, ...]:
        raw = self.manifest.get("keywords", [])
        return tuple(str(item) for item in raw) if isinstance(raw, list) else ()

    @property
    def visibility(self) -> str:
        return str(self.manifest.get("visibility", "private"))

    @property
    def domain(self) -> str | None:
        """The optional Project Domain, resolved from the manifest and nowhere else."""
        return domains.manifest_domain(self.manifest, str(self.root / "project.json"))


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_registry() -> Path:
    configured = os.environ.get("ATLAS_REGISTRY")
    return (
        Path(configured).expanduser()
        if configured
        else codex_home() / "projects" / "registry.json"
    )


def default_index() -> Path:
    configured = os.environ.get("ATLAS_INDEX")
    return (
        Path(configured).expanduser()
        if configured
        else codex_home() / "projects" / "index.jsonl"
    )


def load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AtlasError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AtlasError(f"Invalid {label} JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AtlasError(f"{label} must be a JSON object: {path}")
    return payload


def load_projects(registry_path: Path) -> tuple[dict, list[Project]]:
    registry = load_json(registry_path, "Atlas registry")
    raw_projects = registry.get("projects")
    if not isinstance(raw_projects, list):
        raise AtlasError(f"Atlas registry has no projects list: {registry_path}")
    projects = []
    for raw in raw_projects:
        if not isinstance(raw, dict):
            raise AtlasError("Atlas registry project entries must be objects")
        project_id = str(raw.get("id", "")).strip()
        root = Path(str(raw.get("root", ""))).expanduser()
        if not project_id or not str(raw.get("root", "")).strip():
            raise AtlasError("Every registry project requires id and root")
        manifest_path = root / "project.json"
        manifest = load_json(manifest_path, f"project manifest for {project_id}")
        aliases = raw.get("aliases", manifest.get("aliases", []))
        if not isinstance(aliases, list):
            raise AtlasError(f"Project aliases must be a list: {project_id}")
        projects.append(Project(
            id=project_id,
            name=str(raw.get("name", manifest.get("name", project_id))),
            root=root,
            aliases=tuple(str(alias) for alias in aliases),
            manifest=manifest,
        ))
    return registry, projects


def project_matches(project: Project, requested: str) -> bool:
    wanted = requested.casefold()
    names = (project.id, project.name, *project.aliases)
    return any(name.casefold() == wanted for name in names)


def resolve_project(projects: list[Project], requested: str) -> Project:
    matches = [project for project in projects if project_matches(project, requested)]
    if not matches:
        raise AtlasError(f"Project not found: {requested}")
    if len(matches) > 1:
        raise AtlasError(
            f"Ambiguous project '{requested}': "
            + ", ".join(project.id for project in matches)
        )
    return matches[0]


def source_specs(project: Project) -> list[dict]:
    specs = [{
        "path": str(project.root),
        "kind": "project-home",
        "role": "project-owned manifest and sessions",
        "search": True,
        "skip_nested_repos": True,
    }]
    raw = project.manifest.get("sources", [])
    if isinstance(raw, list):
        specs.extend(spec for spec in raw if isinstance(spec, dict))
    return specs


def declared_searchable_roots(project: Project) -> set[Path]:
    """Resolved directories the manifest itself declares as searchable sources.

    The implicit project-home entry is deliberately excluded: it is the walk
    that refuses to descend into a nested repository, so counting it would
    silence every warning instead of only the answered ones.
    """

    roots: set[Path] = set()
    raw = project.manifest.get("sources", [])
    if not isinstance(raw, list):
        return roots
    for spec in raw:
        if not isinstance(spec, dict) or spec.get("search", True) is not True:
            continue
        raw_path = str(spec.get("path", "")).strip()
        if not raw_path:
            continue
        roots.add(Path(raw_path).expanduser().resolve(strict=False))
    return roots


def source_scope_errors(project: Project, spec: dict) -> list[str]:
    """Return fail-closed privacy violations for one searchable source."""
    if spec.get("search", True) is not True:
        return []
    raw_path = str(spec.get("path", "")).strip()
    if not raw_path:
        return [f"searchable source has no path: {project.id}"]

    source = Path(raw_path).expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    sensitive_roots = (
        home / ".aws",
        home / ".gnupg",
        home / ".password-store",
        home / ".ssh",
        home / "Library",
    )
    for root in sensitive_roots:
        if source == root or root in source.parents:
            return [
                f"searchable source is inside a private system/credential root: "
                f"{project.id} -> {source}"
            ]

    broad_roots = {home, home / "Desktop", home / "Documents", home / "Downloads"}
    if source in broad_roots:
        raw_include = spec.get("include", [])
        includes = (
            [str(item).strip() for item in raw_include if str(item).strip()]
            if isinstance(raw_include, list)
            else []
        )
        max_depth = spec.get("max_depth")
        unsafe_patterns = {"*", "**", "**/*"}
        if (
            not includes
            or any(pattern in unsafe_patterns for pattern in includes)
            or not isinstance(max_depth, int)
            or max_depth > 1
            or max_depth < 0
        ):
            return [
                f"broad searchable source requires a narrow include allowlist "
                f"and max_depth <= 1: {project.id} -> {source}"
            ]
    return []


def included(relative: str, name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


def literal_prefix(pattern: str) -> str:
    """The text an include pattern requires before its first wildcard."""

    for position, character in enumerate(pattern):
        if character in "*?[":
            return pattern[:position]
    return pattern


def directory_in_scope(relative_dir: str, patterns: list[str]) -> bool:
    """True when an include allowlist can still admit a file below a directory.

    Every path inside the directory starts with ``relative_dir`` plus a
    separator, and every string a pattern matches starts with that pattern's
    literal prefix. Two prefixes of one string have to agree where they
    overlap, so a disagreement proves the whole subtree is unreachable: it is
    pruned without being walked, tested for a nested repository, or reported.
    An empty allowlist, and any pattern that begins with a wildcard, keeps
    every directory in scope.
    """

    if not patterns:
        return True
    prefix = f"{relative_dir}{os.sep}"
    for pattern in patterns:
        literal = literal_prefix(pattern).replace("/", os.sep)
        overlap = min(len(literal), len(prefix))
        if literal[:overlap] == prefix[:overlap]:
            return True
    return False


def sensitive_filename(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in SENSITIVE_FILE_NAMES
        or folded.startswith(".env.")
        or Path(folded).suffix in SENSITIVE_FILE_SUFFIXES
    )


def nested_repository(path: Path) -> bool:
    """True when a directory is a separate checkout, worktree, or their container.

    A linked worktree carries ``.git`` as a file, a clone as a directory; both
    answer the same question. The source root itself is never tested, so a
    declared source that *is* a worktree stays searchable.
    """

    if path.name in NESTED_REPO_CONTAINER_NAMES:
        return True
    marker = path / ".git"
    return marker.exists() or marker.is_symlink()


def iter_source_files(spec: dict, skipped: list[str] | None = None) -> Iterable[Path]:
    source = Path(str(spec.get("path", ""))).expanduser()
    if source.is_symlink() or not source.exists():
        return
    if source.is_file():
        if (
            source.name not in IGNORED_FILE_NAMES
            and not sensitive_filename(source.name)
        ):
            yield source
        return
    if not source.is_dir():
        return

    raw_include = spec.get("include", [])
    patterns = [str(item) for item in raw_include] if isinstance(raw_include, list) else []
    raw_exclude = spec.get("exclude", [])
    excludes = [str(item) for item in raw_exclude] if isinstance(raw_exclude, list) else []
    max_depth_raw = spec.get("max_depth")
    max_depth = int(max_depth_raw) if max_depth_raw is not None else None
    skip_nested = spec.get("skip_nested_repos", True) is not False

    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        relative_dir = current.relative_to(source)
        depth = 0 if str(relative_dir) == "." else len(relative_dir.parts)
        if max_depth is not None and depth >= max_depth:
            # Nothing below this directory is searchable, so no child is
            # inspected and none is reported as a skipped nested tree.
            dirnames[:] = []
        else:
            keep = []
            for name in sorted(dirnames):
                if name in IGNORED_DIR_NAMES or (current / name).is_symlink():
                    continue
                child_relative = str(relative_dir / name)
                if any(
                    fnmatch.fnmatch(child_relative, pattern)
                    for pattern in excludes
                ):
                    continue
                if not directory_in_scope(child_relative, patterns):
                    continue
                if skip_nested and nested_repository(current / name):
                    if skipped is not None:
                        skipped.append(str(current / name))
                    continue
                keep.append(name)
            dirnames[:] = keep
        for name in sorted(filenames):
            if name in IGNORED_FILE_NAMES:
                continue
            if sensitive_filename(name):
                continue
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = str(path.relative_to(source))
            if any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
                continue
            if included(relative, name, patterns):
                yield path


def hash_file(path: Path, max_size: int) -> str | None:
    try:
        size = path.stat().st_size
        if size > max_size:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, PermissionError):
        return None


def iso_mtime(path: Path) -> str:
    stamp = path.stat().st_mtime
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def build_index(
    registry: dict,
    projects: list[Project],
    with_hashes: bool,
    max_hash_size: int,
) -> tuple[list[dict], list[str]]:
    node_id = str(registry.get("node_id", "local"))
    by_path: dict[str, dict] = {}
    warnings: list[str] = []

    for project in projects:
        declared_roots = declared_searchable_roots(project)
        for spec in source_specs(project):
            if spec.get("search", True) is not True:
                continue
            policy_errors = source_scope_errors(project, spec)
            if policy_errors:
                raise AtlasError("; ".join(policy_errors))
            raw_path = str(spec.get("path", ""))
            source = Path(raw_path).expanduser()
            if not source.exists():
                warnings.append(f"missing source: {project.id} -> {source}")
                continue
            skipped: list[str] = []
            for path in iter_source_files(spec, skipped):
                resolved = str(path.resolve())
                item = by_path.get(resolved)
                if item is None:
                    try:
                        stat = path.stat()
                    except (OSError, PermissionError) as exc:
                        warnings.append(f"unreadable file: {path} ({exc})")
                        continue
                    content_hash = hash_file(path, max_hash_size) if with_hashes else None
                    primary = project.id
                    if content_hash:
                        uri = f"atlas://{node_id}/{primary}/sha256/{content_hash}"
                    else:
                        uri = f"atlas://{node_id}/{primary}/path/{quote(resolved, safe='')}"
                    item = {
                        "schema_version": 1,
                        "node": node_id,
                        "uri": uri,
                        "path": resolved,
                        "name": path.name,
                        "extension": path.suffix.casefold(),
                        "size": stat.st_size,
                        "mtime": iso_mtime(path),
                        "sha256": content_hash,
                        "projects": [],
                        "project_names": [],
                        "visibilities": [],
                        "keywords": [],
                        "kinds": [],
                        "source_roots": [],
                        "roles": [],
                    }
                    by_path[resolved] = item
                for key, value in (
                    ("projects", project.id),
                    ("project_names", project.name),
                    ("visibilities", project.visibility),
                    ("kinds", str(spec.get("kind", "source"))),
                    ("source_roots", str(source.resolve())),
                    ("roles", str(spec.get("role", ""))),
                ):
                    if value and value not in item[key]:
                        item[key].append(value)
                for keyword in project.keywords:
                    if keyword not in item["keywords"]:
                        item["keywords"].append(keyword)
            for nested in skipped:
                if Path(nested).resolve(strict=False) in declared_roots:
                    # Already declared and walked as its own searchable source
                    # for this project, so the skip is the intended split and
                    # the remedy the warning asks for is already in place.
                    continue
                warnings.append(
                    f"skipped nested repository/worktree: {project.id} -> {nested}; "
                    "declare it as its own manifest source to index it"
                )

    items = sorted(by_path.values(), key=lambda item: item["path"].casefold())
    for item in items:
        for key in (
            "projects", "project_names", "visibilities", "keywords", "kinds",
            "source_roots", "roles",
        ):
            item[key] = sorted(item[key], key=str.casefold)
    return items, warnings


def atomic_write_index(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    temporary.replace(path)


def load_index(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise AtlasError(f"Atlas index not found; run 'atlas index': {path}") from exc
    result = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AtlasError(f"Invalid Atlas index line {number}: {exc}") from exc
        if isinstance(item, dict):
            result.append(item)
    return result


def terms(query: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", query.casefold())
    return [term for term in raw if term not in STOP_WORDS] or raw


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def score_item(item: dict, query: str) -> tuple[float, int]:
    query_terms = terms(query)
    phrase = normalized(query)
    fields = {
        "name": str(item.get("name", "")),
        "path": str(item.get("path", "")),
        "projects": " ".join(item.get("projects", []) + item.get("project_names", [])),
        "keywords": " ".join(item.get("keywords", [])),
        "kinds": " ".join(item.get("kinds", []) + item.get("roles", [])),
    }
    field_terms = {
        key: set(re.findall(r"[a-z0-9]+", value.casefold()))
        for key, value in fields.items()
    }
    score = 0.0
    matched = 0
    if phrase:
        if phrase == normalized(fields["name"]):
            score += 45
        elif phrase in normalized(fields["name"]):
            score += 30
        elif phrase in normalized(fields["path"]):
            score += 20
        elif phrase in normalized(fields["keywords"]):
            # Project keywords are broad discovery hints repeated across every
            # file in that project. They must not outrank a distilled session
            # or a filename/path that actually names the query.
            score += 5
    for term in query_terms:
        term_score = 0.0
        for field, weight in (
            ("name", 16), ("projects", 10), ("keywords", 3),
            ("path", 7), ("kinds", 4),
        ):
            if term in field_terms[field]:
                term_score = max(term_score, weight)
        if term_score:
            matched += 1
            score += term_score
    if not matched:
        return 0.0, 0
    coverage = matched / max(len(query_terms), 1)
    if len(query_terms) >= 3 and coverage < 0.60:
        return 0.0, 0
    return score + coverage * 10, matched


def session_results(query: str, limit: int, project: str | None) -> list[dict]:
    executable = codex_home() / "bin" / "session-vault"
    if not executable.is_file():
        return []
    command = [str(executable), "search", query, "--limit", str(limit), "--json"]
    if project:
        command.extend(["--project", project])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode not in (0, 1) or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    result = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        enriched = dict(entry)
        enriched["type"] = "session"
        result.append(enriched)
    return result


def duplicate_groups(items: list[dict], min_size: int) -> list[dict]:
    grouped: dict[tuple[str, int], list[dict]] = {}
    for item in items:
        digest = item.get("sha256")
        size = int(item.get("size", 0))
        if not digest or size < min_size:
            continue
        grouped.setdefault((str(digest), size), []).append(item)

    result = []
    for (digest, size), copies in grouped.items():
        paths = {copy.get("path") for copy in copies}
        source_roots = {
            root for copy in copies for root in copy.get("source_roots", [])
        }
        if len(paths) < 2 or len(source_roots) < 2:
            continue
        all_kinds = {kind for copy in copies for kind in copy.get("kinds", [])}
        if all_kinds & PROTECTED_KINDS:
            classification = "protected-operational"
        elif any("/Downloads/" in str(path) for path in paths) and any(
            "/Downloads/" not in str(path) for path in paths
        ):
            classification = "review-download-copy"
        elif len({project for copy in copies for project in copy.get("projects", [])}) > 1:
            classification = "review-cross-project"
        else:
            classification = "review-cross-source"

        def keeper_score(copy: dict) -> tuple[int, int, str]:
            path = str(copy.get("path", ""))
            kinds = set(copy.get("kinds", []))
            score = 0
            if "/Downloads/" not in path:
                score += 100
            if "/Projects/" in path:
                score += 50
            if kinds & {"canonical-repo", "canonical-project-root", "artifact-collection", "project-home"}:
                score += 30
            if kinds & PROTECTED_KINDS:
                score += 500
            return (-score, len(path), path.casefold())

        ordered = sorted(copies, key=keeper_score)
        result.append({
            "sha256": digest,
            "size": size,
            "classification": classification,
            "recommended_keeper": ordered[0].get("path"),
            "copies": ordered,
        })
    return sorted(result, key=lambda group: (-group["size"], group["sha256"]))


def write_duplicate_report(path: Path, groups: list[dict]) -> None:
    lines = [
        "# Project Atlas exact-duplicate report",
        "",
        "This report is advisory. It authorizes no deletion or movement. Content hashes prove byte identity, not that a path is operationally disposable. Protected runtime/worktree groups must remain untouched until their owning tool confirms otherwise.",
        "",
        f"Generated: {datetime.now(tz=timezone.utc).isoformat()}",
        "",
        f"Groups: {len(groups)}",
        "",
    ]
    for index, group in enumerate(groups, start=1):
        lines.extend([
            f"## {index}. {group['classification']} — {group['size']} bytes",
            "",
            f"- SHA-256: `{group['sha256']}`",
            f"- Suggested keeper for review: `{group['recommended_keeper']}`",
            "- Copies:",
        ])
        for copy in group["copies"]:
            projects = ", ".join(copy.get("projects", []))
            kinds = ", ".join(copy.get("kinds", []))
            lines.append(f"  - `{copy.get('path')}` — {projects}; {kinds}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def command_index(args: argparse.Namespace, registry: dict, projects: list[Project]) -> int:
    items, warnings = build_index(
        registry,
        projects,
        with_hashes=args.hash,
        max_hash_size=args.max_hash_size,
    )
    atomic_write_index(args.index_file, items)
    payload = {
        "version": VERSION,
        "node": registry.get("node_id", "local"),
        "projects": len(projects),
        "files": len(items),
        "hashed_files": sum(1 for item in items if item.get("sha256")),
        "index": str(args.index_file.resolve()),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Atlas index OK: {payload['files']} file(s), "
            f"{payload['projects']} project(s), {payload['hashed_files']} hash(es)"
        )
        print(f"  {payload['index']}")
        for warning in warnings:
            print(f"Warning: {warning}")
    return 0


def command_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    items = load_index(args.index_file)
    selected = items
    if args.project:
        wanted = args.project.casefold()
        selected = [
            item for item in items
            if any(str(project).casefold() == wanted for project in item.get("projects", []))
        ]
    ranked = []
    for item in selected:
        score, matched = score_item(item, query)
        if score > 0:
            enriched = dict(item)
            enriched.update({
                "type": "file",
                "score": round(score, 2),
                "matched_terms": matched,
            })
            ranked.append(enriched)
    ranked.sort(key=lambda item: (-item["score"], item["path"].casefold()))
    file_results = ranked[: args.limit]
    sessions = [] if args.no_sessions else session_results(
        query, args.limit, args.project
    )
    combined = sessions + file_results
    combined.sort(
        key=lambda item: (
            -float(item.get("score", 0)),
            str(item.get("identity", item.get("path", ""))).casefold(),
        )
    )
    combined = combined[: args.limit]
    if args.json:
        print(json.dumps(combined, indent=2))
    elif combined:
        for item in combined:
            if item.get("type") == "session":
                print(f"SESSION {item.get('identity')} — {item.get('title')}  [score {item.get('score')}]" )
                print(f"  {item.get('summary', '')}")
                print(f"  {item.get('path')}")
            else:
                projects = ", ".join(item.get("projects", []))
                print(f"FILE {item.get('name')}  [score {item.get('score'):.1f}]" )
                print(f"  {projects} · {item.get('size')} bytes · {item.get('uri')}")
                print(f"  {item.get('path')}")
    else:
        print(f"No Atlas records matched: {query}")
        return 1
    return 0


def command_locate(args: argparse.Namespace, projects: list[Project]) -> int:
    project = resolve_project(projects, args.project)
    payload = {
        "id": project.id,
        "name": project.name,
        "root": str(project.root.resolve()),
        "aliases": list(project.aliases),
        "status": project.manifest.get("status", ""),
        "visibility": project.visibility,
        "domain": project.domain,
        "domain_label": domains.domain_label(project.domain),
        "keywords": list(project.keywords),
        "sources": source_specs(project),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{project.id} — {project.name}")
        print(f"  root: {project.root.resolve()}")
        print(f"  domain: {domains.domain_label(project.domain)}")
        for spec in source_specs(project):
            state = "indexed" if spec.get("search", True) else "pointer-only"
            print(
                f"  {state}\t{spec.get('kind', 'source')}\t"
                f"{Path(str(spec.get('path', ''))).expanduser()}"
            )
    return 0


def command_projects(args: argparse.Namespace, projects: list[Project]) -> int:
    payload = [
        {
            "id": project.id,
            "name": project.name,
            "root": str(project.root.resolve()),
            "status": project.manifest.get("status", ""),
            "visibility": project.visibility,
            "domain": project.domain,
            "domain_label": domains.domain_label(project.domain),
            "aliases": list(project.aliases),
        }
        for project in projects
    ]
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in payload:
            print(
                f"{item['id']}\t{item['status']}\t{item['visibility']}\t"
                f"{item['root']}"
            )
    return 0


def command_duplicates(args: argparse.Namespace) -> int:
    items = load_index(args.index_file)
    groups = duplicate_groups(items, args.min_size)
    if args.report:
        write_duplicate_report(args.report, groups)
    if args.json:
        print(json.dumps(groups, indent=2))
    elif not groups:
        print("No cross-source exact duplicates found in the Atlas index.")
    else:
        print(f"Exact duplicate groups: {len(groups)}")
        for group in groups:
            print(
                f"{group['classification']}\t{group['size']} bytes\t"
                f"{len(group['copies'])} copies\t{group['sha256']}"
            )
            for copy in group["copies"]:
                print(f"  {copy.get('path')}")
        if args.report:
            print(f"Report: {args.report.resolve()}")
    return 0


def command_doctor(
    args: argparse.Namespace,
    registry_path: Path,
    registry: dict,
    projects: list[Project],
) -> int:
    errors = []
    warnings = []
    seen_ids = set()
    seen_roots = set()
    for project in projects:
        if not SAFE_ID.fullmatch(project.id):
            errors.append(f"unsafe project id: {project.id}")
        folded = project.id.casefold()
        if folded in seen_ids:
            errors.append(f"duplicate project id: {project.id}")
        seen_ids.add(folded)
        root = str(project.root.resolve()) if project.root.exists() else str(project.root)
        if root in seen_roots:
            errors.append(f"duplicate project root: {root}")
        seen_roots.add(root)
        if project.manifest.get("id") != project.id:
            errors.append(f"manifest id mismatch: {project.id}")
        try:
            project.domain
        except domains.DomainError as exc:
            # An optional field is optional; a malformed one is never quietly
            # downgraded to "unscoped".
            errors.append(str(exc))
        if not project.root.is_dir():
            errors.append(f"missing project root: {project.root}")
        if not (project.root / "sessions").is_dir():
            errors.append(f"missing sessions directory: {project.root / 'sessions'}")
        for spec in source_specs(project):
            errors.extend(source_scope_errors(project, spec))
            source_node = str(spec.get("node", registry.get("node_id", "local")))
            if source_node != str(registry.get("node_id", "local")):
                # Remote sources are last-known metadata until v1 federates a
                # live node index. Their paths must not be resolved locally.
                continue
            source = Path(str(spec.get("path", ""))).expanduser()
            if not source.exists():
                warnings.append(f"missing source: {project.id} -> {source}")

    nodes = registry.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        warnings.append("registry has no node catalog")
    if args.index_file.exists():
        try:
            load_index(args.index_file)
        except AtlasError as exc:
            errors.append(str(exc))
    else:
        warnings.append(f"index missing: {args.index_file}")

    vault = codex_home() / "bin" / "session-vault"
    if vault.is_file():
        completed = subprocess.run(
            [str(vault), "doctor"], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            errors.append(completed.stderr.strip() or completed.stdout.strip())
    else:
        warnings.append(f"Session Vault wrapper missing: {vault}")

    if errors:
        print("Project Atlas validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Project Atlas OK: {len(projects)} project(s), "
        f"node {registry.get('node_id', 'local')}, registry {registry_path.resolve()}"
    )
    for warning in warnings:
        print(f"Warning: {warning}")
    return 0


def command_ui(args: argparse.Namespace) -> int:
    """Start the loopback-only Atlas Studio workspace."""
    from atlas_ui import UiError, serve

    try:
        return serve(
            args.registry,
            args.index_file,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            verbose=args.verbose,
        )
    except UiError as exc:
        raise AtlasError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--registry", type=Path, default=default_registry())
    result.add_argument("--index-file", type=Path, default=default_index())
    result.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = result.add_subparsers(dest="command", required=True)

    indexing = commands.add_parser("index", help="build the pointer-only file index")
    indexing.add_argument("--hash", action="store_true", help="include SHA-256 content identities")
    indexing.add_argument("--max-hash-size", type=int, default=1024 * 1024 * 1024)
    indexing.add_argument("--json", action="store_true")

    search = commands.add_parser("search", help="search sessions and indexed file metadata")
    search.add_argument("query", nargs="+")
    search.add_argument("--project")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--no-sessions", action="store_true")
    search.add_argument("--json", action="store_true")

    locate = commands.add_parser("locate", help="show a project home and source pointers")
    locate.add_argument("project")
    locate.add_argument("--json", action="store_true")

    projects = commands.add_parser("projects", help="list canonical project identities")
    projects.add_argument("--json", action="store_true")

    duplicates = commands.add_parser("duplicates", help="report cross-source exact duplicates")
    duplicates.add_argument("--min-size", type=int, default=1)
    duplicates.add_argument("--report", type=Path)
    duplicates.add_argument("--json", action="store_true")

    commands.add_parser("doctor", help="validate registry, manifests, index, and Session Vault")
    bootstrap.add_resume_parser(commands)
    ownership.add_own_parser(commands)
    ui = commands.add_parser("ui", help="open the local Atlas Studio workspace")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=4732)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--verbose", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    args.registry = args.registry.expanduser()
    args.index_file = args.index_file.expanduser()
    try:
        if args.command == "resume":
            # Bootstrap resolves its own registry from the trusted node
            # configuration, so it must not depend on the default one existing.
            return bootstrap.command_resume(args)
        registry, projects = load_projects(args.registry)
        if args.command == "own":
            return ownership.command_own(
                args, projects, str(registry.get("node_id", "local")), resolve_project
            )
        if args.command == "index":
            return command_index(args, registry, projects)
        if args.command == "search":
            return command_search(args)
        if args.command == "locate":
            return command_locate(args, projects)
        if args.command == "projects":
            return command_projects(args, projects)
        if args.command == "duplicates":
            if args.report:
                args.report = args.report.expanduser()
            return command_duplicates(args)
        if args.command == "doctor":
            return command_doctor(args, args.registry, registry, projects)
        if args.command == "ui":
            return command_ui(args)
    except (AtlasError, domains.DomainError, ownership.OwnershipError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except bootstrap.BootstrapError as exc:
        print(json.dumps({
            "schema": bootstrap.RECEIPT_SCHEMA,
            "action": "resume",
            "state": "error",
            "error": str(exc),
            "authority": bootstrap.AUTHORITY_NOTE,
        }, indent=2, sort_keys=True))
        print(str(exc), file=sys.stderr)
        return bootstrap.EXIT_ERROR
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
