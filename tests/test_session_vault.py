from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT = REPO_ROOT / "tools" / "session_vault.py"


class VaultCliHarness:
    """Shared subprocess harness for the Session Vault CLI."""

    def run_vault(
        self,
        registry: Path,
        *args: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        if environment:
            env.update(environment)
        return subprocess.run(
            [sys.executable, str(VAULT), "--registry", str(registry), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def write_project(self, base: Path, project_id: str, body: str) -> Path:
        root = base / project_id
        sessions = root / "sessions"
        sessions.mkdir(parents=True)
        (root / "project.json").write_text(json.dumps({
            "schema_version": 1,
            "id": project_id,
            "name": project_id.title(),
            "status": "active",
            "visibility": "private",
            "aliases": [],
            "keywords": [],
            "sources": [],
        }), encoding="utf-8")
        (sessions / "review.md").write_text(
            "---\n"
            f"project: {project_id}\n"
            "session: review\n"
            f"title: {project_id.title()} review\n"
            "date: 2026-08-14\n"
            "updated: 2026-08-14\n"
            "status: active\n"
            f"aliases: {'WELLHELD' if project_id == 'alpha' else ''}\n"
            f"keywords: {body}\n"
            "---\n\n"
            f"# {project_id}/review\n\n## Summary\n\n{body}.\n",
            encoding="utf-8",
        )
        return root


class SessionVaultCliTests(VaultCliHarness, unittest.TestCase):
    def test_composite_identity_allows_duplicate_session_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "claimant controlled records")
            beta = self.write_project(base, "beta", "orbit v3 ux design")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [
                    {"id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": []},
                    {"id": "beta", "name": "Beta", "root": str(beta), "aliases": []},
                ],
            }), encoding="utf-8")

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn("2 record(s)", doctor.stdout)
            self.assertIn("Storage:", doctor.stdout)

            ambiguous = self.run_vault(registry, "show", "review")
            self.assertEqual(ambiguous.returncode, 1)
            self.assertIn("alpha/review", ambiguous.stderr)
            self.assertIn("beta/review", ambiguous.stderr)

            qualified = self.run_vault(registry, "show", "beta/review")
            self.assertEqual(qualified.returncode, 0, qualified.stderr)
            self.assertIn("orbit v3 ux design", qualified.stdout)

            legacy_alias = self.run_vault(registry, "path", "WELLHELD")
            self.assertEqual(legacy_alias.returncode, 0, legacy_alias.stderr)
            self.assertTrue(legacy_alias.stdout.strip().endswith("alpha/sessions/review.md"))

            searched = self.run_vault(
                registry, "search", "claimant-controlled records", "--json"
            )
            self.assertEqual(searched.returncode, 0, searched.stderr)
            results = json.loads(searched.stdout)
            self.assertEqual(results[0]["identity"], "alpha/review")

            portable_only = self.run_vault(
                registry, "resume", "alpha/review", "--json"
            )
            self.assertEqual(portable_only.returncode, 1)
            self.assertIn("No native origin", portable_only.stderr)

    def test_native_codex_origin_is_captured_and_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "demo-pet"
            (root / "sessions").mkdir(parents=True)
            (root / "project.json").write_text(json.dumps({
                "schema_version": 1,
                "id": "demo-pet",
                "name": "Demo-pet",
                "status": "active",
                "visibility": "private",
                "aliases": [],
                "keywords": [],
                "sources": [],
            }), encoding="utf-8")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-mac",
                "projects": [{
                    "id": "demo-pet",
                    "name": "Demo-pet",
                    "root": str(root),
                    "aliases": [],
                }],
            }), encoding="utf-8")
            codex_home = base / "codex-home"
            session_id = "019fff1a-e2e8-7e63-a9b1-9c9038eb8026"
            transcript = (
                codex_home / "sessions" / "2026" / "08" / "15"
                / f"rollout-test-{session_id}.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}\n", encoding="utf-8")
            environment = {
                "CODEX_HOME": str(codex_home),
                "CODEX_THREAD_ID": session_id,
            }

            started = self.run_vault(
                registry,
                "start",
                "demo-pet/v3-builder",
                "--title",
                "Demo-pet v3 builder",
                "--summary",
                "Near-scratch rebuild",
                "--json",
                environment=environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            result = json.loads(started.stdout)
            self.assertEqual(result["linked_origin"]["session_id"], session_id)
            self.assertTrue(result["linked_origin"]["primary"])

            record = root / "sessions" / "v3-builder.md"
            origins_path = root / "sessions" / "v3-builder.origins.json"
            self.assertIn('origins: "v3-builder.origins.json"', record.read_text())
            origins = json.loads(origins_path.read_text())
            self.assertEqual(len(origins["origins"]), 1)
            self.assertEqual(origins["origins"][0]["node"], "test-mac")

            repeated = self.run_vault(
                registry,
                "link",
                "demo-pet/v3-builder",
                "--session-id",
                session_id,
                "--primary",
                "--json",
                environment=environment,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                len(json.loads(origins_path.read_text())["origins"]), 1
            )

            named = self.run_vault(
                registry,
                "link",
                "demo-pet/v3-builder",
                "--session-name",
                "named-session",
                "--json",
                environment=environment,
            )
            self.assertEqual(named.returncode, 0, named.stderr)
            named_origin = json.loads(named.stdout)["origin"]
            self.assertEqual(named_origin["session_id"], "")
            self.assertEqual(named_origin["session_name"], "named-session")

            resumed = self.run_vault(
                registry,
                "resume",
                "demo-pet/v3-builder",
                "--json",
                environment=environment,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resolution = json.loads(resumed.stdout)
            self.assertEqual(resolution["mode"], "exact-native-session")
            self.assertEqual(resolution["session_id"], session_id)
            self.assertTrue(resolution["transcript_available"])
            self.assertEqual(resolution["command"][-2:], ["resume", session_id])

            doctor = self.run_vault(
                registry, "doctor", environment=environment
            )
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertNotIn("portable-only", doctor.stdout)

    def test_start_uses_project_alias_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "universal-builder"
            (root / "sessions").mkdir(parents=True)
            (root / "project.json").write_text(json.dumps({
                "schema_version": 1,
                "id": "universal-builder",
                "name": "Universal Builder",
                "status": "active",
                "visibility": "private",
                "aliases": ["builder", "builders"],
                "keywords": [],
                "sources": [],
            }), encoding="utf-8")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [{
                    "id": "universal-builder",
                    "name": "Universal Builder",
                    "root": str(root),
                    "aliases": ["builder", "builders"],
                }],
            }), encoding="utf-8")

            started = self.run_vault(
                registry,
                "start",
                "builders/orbit-builder",
                "--title",
                "Orbit builder",
                "--summary",
                "Building Orbit v3 UX flows",
                "--json",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            self.assertTrue(payload["created"])
            self.assertEqual(payload["identity"], "universal-builder/orbit-builder")
            record = Path(payload["path"])
            self.assertIn("Building Orbit v3 UX flows", record.read_text())

            repeated = self.run_vault(
                registry,
                "start",
                "universal-builder/orbit-builder",
                "--summary",
                "must not replace the original",
                "--json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["created"])
            self.assertNotIn("must not replace", record.read_text())

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)

    def test_checkpoint_is_machine_readable_and_optimistic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "shared builder memory")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-mac",
                "projects": [
                    {"id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": []},
                ],
            }), encoding="utf-8")

            checkpoint = self.run_vault(
                registry,
                "checkpoint",
                "alpha/review",
                "--reporter",
                "builder-workspace",
                "--state",
                "needs-human",
                "--need",
                "review",
                "--next",
                "Review exact commit abc123 before integration",
                "--at",
                "2026-08-18T01:02:03-07:00",
                "--evidence",
                "commit:abc123",
                "--json",
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
            payload = json.loads(checkpoint.stdout)
            self.assertEqual(payload["checkpoint"]["schema_version"], 1)
            self.assertEqual(payload["checkpoint"]["at"], "2026-08-18T08:02:03Z")
            self.assertEqual(payload["checkpoint"]["node"], "test-mac")
            self.assertEqual(payload["checkpoint"]["need"], "review")

            record = alpha / "sessions" / "review.md"
            text = record.read_text(encoding="utf-8")
            self.assertIn("checkpoint_schema: 1", text)
            self.assertIn('checkpoint_reporter: "builder-workspace"', text)
            self.assertIn("## Current checkpoint", text)
            self.assertIn("commit:abc123", text)

            listing = self.run_vault(registry, "list", "--project", "alpha", "--json")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            listed = json.loads(listing.stdout)[0]["checkpoint"]
            self.assertEqual(listed["state"], "needs-human")
            self.assertEqual(listed["next"], "Review exact commit abc123 before integration")

            replacement = self.run_vault(
                registry,
                "checkpoint",
                "alpha/review",
                "--reporter",
                "builder-workspace",
                "--state",
                "working",
                "--need",
                "none",
                "--next",
                "Fix: #249 then resume",
                "--at",
                "2026-08-18T08:03:03Z",
                "--note",
                "## heading-like note",
                "--evidence",
                "commit:def456",
                "--if-checkpoint-at",
                "2026-08-18T08:02:03Z",
                "--json",
            )
            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced = json.loads(replacement.stdout)["checkpoint"]
            self.assertEqual(replaced["state"], "working")
            self.assertEqual(replaced["next"], "Fix: #249 then resume")
            text = record.read_text(encoding="utf-8")
            self.assertIn('checkpoint_next: "Fix: #249 then resume"', text)
            self.assertIn("Note: ## heading-like note", text)
            self.assertIn("## Checkpoint history", text)
            self.assertIn("2026-08-18T08:02:03Z", text)
            self.assertIn("commit:abc123", text)
            self.assertIn("commit:def456", text)

            before = record.read_bytes()
            stale = self.run_vault(
                registry,
                "checkpoint",
                "alpha/review",
                "--reporter",
                "builder-workspace",
                "--state",
                "working",
                "--need",
                "none",
                "--next",
                "Continue implementation",
                "--if-checkpoint-at",
                "2026-08-18T00:00:00Z",
            )
            self.assertEqual(stale.returncode, 1)
            self.assertIn("Checkpoint changed", stale.stderr)
            self.assertEqual(record.read_bytes(), before)

            not_newer = self.run_vault(
                registry,
                "checkpoint",
                "alpha/review",
                "--reporter",
                "builder-workspace",
                "--state",
                "working",
                "--need",
                "none",
                "--next",
                "Attempt same-instant replacement",
                "--at",
                "2026-08-18T08:03:03Z",
                "--if-checkpoint-at",
                "2026-08-18T08:03:03Z",
            )
            self.assertEqual(not_newer.returncode, 1)
            self.assertIn("must be newer", not_newer.stderr)
            self.assertEqual(record.read_bytes(), before)

            invalid = self.run_vault(
                registry,
                "checkpoint",
                "alpha/review",
                "--reporter",
                "builder-workspace",
                "--state",
                "working",
                "--need",
                "review",
                "--next",
                "Contradictory current state",
            )
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("allows checkpoint_need: none", invalid.stderr)
            self.assertEqual(record.read_bytes(), before)

            unqualified = self.run_vault(
                registry,
                "checkpoint",
                "review",
                "--reporter",
                "builder-workspace",
                "--state",
                "working",
                "--need",
                "none",
                "--next",
                "Must not mutate without project identity",
            )
            self.assertEqual(unqualified.returncode, 1)
            self.assertIn("composite identity", unqualified.stderr)
            self.assertEqual(record.read_bytes(), before)

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)

            (alpha / "sessions" / "review.origins.json").write_text(
                "{broken", encoding="utf-8"
            )
            resilient = self.run_vault(
                registry, "list", "--project", "alpha", "--json"
            )
            self.assertEqual(resilient.returncode, 0, resilient.stderr)
            projection = json.loads(resilient.stdout)[0]
            self.assertIsNone(projection["native_origins"])
            self.assertEqual(projection["origins_status"], "invalid")

    def checkpoint(
        self,
        registry: Path,
        identity: str,
        state: str,
        need: str,
        next_action: str,
        at: str,
        evidence: str,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_vault(
            registry, "checkpoint", identity,
            "--reporter", "builder-workspace",
            "--state", state, "--need", need,
            "--next", next_action, "--at", at,
            "--evidence", evidence, "--json",
        )

    def test_attention_prefers_the_current_checkpoint_over_lifecycle_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "derived attention state")
            (alpha / "sessions" / "audit.md").write_text(
                "---\n"
                "project: alpha\n"
                "session: audit\n"
                "title: Alpha audit\n"
                "date: 2026-08-14\n"
                "updated: 2026-08-14\n"
                "status: validation\n"
                "keywords: derived attention state\n"
                "---\n\n"
                "# alpha/audit\n\n## Summary\n\nAwaiting validation.\n",
                encoding="utf-8",
            )
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-mac",
                "projects": [
                    {"id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": []},
                ],
            }), encoding="utf-8")
            record = alpha / "sessions" / "review.md"

            def attention(identity: str) -> dict:
                listing = self.run_vault(registry, "list", "--json")
                self.assertEqual(listing.returncode, 0, listing.stderr)
                entries = {
                    entry["identity"]: entry for entry in json.loads(listing.stdout)
                }
                return entries[identity]

            before = record.read_bytes()
            # No checkpoint: the long-lived lifecycle status is the only signal.
            entry = attention("alpha/review")
            self.assertEqual(entry["attention"]["state"], "none")
            self.assertEqual(entry["attention"]["source"], "status")
            self.assertEqual(entry["attention"]["lifecycle_status"], "active")
            self.assertEqual(entry["attention"]["rank"], 0)
            self.assertEqual(
                attention("alpha/audit")["attention"]["state"], "review_ready"
            )
            # Reading never mutates a record.
            self.assertEqual(record.read_bytes(), before)

            for state, need, at, expected, rank in (
                ("needs-human", "approval", "2026-08-18T01:00:00Z", "needs_now", 3),
                ("needs-human", "review", "2026-08-18T02:00:00Z", "review_ready", 1),
                ("blocked", "external", "2026-08-18T03:00:00Z", "needs_soon", 2),
                ("complete", "none", "2026-08-18T04:00:00Z", "none", 0),
            ):
                written = self.checkpoint(
                    registry, "alpha/review", state, need,
                    f"Next after {state}", at, f"commit:{at}",
                )
                self.assertEqual(written.returncode, 0, written.stderr)
                entry = attention("alpha/review")
                self.assertEqual(entry["attention"]["state"], expected)
                self.assertEqual(entry["attention"]["rank"], rank)
                self.assertEqual(entry["attention"]["source"], "checkpoint")
                self.assertEqual(entry["attention"]["basis"], f"checkpoint {state}/{need}")
                self.assertEqual(entry["attention"]["checkpoint_at"], at)
                # The documented distinction survives: the lifecycle status is
                # still `active` and the checkpoint is still reported verbatim.
                self.assertEqual(entry["status"], "active")
                self.assertEqual(entry["attention"]["lifecycle_status"], "active")
                self.assertEqual(entry["checkpoint"]["state"], state)

            searched = self.run_vault(
                registry, "search", "derived attention state", "--json"
            )
            self.assertEqual(searched.returncode, 0, searched.stderr)
            found = {
                entry["identity"]: entry for entry in json.loads(searched.stdout)
            }
            self.assertEqual(found["alpha/review"]["attention"]["state"], "none")
            self.assertEqual(found["alpha/audit"]["attention"]["state"], "review_ready")
            self.assertEqual(found["alpha/audit"]["attention"]["source"], "status")

    def test_archive_history_moves_only_history_and_never_deletes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "checkpoint history archive")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "test-mac",
                "projects": [
                    {"id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": []},
                ],
            }), encoding="utf-8")
            record = alpha / "sessions" / "review.md"
            history_dir = alpha / "sessions" / "history"

            empty = self.run_vault(registry, "archive-history", "alpha/review")
            self.assertEqual(empty.returncode, 1)
            self.assertIn("No '## Checkpoint history' section", empty.stderr)
            self.assertFalse(history_dir.exists())

            for index, at in enumerate(
                ("2026-08-18T01:00:00Z", "2026-08-18T02:00:00Z", "2026-08-18T03:00:00Z"),
                start=1,
            ):
                written = self.checkpoint(
                    registry, "alpha/review", "working", "none",
                    f"Step {index}", at, f"commit:step{index}",
                )
                self.assertEqual(written.returncode, 0, written.stderr)

            before = record.read_bytes()
            dry = self.run_vault(
                registry, "archive-history", "alpha/review", "--dry-run", "--json"
            )
            self.assertEqual(dry.returncode, 0, dry.stderr)
            planned = json.loads(dry.stdout)
            self.assertFalse(planned["archived"])
            self.assertTrue(planned["dry_run"])
            self.assertEqual(planned["entries"], 2)
            self.assertEqual(planned["archive_version"], 1)
            self.assertEqual(record.read_bytes(), before)
            self.assertFalse(history_dir.exists())

            archived = self.run_vault(
                registry, "archive-history", "alpha/review", "--json"
            )
            self.assertEqual(archived.returncode, 0, archived.stderr)
            payload = json.loads(archived.stdout)
            self.assertTrue(payload["archived"])
            self.assertTrue(payload["current_checkpoint_preserved"])
            self.assertEqual(payload["entries"], 2)
            archive_path = Path(payload["archive_path"])
            self.assertEqual(archive_path.parent, history_dir.resolve())
            self.assertEqual(archive_path.name, "review.checkpoint-history.001.md")

            archive_text = archive_path.read_text(encoding="utf-8")
            self.assertIn("kind: checkpoint-history-archive", archive_text)
            self.assertIn("archive_of: alpha/review", archive_text)
            self.assertIn(f"source_sha256: {payload['source_sha256']}", archive_text)
            self.assertIn("## Checkpoint history", archive_text)
            # Every historical byte survives the move.
            self.assertIn("commit:step1", archive_text)
            self.assertIn("commit:step2", archive_text)
            self.assertIn("2026-08-18T01:00:00Z", archive_text)

            text = record.read_text(encoding="utf-8")
            self.assertNotIn("## Checkpoint history", text)
            self.assertNotIn("commit:step1", text)
            self.assertNotIn("commit:step2", text)
            # Only that section moved.
            self.assertIn("## Current checkpoint", text)
            self.assertIn("commit:step3", text)
            self.assertIn("## Summary", text)
            self.assertIn('checkpoint_at: "2026-08-18T03:00:00Z"', text)
            self.assertIn("checkpoint_schema: 1", text)

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            listing = self.run_vault(registry, "list", "--json")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            listed = json.loads(listing.stdout)
            # A nested archive is never loaded as a session record.
            self.assertEqual([entry["identity"] for entry in listed], ["alpha/review"])
            self.assertEqual(listed[0]["checkpoint"]["at"], "2026-08-18T03:00:00Z")

            self.assertEqual(
                self.checkpoint(
                    registry, "alpha/review", "working", "none", "Step 4",
                    "2026-08-18T04:00:00Z", "commit:step4",
                ).returncode,
                0,
            )
            again = self.run_vault(
                registry, "archive-history", "alpha/review", "--json"
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            second = json.loads(again.stdout)
            self.assertEqual(second["archive_version"], 2)
            self.assertEqual(second["entries"], 1)
            self.assertTrue(
                Path(second["archive_path"]).name
                == "review.checkpoint-history.002.md"
            )
            # Nothing was overwritten and no history was lost.
            self.assertTrue(archive_path.is_file())
            entries = sum(
                len(re.findall(r"(?m)^### ", path.read_text(encoding="utf-8")))
                for path in sorted(history_dir.glob("*.md"))
            )
            self.assertEqual(entries, 3)

            exhausted = self.run_vault(registry, "archive-history", "alpha/review")
            self.assertEqual(exhausted.returncode, 1)
            self.assertIn("No '## Checkpoint history' section", exhausted.stderr)

            unqualified = self.run_vault(registry, "archive-history", "review")
            self.assertEqual(unqualified.returncode, 1)
            self.assertIn("composite identity", unqualified.stderr)

            malformed = record.read_text(encoding="utf-8").replace(
                'checkpoint_state: "working"', 'checkpoint_state: "invented"'
            )
            record.write_text(malformed, encoding="utf-8")
            refused = self.run_vault(registry, "archive-history", "alpha/review")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("malformed checkpoint", refused.stderr)

    def test_doctor_surfaces_shared_origins_and_checkpoint_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "shared memory alpha")
            beta = self.write_project(base, "beta", "shared memory beta")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "local",
                "projects": [
                    {"id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": []},
                    {"id": "beta", "name": "Beta", "root": str(beta), "aliases": []},
                ],
            }), encoding="utf-8")
            session_id = "019fff1a-e2e8-7e63-a9b1-9c9038eb8026"
            for project_id, root in (("alpha", alpha), ("beta", beta)):
                record = root / "sessions" / "review.md"
                record.write_text(
                    record.read_text(encoding="utf-8").replace(
                        "status: active\n", "status: active\norigins: review.origins.json\n"
                    ),
                    encoding="utf-8",
                )
                (root / "sessions" / "review.origins.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "project": project_id,
                        "session": "review",
                        "origins": [{
                            "runtime": "codex",
                            "node": "remote-node",
                            "session_id": session_id,
                            "session_name": "",
                            "primary": True,
                        }],
                    }),
                    encoding="utf-8",
                )
            alpha_record = alpha / "sessions" / "review.md"
            checkpoint_count = 20
            with alpha_record.open("a", encoding="utf-8") as handle:
                handle.write("\n## Checkpoint history\n\n")
                for index in range(checkpoint_count):
                    handle.write(
                        f"### 2026-08-18T00:00:{index:02d}Z — builder — working/none\n\n"
                        "Prior checkpoint.\n\n"
                    )

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertIn(
                f"native origin reused by 2 record(s): codex {session_id}",
                doctor.stdout,
            )
            self.assertIn(
                f"alpha/review has {checkpoint_count} archived checkpoints",
                doctor.stdout,
            )

    def test_doctor_rejects_orphan_duplicate_origins_and_bad_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "structural checks")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "node_id": "local",
                "projects": [{
                    "id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": [],
                }],
            }), encoding="utf-8")
            record = alpha / "sessions" / "review.md"
            record.write_text(
                record.read_text(encoding="utf-8")
                .replace("date: 2026-08-14\n", "date: last-tuesday\n")
                .replace(
                    "status: active\n",
                    "status: invented\n"
                    "checkpoint_schema: 1\n"
                    "checkpoint_at: 2026-08-18T00:00:00Z\n"
                    "checkpoint_node: local\n"
                    "checkpoint_reporter: builder\n"
                    "checkpoint_state: complete\n"
                    "checkpoint_need: none\n"
                    "checkpoint_next: Finish structural checks\n"
                    "origins: review.origins.json\n",
                ),
                encoding="utf-8",
            )
            origin = {
                "runtime": "codex",
                "node": "remote-node",
                "session_id": "019fff1a-e2e8-7e63-a9b1-9c9038eb8026",
                "session_name": "",
                "primary": False,
            }
            (alpha / "sessions" / "review.origins.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "project": "alpha",
                    "session": "review",
                    "origins": [origin, {**origin, "node": "renamed-node"}],
                }),
                encoding="utf-8",
            )
            (alpha / "sessions" / "lost.origins.json").write_text(
                json.dumps({"schema_version": 1, "origins": []}), encoding="utf-8"
            )

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 1)
            self.assertIn("unsupported status 'invented'", doctor.stderr)
            self.assertIn("date must be an ISO date", doctor.stderr)
            self.assertIn("require a Current checkpoint section", doctor.stderr)
            self.assertIn("duplicate native origin", doctor.stderr)
            self.assertIn("orphan origins file", doctor.stderr)

    def test_unreadable_record_does_not_break_list_or_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            alpha = self.write_project(base, "alpha", "readable record")
            registry = base / "registry.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [{
                    "id": "alpha", "name": "Alpha", "root": str(alpha), "aliases": [],
                }],
            }), encoding="utf-8")
            (alpha / "sessions" / "bad.md").write_bytes(b"\xff\xfe")

            listing = self.run_vault(registry, "list", "--json")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertEqual(len(json.loads(listing.stdout)), 1)
            self.assertIn("cannot read session record", listing.stderr)
            self.assertNotIn("Traceback", listing.stderr)

            doctor = self.run_vault(registry, "doctor")
            self.assertEqual(doctor.returncode, 1)
            self.assertIn("cannot read session record", doctor.stderr)
            self.assertNotIn("Traceback", doctor.stderr)


if __name__ == "__main__":
    unittest.main()


FAKE_TELL = r'''#!/usr/bin/env python3
"""A stand-in for the Tell client: records argv and returns scripted receipts."""

import json
import os
import sys

log = os.environ.get("FAKE_TELL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")

argv = sys.argv[1:]
mode = os.environ.get("FAKE_TELL_MODE", "ok")
if mode == "broken":
    print("tell: registry unavailable", file=sys.stderr)
    sys.exit(4)

if argv[:1] == ["--as"]:
    argv = argv[2:]

if argv[:2] == ["session", "register"]:
    if mode == "garbage":
        print("registered, boss")
        sys.exit(0)
    print(json.dumps({
        "session_id": os.environ.get("FAKE_TELL_SESSION", "s_deadbeefdeadbeef"),
        "label": "registered",
    }, indent=2))
    sys.exit(0)
if argv[:2] == ["session", "heartbeat"]:
    sys.exit(int(os.environ.get("FAKE_TELL_BEAT_EXIT", "0")))
if argv[:2] == ["session", "end"]:
    sys.exit(int(os.environ.get("FAKE_TELL_END_EXIT", "0")))
if argv[:2] == ["session", "check"]:
    state = os.environ.get("FAKE_TELL_STATE", "live")
    print(json.dumps({"session_id": argv[-1], "state": state}, indent=2))
    sys.exit(0 if state == "live" else 2)
if argv[:2] == ["session", "list"]:
    print(os.environ.get("FAKE_TELL_LIST", "[]"))
    sys.exit(0)

print("fake tell: unsupported command", file=sys.stderr)
sys.exit(3)
'''


class SessionVaultTellRouteTests(VaultCliHarness, unittest.TestCase):
    """The Tell boundary is exercised through a fake client, never the daemon."""

    def build_world(self, base: Path) -> tuple[Path, Path, Path, Path]:
        project = self.write_project(base, "alpha", "tell route binding")
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "projects": [
                {"id": "alpha", "name": "Alpha", "root": str(project), "aliases": []},
            ],
        }), encoding="utf-8")
        tell_root = base / "telld"
        client = tell_root / "src" / "tell" / "cli.py"
        client.parent.mkdir(parents=True)
        client.write_text(FAKE_TELL, encoding="utf-8")
        return project, registry, tell_root, base / "tell-argv.log"

    def route_environment(self, base: Path, log: Path, **extra: str) -> dict[str, str]:
        environment = {
            "SESSION_VAULT_ROUTE_STATE": str(base / "runtime"),
            "SESSION_VAULT_TELL_PRINCIPAL": "cp-workspace",
            "FAKE_TELL_LOG": str(log),
            "TELL_SESSION_ID": "",
        }
        environment.update(extra)
        return environment

    def run_route(
        self,
        registry: Path,
        base: Path,
        log: Path,
        tell_root: Path,
        *args: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.route_environment(base, log, **(environment or {}))
        return self.run_vault(
            registry, "route", *args, "--tell-root", str(tell_root), environment=env
        )

    def calls(self, log: Path) -> list[list[str]]:
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def test_bind_passes_route_arguments_safely_and_parses_the_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, tell_root, log = self.build_world(base)
            record = project / "sessions" / "review.md"
            before = record.read_text(encoding="utf-8")

            bind = self.run_route(
                registry, base, log, tell_root, "bind", "alpha/review",
                "--collective", "workspace", "--ttl", "600",
                "--wake", '{"path": "/tmp/wake", "kind": "touch"}',
                "--label", "cp macbook window", "--json",
            )
            self.assertEqual(bind.returncode, 0, bind.stderr)
            payload = json.loads(bind.stdout)
            self.assertEqual(payload["session_id"], "s_deadbeefdeadbeef")
            self.assertEqual(payload["identity"], "alpha/review")
            self.assertEqual(payload["state"], "bound")
            self.assertEqual(payload["collective"], "workspace")
            self.assertEqual(payload["ttl_seconds"], 600)
            self.assertEqual(payload["wake_kind"], "touch")
            self.assertIn("not authority", payload["authority"])
            # The wake spec may name local paths or argv; only its kind escapes.
            self.assertNotIn("/tmp/wake", bind.stdout)

            self.assertEqual(self.calls(log), [[
                "--as", "cp-workspace", "session", "register",
                "--vault", "alpha/review", "--collective", "workspace",
                "--ttl", "600", "--label", "cp macbook window",
                "--wake", '{"kind":"touch","path":"/tmp/wake"}',
            ]])

            # Durable project memory is untouched: no TTL, lease, or session id.
            self.assertEqual(record.read_text(encoding="utf-8"), before)
            self.assertFalse((project / "sessions" / "review.origins.json").exists())
            state_files = sorted((base / "runtime").glob("*.json"))
            self.assertEqual(len(state_files), 1)
            runtime = json.loads(state_files[0].read_text(encoding="utf-8"))
            self.assertTrue(runtime["rebuildable"])
            self.assertEqual(runtime["kind"], "tell-route-runtime")
            self.assertEqual(runtime["source_of_truth"], "tell-session-registry")
            self.assertEqual(runtime["session_id"], "s_deadbeefdeadbeef")

    def test_beat_check_and_end_reuse_the_bound_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)
            self.assertEqual(
                self.run_route(registry, base, log, tell_root, "bind", "alpha/review").returncode,
                0,
            )

            beat = self.run_route(registry, base, log, tell_root, "beat", "alpha/review", "--json")
            self.assertEqual(beat.returncode, 0, beat.stderr)
            self.assertEqual(json.loads(beat.stdout)["state"], "live")

            check = self.run_route(registry, base, log, tell_root, "check", "alpha/review", "--json")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(json.loads(check.stdout)["state"], "live")
            self.assertEqual(json.loads(check.stdout)["session_id_source"], "runtime-state")

            end = self.run_route(registry, base, log, tell_root, "end", "alpha/review", "--json")
            self.assertEqual(end.returncode, 0, end.stderr)
            self.assertEqual(json.loads(end.stdout)["state"], "ended")
            self.assertEqual(sorted((base / "runtime").glob("*.json")), [])

            actions = [call[3] for call in self.calls(log)]
            self.assertEqual(actions, ["register", "heartbeat", "check", "end"])
            for call in self.calls(log)[1:]:
                self.assertIn("s_deadbeefdeadbeef", call)

            after_end = self.run_route(
                registry, base, log, tell_root, "beat", "alpha/review", "--json"
            )
            self.assertEqual(after_end.returncode, 2)
            self.assertEqual(json.loads(after_end.stdout)["state"], "unbound")

    def test_check_reports_the_registry_verdict_not_the_local_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)
            self.run_route(registry, base, log, tell_root, "bind", "alpha/review")

            check = self.run_route(
                registry, base, log, tell_root, "check", "alpha/review", "--json",
                environment={"FAKE_TELL_STATE": "absent"},
            )
            self.assertEqual(check.returncode, 2)
            self.assertEqual(json.loads(check.stdout)["state"], "absent")

            beat = self.run_route(
                registry, base, log, tell_root, "beat", "alpha/review", "--json",
                environment={"FAKE_TELL_BEAT_EXIT": "2"},
            )
            self.assertEqual(beat.returncode, 2)
            self.assertEqual(json.loads(beat.stdout)["state"], "stale")

    def test_check_rebuilds_a_lost_session_id_from_the_tell_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)
            listing = json.dumps([
                {
                    "session_id": "s_otherwindow00001",
                    "vault": "alpha/other",
                    "collective": "workspace",
                },
                {
                    "session_id": "s_rebuilt000000001",
                    "vault": "alpha/review",
                    "collective": "workspace",
                },
            ])

            check = self.run_route(
                registry, base, log, tell_root, "check", "alpha/review",
                "--collective", "workspace", "--json",
                environment={"FAKE_TELL_LIST": listing},
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            payload = json.loads(check.stdout)
            self.assertEqual(payload["session_id"], "s_rebuilt000000001")
            self.assertEqual(payload["session_id_source"], "tell-registry")
            self.assertEqual([call[3] for call in self.calls(log)], ["list", "check"])

            ambiguous = self.run_route(
                registry, base, log, tell_root, "check", "alpha/review", "--json",
                environment={
                    "FAKE_TELL_LIST": json.dumps([
                        {"session_id": "s_one0000000000001", "vault": "alpha/review"},
                        {"session_id": "s_two0000000000002", "vault": "alpha/review"},
                    ]),
                    "SESSION_VAULT_ROUTE_STATE": str(base / "empty"),
                },
            )
            self.assertEqual(ambiguous.returncode, 2)
            self.assertEqual(json.loads(ambiguous.stdout)["state"], "unbound")

    def test_prune_removes_only_cache_entries_tell_proves_are_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, tell_root, log = self.build_world(base)
            runtime = base / "runtime"
            for session in ("handoff", "stale"):
                started = self.run_vault(
                    registry, "start", f"alpha/{session}", "--no-link-current"
                )
                self.assertEqual(started.returncode, 0, started.stderr)
            bound = {
                "review": "s_reviewlive00001",
                "handoff": "s_handoffgone002",
                "stale": "s_staleexpired03",
            }
            for session, session_id in bound.items():
                result = self.run_route(
                    registry, base, log, tell_root, "bind", f"alpha/{session}",
                    environment={"FAKE_TELL_SESSION": session_id},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(sorted(runtime.glob("*.json"))), 3)

            # Cache files this principal must never judge or touch.
            (runtime / "alpha__foreign__keel-workspace.json").write_text(json.dumps({
                "schema_version": 1,
                "kind": "tell-route-runtime",
                "rebuildable": True,
                "identity": "alpha/foreign",
                "principal": "local-workspace",
                "session_id": "s_foreignwindow01",
            }), encoding="utf-8")
            (runtime / "not-a-route.json").write_text("{broken", encoding="utf-8")

            listing = json.dumps([
                {
                    "session_id": bound["review"],
                    "vault": "alpha/review",
                    "collective": "workspace",
                    "state": "live",
                },
                {
                    "session_id": bound["stale"],
                    "vault": "alpha/stale",
                    "collective": "workspace",
                    "state": "expired",
                },
            ])

            before = sorted(path.name for path in runtime.glob("*.json"))
            dry = self.run_route(
                registry, base, log, tell_root, "prune", "--dry-run", "--json",
                environment={"FAKE_TELL_LIST": listing},
            )
            self.assertEqual(dry.returncode, 0, dry.stderr)
            planned = json.loads(dry.stdout)
            self.assertTrue(planned["dry_run"])
            self.assertFalse(planned["tell_mutated"])
            self.assertEqual(
                sorted(item["identity"] for item in planned["pruned"]),
                ["alpha/handoff", "alpha/stale"],
            )
            self.assertTrue(all(not item["removed"] for item in planned["pruned"]))
            self.assertEqual(sorted(path.name for path in runtime.glob("*.json")), before)

            pruned = self.run_route(
                registry, base, log, tell_root, "prune", "--json",
                environment={"FAKE_TELL_LIST": listing},
            )
            self.assertEqual(pruned.returncode, 0, pruned.stderr)
            payload = json.loads(pruned.stdout)
            reasons = {item["identity"]: item["reason"] for item in payload["pruned"]}
            self.assertEqual(reasons["alpha/handoff"], "absent-from-tell-registry")
            self.assertEqual(reasons["alpha/stale"], "stale-registry-state-expired")
            self.assertTrue(all(item["removed"] for item in payload["pruned"]))
            self.assertEqual(
                [item["identity"] for item in payload["kept"]], ["alpha/review"]
            )
            skipped = {item["reason"] for item in payload["skipped"]}
            self.assertEqual(
                skipped, {"owned-by-another-principal", "not-a-route-cache-file"}
            )
            self.assertEqual(
                sorted(path.name for path in runtime.glob("*.json")),
                [
                    "alpha__foreign__keel-workspace.json",
                    "alpha__review__cp-workspace.json",
                    "not-a-route.json",
                ],
            )

            # Prune only ever reads the Tell registry.
            prune_calls = [call for call in self.calls(log) if call[3] == "list"]
            self.assertEqual(len(prune_calls), 2)
            for call in prune_calls:
                self.assertEqual(call, ["--as", "cp-workspace", "session", "list"])
            self.assertNotIn(
                "end", [call[3] for call in self.calls(log) if call[2] == "session"]
            )

            # An unreadable registry is not proof of absence.
            unknown = self.run_route(
                registry, base, log, tell_root, "prune", "--json",
                environment={"FAKE_TELL_LIST": ""},
            )
            self.assertEqual(unknown.returncode, 2)
            unknown_payload = json.loads(unknown.stdout)
            self.assertEqual(unknown_payload["state"], "unknown")
            self.assertEqual(unknown_payload["reason"], "tell-registry-unreadable")
            self.assertEqual(unknown_payload["pruned"], [])
            self.assertEqual(len(sorted(runtime.glob("*.json"))), 3)

            # Durable project memory is untouched throughout.
            self.assertEqual(
                sorted(path.name for path in (project / "sessions").glob("*")),
                ["handoff.md", "review.md", "stale.md"],
            )

    def test_route_refuses_unsafe_input_before_touching_the_tell_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)

            unknown = self.run_route(registry, base, log, tell_root, "bind", "alpha/missing")
            self.assertEqual(unknown.returncode, 1)
            self.assertIn("alpha/missing", unknown.stderr)

            for arguments, needle in (
                (("--collective", "workspace; rm -rf /"), "Tell collective"),
                (("--principal", "$(whoami)"), "Tell principal"),
                (("--session-id", "not-a-session"), "Tell session id"),
                (("--ttl", "0"), "TTL"),
                (("--wake", "[]"), "JSON object"),
                (("--wake", "{}"), "kind"),
            ):
                result = self.run_route(
                    registry, base, log, tell_root, "bind", "alpha/review", *arguments
                )
                self.assertEqual(result.returncode, 1, f"{arguments}: {result.stdout}")
                self.assertIn(needle, result.stderr, arguments)

            self.assertEqual(self.calls(log), [])

    def test_route_reports_a_missing_or_failing_tell_client_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)

            missing = self.run_vault(
                registry, "route", "bind", "alpha/review",
                "--tell-root", str(base / "nowhere"),
                environment=self.route_environment(base, log),
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("Tell root not found", missing.stderr)

            empty_root = base / "bare"
            empty_root.mkdir()
            bare = self.run_vault(
                registry, "route", "bind", "alpha/review", "--tell-root", str(empty_root),
                environment=self.route_environment(base, log),
            )
            self.assertEqual(bare.returncode, 1)
            self.assertIn("src/tell/cli.py", bare.stderr)

            failing = self.run_route(
                registry, base, log, tell_root, "bind", "alpha/review",
                environment={"FAKE_TELL_MODE": "broken"},
            )
            self.assertEqual(failing.returncode, 1)
            self.assertIn("registry unavailable", failing.stderr)

            garbage = self.run_route(
                registry, base, log, tell_root, "bind", "alpha/review",
                environment={"FAKE_TELL_MODE": "garbage"},
            )
            self.assertEqual(garbage.returncode, 1)
            self.assertIn("was not JSON", garbage.stderr)

    def test_route_root_comes_from_the_environment_when_no_flag_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, tell_root, log = self.build_world(base)
            result = self.run_vault(
                registry, "route", "bind", "alpha/review", "--json",
                environment=self.route_environment(
                    base, log, SESSION_VAULT_TELL_ROOT=str(tell_root)
                ),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["tell_root"], str(tell_root))
            self.assertEqual(payload["principal"], "cp-workspace")
            self.assertEqual(payload["collective"], "workspace")
            self.assertEqual(payload["ttl_seconds"], 900)
            self.assertEqual(payload["label"], "registered")


class NativeOriginOwnershipTests(VaultCliHarness, unittest.TestCase):
    NATIVE = "019fff1a-e2e8-7e63-a9b1-9c9038eb8026"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.alpha = self.write_project(self.base, "alpha", "owner")
        self.beta = self.write_project(self.base, "beta", "capture")
        self.registry = self.base / "registry.json"
        self.registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "test-mac", "projects": [
                {"id": name, "name": name.title(), "root": str(root)}
                for name, root in [("alpha", self.alpha), ("beta", self.beta)]
            ],
        }))

    def call(self, *args, **kwargs):
        return self.run_vault(self.registry, *args, **kwargs)

    def own(self, *extra):
        result = self.call("link", "alpha/review", "--session-id", self.NATIVE, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_implicit_and_explicit_start_refuse_before_scaffolding(self):
        self.own()
        for args, env in [([], {"CODEX_THREAD_ID": self.NATIVE}),
                          (["--session-id", self.NATIVE], {})]:
            with self.subTest(args=args):
                result = self.call("start", "beta/topic", *args, environment=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("alpha/review", result.stderr)
                self.assertIn("--no-link-current", result.stderr)
                self.assertFalse((self.beta / "sessions/topic.md").exists())
                self.assertFalse((self.beta / "sessions/topic.origins.json").exists())
        portable = self.call("start", "beta/topic", "--no-link-current",
                             environment={"CODEX_THREAD_ID": self.NATIVE})
        self.assertEqual(portable.returncode, 0, portable.stderr)
        self.assertFalse((self.beta / "sessions/topic.origins.json").exists())

    def test_existing_start_and_explicit_link_leave_target_bytes_unchanged(self):
        self.own()
        record = self.beta / "sessions/review.md"
        before = record.read_bytes()
        for command in ("start", "link"):
            result = self.call(command, "beta/review", "--session-id", self.NATIVE)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(record.read_bytes(), before)
            self.assertFalse(record.with_suffix(".origins.json").exists())

    def test_existing_owner_reentry_survives_legacy_duplicate(self):
        self.own()
        source = self.alpha / "sessions/review.origins.json"
        payload = json.loads(source.read_text())
        payload["project"] = "beta"
        (self.beta / "sessions/review.origins.json").write_text(json.dumps(payload))
        for command in ("start", "link"):
            result = self.call(command, "alpha/review", "--session-id", self.NATIVE,
                               "--session-name", "optional-display-name")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(source.read_text())["origins"]), 1)
        rejected = self.call("start", "beta/new", "--session-id", self.NATIVE)
        self.assertIn("alpha/review, beta/review", rejected.stderr)

    def test_same_id_on_another_node_or_runtime_is_independent(self):
        self.own()
        for extra in [("--node", "other-node"), ("--runtime", "claude")]:
            result = self.call("link", "beta/review", "--session-id", self.NATIVE, *extra)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_named_session_ownership_is_scoped_by_node(self):
        result = self.call("link", "alpha/review", "--session-name", "my-thread")
        self.assertEqual(result.returncode, 0, result.stderr)
        denied = self.call("start", "beta/named", "--session-name", "my-thread")
        self.assertEqual(denied.returncode, 1, denied.stderr)
        allowed = self.call("start", "beta/named", "--session-name", "my-thread",
                            "--node", "other-node")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_concurrent_starts_produce_one_owner_and_no_losing_scaffolds(self):
        from concurrent.futures import ThreadPoolExecutor
        def start(index):
            project = "alpha" if index % 2 else "beta"
            return self.call("start", f"{project}/race-{index}", "--session-id", self.NATIVE)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, range(8)))
        self.assertEqual(sum(result.returncode == 0 for result in results), 1,
                         [result.stderr for result in results])
        paths = list(self.alpha.glob("sessions/race-*")) + list(self.beta.glob("sessions/race-*"))
        self.assertEqual(len(paths), 2)  # one Markdown owner and its origin sidecar
