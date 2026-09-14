"""Regression check for the speech cleanup in tools/atlas_voice.py.

User heard the local voice read "asterisk asterisk" out of formatted replies.
These tests pin the behaviour the fix owes a listener: recognised Markdown
markers go unspoken, their content survives, and arithmetic and dosages are
left exactly as written. They test what comes out of ``speech_text`` and what
reaches synthesis, never how the cleanup is written.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import atlas_voice as voice  # noqa: E402

SAMPLE = """# Morning summary

The **insulin** plan is *unchanged* for now. See [the dosing note](https://example.test/dose) first.

1. Give 2.5 units before food.
2. Log a reading of -0.4 mmol below target.

- Watch for a drop under -1.2 mmol/L.
- The rate is 3 * 4 units per day, not 3 \\* 5.

Run `atlas doctor --voice` when the console restarts.

```python
total = base * 2 - 1
```

| Time | Units |
| ---- | ----- |
| 08:00 | 2.5 |

> Escalate if the trend holds.
"""


class SpeechTextTests(unittest.TestCase):
    def setUp(self):
        self.spoken = voice.speech_text(SAMPLE)

    def test_no_recognised_marker_is_spoken(self):
        for marker in ("**", "#", "> ", "```", "|", "](", "`atlas"):
            self.assertNotIn(marker, self.spoken, f"{marker!r} would be read aloud")

    def test_content_of_every_marker_survives(self):
        for kept in ("Morning summary", "insulin", "unchanged",
                     "Give 2.5 units before food.",
                     "Watch for a drop under -1.2 mmol/L.",
                     "atlas doctor --voice", "total = base * 2 - 1",
                     "Escalate if the trend holds."):
            self.assertIn(kept, self.spoken)

    def test_link_speaks_its_label_and_not_its_url(self):
        self.assertIn("See the dosing note first.", self.spoken)
        self.assertNotIn("example.test", self.spoken)

    def test_bare_urls_are_still_read(self):
        self.assertEqual(voice.speech_text("Open https://example.test/a_b now"),
                         "Open https://example.test/a_b now")

    def test_arithmetic_and_negative_dosages_are_untouched(self):
        self.assertIn("3 * 4 units per day", self.spoken)
        self.assertIn("not 3 * 5", self.spoken)          # the escape spoke literally
        self.assertIn("-0.4 mmol below target", self.spoken)
        self.assertEqual(voice.speech_text("2*3*4 = 24 and -7 degrees"),
                         "2*3*4 = 24 and -7 degrees")
        self.assertEqual(voice.speech_text("a snake_case_name stays"),
                         "a snake_case_name stays")

    def test_list_markers_go_but_numbering_stays(self):
        self.assertIn("1. Give 2.5 units", self.spoken)
        self.assertNotIn("- Watch", self.spoken)

    def test_table_rows_are_read_as_cells(self):
        self.assertIn("Time, Units", self.spoken)
        self.assertIn("08:00, 2.5", self.spoken)
        self.assertNotIn("----", self.spoken)

    def test_unknown_syntax_keeps_its_words(self):
        odd = "A <span>tagged</span> line with [an unresolved ref] and 50% left"
        self.assertIn("tagged", voice.speech_text(odd))
        self.assertIn("50% left", voice.speech_text(odd))

    def test_the_saved_markdown_is_never_rewritten(self):
        before = SAMPLE
        voice.speech_text(SAMPLE)
        self.assertEqual(SAMPLE, before)

    def test_nothing_speakable_is_still_nothing(self):
        self.assertEqual(voice.speech_text("```\n"), "")
        self.assertEqual(voice.speech_text("   "), "")


class FakeKokoro:
    """Stands in for the local ONNX model; records exactly what it was told."""

    def __init__(self):
        self.heard = []

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        self.heard.append(text)
        return [0.0] * 2400, 24000


class SynthesisReceivesCleanTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = voice.VoiceService("/tmp/atlas-voice-does-not-exist",
                                          Path(self.tmp.name) / "cache")
        self.fake = FakeKokoro()
        self.service._kokoro = self.fake
        self.addCleanup(self.tmp.cleanup)

    def test_kokoro_is_given_the_spoken_text_and_metadata_counts_it(self):
        speech = self.service.speak(SAMPLE)
        self.assertEqual(self.fake.heard, [voice.speech_text(SAMPLE)])
        payload = speech.as_json()
        self.assertEqual(payload["total_chars"], len(voice.speech_text(SAMPLE)))
        self.assertEqual(payload["source_chars"], len(SAMPLE.strip()))
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["cached"])
        self.assertGreater(payload["duration_s"], 0)

    def test_cache_key_follows_the_spoken_text_not_the_markdown(self):
        plain = self.service.speak("Take 2.5 units.")
        marked = self.service.speak("**Take 2.5 units.**")
        self.assertEqual(plain.key, marked.key)
        self.assertTrue(marked.cached)               # the second one reused the file
        self.assertEqual(len(self.fake.heard), 1)

    def test_cached_audio_from_before_the_cleanup_is_not_replayed(self):
        key_now = self.service.key_for("Take 2.5 units.", voice.DEFAULT_VOICE)
        saved, voice.SPEECH_VERSION = voice.SPEECH_VERSION, voice.SPEECH_VERSION + 1
        try:
            self.assertNotEqual(key_now,
                                self.service.key_for("Take 2.5 units.", voice.DEFAULT_VOICE))
        finally:
            voice.SPEECH_VERSION = saved

    def test_markdown_that_says_nothing_is_refused(self):
        with self.assertRaises(voice.VoiceError) as caught:
            self.service.speak("```\n```")
        self.assertEqual(caught.exception.code, "empty_text")


if __name__ == "__main__":
    unittest.main()
