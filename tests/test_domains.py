"""Optional Project Domain metadata: additive, canonical, and fail-closed.

Every assertion drives the real CLI. The point of these tests is the boundary:
an old manifest stays valid and unscoped, keyword text never assigns a domain,
and a declared domain reaches the central publication and catalog without
changing project/session identity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ATLAS = REPO_ROOT / "tools" / "atlas.py"
VAULT = REPO_ROOT / "tools" / "session_vault.py"

RECORD = """---
project: nightwatch
session: review
title: Nightwatch review
date: 2026-08-20
updated: 2026-08-20
status: active
keywords: demo-network, nightwatch detection review
checkpoint_schema: 1
checkpoint_at: "2026-08-20T10:00:00Z"
checkpoint_node: server
checkpoint_reporter: cc-workspace
checkpoint_state: working
checkpoint_need: none
checkpoint_next: "Continue the review"
---

# nightwatch/review — Nightwatch review

## Summary

Cross-node domain propagation.
"""


class DomainHarness:
    def build_world(self, base: Path, manifest_extra: dict) -> tuple[Path, Path, Path]:
        project = base / "nightwatch"
        (project / "sessions").mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "id": "nightwatch",
            # Deliberately loaded with Demo-network-shaped retrieval text. None of it
            # may ever assign an ownership boundary.
            "name": "Demo-network Nightwatch",
            "status": "active",
            "visibility": "private",
            "aliases": ["demo-network-nightwatch"],
            "keywords": ["demo-network", "Demo-network domain", "demo-studio"],
            "related_projects": ["hophound"],
            "sources": [],
        }
        manifest.update(manifest_extra)
        (project / "project.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (project / "sessions" / "review.md").write_text(RECORD, encoding="utf-8")
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "node_id": "server",
            "projects": [
                {"id": "nightwatch", "name": "Nightwatch", "root": str(project), "aliases": []},
            ],
        }), encoding="utf-8")
        store = base / "store"
        store.mkdir()
        return project, registry, store

    def run_tool(self, tool: Path, registry: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env.pop("ATLAS_STORE_CACHE", None)
        return subprocess.run(
            [sys.executable, str(tool), "--registry", str(registry), *args],
            capture_output=True, text=True, check=False, env=env,
        )


class ProjectDomainTests(DomainHarness, unittest.TestCase):
    def test_manifest_without_a_domain_stays_valid_and_reports_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base, {})

            locate = self.run_tool(ATLAS, registry, "locate", "nightwatch", "--json")
            self.assertEqual(locate.returncode, 0, locate.stderr)
            payload = json.loads(locate.stdout)
            self.assertIsNone(payload["domain"])
            self.assertEqual(payload["domain_label"], "unscoped")

            listing = self.run_tool(ATLAS, registry, "projects", "--json")
            self.assertEqual(json.loads(listing.stdout)[0]["domain_label"], "unscoped")

            # An unscoped project still publishes; the manifest records the absence.
            publish = self.run_tool(
                VAULT, registry, "store", "publish", "nightwatch/review",
                "--store-root", str(store), "--principal", "cc-workspace", "--json",
            )
            self.assertEqual(publish.returncode, 0, publish.stderr)
            receipt = json.loads(publish.stdout)
            self.assertIsNone(receipt["domain"])
            self.assertIn("unscoped", receipt["domain_source"])

    def test_keyword_alias_and_name_text_never_assign_a_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base, {})

            self.assertEqual(
                self.run_tool(
                    VAULT, registry, "store", "publish", "nightwatch/review",
                    "--store-root", str(store), "--principal", "cc-workspace", "--json",
                ).returncode,
                0,
            )
            catalog = self.run_tool(
                VAULT, registry, "store", "catalog", "--store-root", str(store),
                "--domain", "demo-network", "--json",
            )
            self.assertEqual(catalog.returncode, 0, catalog.stderr)
            # 'demo-network' appears in the name, an alias, and the keywords. The
            # project is still not in the Demo-network domain.
            self.assertEqual(json.loads(catalog.stdout)["count"], 0)

            unscoped = self.run_tool(
                VAULT, registry, "store", "catalog", "--store-root", str(store),
                "--domain", "unscoped", "--json",
            )
            self.assertEqual(json.loads(unscoped.stdout)["count"], 1)

    def test_declared_domain_propagates_without_changing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base, {"domain": "demo-network"})

            publish = self.run_tool(
                VAULT, registry, "store", "publish", "nightwatch/review",
                "--store-root", str(store), "--principal", "cc-workspace",
                "--classification", "restricted", "--json",
            )
            self.assertEqual(publish.returncode, 0, publish.stderr)
            receipt = json.loads(publish.stdout)
            self.assertEqual(receipt["domain"], "demo-network")
            self.assertEqual(receipt["domain_source"], "project manifest")
            # Identity is untouched by the domain.
            self.assertEqual(receipt["identity"], "nightwatch/review")

            manifest = json.loads(
                (store / receipt["version"]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["domain"], "demo-network")
            self.assertEqual(manifest["classification"], "restricted")
            pointer = json.loads(
                (store / receipt["current"]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(pointer["domain"], "demo-network")
            self.assertEqual(pointer["identity"], "nightwatch/review")

            catalog = self.run_tool(
                VAULT, registry, "store", "catalog", "--store-root", str(store),
                "--domain", "demo-network", "--json",
            )
            entries = json.loads(catalog.stdout)["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["domain"], "demo-network")
            self.assertEqual(entries[0]["identity"], "nightwatch/review")

            other = self.run_tool(
                VAULT, registry, "store", "catalog", "--store-root", str(store),
                "--domain", "demo-studio", "--json",
            )
            self.assertEqual(json.loads(other.stdout)["count"], 0)

            pull = self.run_tool(
                VAULT, registry, "store", "pull", "nightwatch/review",
                "--store-root", str(store), "--cache-dir", str(base / "cache"), "--json",
            )
            self.assertEqual(pull.returncode, 0, pull.stderr)
            self.assertEqual(json.loads(pull.stdout)["domain"], "demo-network")

    def test_a_malformed_or_reserved_domain_fails_closed(self) -> None:
        for value, expected in (
            ("Demo-network", "lowercase"),
            ("unscoped", "reserved"),
            ("", "empty"),
        ):
            with self.subTest(domain=value), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                _, registry, store = self.build_world(base, {"domain": value})

                doctor = self.run_tool(ATLAS, registry, "doctor")
                self.assertEqual(doctor.returncode, 1)
                self.assertIn(expected, doctor.stderr)

                publish = self.run_tool(
                    VAULT, registry, "store", "publish", "nightwatch/review",
                    "--store-root", str(store), "--principal", "cc-workspace",
                )
                self.assertEqual(publish.returncode, 1)
                self.assertIn(expected, publish.stderr)
                # Nothing was written on a fail-closed manifest.
                self.assertFalse((store / "collectives").exists())


if __name__ == "__main__":
    unittest.main()
