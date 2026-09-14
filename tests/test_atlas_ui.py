from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("atlas_ui", REPO_ROOT / "tools" / "atlas_ui.py")
assert SPEC and SPEC.loader
atlas_ui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = atlas_ui
SPEC.loader.exec_module(atlas_ui)


class AtlasStudioTests(unittest.TestCase):
    def make_service(self, base: Path) -> atlas_ui.AtlasService:
        project = base / "Projects" / "orbit"
        sessions = project / "sessions"
        sessions.mkdir(parents=True)
        source = base / "orbit-source"
        source.mkdir()
        (project / "project.json").write_text(json.dumps({
            "schema_version": 1,
            "id": "orbit",
            "name": "ORBIT",
            "status": "active",
            "visibility": "private",
            "aliases": ["orbit-v3"],
            "keywords": ["ORBIT v3", "mockups"],
            "sources": [{
                "path": str(source),
                "kind": "canonical-repo",
                "role": "application source",
                "search": True,
            }],
        }), encoding="utf-8")
        record = sessions / "v3-ux.md"
        record.write_text(
            "---\nproject: \"orbit\"\nsession: \"v3-ux\"\ntitle: \"ORBIT v3 UX\"\n"
            "date: \"2026-08-15\"\nupdated: \"2026-08-15\"\nstatus: \"active\"\n"
            "origins: \"v3-ux.origins.json\"\n---\n\n"
            "# ORBIT v3 UX\n\n## Summary\n\nDesigned the v3 adaptive workspace.\n\n"
            "## Open loops\n\n- Validate it.\n",
            encoding="utf-8",
        )
        (sessions / "v3-ux.origins.json").write_text(json.dumps({
            "schema_version": 1,
            "identity": "orbit/v3-ux",
            "origins": [{
                "runtime": "codex",
                "node": "test-node",
                "session_id": "019fff1a-e2e8-7e63-a9b1-9c9038eb8026",
                "primary": True,
            }],
        }), encoding="utf-8")
        registry = base / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "node_id": "test-node",
            "projects": [{
                "id": "orbit", "name": "ORBIT", "root": str(project),
                "aliases": ["orbit-v3"],
            }],
        }), encoding="utf-8")
        index_file = base / "index.jsonl"
        index_file.write_text("", encoding="utf-8")
        return atlas_ui.AtlasService(registry, index_file, base / "atlas-state.json")

    @staticmethod
    def attention_request() -> dict[str, object]:
        return {
            "project_id": "orbit",
            "session_id": "orbit/v3-ux",
            "blocked_dependency_key": "navigation-labels",
            "requested_by": "Mira",
            "source": {"runtime": "Codex", "room": "v3 UX", "machine": "test-node"},
            "interruption_class": "needs_now",
            "type": "Product decision",
            "priority": "Blocking one workstream",
            "need_from_aaron": "Choose the navigation labels.",
            "why_aaron": "This is a product-language decision with two viable choices.",
            "paused": "Final mobile navigation labels.",
            "still_continuing": "Accessibility and tablet layout.",
            "if_no_action": "Only the final mobile labels remain paused.",
            "options": [{"id": "A", "label": "Home · Records"}, {"id": "B", "label": "Today · History"}],
            "recommendation": "Option A",
            "evidence": [],
            "after_response": "Mira will apply the choice and continue the focused build.",
            "authoritative_action": {"mode": "inline", "system": None, "destination_ref": None, "completion_signal": "Source acknowledgement"},
        }

    def test_bootstrap_exposes_project_owned_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            payload = service.bootstrap("csrf-test")
            self.assertEqual(payload["node"], "test-node")
            self.assertEqual(payload["projects"][0]["session_count"], 1)
            session = payload["sessions"][0]
            self.assertEqual(session["identity"], "orbit/v3-ux")
            self.assertEqual(session["summary"], "Designed the v3 adaptive workspace.")
            self.assertEqual(session["created_at"], "2026-08-15T00:00:00Z")
            self.assertEqual(session["meaningful_activity_at"], "2026-08-15T00:00:00Z")
            self.assertTrue(session["exact_resume"])
            self.assertEqual(payload["csrf"], "csrf-test")

    def test_terminal_allowlist_accepts_only_codex_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = {
                "command": [
                    "/opt/bin/codex", "resume",
                    "019fff1a-e2e8-7e63-a9b1-9c9038eb8026",
                ],
                "saved_cwd": temporary,
            }
            command, cwd = atlas_ui.TerminalManager.validate_resume(payload)
            self.assertEqual(command[1], "resume")
            self.assertEqual(cwd, str(Path(temporary).resolve()))

            payload["command"] = ["/bin/sh", "-c", "anything"]
            with self.assertRaises(atlas_ui.UiError):
                atlas_ui.TerminalManager.validate_resume(payload)

    def test_session_and_project_detail_remain_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            session = service.session("orbit/v3-ux")
            self.assertIn("## Summary", session["record"])
            project = service.project("orbit")
            self.assertEqual(project["sources"][0]["kind"], "canonical-repo")
            self.assertEqual(project["sessions"][0]["identity"], "orbit/v3-ux")

    def test_workspace_persists_views_and_close_does_not_stop_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            service = self.make_service(base)
            initial = service.bootstrap("csrf")
            view = initial["workspace"]["windows"][0]["views"][0]
            service.workspace_action("close", {"view_id": view["id"]})

            restored = atlas_ui.AtlasService(
                base / "registry.json", base / "index.jsonl", base / "atlas-state.json"
            ).bootstrap("csrf")
            self.assertEqual(restored["workspace"]["windows"][0]["views"], [])
            self.assertEqual(len(restored["sessions"]), 1)
            self.assertTrue(restored["sessions"][0]["exact_resume"])

    def test_open_existing_focuses_without_duplicate_and_control_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            first = service.bootstrap("csrf")["workspace"]["windows"][0]["views"][0]
            opened = service.workspace_action("open", {"session_id": "orbit/v3-ux"})
            self.assertTrue(opened["existing"])
            self.assertEqual(opened["view"]["id"], first["id"])
            duplicate = service.workspace_action("open", {"session_id": "orbit/v3-ux", "duplicate": True})
            service.workspace_action("control", {"view_id": duplicate["view"]["id"]})
            views = service.state()["workspace"]["windows"][0]["views"]
            controls = [view["control_state"] for view in views if view["session_id"] == "orbit/v3-ux"]
            self.assertEqual(controls.count("controlled"), 1)

    def test_detach_attach_and_undo_are_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            view = service.bootstrap("csrf")["workspace"]["windows"][0]["views"][0]
            detached = service.workspace_action("detach", {"view_id": view["id"]})
            self.assertEqual(detached["window"]["views"][0]["id"], view["id"])
            service.workspace_action("attach", {"view_id": view["id"]})
            self.assertEqual(len(service.state()["workspace"]["windows"]), 1)
            undone = service.workspace_action("undo", {})
            self.assertEqual(undone["undone"], "Attach view")
            self.assertEqual(len(service.state()["workspace"]["windows"]), 2)

    def test_attention_deduplicates_and_requires_source_ack_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            first = service.attention_action("create", self.attention_request())["attention"]
            duplicate = self.attention_request()
            duplicate["requested_by"] = "Local"
            second = service.attention_action("create", duplicate)["attention"]
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["reporters"], ["Mira", "Local"])

            service.attention_action("open", {"id": first["id"]})
            answered = service.attention_action("answer", {
                "id": first["id"], "answer": {"answer_type": "option", "value": "A"},
            })["attention"]
            self.assertEqual(answered["lifecycle_state"], "answered")
            with self.assertRaises(atlas_ui.UiError):
                service.attention_action("resolve", {"id": first["id"]})
            service.attention_action("acknowledge", {"id": first["id"]})
            resolved = service.attention_action("resolve", {"id": first["id"]})["attention"]
            self.assertEqual(resolved["lifecycle_state"], "resolved")

    def test_external_attention_cannot_fake_inline_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            request = self.attention_request()
            request["authoritative_action"] = {
                "mode": "external", "system": "GitHub", "destination_ref": "https://example.invalid/action", "completion_signal": "Webhook",
            }
            item = service.attention_action("create", request)["attention"]
            with self.assertRaises(atlas_ui.UiError):
                service.attention_action("answer", {"id": item["id"], "answer": {"answer_type": "option", "value": "approve"}})
            completed = service.attention_action("answer", {
                "id": item["id"], "answer": {"answer_type": "external_completed", "value": "authoritative signal received"},
            })["attention"]
            self.assertEqual(completed["lifecycle_state"], "answered")

    def test_command_lists_project_sessions_and_opens_existing_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            listed = service.command("Show me all Orbit sessions")
            self.assertEqual(listed["kind"], "session_list")
            self.assertEqual(listed["sessions"][0]["identity"], "orbit/v3-ux")
            opened = service.command("Open Orbit v3 UX")
            self.assertEqual(opened["kind"], "session_opened")
            self.assertTrue(opened["existing"])

    def test_multi_project_command_composes_current_sessions_dynamically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self.make_service(base)
            project = base / "Projects" / "demo-pet"
            (project / "sessions").mkdir(parents=True)
            (project / "project.json").write_text(json.dumps({
                "schema_version": 1, "id": "demo-pet", "name": "Demo-pet",
                "status": "active", "visibility": "private", "keywords": [], "sources": [],
            }), encoding="utf-8")
            (project / "sessions" / "current.md").write_text(
                "---\nproject: demo-pet\nsession: current\ntitle: Current Demo-pet build\n"
                "date: 2026-08-15\nupdated: 2026-08-15\nstatus: active\n---\n\n"
                "# Current\n\n## Summary\n\nBuilding Demo-pet.\n",
                encoding="utf-8",
            )
            registry = json.loads((base / "registry.json").read_text(encoding="utf-8"))
            registry["projects"].append({"id": "demo-pet", "name": "Demo-pet", "root": str(project), "aliases": []})
            (base / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
            service = atlas_ui.AtlasService(base / "registry.json", base / "index.jsonl", base / "atlas-state.json")

            composed = service.command("Give me current Orbit and current Demo-pet")
            self.assertEqual(composed["kind"], "workspace_composed")
            self.assertEqual({item["project"] for item in composed["sessions"]}, {"orbit", "demo-pet"})
            views = service.state()["workspace"]["windows"][0]["views"]
            self.assertEqual({item["session_id"] for item in views}, {"orbit/v3-ux", "demo-pet/current"})

    def test_search_cache_reads_pointer_index_and_refreshes_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            service = self.make_service(base)
            first = {
                "name": "atlas-patent-map.md", "path": str(base / "atlas-patent-map.md"),
                "projects": ["orbit"], "project_names": ["ORBIT"],
                "keywords": ["patent evaluation"], "kinds": ["loose-project-document"],
                "roles": ["research"], "size": 12,
            }
            (base / "index.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")
            found = service.search("atlas patent map")
            self.assertEqual(found[0]["name"], "atlas-patent-map.md")

            second = {
                "name": "claimant-records.txt", "path": str(base / "claimant-records.txt"),
                "projects": ["orbit"], "project_names": ["ORBIT"],
                "keywords": ["claimant controlled records"], "kinds": ["artifact"],
                "roles": ["governance"], "size": 18,
            }
            (base / "index.jsonl").write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
            )
            refreshed = service.search("claimant records")
            self.assertEqual(refreshed[0]["name"], "claimant-records.txt")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_terminal_launch_is_isolated_and_capturable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_codex = base / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "print('fake codex ready', flush=True)\n"
                "time.sleep(5)\n",
                encoding="utf-8",
            )
            fake_vault = base / "session-vault"
            fake_vault.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                f"print(json.dumps({{'command': [{str(fake_codex)!r}, 'resume', "
                "'019fff1a-e2e8-7e63-a9b1-9c9038eb8026'], "
                f"'saved_cwd': {str(base)!r}}}))\n",
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o700)
            os.chmod(fake_vault, 0o700)
            manager = atlas_ui.TerminalManager(base / "registry.json", fake_vault)
            launched = manager.launch("orbit/v3-ux", "ORBIT v3 UX")
            try:
                snapshot = launched
                for _ in range(12):
                    snapshot = manager.snapshot(launched["panel_id"])
                    if "fake codex ready" in snapshot["content"]:
                        break
                    time.sleep(0.1)
                self.assertTrue(snapshot["alive"])
                self.assertIn("fake codex ready", snapshot["content"])
            finally:
                manager.close(launched["panel_id"])


if __name__ == "__main__":
    unittest.main()
