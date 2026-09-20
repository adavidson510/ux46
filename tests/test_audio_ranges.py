"""Byte-range behavior required for browser pause/seek controls."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from atlas_voice import audio_response
from atlas_remote import FORWARDED_REQUEST_HEADERS, FORWARDED_RESPONSE_HEADERS


class AudioRangeTests(unittest.TestCase):
    def test_ranges_return_exact_bytes_and_total(self):
        for request, expected, interval in [
            ("bytes=2-4", b"234", "2-4"),
            ("bytes=7-", b"789", "7-9"),
            ("bytes=-3", b"789", "7-9"),
            ("bytes=7-999", b"789", "7-9"),
            ("bytes=-999", b"0123456789", "0-9"),
        ]:
            with self.subTest(request=request):
                status, data, headers = audio_response(b"0123456789", request)
                self.assertEqual((status, data), (206, expected))
                self.assertEqual(dict(headers)["Content-Range"], f"bytes {interval}/10")

    def test_unsatisfiable_range_is_explicit(self):
        for request in ("bytes=10-", "bytes=5-2", "bytes=-0"):
            status, data, headers = audio_response(b"0123456789", request)
            self.assertEqual((status, data, dict(headers)["Content-Range"]), (416, b"", "bytes */10"))

    def test_ordinary_or_unsupported_requests_receive_whole_clip(self):
        for request in ("", "bytes=", "bytes=0-2,5-7", "other=1-3"):
            status, data, headers = audio_response(b"0123456789", request)
            self.assertEqual((status, data), (200, b"0123456789"))
            self.assertEqual(dict(headers)["Accept-Ranges"], "bytes")

    def test_remote_hop_preserves_media_range_contract(self):
        self.assertIn("Range", FORWARDED_REQUEST_HEADERS)
        self.assertIn("Content-Range", FORWARDED_RESPONSE_HEADERS)
        self.assertIn("Accept-Ranges", FORWARDED_RESPONSE_HEADERS)


if __name__ == "__main__":
    unittest.main()
