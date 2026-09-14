"""A slow native attach is not a dead host and must never be replayed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import atlas_remote as remote

class ControlDeadlineTests(unittest.TestCase):
    def test_native_attach_gets_time_to_resume_and_timeout_keeps_agent_available(self):
        console = Mock()
        def answer(method, url, **kwargs):
            # Reproduce a native cold resume exceeding the old 30s deadline,
            # without making the test sleep or invoking an actual runtime.
            if kwargs['timeout'] < 40:
                raise remote.RemoteError('native resume still running', code='agent_request_timeout')
            return 200, {'Content-Type': 'application/json'}, b'{"room":{"ownership":{"atlas_owned":true}}}'
        console.request.side_effect = answer
        agent = remote.Agent(agent_id='agent2', label='Agent2', kind='remote', runtime='codex', node='server', console=console)
        registry = object.__new__(remote.AgentRegistry)
        result = registry.proxy(agent, 'POST', '/api/room/orbit/agent2-controller/continue', '', headers={}, body=b'{}')
        self.assertEqual(result.status, 200)
        console.request.assert_called_once()
        self.assertEqual(agent.as_json()['availability']['state'], 'available')
        console.request.side_effect = remote.RemoteError('still pending', code='agent_request_timeout')
        with self.assertRaises(remote.RemoteError):
            registry.proxy(agent, 'POST', '/api/room/orbit/agent2-controller/continue', '', headers={}, body=b'{}')
        self.assertEqual(console.request.call_count, 2)  # once per explicit request
        self.assertEqual(agent.as_json()['availability']['state'], 'available')

    def test_response_timeout_keeps_shared_tunnel_and_never_replays_write(self):
        transport = Mock()
        console = remote.RemoteConsole(transport, 8878)
        conn = Mock()
        conn.getresponse.side_effect = TimeoutError('slow native resume')
        console._open = Mock(return_value=conn)
        console._csrf = 'fixture-token'
        with self.assertRaises(remote.RemoteError) as error:
            console.request('POST', '/api/room/orbit/agent2-controller/continue', headers={}, body=b'{}', timeout=120)
        self.assertEqual(error.exception.code, 'agent_request_timeout')
        conn.request.assert_called_once()
        transport.mark_broken.assert_not_called()
        conn.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
