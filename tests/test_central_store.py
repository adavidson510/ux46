from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT = REPO_ROOT / "tools" / "session_vault.py"

RECORD = """---
project: alpha
session: review
title: Alpha review
date: 2026-08-20
updated: 2026-08-20
status: active
keywords: central store publication, attributed replica
checkpoint_schema: 1
checkpoint_at: "2026-08-20T10:00:00Z"
checkpoint_node: user-mac
checkpoint_reporter: cp-workspace
checkpoint_state: working
checkpoint_need: none
checkpoint_next: "Publish the bounded slice"
origins: review.origins.json
---

# alpha/review — Alpha review

## Summary

{summary}
"""

ORIGINS = {
    "schema_version": 1,
    "project": "alpha",
    "session": "review",
    "origins": [{
        "runtime": "codex",
        "node": "user-mac",
        "session_id": "6f1d0f6e-6f1d-4f6e-8f6e-6f1d0f6e6f1d",
        "session_name": "",
        "cwd": "/Users/example/Projects/alpha",
        "captured": "2026-08-20",
        "last_seen": "2026-08-20",
        "primary": True,
    }],
}


class CentralStoreHarness:
    """Every assertion drives the real CLI against a throwaway store root."""

    def build_world(
        self, base: Path, summary: str = "Bounded publication slice."
    ) -> tuple[Path, Path, Path]:
        project = base / "alpha"
        sessions = project / "sessions"
        sessions.mkdir(parents=True)
        (project / "project.json").write_text(json.dumps({
            "schema_version": 1, "id": "alpha", "name": "Alpha",
            "status": "active", "visibility": "private",
            "aliases": [], "keywords": [], "sources": [],
        }), encoding="utf-8")
        (sessions / "review.md").write_text(RECORD.format(summary=summary), encoding="utf-8")
        (sessions / "review.origins.json").write_text(
            json.dumps(ORIGINS, indent=2) + "\n", encoding="utf-8"
        )
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "node_id": "user-mac",
            "collective_id": "workspace",
            "projects": [{"id": "alpha", "name": "Alpha", "root": str(project), "aliases": []}],
        }), encoding="utf-8")
        store = base / "store"
        store.mkdir()
        return project, registry, store

    def run_store(self, registry: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env.pop("ATLAS_STORE_CACHE", None)
        return subprocess.run(
            [sys.executable, str(VAULT), "--registry", str(registry), "store", *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def publish(self, registry: Path, store: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_store(
            registry, "publish", "alpha/review",
            "--store-root", str(store), "--principal", "cp-workspace", "--json", *extra,
        )

    def room(self, store: Path) -> Path:
        return store / "collectives" / "workspace" / "projects" / "alpha" / "rooms" / "review"

    def current(self, store: Path) -> dict:
        return json.loads((self.room(store) / "current.json").read_text(encoding="utf-8"))


class CentralStorePublishTests(CentralStoreHarness, unittest.TestCase):
    def test_first_publish_initializes_current_and_writes_immutable_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)

            self.assertEqual(receipt["identity"], "alpha/review")
            self.assertEqual(receipt["collective"], "workspace")
            self.assertEqual(receipt["current"]["state"], "initialized")
            self.assertTrue(receipt["promoted"])
            self.assertIn("explicit", receipt["promotion"])
            self.assertIn("not review", receipt["authority"])
            self.assertEqual(receipt["objects"], {"created": 2, "existing": 0})
            self.assertEqual(receipt["version"]["state"], "created")

            # The allow-list is exactly the record and its origins sidecar.
            roles = sorted(item["role"] for item in receipt["items"])
            self.assertEqual(roles, ["origins", "record"])

            record_bytes = (project / "sessions" / "review.md").read_bytes()
            objects = store / "collectives" / "workspace" / "objects" / "sha256"
            digest = receipt["current"]["record_sha256"]
            blob = objects / digest[:2] / digest[2:4] / digest
            self.assertEqual(blob.read_bytes(), record_bytes)

            pointer = self.current(store)
            self.assertEqual(pointer["record_sha256"], digest)
            self.assertIsNone(pointer["previous_record_sha256"])
            self.assertEqual(pointer["checkpoint_at"], "2026-08-20T10:00:00Z")
            self.assertEqual(pointer["classification"], "internal")

            version = json.loads((store / receipt["version"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(version["record_sha256"], digest)

    def test_publish_is_idempotent_for_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)

            first = json.loads(self.publish(registry, store).stdout)
            pointer_before = (self.room(store) / "current.json").read_bytes()

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 0, result.stderr)
            second = json.loads(result.stdout)

            self.assertEqual(second["publish_id"], first["publish_id"])
            self.assertEqual(second["current"]["state"], "unchanged")
            self.assertFalse(second["promoted"])
            self.assertEqual(second["objects"], {"created": 0, "existing": 2})
            self.assertEqual(second["version"]["state"], "existing")
            self.assertEqual(second["incoming"]["state"], "existing")
            # Idempotent means the pointer bytes are untouched, not rewritten.
            self.assertEqual((self.room(store) / "current.json").read_bytes(), pointer_before)

    def test_compare_and_set_updates_current_and_keeps_the_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)

            first = json.loads(self.publish(registry, store).stdout)
            original = first["current"]["record_sha256"]

            record = project / "sessions" / "review.md"
            record.write_text(
                RECORD.format(summary="Bounded publication slice, revised."), encoding="utf-8"
            )

            result = self.publish(registry, store, "--if-central-sha256", original)
            self.assertEqual(result.returncode, 0, result.stderr)
            second = json.loads(result.stdout)

            self.assertEqual(second["current"]["state"], "updated")
            self.assertTrue(second["promoted"])
            self.assertEqual(second["current"]["previous_record_sha256"], original)
            self.assertNotEqual(second["current"]["record_sha256"], original)

            pointer = self.current(store)
            self.assertEqual(pointer["record_sha256"], second["current"]["record_sha256"])

            # History is immutable: the superseded version manifest survives.
            versions = sorted(p.name for p in (self.room(store) / "versions").glob("*.json"))
            self.assertIn(f"{original}.json", versions)
            self.assertEqual(len(versions), 2)

    def test_stale_compare_and_set_refuses_without_mutating_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)

            self.publish(registry, store)
            pointer_before = (self.room(store) / "current.json").read_bytes()
            objects_before = sorted(
                str(p) for p in (store / "collectives").rglob("*") if p.is_file()
            )

            (project / "sessions" / "review.md").write_text(
                RECORD.format(summary="Revised under a stale observation."), encoding="utf-8"
            )
            stale = "0" * 64
            result = self.publish(registry, store, "--if-central-sha256", stale)

            self.assertEqual(result.returncode, 1)
            self.assertIn("Stale compare-and-set", result.stderr)
            self.assertIn("was not modified", result.stderr)
            self.assertEqual((self.room(store) / "current.json").read_bytes(), pointer_before)
            after = sorted(str(p) for p in (store / "collectives").rglob("*") if p.is_file())
            self.assertEqual(after, objects_before)

    def test_missing_compare_and_set_refuses_and_names_the_observed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)

            first = json.loads(self.publish(registry, store).stdout)
            (project / "sessions" / "review.md").write_text(
                RECORD.format(summary="Revised without a comparison."), encoding="utf-8"
            )
            pointer_before = (self.room(store) / "current.json").read_bytes()

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 1)
            self.assertIn("--if-central-sha256", result.stderr)
            self.assertIn(first["current"]["record_sha256"], result.stderr)
            self.assertEqual((self.room(store) / "current.json").read_bytes(), pointer_before)

    def test_compare_and_set_against_an_absent_current_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)

            result = self.publish(registry, store, "--if-central-sha256", "0" * 64)
            self.assertEqual(result.returncode, 1)
            self.assertIn("nothing to compare against", result.stderr)
            self.assertFalse((self.room(store) / "current.json").exists())

    def test_no_promote_stores_the_attributed_replica_without_promoting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)

            result = self.publish(registry, store, "--no-promote")
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)

            self.assertEqual(receipt["current"]["state"], "untouched")
            self.assertFalse(receipt["promoted"])
            self.assertIn("attributed incoming path only", receipt["promotion"])
            self.assertFalse((self.room(store) / "current.json").exists())
            self.assertTrue((store / receipt["incoming"]["path"]).is_file())

    def test_manifest_carries_per_node_per_principal_and_git_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            for argv in (
                ["init", "-q"],
                ["add", "-A"],
                ["-c", "user.email=t@example.invalid", "-c", "user.name=T",
                 "commit", "-q", "-m", "record"],
            ):
                subprocess.run(["git", "-C", str(project), *argv], check=True,
                               capture_output=True, text=True)

            result = self.publish(
                registry, store, "--node", "user-mac", "--reporter", "cp-workspace",
                "--classification", "restricted",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)

            self.assertEqual(receipt["incoming"]["attribution"], "user-mac/cp-workspace")
            self.assertEqual(
                receipt["incoming"]["path"],
                "collectives/workspace/projects/alpha/incoming/user-mac/cp-workspace/"
                f"rooms/review/{receipt['publish_id']}.json",
            )

            manifest = json.loads((store / receipt["incoming"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["node"], "user-mac")
            self.assertEqual(manifest["principal"], "cp-workspace")
            self.assertEqual(manifest["reporter"], "cp-workspace")
            self.assertEqual(manifest["classification"], "restricted")
            self.assertEqual(manifest["checkpoint_at"], "2026-08-20T10:00:00Z")
            self.assertEqual(
                manifest["source_record"], str((project / "sessions" / "review.md").resolve())
            )
            self.assertTrue(manifest["git"]["available"])
            self.assertEqual(len(manifest["git"]["commit"]), 40)
            self.assertFalse(manifest["git"]["dirty"])
            self.assertIn("not review", manifest["authority"])

            for item in manifest["items"]:
                self.assertTrue(item["captured_at"].endswith("Z"))
                self.assertEqual(len(item["sha256"]), 64)
                self.assertGreater(item["size"], 0)

            # The project-wide manifest mirror holds the same immutable entry.
            mirror = json.loads((store / receipt["manifest"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(mirror["publish_id"], manifest["publish_id"])


class CentralStoreRejectionTests(CentralStoreHarness, unittest.TestCase):
    def test_traversal_in_the_identity_is_rejected_as_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)

            result = self.run_store(
                registry, "publish", "alpha/..",
                "--store-root", str(store), "--principal", "cp-workspace",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("path separator or traversal", result.stderr)
            self.assertFalse((store / "collectives").exists())

    def test_uppercase_and_windows_reserved_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)

            upper = self.run_store(
                registry, "publish", "Alpha/review",
                "--store-root", str(store), "--principal", "cp-workspace",
            )
            self.assertEqual(upper.returncode, 1)
            self.assertIn("must be lowercase", upper.stderr)

            reserved = self.run_store(
                registry, "publish", "alpha/aux",
                "--store-root", str(store), "--principal", "cp-workspace",
            )
            self.assertEqual(reserved.returncode, 1)
            self.assertIn("Windows reserved name", reserved.stderr)

            collective = self.publish(registry, store, "--collective", "Workspace")
            self.assertEqual(collective.returncode, 1)
            self.assertIn("collective must be lowercase", collective.stderr)
            self.assertFalse((store / "collectives").exists())

    def test_a_case_colliding_central_room_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)
            colliding = (
                store / "collectives" / "workspace" / "projects" / "alpha" / "rooms" / "REVIEW"
            )
            colliding.mkdir(parents=True)

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Case collision", result.stderr)
            self.assertFalse((self.room(store) / "current.json").exists())

    def test_a_symlinked_record_escaping_the_project_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            outside = base / "outside.md"
            outside.write_text((project / "sessions" / "review.md").read_text(encoding="utf-8"),
                               encoding="utf-8")
            record = project / "sessions" / "review.md"
            record.unlink()
            record.symlink_to(outside)

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 1)
            self.assertIn("symlinked session record", result.stderr)
            self.assertFalse((store / "collectives").exists())

    def test_secret_shaped_content_fails_closed_without_echoing_the_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            # AWS's own documented example key id: inert, but exactly the shape.
            secret = "AKIA" + "IOSFODNN7EXAMPLE"
            (project / "sessions" / "review.md").write_text(
                RECORD.format(summary=f"Deploy key {secret} was pasted here by mistake."),
                encoding="utf-8",
            )

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 1)
            self.assertIn("aws-access-key-id", result.stderr)
            self.assertIn("line 23", result.stderr)
            self.assertNotIn(secret, result.stderr)
            self.assertNotIn(secret, result.stdout)
            self.assertFalse((store / "collectives").exists())

    def test_a_transcript_shaped_origins_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            payload = json.loads(json.dumps(ORIGINS))
            payload["origins"][0]["transcript"] = "user: hello"
            (project / "sessions" / "review.origins.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )

            result = self.publish(registry, store)
            self.assertEqual(result.returncode, 1)
            self.assertIn("body-shaped field", result.stderr)
            self.assertIn("pointers only", result.stderr)


class CentralStorePullTests(CentralStoreHarness, unittest.TestCase):
    def pull(self, registry: Path, store: Path, cache: Path, *extra: str):
        return self.run_store(
            registry, "pull", "alpha/review", "--store-root", str(store),
            "--cache-dir", str(cache), "--json", *extra,
        )

    def test_pull_verifies_every_digest_and_writes_only_to_the_read_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            published = json.loads(self.publish(registry, store).stdout)
            record = project / "sessions" / "review.md"
            before = record.read_bytes()
            cache = base / "cache"

            result = self.pull(registry, store, cache)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)

            self.assertEqual(receipt["record_sha256"], published["current"]["record_sha256"])
            self.assertEqual(receipt["node"], "user-mac")
            self.assertEqual(receipt["principal"], "cp-workspace")
            self.assertEqual(receipt["reporter"], "cp-workspace")
            self.assertEqual(receipt["checkpoint_at"], "2026-08-20T10:00:00Z")
            self.assertTrue(all(item["verified"] for item in receipt["items"]))
            self.assertEqual(len(receipt["items"]), 2)
            self.assertEqual(receipt["reentry"], "semantic-reentry")
            self.assertFalse(receipt["native_transcript_resumption"])
            self.assertIn("Read cache only", receipt["cache"])

            cached = cache / "workspace" / "alpha" / "review"
            self.assertEqual((cached / "review.md").read_bytes(), before)
            self.assertTrue((cached / "review.origins.json").is_file())
            self.assertTrue((cached / "pull-receipt.json").is_file())

            # The caller's local writer copy is never touched.
            self.assertEqual(record.read_bytes(), before)
            self.assertNotEqual(cached.resolve(), record.parent.resolve())

    def test_pull_refuses_a_tampered_object_and_caches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)
            published = json.loads(self.publish(registry, store).stdout)
            digest = published["current"]["record_sha256"]
            objects = store / "collectives" / "workspace" / "objects" / "sha256"
            blob = objects / digest[:2] / digest[2:4] / digest
            blob.write_text("tampered\n", encoding="utf-8")
            cache = base / "cache"

            result = self.pull(registry, store, cache)
            self.assertEqual(result.returncode, 1)
            self.assertIn("failed verification", result.stderr)
            self.assertIn("quarantine review", result.stderr)
            self.assertFalse(cache.exists())

    def test_pull_refuses_a_cache_that_overlaps_a_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, store = self.build_world(base)
            self.publish(registry, store)

            result = self.pull(registry, store, project / "sessions")
            self.assertEqual(result.returncode, 1)
            self.assertIn("overlaps the registered project root", result.stderr)
            self.assertIn("never allowed to touch a writer copy", result.stderr)

    def test_pull_without_a_central_current_is_a_clear_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)
            result = self.pull(registry, store, base / "cache")
            self.assertEqual(result.returncode, 1)
            self.assertIn("No central current pointer for alpha/review", result.stderr)


class CentralStoreCatalogTests(CentralStoreHarness, unittest.TestCase):
    def test_catalog_emits_attributed_current_room_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)
            published = json.loads(self.publish(registry, store).stdout)

            lines = self.run_store(registry, "catalog", "--store-root", str(store))
            self.assertEqual(lines.returncode, 0, lines.stderr)
            entries = [json.loads(line) for line in lines.stdout.splitlines()]
            self.assertEqual(len(entries), 1)
            entry = entries[0]

            self.assertEqual(entry["identity"], "alpha/review")
            self.assertEqual(entry["state"], "current")
            self.assertEqual(entry["node"], "user-mac")
            self.assertEqual(entry["principal"], "cp-workspace")
            self.assertEqual(entry["reporter"], "cp-workspace")
            self.assertEqual(entry["classification"], "internal")
            self.assertEqual(entry["record_sha256"], published["current"]["record_sha256"])
            self.assertEqual(entry["origins_sha256"], published["items"][1]["sha256"])
            self.assertEqual(entry["checkpoint_at"], "2026-08-20T10:00:00Z")
            self.assertGreater(entry["checkpoint_age_seconds"], 0)
            self.assertTrue(entry["version_present"])
            self.assertTrue(entry["record_object_present"])
            self.assertFalse(entry["verified"])
            self.assertFalse(entry["native_transcript_resumption"])
            self.assertEqual(
                entry["central_current"],
                "collectives/workspace/projects/alpha/rooms/review/current.json",
            )
            self.assertIn("reported context", entry["reported_context"])

            document = self.run_store(
                registry, "catalog", "--store-root", str(store), "--project", "alpha", "--json"
            )
            self.assertEqual(document.returncode, 0, document.stderr)
            payload = json.loads(document.stdout)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["collective"], "workspace")
            self.assertEqual(payload["entries"][0]["identity"], "alpha/review")

    def test_catalog_omits_rooms_that_were_never_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, store = self.build_world(base)
            self.publish(registry, store, "--no-promote")

            result = self.run_store(registry, "catalog", "--store-root", str(store))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
