from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ux46_skills


class Ux46SkillsTests(unittest.TestCase):
    def test_catalog_is_sorted_and_metadata_only(self):
        entries = ux46_skills.catalog()
        self.assertEqual([entry["name"] for entry in entries], sorted(ux46_skills.SKILLS))
        efficiency = next(entry for entry in entries if entry["name"] == "ux46-efficiency")
        self.assertEqual(efficiency["version"], "0.1.0")
        self.assertEqual(len(efficiency["sha256"]), 64)
        self.assertNotIn("content", efficiency)
        self.assertEqual(efficiency["files"], ["SKILL.md", "references/operating-guide-1.md"])

    def test_read_requires_an_allowlisted_relative_file(self):
        result = ux46_skills.read("ux46-efficiency")
        self.assertEqual(result["path"], "SKILL.md")
        self.assertIn("# UX46 efficiency", result["content"])
        with self.assertRaises(ValueError):
            ux46_skills.read("ux46-efficiency", "../../AGENTS.md")

    def test_discovery_names_the_local_catalog_without_loading_contents(self):
        result = ux46_skills.discovery()
        self.assertTrue(result["catalog_cli"].endswith("tools/ux46_skills.py"))
        self.assertEqual(result["repository"], str(Path(__file__).resolve().parents[1]))

    def test_install_is_idempotent_and_copies_only_the_declared_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            first = ux46_skills.install("ux46-efficiency", target)
            second = ux46_skills.install("ux46-efficiency", target)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertTrue((target / "ux46-efficiency" / "SKILL.md").is_file())
            self.assertTrue((target / "ux46-efficiency" / "references" / "operating-guide-1.md").is_file())

    def test_measure_reports_literal_deltas_without_token_estimate(self):
        with tempfile.TemporaryDirectory() as temporary:
            before, after = Path(temporary) / "before.md", Path(temporary) / "after.md"
            before.write_text("one two three four\n", encoding="utf-8")
            after.write_text("one two\n", encoding="utf-8")
            result = ux46_skills.measure(before, after)
            self.assertEqual(result["delta"]["words"], -2)
            self.assertNotIn("tokens", result)


if __name__ == "__main__":
    unittest.main()
