import unittest

from src.report_schema import (
    CRITERIA,
    ExaminerResultError,
    calculate_overall,
    submission_hash,
    validate_examiner_result,
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
