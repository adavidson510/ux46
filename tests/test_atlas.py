from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ATLAS = REPO_ROOT / "tools" / "atlas.py"


class AtlasCliTests(unittest.TestCase):
    def run_atlas(self, registry: Path, index: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ATLAS),
                "--registry",
                str(registry),
                "--index-file",
                str(index),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def make_project(self, root: Path, project_id: str, source: Path, keywords: list[str]) -> None:
        (root / "sessions").mkdir(parents=True)
        (root / "project.json").write_text(json.dumps({
            "schema_version": 1,
            "id": project_id,
            "name": project_id.title(),
            "status": "active",
            "visibility": "private",
            "aliases": [],
            "keywords": keywords,
            "related_projects": [],
            "sources": [{
                "path": str(source),
                "kind": "artifact-collection",
                "role": "test artifacts",
                "search": True,
            }],
        }), encoding="utf-8")

    def test_index_search_and_cross_source_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            orbit_home = base / "projects" / "orbit"
            atlas_home = base / "projects" / "project-atlas"
            orbit_source = base / "sources" / "orbit"
            atlas_source = base / "sources" / "atlas"
            orbit_source.mkdir(parents=True)
            atlas_source.mkdir(parents=True)
            payload = b"same exact mockup bytes"
            (orbit_source / "ORBIT-v3-mobile-mockup.png").write_bytes(payload)
            (orbit_source / ".env.production").write_text("DO_NOT_INDEX=yes")
            (atlas_source / "review-copy.png").write_bytes(payload)
            self.make_project(
                orbit_home, "orbit", orbit_source,
                ["ORBIT v3", "UX mockups", "truth chrome"],
            )
            self.make_project(
                atlas_home, "project-atlas", atlas_source,
                ["global project search"],
            )
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-node",
                "nodes": [{"id": "test-node"}],
                "projects": [
                    {"id": "orbit", "name": "ORBIT", "root": str(orbit_home), "aliases": []},
                    {"id": "project-atlas", "name": "Project Atlas", "root": str(atlas_home), "aliases": ["atlas"]},
                ],
            }), encoding="utf-8")
            index = base / "index.jsonl"

            built = self.run_atlas(registry, index, "index", "--hash", "--json")
            self.assertEqual(built.returncode, 0, built.stderr)
            summary = json.loads(built.stdout)
            self.assertEqual(summary["hashed_files"], summary["files"])
            indexed_paths = [json.loads(line)["path"] for line in index.read_text().splitlines()]
            self.assertNotIn(str(orbit_source / ".env.production"), indexed_paths)

            searched = self.run_atlas(
                registry, index, "search", "ORBIT v3 mockup",
                "--no-sessions", "--json",
            )
            self.assertEqual(searched.returncode, 0, searched.stderr)
            results = json.loads(searched.stdout)
            self.assertEqual(results[0]["name"], "ORBIT-v3-mobile-mockup.png")
            self.assertEqual(results[0]["projects"], ["orbit"])

            duplicates = self.run_atlas(registry, index, "duplicates", "--json")
            self.assertEqual(duplicates.returncode, 0, duplicates.stderr)
            groups = json.loads(duplicates.stdout)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["classification"], "review-cross-project")
            self.assertEqual(len(groups[0]["copies"]), 2)

    def test_locate_returns_pointer_only_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            source = base / "runtime"
            source.mkdir()
            (home / "sessions").mkdir(parents=True)
            (home / "project.json").write_text(json.dumps({
                "schema_version": 1,
                "id": "tell",
                "name": "Tell",
                "status": "active",
                "visibility": "private",
                "aliases": ["telld"],
                "keywords": ["messaging"],
                "sources": [{
                    "path": str(source),
                    "kind": "live-runtime",
                    "role": "protected",
                    "search": False,
                }],
            }), encoding="utf-8")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-node",
                "nodes": [{"id": "test-node"}],
                "projects": [{
                    "id": "tell", "name": "Tell", "root": str(home),
                    "aliases": ["telld"],
                }],
            }), encoding="utf-8")
            located = self.run_atlas(
                registry, base / "index.jsonl", "locate", "telld", "--json"
            )
            self.assertEqual(located.returncode, 0, located.stderr)
            payload = json.loads(located.stdout)
            self.assertEqual(payload["id"], "tell")
            self.assertFalse(payload["sources"][1]["search"])

    def test_index_skips_nested_worktrees_but_keeps_declared_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            (home / "notes.md").write_text("project-owned note", encoding="utf-8")

            # A linked worktree carries .git as a file, a clone as a directory.
            worktree = home / ".worktrees" / "feature-a"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/a\n")
            (worktree / "worktree-note.md").write_text("foreign", encoding="utf-8")

            container = home / "worktrees" / "feature-b"
            container.mkdir(parents=True)
            (container / "container-note.md").write_text("foreign", encoding="utf-8")

            checkout = home / "vendored-checkout"
            (checkout / ".git").mkdir(parents=True)
            (checkout / "checkout-note.md").write_text("foreign", encoding="utf-8")

            # The same shape, but declared as its own manifest source.
            declared = home / "declared-worktree"
            declared.mkdir()
            (declared / ".git").write_text("gitdir: /elsewhere/.git/worktrees/d\n")
            (declared / "declared-note.md").write_text("declared", encoding="utf-8")

            (home / "project.json").write_text(json.dumps({
                "schema_version": 1,
                "id": "atlas-home",
                "name": "Atlas Home",
                "status": "active",
                "visibility": "private",
                "aliases": [],
                "keywords": [],
                "related_projects": [],
                "sources": [{
                    "path": str(declared),
                    "kind": "active-worktree",
                    "role": "explicitly declared searchable worktree",
                    "search": True,
                }],
            }), encoding="utf-8")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-node",
                "nodes": [{"id": "test-node"}],
                "projects": [{
                    "id": "atlas-home", "name": "Atlas Home",
                    "root": str(home), "aliases": [],
                }],
            }), encoding="utf-8")
            index = base / "index.jsonl"

            built = self.run_atlas(registry, index, "index", "--json")
            self.assertEqual(built.returncode, 0, built.stderr)
            summary = json.loads(built.stdout)
            names = {
                json.loads(line)["name"]
                for line in index.read_text().splitlines()
            }
            self.assertIn("notes.md", names)
            self.assertIn("project.json", names)
            self.assertNotIn("worktree-note.md", names)
            self.assertNotIn("container-note.md", names)
            self.assertNotIn("checkout-note.md", names)
            # An explicitly declared source stays searchable even though the
            # project-home walk refuses to descend into it.
            self.assertIn("declared-note.md", names)

            skipped = [
                warning for warning in summary["warnings"]
                if "skipped nested repository/worktree" in warning
            ]
            # Only the trees that are still undeclared are reported: the
            # warning asks for a manifest source, so a nested repo that
            # already has one has nothing left to act on.
            self.assertEqual(
                {Path(warning.split(" -> ")[1].split(";")[0]).name
                 for warning in skipped},
                {".worktrees", "worktrees", "vendored-checkout"},
            )

            searched = self.run_atlas(
                registry, index, "search", "declared note",
                "--no-sessions", "--json",
            )
            self.assertEqual(searched.returncode, 0, searched.stderr)
            self.assertEqual(
                json.loads(searched.stdout)[0]["name"], "declared-note.md"
            )

    def make_shallow_allowlist_project(
        self, home: Path, shallow_root: Path, normal_root: Path
    ) -> Path:
        (home / "sessions").mkdir(parents=True)
        (home / "project.json").write_text(json.dumps({
            "schema_version": 1,
            "id": "allowlisted",
            "name": "Allowlisted",
            "status": "active",
            "visibility": "private",
            "aliases": [],
            "keywords": [],
            "related_projects": [],
            "sources": [
                {
                    "path": str(shallow_root),
                    "kind": "loose-project-documents",
                    "role": "top-level allowlisted documents",
                    "search": True,
                    "max_depth": 1,
                    "include": ["keep-*"],
                },
                {
                    "path": str(normal_root),
                    "kind": "artifact-collection",
                    "role": "ordinary unconstrained source",
                    "search": True,
                },
            ],
        }), encoding="utf-8")
        registry = home.parent / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "node_id": "test-node",
            "nodes": [{"id": "test-node"}],
            "projects": [{
                "id": "allowlisted", "name": "Allowlisted",
                "root": str(home), "aliases": [],
            }],
        }), encoding="utf-8")
        return registry

    def test_shallow_allowlist_prunes_irrelevant_nested_repos_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            shallow_root = base / "broad"
            normal_root = base / "normal"

            # Matching top-level file and matching top-level directory.
            shallow_root.mkdir(parents=True)
            (shallow_root / "keep-top.md").write_text("kept", encoding="utf-8")
            keep_dir = shallow_root / "keep-dir"
            keep_dir.mkdir()
            (keep_dir / "inside.md").write_text("kept", encoding="utf-8")
            # A nested repo one level below the allowlisted directory is past
            # max_depth, so it is never searchable and must not be reported.
            deep_repo = keep_dir / "deep-checkout"
            (deep_repo / ".git").mkdir(parents=True)
            (deep_repo / "deep-note.md").write_text("foreign", encoding="utf-8")

            # An unrelated sibling checkout the allowlist cannot admit.
            unrelated = shallow_root / "unrelated-checkout"
            (unrelated / ".git").mkdir(parents=True)
            (unrelated / "keep-basename.md").write_text("foreign", encoding="utf-8")

            # An ordinary source still names the nested tree it refuses.
            normal_root.mkdir(parents=True)
            (normal_root / "normal-note.md").write_text("kept", encoding="utf-8")
            normal_repo = normal_root / "vendored-checkout"
            (normal_repo / ".git").mkdir(parents=True)
            (normal_repo / "vendored-note.md").write_text("foreign", encoding="utf-8")

            registry = self.make_shallow_allowlist_project(
                home, shallow_root, normal_root
            )
            index = base / "index.jsonl"
            built = self.run_atlas(registry, index, "index", "--json")
            self.assertEqual(built.returncode, 0, built.stderr)
            summary = json.loads(built.stdout)
            names = {
                json.loads(line)["name"]
                for line in index.read_text().splitlines()
            }

            self.assertIn("keep-top.md", names)
            self.assertIn("inside.md", names)
            self.assertIn("normal-note.md", names)
            self.assertNotIn("keep-basename.md", names)
            self.assertNotIn("deep-note.md", names)
            self.assertNotIn("vendored-note.md", names)

            skipped = [
                warning for warning in summary["warnings"]
                if "skipped nested repository/worktree" in warning
            ]
            # (b) the ordinary source still names what it refused ...
            self.assertEqual(len(skipped), 1)
            self.assertIn("vendored-checkout", skipped[0])
            # ... and (a) the allowlisted shallow source is silent about trees
            # it could never have searched.
            self.assertNotIn("unrelated-checkout", " ".join(skipped))
            self.assertNotIn("deep-checkout", " ".join(skipped))


    def test_index_fails_closed_for_unbounded_home_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project_home = base / "project"
            (project_home / "sessions").mkdir(parents=True)
            (project_home / "project.json").write_text(json.dumps({
                "schema_version": 1,
                "id": "privacy-test",
                "name": "Privacy Test",
                "status": "active",
                "visibility": "private",
                "aliases": [],
                "keywords": [],
                "sources": [{
                    "path": str(Path.home()),
                    "kind": "unsafe-broad-source",
                    "search": True,
                }],
            }), encoding="utf-8")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-node",
                "nodes": [{"id": "test-node"}],
                "projects": [{
                    "id": "privacy-test",
                    "name": "Privacy Test",
                    "root": str(project_home),
                    "aliases": [],
                }],
            }), encoding="utf-8")
            built = self.run_atlas(registry, base / "index.jsonl", "index", "--json")
            self.assertNotEqual(built.returncode, 0)
            self.assertIn("broad searchable source requires", built.stderr)


if __name__ == "__main__":
    unittest.main()
