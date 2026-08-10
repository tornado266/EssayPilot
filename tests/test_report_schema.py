import unittest

from src.report_schema import (
    CRITERIA,
    ExaminerResultError,
    calculate_overall,
    estimated_band_range,
    submission_hash,
    validate_examiner_result,
    validate_scoring_decision,
)
from src.chinese_report import examiner_result_to_markdown


ESSAY = "Public transport reduces traffic. Governments should improve bus services."


def valid_result():
    return {
        "essay_topic_category": "cities_transport",
        "summary": "立场清楚，但论证仍然偏简略。",
        "criteria": [
            {"criterion": label, "score": score, "reason": "表达清楚，但展开有限。", "evidence": ["Public transport reduces traffic."], "next_band_limit": "进一步解释观点。"}
            for label, score in zip(CRITERIA, [7, 6, 6, 7], strict=True)
        ],
        "priorities": [{"title": "展开观点", "evidence": "Public transport reduces traffic.", "why": "论证太短。", "action": "补充因果解释。"}] * 2,
        "problems": [{"title": "支撑不足", "evidence": "Public transport reduces traffic.", "why": "观点没有继续展开。", "action": "补充一个例子。"}] * 2,
        "sentence_corrections": [{"original": "Public transport reduces traffic.", "problem": "论证简略", "improved": "Reliable public transport can reduce urban congestion."}] * 3,
        "paragraph_feedback": [{"paragraph": 1, "strength": "观点清楚", "limitation": "展开不足", "improvement": "增加因果解释"}],
        "band_75_rewrite": "Reliable public transport can reduce urban congestion.",
        "useful_expressions": [{
            "expression": "urban congestion", "meaning": "城市拥堵",
            "usage_note": "用于城市交通题。", "example": "It reduces urban congestion.",
            "function_category": "core_collocation",
        }] * 6,
        "next_practice": {"task": "Write a paragraph about public transport.", "sentence_pattern": "If..., then...", "warning": "避免没有解释的断言。"},
        "sentence_training": [{"original": "Public transport reduces traffic.", "goal": "展开这一观点", "reference": "Reliable public transport can reduce congestion."}] * 2,
        "logic_training": [{"problem": "观点简略", "original": "Public transport reduces traffic.", "task": "补充因果链", "requirements": ["增加一层解释"]}],
        "error_tags": ["idea_development"],
    }


class ReportSchemaTests(unittest.TestCase):
    def test_program_calculates_half_band(self):
        result = validate_examiner_result(valid_result(), ESSAY)
        self.assertEqual(result["overall_band"], 6.5)
        report = examiner_result_to_markdown(result)
        self.assertIn("**最可能分数：6.5**", report)
        self.assertIn("任务回应（TR）", report)
        self.assertIn("Public transport reduces traffic.", report)
        self.assertIn("Reliable public transport can reduce urban congestion.", report)
        self.assertNotIn("Score Summary", report)

    def test_requires_exact_essay_evidence(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = ["This quote was invented."]
        with self.assertRaises(ExaminerResultError):
            validate_examiner_result(data, ESSAY)

    def test_every_evidence_item_must_be_exact(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = [
            "Public transport reduces traffic.",
            "This quote was invented.",
        ]
        with self.assertRaises(ExaminerResultError):
            validate_examiner_result(data, ESSAY)

    def test_rejects_ellipsis_wrapped_evidence(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = ["…Public transport reduces traffic.…"]
        with self.assertRaises(ExaminerResultError):
            validate_examiner_result(data, ESSAY)

    def test_allows_outer_quote_marks(self):
        data = valid_result()
        data["criteria"][0]["evidence"] = ["“Public transport reduces traffic.”"]
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 6.5)

    def test_allows_separate_exact_lexical_items(self):
        data = valid_result()
        data["criteria"][2]["evidence"] = ["transport", "traffic", "services"]
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 6.5)

    def test_exact_evidence_normalizes_smart_typography_and_newlines(self):
        essay = ESSAY + "\r\n\r\nDon’t use long‑term shortcuts."
        data = valid_result()
        data["criteria"][0]["evidence"] = ["Don't use long-term shortcuts."]
        self.assertEqual(validate_examiner_result(data, essay)["overall_band"], 6.5)

    def test_teaching_evidence_cannot_join_distant_fragments(self):
        data = valid_result()
        data["priorities"][0]["evidence"] = "Public transport / traffic"
        with self.assertRaises(ExaminerResultError):
            validate_examiner_result(data, ESSAY)

    def test_requires_four_scores(self):
        with self.assertRaises(ExaminerResultError):
            calculate_overall(valid_result()["criteria"][:3])

    def test_overall_rounding_is_program_owned(self):
        first = [{"score": value} for value in [7, 7, 6, 6]]
        second = [{"score": value} for value in [7, 7, 7, 6]]
        self.assertEqual(calculate_overall(first), 6.5)
        self.assertEqual(calculate_overall(second), 7.0)
        with self.assertRaises(ExaminerResultError):
            calculate_overall([{"score": value} for value in [7, 7, 6.5, 6]])

    def test_overall_rejects_boolean_missing_and_out_of_range_scores(self):
        invalid_vectors = (
            [7, 7, True, 6],
            [7, 7, None, 6],
            [7, 7, 10, 6],
            [7, 7, -1, 6],
        )
        for vector in invalid_vectors:
            with self.subTest(vector=vector), self.assertRaises(ExaminerResultError):
                calculate_overall([{"score": value} for value in vector])

    def test_independent_asymmetric_criteria_are_allowed(self):
        scoring = {
            "criteria": [
                {
                    "criterion": label,
                    "score": score,
                    "reason": "当前表现",
                    "positive_evidence": ["Public transport reduces traffic."],
                    "limitation_evidence": ["Governments should improve bus services."],
                    "limitation_frequency": "occasional",
                    "readability_impact": "minor",
                    "why_not_lower_band": "Sustained control exceeds the lower band.",
                    "next_band_limit": "下一档差距",
                }
                for label, score in zip(CRITERIA, [4, 8, 8, 7], strict=True)
            ],
            "uncertainty": {"level": "low", "adjacent_band_direction": "none", "reason": "证据充分"},
        }
        result = validate_scoring_decision(scoring, ESSAY)
        self.assertEqual([item["score"] for item in result["criteria"]], [4, 8, 8, 7])
        self.assertEqual(result["overall_band"], 7.0)
        self.assertEqual(estimated_band_range(result), (7.0, 7.0))

    def test_material_uncertainty_drives_a_real_range(self):
        scoring = {
            "criteria": [
                {
                    "criterion": item["criterion"],
                    "score": item["score"],
                    "reason": item["reason"],
                    "positive_evidence": ["Public transport reduces traffic."],
                    "limitation_evidence": ["Governments should improve bus services."],
                    "limitation_frequency": "occasional",
                    "readability_impact": "minor",
                    "why_not_lower_band": "Sustained control exceeds the lower band.",
                    "next_band_limit": item["next_band_limit"],
                }
                for item in valid_result()["criteria"]
            ],
            "uncertainty": {"level": "material", "adjacent_band_direction": "higher", "reason": "相邻档证据接近"},
        }
        result = validate_scoring_decision(scoring, ESSAY)
        self.assertEqual(estimated_band_range(result), (6.5, 7.0))

    def test_recurring_limitation_requires_multiple_exact_examples(self):
        scoring = {
            "criteria": [
                {
                    "criterion": label,
                    "score": 6,
                    "reason": "当前表现",
                    "positive_evidence": ["Public transport reduces traffic."],
                    "limitation_evidence": ["Governments should improve bus services."],
                    "limitation_frequency": "recurring",
                    "readability_impact": "minor",
                    "why_not_lower_band": "Sustained control exceeds the lower band.",
                    "next_band_limit": "下一档差距",
                }
                for label in CRITERIA
            ],
            "uncertainty": {"level": "low", "adjacent_band_direction": "none", "reason": "证据充分"},
        }
        with self.assertRaises(ExaminerResultError):
            validate_scoring_decision(scoring, ESSAY)

    def test_gra_six_cannot_be_justified_by_only_occasional_minor_errors(self):
        criteria = []
        for label in CRITERIA:
            criteria.append(
                {
                    "criterion": label,
                    "score": 7 if label != "Grammatical Range and Accuracy" else 6,
                    "reason": "当前表现",
                    "positive_evidence": ["Public transport reduces traffic."],
                    "limitation_evidence": ["Governments should improve bus services."],
                    "limitation_frequency": "occasional",
                    "readability_impact": "minor",
                    "why_not_lower_band": "Sustained control exceeds the lower band.",
                    "next_band_limit": "下一档差距",
                }
            )
        scoring = {
            "criteria": criteria,
            "uncertainty": {"level": "low", "adjacent_band_direction": "none", "reason": "证据充分"},
        }
        with self.assertRaises(ExaminerResultError):
            validate_scoring_decision(scoring, ESSAY)

    def test_submission_hash_ignores_spacing_and_case(self):
        self.assertEqual(
            submission_hash(" Question ", "Essay   Text"),
            submission_hash("question", "essay text"),
        )


if __name__ == "__main__":
    unittest.main()
