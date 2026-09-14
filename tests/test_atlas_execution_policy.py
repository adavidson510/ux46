"""The host's execution policy: what UX46 asks a native session to run under.

Two policies that make two different kinds of claim, and must never be
confused. ``preserve`` says "this session runs as the CLI last recorded"; it
reads the rollout and refuses anything it cannot reproduce exactly.
``full-access`` says "this host's owner has decided its sessions run with full
tools"; it deliberately overrides a saved restrictive session profile, and
takes nothing from the session but its identity, directory and roots.

Nothing here starts a runtime. Every exchange below is against a mock speaking
the app-server protocol, so no thread of anyone's is resumed, no rollout file
is written, and no approval is answered.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import atlas_native as native
import atlas_native_profile as profiles
import atlas_workers as workers


class PolicyTestCase(unittest.TestCase):
    """One rollout on disk, recording a restrictive saved session profile.

    Exactly the shape User reported: workspace-write, no network, approvals on
    request, and a working directory that cannot reach the work.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.thread = {"id": "thread-1", "path": str(self.path), "cwd": "/wrong/default"}
        self.cwd = str(Path(self.tmp.name) / "codex-home")
        self.roots = [self.cwd]
        self.write_context({
            "cwd": self.cwd, "workspace_roots": self.roots,
            "sandbox_policy": {"type": "workspace-write", "network_access": False,
                               "writable_roots": [], "exclude_tmpdir_env_var": False,
                               "exclude_slash_tmp": False},
            "approval_policy": "on-request", "approvals_reviewer": "user",
        })

    def write_context(self, context):
        records = [{"type": "session_meta", "payload": {"id": "thread-1"}},
                   {"type": "turn_context", "timestamp": "2026-09-08T09:00:00Z",
                    "payload": context}]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def granted(self, params, thread_id="thread-1"):
        """Exactly what a runtime that granted the request would report back."""

        return {
            "thread": {"id": thread_id}, "cwd": params["cwd"],
            "sandbox": {"type": "dangerFullAccess"},
            "approvalPolicy": params["approvalPolicy"],
            "approvalsReviewer": params["approvalsReviewer"],
            "runtimeWorkspaceRoots": params.get("runtimeWorkspaceRoots", []),
            "activePermissionProfile": {"id": ":danger-full-access"},
        }

    def sessions(self, request, policy):
        server = Mock()
        server.request.side_effect = request
        made = native.NativeSessions(server, execution_policy=policy)
        made.require_control = Mock()
        return made


class ResumeTests(PolicyTestCase):
    def test_full_access_overrides_a_saved_restrictive_session_profile(self):
        """The whole point: a workspace-write rollout resumes with full tools."""

        seen = {}

        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            self.assertEqual(method, "thread/resume")
            seen["params"] = params
            return self.granted(params)

        sessions = self.sessions(request, profiles.FULL_ACCESS)
        result = sessions.continue_here("thread-1", room="p/s")
        asked = seen["params"]
        self.assertEqual(asked["threadId"], "thread-1")
        self.assertEqual(asked["sandbox"], "danger-full-access")
        self.assertEqual(asked["approvalPolicy"], "never")
        self.assertEqual(asked["approvalsReviewer"], "user")
        # Identity and place come from the session; permissions do not.
        self.assertEqual(asked["cwd"], self.cwd)
        self.assertEqual(asked["runtimeWorkspaceRoots"], self.roots)
        # No model, effort, config or instruction override rides along, so the
        # configured MCP servers and tools are the ones already there.
        self.assertEqual(set(asked), {"threadId", "cwd", "sandbox", "approvalPolicy",
                                      "approvalsReviewer", "runtimeWorkspaceRoots", "excludeTurns"})
        profile = result["execution_profile"]
        self.assertEqual(profile["policy"], "full-access")
        self.assertTrue(profile["granted"])
        self.assertEqual(profile["approvals_reviewer"], "user")
        self.assertEqual(profile["source"]["chosen_by"], "host")
        self.assertIn("thread-1", sessions.owned_threads())

    def test_full_access_resumes_the_exact_thread_and_never_forks(self):
        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            return self.granted(params)

        result = self.sessions(request, profiles.FULL_ACCESS).continue_here("thread-1")
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertFalse(result["forked"])
        self.assertEqual(result["cwd"], self.cwd)

    def test_full_access_still_needs_the_thread_identified(self):
        """A rollout naming another thread is refused, policy or no policy."""

        self.path.write_text(json.dumps(
            {"type": "session_meta", "payload": {"id": "someone-else"}}) + "\n")
        with self.assertRaises(profiles.ProfileError):
            profiles.full_access(self.thread, "thread-1")

    def test_full_access_still_needs_a_real_working_directory(self):
        self.write_context({
            "cwd": "relative/path", "workspace_roots": ["relative/path"],
            "sandbox_policy": {"type": "danger-full-access"},
            "approval_policy": "never", "approvals_reviewer": "user"})
        with self.assertRaises(profiles.ProfileError):
            profiles.full_access(self.thread, "thread-1")

    def test_full_access_locates_a_session_preserve_could_not_reproduce(self):
        """A saved profile too complex to reproduce is not a reason to refuse.

        Preserve must refuse it — it cannot reproduce it exactly. An explicit
        host policy replaces it outright, so rejecting the session for the
        shape of a policy that is being discarded would refuse it for a reason
        that no longer applies.
        """

        self.write_context({
            "cwd": self.cwd, "workspace_roots": self.roots,
            "sandbox_policy": {"type": "workspace-write", "network_access": False,
                               "writable_roots": [], "exclude_tmpdir_env_var": False,
                               "exclude_slash_tmp": False},
            "approval_policy": "on-request", "approvals_reviewer": "user",
            "active_permission_profile": {"id": "some-named-house-profile"}})
        with self.assertRaises(profiles.ProfileError):
            profiles.from_thread(self.thread, "thread-1")
        profile = profiles.full_access(self.thread, "thread-1")
        self.assertEqual(profile.params["cwd"], self.cwd)
        self.assertEqual(profile.params["sandbox"], "danger-full-access")


class MismatchTests(PolicyTestCase):
    def test_a_runtime_that_refuses_full_access_admits_no_writes(self):
        """Asking is not being granted, and a narrower answer is a refusal."""

        calls = []

        def request(method, params):
            calls.append(method)
            if method == "thread/read":
                return {"thread": self.thread}
            if method == "thread/resume":
                # The host did not grant it: workspace-write came back instead.
                return {
                    "thread": {"id": "thread-1"}, "cwd": params["cwd"],
                    "sandbox": {"type": "workspaceWrite", "networkAccess": False,
                                "writableRoots": [], "excludeTmpdirEnvVar": False,
                                "excludeSlashTmp": False},
                    "approvalPolicy": "on-request", "approvalsReviewer": "user",
                    "runtimeWorkspaceRoots": params["runtimeWorkspaceRoots"],
                    "activePermissionProfile": {"id": ":workspace"},
                }
            return {}

        sessions = self.sessions(request, profiles.FULL_ACCESS)
        with self.assertRaises(native.NativeError) as caught:
            sessions.continue_here("thread-1")
        self.assertEqual(caught.exception.code, "execution_profile_mismatch")
        self.assertIn("did not grant", str(caught.exception))
        # Nothing is left held, and the thread was unloaded rather than kept.
        self.assertNotIn("thread-1", sessions.owned_threads())
        self.assertIn("thread/unsubscribe", calls)

    def test_a_reviewer_swap_is_a_refusal_too(self):
        """auto_review is not the user, and must not pass as full access."""

        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            if method == "thread/resume":
                return dict(self.granted(params), approvalsReviewer="auto_review")
            return {}

        sessions = self.sessions(request, profiles.FULL_ACCESS)
        with self.assertRaises(native.NativeError) as caught:
            sessions.continue_here("thread-1")
        self.assertEqual(caught.exception.code, "execution_profile_mismatch")
        self.assertNotIn("thread-1", sessions.owned_threads())


class FreshThreadTests(PolicyTestCase):
    def _server(self, answer):
        calls = []

        def request(method, params):
            calls.append((method, params))
            if method == "thread/start":
                return answer(params)
            if method == "thread/read":
                return {"thread": {"id": "new-1", "cwd": self.cwd,
                                   "model": "gpt-6", "reasoningEffort": "medium"}}
            return {}

        return request, calls

    def test_full_access_new_thread_asks_and_reports_a_verified_profile(self):
        def answer(params):
            return dict(self.granted(params, thread_id="new-1"),
                        thread={"id": "new-1"})

        request, calls = self._server(answer)
        sessions = self.sessions(request, profiles.FULL_ACCESS)
        fresh = sessions.start_fresh_thread(self.cwd, model="gpt-6", effort="medium")
        start = next(p for m, p in calls if m == "thread/start")
        self.assertEqual(start["sandbox"], "danger-full-access")
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["approvalsReviewer"], "user")
        self.assertEqual(start["cwd"], self.cwd)
        self.assertEqual(start["model"], "gpt-6")
        profile = fresh["execution_profile"]
        self.assertEqual(profile["policy"], "full-access")
        self.assertTrue(profile["granted"])
        self.assertEqual(profile["requested"]["approval_policy"], "never")
        self.assertEqual(fresh["thread_id"], "new-1")

    def test_a_new_thread_that_was_not_granted_full_access_is_refused(self):
        def answer(params):
            return {"thread": {"id": "new-1"}, "cwd": params["cwd"],
                    "sandbox": {"type": "workspaceWrite", "networkAccess": False,
                                "writableRoots": [], "excludeTmpdirEnvVar": False,
                                "excludeSlashTmp": False},
                    "approvalPolicy": "on-request", "approvalsReviewer": "user"}

        request, calls = self._server(answer)
        sessions = self.sessions(request, profiles.FULL_ACCESS)
        with self.assertRaises(native.NativeError) as caught:
            sessions.start_fresh_thread(self.cwd)
        self.assertEqual(caught.exception.code, "execution_profile_mismatch")
        self.assertIn(("thread/unsubscribe", {"threadId": "new-1"}), calls)
        self.assertNotIn("new-1", sessions.owned_threads())

    def test_preserve_leaves_a_new_thread_alone_and_claims_nothing(self):
        """Default asks for no permissions and reports only what it observed.

        An unknown restrictive startup policy must never come back labelled as
        preserved: nothing was requested, so nothing was granted.
        """

        def answer(params):
            self.assertNotIn("sandbox", params)
            self.assertNotIn("approvalPolicy", params)
            self.assertNotIn("approvalsReviewer", params)
            return {"thread": {"id": "new-1"}, "cwd": params["cwd"],
                    "sandbox": {"type": "workspaceWrite", "networkAccess": False,
                                "writableRoots": [], "excludeTmpdirEnvVar": False,
                                "excludeSlashTmp": False},
                    "approvalPolicy": "on-request", "approvalsReviewer": "user"}

        request, _ = self._server(answer)
        sessions = self.sessions(request, profiles.PRESERVE)
        fresh = sessions.start_fresh_thread(self.cwd)
        profile = fresh["execution_profile"]
        self.assertEqual(profile["policy"], "preserve")
        self.assertIsNone(profile["granted"])
        self.assertIsNone(profile["requested"])
        self.assertEqual(profile["source"]["chosen_by"], "native-default")
        self.assertEqual(profile["approval_policy"], "on-request")


class PreserveUnchangedTests(PolicyTestCase):
    def test_the_default_is_preserve(self):
        self.assertEqual(native.NativeSessions(Mock()).execution_policy, "preserve")
        self.assertEqual(profiles.PRESERVE, "preserve")

    def test_preserve_still_refuses_what_it_cannot_reproduce(self):
        self.write_context({
            "cwd": self.cwd, "workspace_roots": self.roots,
            "sandbox_policy": {"type": "workspace-write", "network_access": False,
                               "writable_roots": [], "exclude_tmpdir_env_var": False,
                               "exclude_slash_tmp": False},
            "approval_policy": "on-request", "approvals_reviewer": "user",
            "active_permission_profile": {"id": "some-named-house-profile"}})

        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            raise AssertionError("preserve must not resume what it cannot reproduce")

        sessions = self.sessions(request, profiles.PRESERVE)
        with self.assertRaises(native.NativeError) as caught:
            sessions.continue_here("thread-1")
        self.assertEqual(caught.exception.code, "execution_profile_unsupported")

    def test_preserve_resume_asks_for_exactly_the_saved_policy(self):
        seen = {}

        def request(method, params):
            if method == "thread/read":
                return {"thread": self.thread}
            seen["params"] = params
            return {"thread": {"id": "thread-1"}, "cwd": params["cwd"],
                    "sandbox": {"type": "workspaceWrite", "networkAccess": False,
                                "writableRoots": [], "excludeTmpdirEnvVar": False,
                                "excludeSlashTmp": False},
                    "approvalPolicy": "on-request", "approvalsReviewer": "user",
                    "runtimeWorkspaceRoots": params["runtimeWorkspaceRoots"],
                    "activePermissionProfile": {"id": ":workspace"}}

        result = self.sessions(request, profiles.PRESERVE).continue_here("thread-1")
        self.assertEqual(seen["params"]["sandbox"], "workspace-write")
        self.assertEqual(seen["params"]["approvalPolicy"], "on-request")
        self.assertEqual(result["execution_profile"]["policy"], "preserve")
        self.assertEqual(result["execution_profile"]["source"]["chosen_by"], "session")


class WorkerPoolTests(unittest.TestCase):
    """The option has to reach the runtimes, not just the parser."""

    def pool(self, policy):
        made = workers.SessionWorkerPool(
            command=["/nonexistent/codex"], on_event=lambda event: None,
            execution_policy=policy)
        self.addCleanup(made.shutdown)
        return made

    def test_the_pool_defaults_to_preserve(self):
        self.assertEqual(self.pool(profiles.PRESERVE).execution_policy, "preserve")
        default = workers.SessionWorkerPool(
            command=["/nonexistent/codex"], on_event=lambda event: None)
        self.addCleanup(default.shutdown)
        self.assertEqual(default.execution_policy, "preserve")

    def test_an_unknown_policy_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            workers.SessionWorkerPool(command=["/nonexistent/codex"],
                                      on_event=lambda e: None,
                                      execution_policy="yolo")
        with self.assertRaises(ValueError):
            native.NativeSessions(Mock(), execution_policy="danger")

    def test_every_worker_the_pool_spawns_adopts_the_host_policy(self):
        """Including the read/catalog runtime, which still refuses to write."""

        pool = self.pool(profiles.FULL_ACCESS)
        spawned = []

        class FakeServer:
            running = True
            pids: tuple = ()

            def start(self):
                spawned.append(self)

            def stop(self, *a, **k):
                return ()

        # The adapter is frozen, so the whole adapter is swapped rather than
        # one of its fields: nothing real is launched by this test.
        class FakeAdapter:
            agent = workers.CODEX

            def launch(self, on_event, read_only=False):
                return FakeServer()

        pool.adapter = FakeAdapter()
        worker = pool._spawn(workers.CONVERSATION, thread_id="t", room="p/s")
        reader = pool._spawn(workers.READER)
        self.assertEqual(worker.sessions.execution_policy, "full-access")
        self.assertEqual(reader.sessions.execution_policy, "full-access")
        # A worker reports what its thread was verified to run under, and
        # reports nothing at all until something was.
        self.assertIsNone(worker.as_json()["execution_profile"])
        worker.profile = {"policy": "full-access", "granted": True}
        self.assertEqual(worker.as_json()["execution_profile"]["policy"], "full-access")


class ConsoleSurfaceTests(unittest.TestCase):
    def test_the_flag_exists_defaults_to_preserve_and_refuses_nonsense(self):
        import atlas_console

        parser = atlas_console.build_parser()
        self.assertEqual(parser.parse_args([]).execution_policy, "preserve")
        self.assertEqual(
            parser.parse_args(["--execution-policy", "full-access"]).execution_policy,
            "full-access")
        with self.assertRaises(SystemExit):
            parser.parse_args(["--execution-policy", "yolo"])


if __name__ == "__main__":
    unittest.main()
