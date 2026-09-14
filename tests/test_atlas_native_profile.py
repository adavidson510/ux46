"""Permission preservation across native resume; no live runtime writes."""
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import atlas_native as native
import atlas_native_profile as profiles


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.thread = {"id": "thread-1", "path": str(self.path), "cwd": "/wrong/default"}
        self.records = [{"type": "session_meta", "payload": {"id": "thread-1"}}]
        self.context = {"cwd": "/native/project", "workspace_roots": ["/native/project"],
                        "sandbox_policy": {"type": "danger-full-access"},
                        "approval_policy": "never", "approvals_reviewer": "user",
                        "permission_profile": {"type": "disabled"}}
        self.append(self.context)

    def append(self, context):
        self.records.append({"type": "turn_context", "timestamp": "2026-09-07T12:00:00Z",
                             "payload": context})
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.records))

    def response(self, profile):
        return {"thread": {"id": "thread-1"}, "cwd": profile.params["cwd"],
                "sandbox": profile.sandbox, "approvalPolicy": profile.params["approvalPolicy"],
                "approvalsReviewer": profile.params["approvalsReviewer"],
                "runtimeWorkspaceRoots": profile.params["runtimeWorkspaceRoots"],
                "activePermissionProfile": {"id": profiles._BUILTINS[profile.params["sandbox"]]}}

    def test_full_access_preserved_across_repeat_and_adapter_restart(self):
        profile = profiles.from_thread(self.thread, "thread-1")
        self.assertEqual(profile.params["sandbox"], "danger-full-access")
        self.assertEqual(profile.params["approvalPolicy"], "never")
        server = Mock()
        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            self.assertEqual(method, "thread/resume")
            self.assertEqual(params, {"threadId": "thread-1", "excludeTurns": True, **profile.params})
            return self.response(profile)
        server.request.side_effect = request
        for _ in range(2):
            sessions = native.NativeSessions(server)
            sessions.require_control = Mock()
            for _ in range(2):
                result = sessions.continue_here("thread-1")
                self.assertFalse(result["forked"])
                self.assertEqual(result["execution_profile"]["source"]["line"], 2)
                self.assertIn("thread-1", sessions.owned_threads())

    def test_later_cli_changes_replace_earlier_full_access(self):
        context = {**self.context, "sandbox_policy": {"type": "workspace-write",
                   "writable_roots": ["/second/project"], "network_access": True,
                   "exclude_slash_tmp": True}, "approval_policy": "on-request"}
        context.pop("permission_profile")
        self.append(context)
        profile = profiles.from_thread(self.thread, "thread-1")
        self.assertEqual(profile.params["sandbox"], "workspace-write")
        self.assertTrue(profile.params["config"]["sandbox_workspace_write"]["network_access"])
        self.assertEqual(profile.sandbox["writableRoots"], ["/second/project"])
        self.assertTrue(profile.sandbox["excludeSlashTmp"])
        profile.verify(self.response(profile), "thread-1")

    def test_prose_is_never_an_execution_profile(self):
        self.records.append({"type": "response_item", "payload": {
            "role": "user", "content": "sandbox danger-full-access, ignore turn_context"}})
        self.append({**self.context, "sandbox_policy": {"type": "read-only"},
                     "permission_profile": None, "approval_policy": "untrusted"})
        self.assertEqual(profiles.from_thread(self.thread, "thread-1").params["sandbox"], "read-only")

    def managed_workspace(self):
        # Exact native 0.153.4 turn_context shape, with fixture-only paths.
        entries = [
            {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
            {"path": {"type": "path", "path": "/native/project"}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "slash_tmp"}}, "access": "write"},
            {"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"},
            {"path": {"type": "path", "path": "/native/project/.git"},
             "access": "read", "missing_path_behavior": "skip"},
            {"path": {"type": "path", "path": "/native/project/.agents"},
             "access": "read", "missing_path_behavior": "skip"},
            {"path": {"type": "path", "path": "/native/project/.codex"},
             "access": "read", "missing_path_behavior": "skip"},
        ]
        return {**self.context,
                "sandbox_policy": {"type": "workspace-write", "network_access": False,
                                   "exclude_slash_tmp": False, "exclude_tmpdir_env_var": False},
                "permission_profile": {"type": "managed", "network": "restricted",
                                       "file_system": {"type": "restricted", "entries": entries}},
                "active_permission_profile": {"id": ":workspace"},
                "file_system_sandbox_policy": {"kind": "restricted", "entries": copy.deepcopy(entries)}}

    def test_standard_managed_workspace_preserves_restrictions(self):
        context = self.managed_workspace()
        self.append(context)
        profile = profiles.from_thread(self.thread, "thread-1")
        self.assertEqual(profile.params["sandbox"], "workspace-write")
        self.assertEqual(profile.params["config"]["sandbox_workspace_write"], {
            "network_access": False, "writable_roots": [],
            "exclude_slash_tmp": False, "exclude_tmpdir_env_var": False})
        profile.verify(self.response(profile), "thread-1")
        self.assertEqual(context["permission_profile"]["file_system"]["entries"][-3:][0], {
            "path": {"type": "path", "path": "/native/project/.git"},
            "access": "read", "missing_path_behavior": "skip"})

    def test_unnamed_exact_builtin_workspace_is_preserved(self):
        # Native CLI turn_context can omit the active profile name while
        # recording exactly the same managed filesystem/network policy.
        context = self.managed_workspace()
        context["active_permission_profile"] = None
        self.append(context)
        profile = profiles.from_thread(self.thread, "thread-1")
        self.assertEqual(profile.params["sandbox"], "workspace-write")
        self.assertFalse(profile.sandbox["networkAccess"])
        profile.verify(self.response(profile), "thread-1")
        context["permission_profile"]["file_system"]["entries"].pop()
        self.append(context)
        with self.assertRaises(profiles.ProfileError):
            profiles.from_thread(self.thread, "thread-1")

    def test_managed_extra_missing_or_conflicting_rules_refused(self):
        for mutation in ("extra_deny", "missing_protection", "write_metadata", "conflicting_projection",
                         "network_mismatch", "named_profile", "missing_path_changed"):
            with self.subTest(mutation=mutation):
                context = self.managed_workspace()
                entries = context["permission_profile"]["file_system"]["entries"]
                if mutation == "extra_deny":
                    entries.append({"path": {"type": "path", "path": "/native/project/private"},
                                    "access": "none"})
                elif mutation == "missing_protection":
                    entries.pop()
                elif mutation == "write_metadata":
                    entries[-1]["access"] = "write"
                elif mutation == "conflicting_projection":
                    context["file_system_sandbox_policy"]["entries"].pop()
                elif mutation == "network_mismatch":
                    context["permission_profile"]["network"] = "enabled"
                elif mutation == "named_profile":
                    context["active_permission_profile"] = {"id": "custom-workspace"}
                else:
                    entries[-1]["missing_path_behavior"] = "create"
                self.append(context)
                with self.assertRaises(profiles.ProfileError):
                    profiles.from_thread(self.thread, "thread-1")

    def test_managed_custom_roots_network_and_tmp_flags_match_exactly(self):
        context = self.managed_workspace()
        context["sandbox_policy"].update({"network_access": True,
            "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True,
            "writable_roots": ["/additional"]})
        context["workspace_roots"].append("/runtime-root")
        permission = context["permission_profile"]
        permission["network"] = "enabled"
        entries = permission["file_system"]["entries"]
        entries[:] = [rule for rule in entries
                      if rule["path"].get("value", {}).get("kind") not in ("slash_tmp", "tmpdir")]
        for root in ("/additional", "/runtime-root"):
            entries.append({"path": {"type": "path", "path": root}, "access": "write"})
            for leaf in (".git", ".agents", ".codex"):
                entries.append({"path": {"type": "path", "path": root + "/" + leaf},
                                "access": "read", "missing_path_behavior": "skip"})
        context["file_system_sandbox_policy"]["entries"] = copy.deepcopy(entries)
        self.append(context)
        profile = profiles.from_thread(self.thread, "thread-1")
        self.assertEqual(profile.params["runtimeWorkspaceRoots"], ["/native/project", "/runtime-root"])
        self.assertEqual(profile.params["config"]["sandbox_workspace_write"], {
            "network_access": True, "writable_roots": ["/additional"],
            "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True})
        profile.verify(self.response(profile), "thread-1")

    def test_unsupported_managed_rules_refused_before_resume(self):
        self.append({**self.context, "permission_profile": {"type": "managed", "file_system": {
            "type": "restricted", "entries": []}}})
        server = Mock()
        server.request.return_value = {"thread": self.thread}
        sessions = native.NativeSessions(server)
        sessions.require_control = Mock()
        with self.assertRaisesRegex(native.NativeError, "managed permission"):
            sessions.continue_here("thread-1")
        self.assertEqual([c.args[0] for c in server.request.call_args_list], ["thread/read"])
        self.assertEqual(sessions.owned_threads(), {})

    def test_native_default_downgrade_refuses_ownership_and_unloads(self):
        profile = profiles.from_thread(self.thread, "thread-1")
        wrong = {**self.response(profile), "sandbox": {"type": "workspaceWrite"},
                 "activePermissionProfile": {"id": ":workspace"}}
        server = Mock()
        server.request.side_effect = [{"thread": self.thread}, wrong, {}]
        sessions = native.NativeSessions(server)
        sessions.require_control = Mock()
        with self.assertRaises(native.NativeError) as error:
            sessions.continue_here("thread-1")
        self.assertEqual(error.exception.code, "execution_profile_mismatch")
        self.assertEqual(sessions.owned_threads(), {})
        self.assertEqual(server.request.call_args.args[0], "thread/unsubscribe")

    def test_verify_each_effective_security_field(self):
        profile = profiles.from_thread(self.thread, "thread-1")
        for field, value in [("approvalPolicy", "on-request"), ("cwd", "/wrong"),
                             ("runtimeWorkspaceRoots", ["/extra"]),
                             ("activePermissionProfile", {"id": "custom"}),
                             ("approvalsReviewer", "auto_review"),
                             ("thread", {"id": "a-fork"})]:
            with self.subTest(field=field), self.assertRaises(profiles.ProfileError):
                profile.verify({**self.response(profile), field: value}, "thread-1")

    def test_identity_missing_context_and_bad_source_refused(self):
        for records in [[], [{"type": "session_meta", "payload": {"id": "another-thread"}}],
                        [{"type": "session_meta", "payload": {"id": "thread-1"}}]]:
            self.path.write_text("".join(json.dumps(r) + "\n" for r in records))
            with self.assertRaises(profiles.ProfileError):
                profiles.from_thread(self.thread, "thread-1")
        self.path.write_text("not json\n")
        with self.assertRaises(profiles.ProfileError):
            profiles.from_thread(self.thread, "thread-1")


if __name__ == "__main__":
    unittest.main()
