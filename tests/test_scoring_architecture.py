import json
import unittest
from pathlib import Path

from src.report_schema import (
    EXAMINER_JSON_SCHEMA,
    SCHEMA_VERSION,
    SCORING_DECISION_JSON_SCHEMA,
    TEACHING_FEEDBACK_JSON_SCHEMA,
)


class ScoringArchitectureTests(unittest.TestCase):
    def test_external_schema_stays_compatible_with_integer_criteria(self):
        self.assertEqual(SCHEMA_VERSION, "2.2")
        criterion_schema = EXAMINER_JSON_SCHEMA["schema"]["properties"]["criteria"]["items"]
        self.assertEqual(criterion_schema["properties"]["score"]["type"], "integer")
        self.assertIn("criteria", EXAMINER_JSON_SCHEMA["schema"]["required"])

    def test_only_scoring_stage_can_return_criteria(self):
        self.assertIn("criteria", SCORING_DECISION_JSON_SCHEMA["schema"]["properties"])
        self.assertNotIn("overall_band", SCORING_DECISION_JSON_SCHEMA["schema"]["properties"])
        self.assertNotIn("criteria", TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["properties"])
        self.assertNotIn("overall_band", TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["properties"])
        internal = SCORING_DECISION_JSON_SCHEMA["schema"]["properties"]["criteria"]["items"]
        self.assertIn("positive_evidence", internal["properties"])
        self.assertIn("limitation_evidence", internal["properties"])
        self.assertIn("limitation_frequency", internal["properties"])
        self.assertEqual(
            internal["properties"]["positive_evidence"]["items"]["maxLength"], 240
        )
        external = EXAMINER_JSON_SCHEMA["schema"]["properties"]["criteria"]["items"]
        self.assertIn("evidence", external["properties"])
        self.assertNotIn("positive_evidence", external["properties"])

    def test_unverified_samples_are_empty_and_regressions_are_synthetic(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(json.loads((root / "data" / "band_samples.json").read_text(encoding="utf-8")), [])
        cases = json.loads((root / "data" / "calibration_cases.json").read_text(encoding="utf-8"))
        self.assertTrue(all(case["source_type"] == "synthetic_regression" for case in cases))
        self.assertTrue(all("gold" not in case for case in cases))

    def test_unreachable_workspace_and_legacy_graders_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "app.py").read_text(encoding="utf-8-sig")
        grader = (root / "src" / "ai_grader.py").read_text(encoding="utf-8")
        storage = (root / "src" / "storage.py").read_text(encoding="utf-8")
        cloud = (root / "src" / "cloud_store.py").read_text(encoding="utf-8")
        self.assertNotIn("workspace_bookmarks = []", app)
        self.assertNotIn("def extract_score_snapshot", app)
        self.assertNotIn("def _grade_essay_legacy", grader)
        self.assertNotIn("def _compare_draft_progress_legacy", grader)
        self.assertIn("def calculate_overall_band", app)  # legacy report-reader compatibility
        legacy_reader = app.split("def calculate_overall_band", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("calculate_overall(", legacy_reader)
        self.assertNotIn("math.floor", legacy_reader)
        self.assertIn("calculate_overall(structured_criteria)", storage)
        self.assertIn('"p_overall_band": structured["overall_band"]', cloud)


if __name__ == "__main__":
    unittest.main()
