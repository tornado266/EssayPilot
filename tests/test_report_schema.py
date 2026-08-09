import unittest

from src.report_schema import (
    CRITERIA,
    ExaminerResultError,
    calculate_overall,
    examiner_result_to_markdown,
    submission_hash,
    validate_examiner_result,
)


ESSAY = "Public transport reduces traffic. Governments should improve bus services."


def valid_result():
    return {
        "summary": "The position is clear, but support is brief.",
        "criteria": [
            {"criterion": label, "score": score, "reason": "Clear but limited.", "evidence": ["Public transport reduces traffic."], "next_band_limit": "Develop the idea."}
            for label, score in zip(CRITERIA, [7, 6, 6, 7], strict=True)
        ],
        "priorities": [{"title": "Develop ideas", "evidence": "Public transport reduces traffic.", "why": "It is brief.", "action": "Add explanation."}] * 2,
        "problems": [{"title": "Brief support", "evidence": "Public transport reduces traffic.", "why": "It stops early.", "action": "Add an example."}] * 2,
        "sentence_corrections": [{"original": "Public transport reduces traffic.", "problem": "Brief", "improved": "Reliable public transport can reduce urban congestion."}] * 3,
        "paragraph_feedback": [{"paragraph": 1, "strength": "Clear", "limitation": "Brief", "improvement": "Develop it"}],
        "band_75_rewrite": "Reliable public transport can reduce urban congestion.",
        "useful_expressions": [{"expression": "urban congestion", "meaning": "traffic", "example": "It reduces urban congestion."}] * 3,
        "next_practice": {"task": "Write a paragraph", "sentence_pattern": "If..., then...", "warning": "Avoid unsupported claims."},
        "sentence_training": [{"original": "Public transport reduces traffic.", "goal": "Develop the claim", "reference": "Reliable public transport can reduce congestion."}] * 2,
        "logic_training": [{"problem": "Brief idea", "original": "Public transport reduces traffic.", "task": "Develop it", "requirements": ["Add an explanation"]}],
        "error_tags": ["idea_development"],
    }


class ReportSchemaTests(unittest.TestCase):
    def test_program_calculates_half_band(self):
        result = validate_examiner_result(valid_result(), ESSAY)
        self.assertEqual(result["overall_band"], 6.5)
        self.assertIn("**Likely score: 6.5**", examiner_result_to_markdown(result))

    def test_requires_exact_essay_evidence(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = ["This quote was invented."]
        with self.assertRaises(ExaminerResultError):
            validate_examiner_result(data, ESSAY)

    def test_allows_ellipsis_around_a_real_quote(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = ["…Public transport reduces traffic.…"]
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 6.5)

    def test_allows_a_list_of_exact_lexical_items(self):
        data = valid_result()
        data["criteria"][2]["evidence"] = ['"transport", "traffic", "services"']
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 6.5)

    def test_requires_four_scores(self):
        with self.assertRaises(ExaminerResultError):
            calculate_overall(valid_result()["criteria"][:3])

    def test_submission_hash_ignores_spacing_and_case(self):
        self.assertEqual(
            submission_hash(" Question ", "Essay   Text"),
            submission_hash("question", "essay text"),
        )


if __name__ == "__main__":
    unittest.main()
