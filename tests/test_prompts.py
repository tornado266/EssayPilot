import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.prompts import (
    build_scoring_prompt,
    build_structured_grading_prompt,
    build_teaching_prompt,
    load_band_sample_anchors,
    load_skill_scoring_rules,
)
from src.report_schema import TEACHING_FEEDBACK_JSON_SCHEMA


class PromptTests(unittest.TestCase):
    def test_skill_and_all_references_load(self):
        rules = load_skill_scoring_rules()
        self.assertIn("task2-band-descriptors.md", rules)
        self.assertIn("assessment-criteria.md", rules)
        self.assertIn("scoring-protocol.md", rules)
        self.assertIn("| 9 |", rules)
        self.assertIn("| 0 |", rules)

    def test_missing_reference_stops_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.md"
            with patch("src.prompts.SCORING_REFERENCE_PATHS", (missing,)):
                self.assertEqual(load_skill_scoring_rules(), "")

    def test_descriptor_version_mismatch_stops_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            descriptor = root / "descriptor.md"
            other_one = root / "criteria.md"
            other_two = root / "protocol.md"
            for path in (skill, descriptor, other_one, other_two):
                path.write_text("present but wrong descriptor version", encoding="utf-8")
            with (
                patch("src.prompts.IELTS_WRITING_SKILL_PATH", skill),
                patch("src.prompts.SCORING_REFERENCE_PATHS", (descriptor, other_one, other_two)),
            ):
                self.assertEqual(load_skill_scoring_rules(), "")

    def test_scoring_prompt_is_independent_and_score_only(self):
        prompt = build_scoring_prompt("Task 2", "Question", "Essay text")
        self.assertIn("score-only schema", prompt)
        self.assertIn("independently", prompt)
        self.assertIn("positive_evidence", prompt)
        self.assertIn("limitation_evidence", prompt)
        self.assertIn("why_not_lower_band", prompt)
        self.assertIn("A single local error cannot determine a band", prompt)
        self.assertIn("Do not return or infer an Overall Band", prompt)
        self.assertNotIn("provisional overall", prompt.casefold())
        self.assertNotIn("## 11.", prompt)
        self.assertEqual(prompt, build_structured_grading_prompt("Task 2", "Question", "Essay text"))

    def test_teaching_stage_cannot_return_scores(self):
        prompt = build_teaching_prompt(
            "Task 2", "Question", "Essay text", {"criteria": [], "overall_band": 6.5}
        )
        self.assertIn("locked", prompt.casefold())
        self.assertIn("Do not output `criteria`", prompt)
        self.assertNotIn("criteria", TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["properties"])
        self.assertNotIn("criteria", TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["required"])
        self.assertIn("may be an empty list", prompt)

    def test_model_prompt_preserves_submitted_text_verbatim(self):
        essay = "First paragraph — don’t collapse.\r\n\r\nSecond-line with a hyphen.\n"
        prompt = build_scoring_prompt("Task 2", "Question “as typed”", essay)
        between_markers = prompt.split("<<<BEGIN_STUDENT_ESSAY>>>\n", 1)[1].split(
            "\n<<<END_STUDENT_ESSAY>>>", 1
        )[0]
        self.assertEqual(between_markers, essay)

    def test_unverified_band_samples_are_not_loaded_as_anchors(self):
        self.assertEqual(load_band_sample_anchors(), "")

    def test_forbidden_pseudo_rules_do_not_reappear(self):
        root = Path(__file__).resolve().parents[1]
        policy_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [root / "skills" / "ielts-writing" / "SKILL.md", root / "src" / "prompts.py"]
        ).casefold()
        forbidden = (
            "ai scoring is usually 0.5",
            "repeat the same word more than three times",
            "template essay is capped at band 6",
            "must use a relative clause",
            "provisional level",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, policy_text)

    def test_task1_is_not_silently_scored(self):
        with self.assertRaises(ValueError):
            build_scoring_prompt("Task 1", "Question", "Essay text")


if __name__ == "__main__":
    unittest.main()
