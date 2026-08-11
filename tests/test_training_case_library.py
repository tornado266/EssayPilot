import json
import tempfile
import unittest
from pathlib import Path

from src.training_case_library import (
    MAX_EXCERPT_WORDS,
    TrainingCaseError,
    assert_not_scoring_reference,
    attach_training_target_metadata,
    bounded_excerpt,
    find_one_case,
)
from src.text_utils import count_words


EXCERPT = (
    "Many students make a clear claim about public transport. "
    "However, they do not explain why the policy would reduce congestion. "
    "A consequence sentence would connect the claim to the result."
)


def case(case_id: str, *, context: str = "body_paragraph", approved: bool = True) -> dict:
    return {
        "case_id": case_id,
        "source": "kaggle_ielts",
        "task_type": "task2",
        "problem_tags": ["TR.idea_development"],
        "training_goal": "develop_an_idea",
        "essay_context": context,
        "student_excerpt": EXCERPT,
        "similarity_explanation": "两段都提出了观点，但没有继续解释原因或结果。",
        "observation_question": "哪一句可以补充原因或结果？",
        "human_feedback_quality": "high",
        "review_status": "approved" if approved else "candidate",
        "training_value": "core",
        "use_for_training": True,
        "original_overall_score": 9.0,
        "score_confidence": "low",
        "use_for_score_calibration": False,
        "essay_clean": "This full essay must never be returned to the UI.",
    }


class TrainingCaseLibraryTests(unittest.TestCase):
    def _library(self, records: list[dict]) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "cases.jsonl"
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        return temp, path

    def test_returns_at_most_one_approved_excerpt_without_score_or_full_essay(self):
        temp, path = self._library([
            case("case_z", context="sentence"),
            case("case_a", context="body_paragraph"),
        ])
        self.addCleanup(temp.cleanup)
        match = find_one_case(
            "TR.idea_development", "body_paragraph", training_goal="develop_an_idea", library_path=path
        )
        self.assertIsNotNone(match)
        public = match.as_public_dict()
        self.assertEqual(public["case_id"], "case_a")
        self.assertNotIn("score", str(public).casefold())
        self.assertNotIn("full essay", str(public).casefold())
        self.assertLessEqual(count_words(public["excerpt"]), MAX_EXCERPT_WORDS)

    def test_unapproved_or_unmatched_cases_are_not_used_as_fallbacks(self):
        temp, path = self._library([case("case_a", approved=False)])
        self.addCleanup(temp.cleanup)
        self.assertIsNone(find_one_case("TR.idea_development", "body_paragraph", library_path=path))
        self.assertIsNone(find_one_case("LR.collocation", "sentence", library_path=path))

    def test_invalid_calibration_flag_fails_closed(self):
        invalid = case("case_a")
        invalid["use_for_score_calibration"] = True
        temp, path = self._library([invalid])
        self.addCleanup(temp.cleanup)
        with self.assertRaises(TrainingCaseError):
            find_one_case("TR.idea_development", "body_paragraph", library_path=path)

    def test_scoring_reference_guard_rejects_kaggle_records(self):
        with self.assertRaises(TrainingCaseError):
            assert_not_scoring_reference([case("case_a")])

    def test_excerpt_requires_two_to_four_contiguous_sentences(self):
        self.assertIsNone(bounded_excerpt("Only one sentence is available."))
        excerpt = bounded_excerpt(EXCERPT + " A fourth sentence is here. A fifth sentence is hidden.")
        self.assertEqual(excerpt.count("."), 4)
        self.assertNotIn("fifth", excerpt.casefold())

    def test_new_reports_get_hidden_metadata_but_unknown_priorities_do_not(self):
        report = {
            "priorities": [{"title": "展开观点", "why": "论证太短", "action": "补充因果解释"}],
            "error_tags": ["idea_development"],
        }
        enriched = attach_training_target_metadata(report)
        self.assertEqual(enriched["priorities"][0]["problem_tag"], "TR.idea_development")
        self.assertNotIn("problem_tag", report["priorities"][0])
        unknown = attach_training_target_metadata({"priorities": [{"title": "其他", "why": "", "action": ""}], "error_tags": []})
        self.assertNotIn("problem_tag", unknown["priorities"][0])


if __name__ == "__main__":
    unittest.main()
