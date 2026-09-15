"""Policy changes affect future connections, never silently rewrite a live one."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from ux46_execution_policy import ExecutionPolicy, PolicyConflict
import atlas_native as native
import atlas_native_profile as profiles
from test_atlas_session_workers import WorkerHarness, ROOM_A, THREAD_ONE
import test_atlas_native_profile as fixture

class SavedPolicyTests(unittest.TestCase):
    def test_persistence_conflict_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'
            store = ExecutionPolicy(path, 'workspace-write')
            store.set('full-access', 0)
            self.assertEqual(ExecutionPolicy(path, 'preserve').read()['policy'], 'full-access')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(PolicyConflict): store.set('preserve', 0)
            path.write_text('{"policy":"typo","revision":1}')
            with self.assertRaises(ValueError): ExecutionPolicy(path, 'full-access')

class PolicyHTTPTests(unittest.TestCase):
    def test_default_change_keeps_worker_and_is_authenticated(self):
        h = WorkerHarness()
        try:
            self.assertEqual(h.attach(ROOM_A)[0], 200)
            worker = h.worker(THREAD_ONE)
            old = worker.sessions.execution_policy
            body = {'policy': 'full-access', 'base_revision': 0}
            self.assertEqual(h.call('POST', '/api/execution-policy', body=body, csrf=False)[0], 403)
            self.assertEqual(h.call('POST', '/api/execution-policy', body=body,
                                   headers={'Origin': 'https://wrong.example'})[0], 403)
            code, result = h.call('POST', '/api/execution-policy', body=body)
            self.assertEqual(code, 200, result)
            self.assertEqual(h.service.workers.execution_policy, 'full-access')
            self.assertIs(h.worker(THREAD_ONE), worker)
            self.assertEqual(worker.sessions.execution_policy, old)
            self.assertEqual(h.call('POST', '/api/execution-policy', body={'policy':'preserve','base_revision':0})[0], 409)
            self.assertEqual(h.call('POST', '/api/execution-policy', body={'policy':'bad','base_revision':1})[0], 400)
            # Choose project access and reconnect only this synthetic thread.
            self.assertEqual(h.call('POST','/api/execution-policy',body={'policy':'workspace-write','base_revision':1})[0],200)
            self.assertEqual(h.call('POST',f'/api/room/{ROOM_A}/release',body={})[0],200)
            code, result = h.attach(ROOM_A)
            self.assertEqual(code,200,result)
            self.assertIsNot(h.worker(THREAD_ONE), worker)
            self.assertEqual(h.worker(THREAD_ONE).sessions.execution_policy, 'workspace-write')
        finally: h.close()

class ChosenProjectTests(unittest.TestCase):
    setUp = fixture.ProfileTests.setUp
    append = fixture.ProfileTests.append
    response = fixture.ProfileTests.response

    def test_project_choice_replaces_old_full_access_and_rejects_mismatch(self):
        profile = profiles.project_access(self.thread, 'thread-1')
        self.assertEqual(profile.params['approvalPolicy'], 'on-request')
        self.assertFalse(profile.sandbox['networkAccess'])
        self.assertEqual(profile.sandbox['writableRoots'], [])
        self.assertEqual(profile.params['runtimeWorkspaceRoots'], ['/native/project'])
        response = self.response(profile)
        profile.verify(response, 'thread-1')
        response['sandbox'] = {'type':'dangerFullAccess'}
        with self.assertRaises(profiles.ProfileError): profile.verify(response, 'thread-1')

    def test_fresh_project_start_requires_native_confirmation(self):
        profile = profiles.project_access_start('/native/project')
        response = {'cwd':'/native/project', 'sandbox':profile.sandbox,
                    'approvalPolicy':'on-request','approvalsReviewer':'user'}
        profile.verify_start(response)
        response['approvalPolicy'] = 'never'
        with self.assertRaises(profiles.ProfileError): profile.verify_start(response)

    def test_new_native_session_uses_project_choice_without_a_model_turn(self):
        profile = profiles.project_access_start('/native/project')
        server = Mock()
        def request(method, params):
            if method == 'thread/start':
                self.assertEqual(params, profile.params)
                return {'thread':{'id':'fresh'}, 'cwd':'/native/project',
                        'sandbox':profile.sandbox,'approvalPolicy':'on-request',
                        'approvalsReviewer':'user'}
            self.assertEqual(method, 'thread/read')
            return {'thread':{'id':'fresh','cwd':'/native/project'}}
        server.request.side_effect=request
        sessions=native.NativeSessions(server,execution_policy='workspace-write')
        result=sessions.start_fresh_thread('/native/project')
        self.assertTrue(result['execution_profile']['granted'])

class PolicyProxyTests(unittest.TestCase):
    def test_only_read_and_save_are_forwarded(self):
        import atlas_remote as remote
        for method in ['GET','POST']: remote.check_allowed(method,'/api/execution-policy')
        for method in ['DELETE','PUT']:
            with self.assertRaises(remote.RemoteError): remote.check_allowed(method,'/api/execution-policy')
