"""Cold-agent bootstrap: `atlas resume project/session`.

A cold agent knows one thing — a project/session. These tests assert that one
command turns that into a complete, honest receipt without ever reading the
home directory, and that it refuses to install a second writer.
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

CODEX_SESSION = "6f1d0f6e-6f1d-4f6e-8f6e-6f1d0f6e6f1d"

FAKE_TELL = r'''#!/usr/bin/env python3
"""A stand-in for the Tell client: records argv and returns a scripted receipt."""

import json
import os
import sys

log = os.environ.get("FAKE_TELL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")

if os.environ.get("FAKE_TELL_MODE") == "broken":
    print("tell: registry unavailable", file=sys.stderr)
    sys.exit(4)

print(json.dumps({"session_id": "s_deadbeefdeadbeef", "label": "registered"}))
sys.exit(0)
'''


def record(project: str, session: str, checkpoint: bool = True) -> str:
    head = (
        "---\n"
        f"project: {project}\nsession: {session}\ntitle: {session.title()} room\n"
        "date: 2026-08-20\nupdated: 2026-08-20\nstatus: active\n"
        f"keywords: {project}, {session}\n"
    )
    if checkpoint:
        head += (
            "checkpoint_schema: 1\n"
            'checkpoint_at: "2026-08-20T10:00:00Z"\n'
            "checkpoint_node: user-mac\ncheckpoint_reporter: local-workspace\n"
            "checkpoint_state: working\ncheckpoint_need: none\n"
            'checkpoint_next: "Build the bootstrap slice"\n'
        )
    return (
        head + "---\n\n"
        f"# {project}/{session}\n\n## Summary\n\nPortable re-entry context.\n"
    )


class BootstrapHarness:
    def build_world(self, base: Path, **config_extra) -> tuple[Path, Path, Path]:
        project = base / "alpha"
        (project / "sessions").mkdir(parents=True)
        (project / "project.json").write_text(json.dumps({
            "schema_version": 1, "id": "alpha", "name": "Alpha", "status": "active",
            "visibility": "private", "domain": "demo-network",
            "aliases": [], "keywords": [], "sources": [],
        }), encoding="utf-8")
        for session in ("review", "builder"):
            (project / "sessions" / f"{session}.md").write_text(
                record("alpha", session), encoding="utf-8"
            )
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "user-mac",
            "projects": [{"id": "alpha", "name": "Alpha", "root": str(project), "aliases": []}],
        }), encoding="utf-8")
        (base / "work").mkdir()
        config = base / "bootstrap.json"
        payload = {
            "schema": "atlas.bootstrap.v1",
            "node": "user-mac",
            "collective": "workspace",
            "registry": str(registry),
            "work_root": str(base / "work"),
            "principal": "cc-workspace",
        }
        payload.update({
            key: str(value) if isinstance(value, Path) else value
            for key, value in config_extra.items()
        })
        config.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return project, registry, config

    def resume(
        self,
        config: Path,
        *args: str,
        environment: dict[str, str] | None = None,
        home: str = "/nonexistent-home",
    ) -> tuple[int, dict, str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env.pop("ATLAS_STORE_CACHE", None)
        env.pop("ATLAS_PRINCIPAL", None)
        # A cold agent must never need its home directory to bootstrap.
        env["HOME"] = home
        env["ATLAS_BOOTSTRAP_CONFIG"] = str(config)
        if environment:
            env.update(environment)
        done = subprocess.run(
            [sys.executable, str(ATLAS), "resume", *args],
            capture_output=True, text=True, check=False, env=env,
        )
        try:
            payload = json.loads(done.stdout)
        except json.JSONDecodeError:  # pragma: no cover - surfaced by the assertion
            raise AssertionError(f"resume did not emit JSON: {done.stdout!r} {done.stderr!r}")
        return done.returncode, payload, done.stderr

    def run_tool(self, tool: Path, registry: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env.pop("ATLAS_STORE_CACHE", None)
        return subprocess.run(
            [sys.executable, str(tool), "--registry", str(registry), *args],
            capture_output=True, text=True, check=False, env=env,
        )


class ColdResumeTests(BootstrapHarness, unittest.TestCase):
    def test_a_cold_observer_resumes_deterministically_without_a_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, _, config = self.build_world(base)

            code, payload, _ = self.resume(config, "alpha/review", "--runtime", "claude")
            self.assertEqual(code, 0)
            self.assertEqual(payload["schema"], "atlas.bootstrap.receipt.v1")
            self.assertEqual(payload["identity"], "alpha/review")
            self.assertEqual(payload["node"], "user-mac")
            self.assertEqual(payload["collective"], "workspace")
            self.assertEqual(payload["principal"], "cc-workspace")
            self.assertEqual(payload["principal_source"], "node-config")
            self.assertEqual(payload["runtime"], "claude")
            self.assertEqual(payload["domain"], "demo-network")
            self.assertEqual(payload["source"], "local-session-vault")
            self.assertEqual(payload["work_root"]["path"], str(base / "work"))
            self.assertEqual(payload["memory"]["checkpoint"]["reporter"], "local-workspace")
            self.assertIsNotNone(payload["memory"]["checkpoint_age_seconds"])
            self.assertEqual(payload["admission"]["role"], "observer")
            self.assertFalse(payload["admission"]["writer"])
            self.assertIn("not approval", payload["authority"])

    def test_the_principal_comes_from_the_launcher_and_is_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, _, config = self.build_world(base)

            code, payload, _ = self.resume(config, "alpha/review", "--principal", "pane-workspace")
            self.assertEqual(payload["principal"], "pane-workspace")
            self.assertEqual(payload["principal_source"], "argument")

            code, payload, _ = self.resume(
                config, "alpha/review", environment={"ATLAS_PRINCIPAL": "agent3-workspace"}
            )
            self.assertEqual(payload["principal"], "agent3-workspace")
            self.assertEqual(payload["principal_source"], "environment")

            bare = json.loads(config.read_text(encoding="utf-8"))
            bare.pop("principal")
            config.write_text(json.dumps(bare), encoding="utf-8")
            code, payload, stderr = self.resume(config, "alpha/review")
            self.assertEqual(code, 1)
            self.assertEqual(payload["state"], "error")
            self.assertIn("No enrolled principal", payload["error"])
            self.assertIn("never inferred from hostname", payload["error"])

    def test_a_missing_or_invalid_configuration_fails_closed_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, _, config = self.build_world(base)

            code, payload, _ = self.resume(base / "absent.json", "alpha/review")
            self.assertEqual(code, 1)
            self.assertIn(str(base / "absent.json"), payload["error"])
            self.assertIn("never searches the home directory", payload["error"])

            for field in ("node", "registry", "work_root"):
                with self.subTest(field=field):
                    broken = json.loads(config.read_text(encoding="utf-8"))
                    broken.pop(field)
                    partial = base / f"missing-{field}.json"
                    partial.write_text(json.dumps(broken), encoding="utf-8")
                    code, payload, _ = self.resume(partial, "alpha/review")
                    self.assertEqual(code, 1)
                    self.assertIn(f"missing '{field}'", payload["error"])
                    self.assertIn(str(partial), payload["error"])

            code, payload, _ = self.resume(config, "alpha/absent")
            self.assertEqual(code, 1)
            self.assertIn("alpha/absent is not registered locally", payload["error"])
            self.assertIn("'store_root'", payload["error"])

            # An unsafe identity and an unreadable registry are bootstrap errors,
            # not tracebacks, and never fall through to a directory search.
            for identity in ("alpha/../../etc", "alpha", "Alpha/Review"):
                with self.subTest(identity=identity):
                    code, payload, _ = self.resume(config, identity)
                    self.assertEqual(code, 1)
                    self.assertEqual(payload["state"], "error")

            broken = base / "broken-registry.json"
            broken.write_text("{ not json", encoding="utf-8")
            spoiled = json.loads(config.read_text(encoding="utf-8"))
            spoiled["registry"] = str(broken)
            invalid = base / "invalid-registry.json"
            invalid.write_text(json.dumps(spoiled), encoding="utf-8")
            code, payload, _ = self.resume(invalid, "alpha/review")
            self.assertEqual(code, 1)
            self.assertIn("Invalid Atlas registry JSON", payload["error"])

    def test_exact_native_and_semantic_reentry_are_labelled_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, config = self.build_world(base)

            _, portable, _ = self.resume(config, "alpha/review")
            self.assertEqual(portable["mode"], "semantic-re-entry")
            self.assertFalse(portable["native_transcript_resumption"])
            self.assertIsNone(portable["native_origin"])

            self.run_tool(
                VAULT, registry, "link", "alpha/builder", "--runtime", "codex",
                "--node", "user-mac", "--session-id", CODEX_SESSION, "--primary",
            )
            _, native, _ = self.resume(config, "alpha/builder")
            self.assertEqual(native["mode"], "exact-native-session")
            self.assertTrue(native["native_transcript_resumption"])
            self.assertEqual(native["native_origin"]["session_id"], CODEX_SESSION)
            self.assertEqual(native["native_origin"]["node"], "user-mac")
            # Bootstrap reports the pointer; it does not read runtime state.
            self.assertFalse(native["native_origin_verified"])
            self.assertIn("session-vault resume alpha/builder", native["next_actions"])

            # An origin on another node is not this node's exact session.
            self.run_tool(
                VAULT, registry, "link", "alpha/review", "--runtime", "codex",
                "--node", "server", "--session-id", CODEX_SESSION, "--primary",
            )
            _, remote_origin, _ = self.resume(config, "alpha/review")
            self.assertEqual(remote_origin["mode"], "semantic-re-entry")
            self.assertFalse(remote_origin["native_transcript_resumption"])


class WriterAdmissionTests(BootstrapHarness, unittest.TestCase):
    def own(self, registry: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_tool(ATLAS, registry, "own", *args)

    def test_bootstrap_never_installs_a_writer_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, _, config = self.build_world(base)

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "coordinator", "--principal", "local-workspace"
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["state"], "refused")
            self.assertEqual(payload["admission"]["code"], "unowned-room")
            self.assertIn("--claim", payload["admission"]["reason"])
            self.assertFalse((project / "ownership").exists())
            # A refusal still returns the retrieved memory.
            self.assertEqual(payload["memory"]["checkpoint"]["state"], "working")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "coordinator",
                "--principal", "local-workspace", "--claim", "--slice", "coordination",
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["admission"]["writer"])
            self.assertTrue(payload["admission"]["claimed"])
            self.assertEqual(payload["admission"]["ownership"]["owner"], "local-workspace")
            self.assertEqual(payload["admission"]["ownership"]["epoch"], 1)

    def test_a_second_writer_is_refused_and_told_who_holds_the_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, config = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "coordinator",
                "--principal", "cc-workspace", "--claim",
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "writer-conflict")
            self.assertEqual(payload["admission"]["ownership"]["owner"], "local-workspace")
            self.assertEqual(payload["admission"]["ownership"]["epoch"], 1)
            self.assertIn("atlas own offer alpha/review", payload["admission"]["reason"])
            self.assertIsNotNone(payload["admission"]["ownership"]["held_age_seconds"])

            # An observer is always welcome, and is told who writes.
            code, payload, _ = self.resume(
                config, "alpha/review", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 0)
            self.assertFalse(payload["admission"]["writer"])
            self.assertEqual(payload["admission"]["ownership"]["owner"], "local-workspace")
            self.assertIn("local-workspace", payload["admission"]["reason"])

    def test_a_builder_must_use_its_own_distinct_child_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, config = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "builder", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "builder-needs-child-room")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "builder", "--principal", "cc-workspace",
                "--child-session", "review",
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "builder-child-collides-with-parent")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "builder", "--principal", "cc-workspace",
                "--child-session", "absent", "--claim",
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "room-record-missing")
            self.assertIn("session-vault start alpha/absent", payload["admission"]["reason"])

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "builder", "--principal", "cc-workspace",
                "--child-session", "builder", "--claim", "--slice", "project-domain",
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["admission"]["room"], "alpha/builder")
            self.assertEqual(payload["admission"]["parent_room"], "alpha/review")
            self.assertEqual(payload["admission"]["slice"], "project-domain")
            self.assertTrue(payload["admission"]["writer"])
            self.assertEqual(
                payload["work_root"]["bounded_path"], str(base / "work" / "builder")
            )
            # The parent coordination room still belongs to Local.
            self.assertEqual(payload["ownership"]["room"]["owner"], "cc-workspace")
            parent = json.loads(
                self.own(registry, "show", "alpha/review", "--json").stdout
            )
            self.assertEqual(parent["owner"], "local-workspace")

    def test_takeover_happens_only_through_an_accepted_transfer_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, registry, config = self.build_world(base)
            self.own(registry, "claim", "alpha/review", "--principal", "local-workspace")

            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "takeover", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "writer-conflict")

            self.own(
                registry, "offer", "alpha/review", "--principal", "local-workspace",
                "--to", "cc-workspace",
            )
            # An open offer is not a transfer.
            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "takeover", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "writer-conflict")

            self.own(registry, "accept", "alpha/review", "--principal", "cc-workspace")
            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "takeover", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["admission"]["writer"])
            self.assertEqual(
                payload["admission"]["takeover_receipt"],
                "ownership/receipts/sessions/review/000003-accept.json",
            )

            # Hand-editing the state breaks the chain and the takeover is refused.
            state = project / "ownership" / "sessions" / "review.json"
            edited = json.loads(state.read_text(encoding="utf-8"))
            edited["owner_node"] = "somewhere-else"
            state.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            code, payload, _ = self.resume(
                config, "alpha/review", "--role", "takeover", "--principal", "cc-workspace"
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["admission"]["code"], "takeover-receipt-invalid")
            self.assertIn("edited outside the CLI", payload["admission"]["reason"])


class RemoteAndRoutingTests(BootstrapHarness, unittest.TestCase):
    def publish(self, base: Path) -> Path:
        """Publish alpha/review from a first node, then forget the project."""
        _, registry, _ = self.build_world(base)
        store = base / "store"
        store.mkdir()
        done = self.run_tool(
            VAULT, registry, "store", "publish", "alpha/review",
            "--store-root", str(store), "--principal", "local-workspace", "--json",
        )
        assert done.returncode == 0, done.stderr
        return store

    def cold_node(self, base: Path, store: Path, **extra) -> Path:
        registry = base / "cold-registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "node_id": "server", "projects": [],
        }), encoding="utf-8")
        (base / "cold-work").mkdir()
        (base / "cache").mkdir()
        config = base / "cold-bootstrap.json"
        payload = {
            "schema": "atlas.bootstrap.v1",
            "node": "server",
            "collective": "workspace",
            "registry": str(registry),
            "work_root": str(base / "cold-work"),
            "store_root": str(store),
            "cache_root": str(base / "cache"),
            "principal": "cc-workspace",
        }
        payload.update({
            key: str(value) if isinstance(value, Path) else value
            for key, value in extra.items()
        })
        config.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return config

    def test_a_remote_only_record_pulls_by_exact_identity_and_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = self.publish(base)
            config = self.cold_node(base, store)

            code, payload, _ = self.resume(config, "alpha/review")
            self.assertEqual(code, 0)
            self.assertEqual(payload["source"], "central-store")
            self.assertEqual(payload["mode"], "semantic-re-entry")
            self.assertFalse(payload["native_transcript_resumption"])
            # The domain came across in the publish manifest snapshot.
            self.assertEqual(payload["domain"], "demo-network")
            self.assertIn("central publish manifest", payload["domain_source"])
            self.assertEqual(payload["central"]["catalog"]["principal"], "local-workspace")
            self.assertEqual(payload["central"]["catalog"]["node"], "user-mac")
            self.assertTrue(payload["memory"]["verified"])
            self.assertTrue(
                Path(payload["memory"]["path"]).is_relative_to((base / "cache").resolve())
            )
            self.assertEqual(
                payload["memory"]["sha256"], payload["central"]["pull"]["record_sha256"]
            )
            self.assertFalse(payload["ownership"]["available"])

            # A room that is neither local nor central is named, not searched for.
            code, missing, _ = self.resume(config, "alpha/nowhere")
            self.assertEqual(code, 1)
            self.assertIn("neither a registered local record", missing["error"])
            self.assertIn("nor a current room", missing["error"])

            for role in ("coordinator", "builder", "takeover"):
                with self.subTest(role=role):
                    code, payload, _ = self.resume(
                        config, "alpha/review", "--role", role,
                        "--child-session", "builder", "--claim",
                    )
                    self.assertEqual(code, 3)
                    self.assertEqual(payload["admission"]["code"], "remote-only-record")

    def test_a_central_lookup_without_a_configured_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = self.publish(base)
            config = self.cold_node(base, store)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload.pop("cache_root")
            config.write_text(json.dumps(payload), encoding="utf-8")

            code, receipt, _ = self.resume(config, "alpha/review")
            self.assertEqual(code, 1)
            self.assertIn("'cache_root'", receipt["error"])
            self.assertIn("never guesses one under the home directory", receipt["error"])

    def test_tell_binding_is_reported_and_degrades_without_losing_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, registry, _ = self.build_world(base)
            tell_root = base / "telld"
            (tell_root / "src" / "tell").mkdir(parents=True)
            (tell_root / "src" / "tell" / "cli.py").write_text(FAKE_TELL, encoding="utf-8")
            log = base / "tell-argv.log"
            config = base / "routed.json"
            config.write_text(json.dumps({
                "schema": "atlas.bootstrap.v1",
                "node": "user-mac",
                "collective": "workspace",
                "registry": str(registry),
                "work_root": str(base / "work"),
                "principal": "cc-workspace",
                "bind_tell": True,
                "tell_root": str(tell_root),
                "tell_ttl": 600,
                "route_state_dir": str(base / "routes"),
            }), encoding="utf-8")

            code, payload, _ = self.resume(
                config, "alpha/review", environment={"FAKE_TELL_LOG": str(log)}
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["route"]["state"], "bound")
            self.assertEqual(payload["route"]["session_id"], "s_deadbeefdeadbeef")
            self.assertEqual(payload["route"]["ttl_seconds"], 600)
            self.assertEqual(payload["degraded"], [])
            argv = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(argv[0], [
                "--as", "cc-workspace", "session", "register", "--vault", "alpha/review",
                "--collective", "workspace", "--ttl", "600", "--label", "alpha/review",
            ])
            # Route state stays outside every project root.
            state_files = sorted((base / "routes").glob("*.json"))
            self.assertEqual(len(state_files), 1)
            self.assertTrue(json.loads(state_files[0].read_text(encoding="utf-8"))["rebuildable"])

            code, payload, _ = self.resume(
                config, "alpha/review",
                environment={"FAKE_TELL_LOG": str(log), "FAKE_TELL_MODE": "broken"},
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["route"]["state"], "unbound")
            self.assertTrue(payload["route"]["memory_retained"])
            self.assertTrue(any(item.startswith("tell-unbound") for item in payload["degraded"]))
            # Degraded routing never costs the agent its retrieved memory.
            self.assertEqual(payload["memory"]["checkpoint"]["state"], "working")
            self.assertEqual(payload["admission"]["state"], "admitted")


if __name__ == "__main__":
    unittest.main()
