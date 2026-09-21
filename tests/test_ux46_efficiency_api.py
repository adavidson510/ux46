"""Efficiency reads are authenticated, bounded, and never start a model."""
import json
import unittest
from unittest.mock import patch

from test_atlas_console import ConsoleHarness, THREAD_ONE


class EfficiencyApiTests(unittest.TestCase):
    def setUp(self):
        self.h = ConsoleHarness()

    def tearDown(self):
        self.h.close()

    def test_basket_metadata_and_named_file_are_separate(self):
        status, catalog = self.h.call('GET', '/api/skills')
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog['skills']), 8)
        self.assertNotIn('content', catalog['skills'][0])
        status, body = self.h.call('GET', '/api/skills/ux46-efficiency')
        self.assertEqual(status, 200)
        self.assertIn('# UX46 efficiency', body['content'])
        status, _ = self.h.call('GET', '/api/skills/ux46-efficiency?file=../../AGENTS.md')
        self.assertEqual(status, 404)

    def test_usage_uses_resolved_native_path_without_starting_or_resuming(self):
        rollout = self.h.tmp / f'rollout-2026-09-11T01-00-00-{THREAD_ONE}.jsonl'
        rollout.write_text(json.dumps({
            'timestamp': '2026-09-11T01:00:00Z', 'type': 'token_usage_record',
            'payload': {'response_id': 'response-one', 'usage': {'input_tokens': 100, 'output_tokens': 5}},
        }) + '\n')
        with patch.object(self.h.service, 'sessions_for') as sessions:
            sessions.return_value.read_thread.return_value = {'thread': {'path': str(rollout)}}
            status, result = self.h.call('GET', '/api/room/fixture/console-work/usage?day=2026-09-11')
            self.assertEqual(status, 200)
            self.assertEqual(result['usage']['input_tokens'], 100)
            self.assertIsNone(result['usage']['cached_input_tokens'])
            sessions.return_value.read_thread.assert_called_once_with(THREAD_ONE)
            self.assertEqual(len(sessions.return_value.mock_calls), 1)
        self.assertEqual(self.h.service.workers.attached(), {})
        status, result = self.h.call('GET', '/api/usage?day=2026-09-11&project=fixture')
        self.assertEqual(status, 200)
        self.assertEqual(result['usage']['input_tokens'], 100)
        self.assertIn('background', result)

    def test_invalid_day_and_private_routes(self):
        status, _ = self.h.call('GET', '/api/usage?day=yesterday')
        self.assertEqual(status, 400)
        status, _ = self.h.call('GET', '/api/skills', host='untrusted.example')
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()
