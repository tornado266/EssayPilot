import unittest

from src.report_schema import (
    CRITERIA,
    ExaminerResultError,
    OVERALL_CALIBRATION_VERSION,
    calculate_descriptor_overall,
    calculate_overall,
    estimated_band_range,
    feedback_quality_flags,
    format_overall_band,
    format_practice_band_interval,
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
            {
                "criterion": label, "score": score,
                "reason": "表达清楚，但展开有限。",
                "evidence": ["Public transport reduces traffic.", "Governments should improve bus services."],
                "positive_evidence": ["Public transport reduces traffic."],
                "limitation_evidence": ["Governments should improve bus services."],
                "next_band_limit": "进一步解释观点。",
            }
            for label, score in zip(CRITERIA, [7, 6, 6, 7], strict=True)
        ],
        "priorities": [
            {"title": "展开观点", "evidence": "Governments should improve bus services.", "why": "论证太短。", "action": "补充为什么公交改善能缓解交通。", "criterion": "TR", "action_type": "develop", "success_check": "原句后出现清楚的原因和结果。"},
            {"title": "补足逻辑", "evidence": "Public transport reduces traffic.", "why": "关系没有解释。", "action": "补充中间推理。", "criterion": "CC", "action_type": "support", "success_check": "读者能顺着原因理解结论。"},
        ],
        "problems": [{"title": "支撑不足", "evidence": "Public transport reduces traffic.", "why": "观点没有继续展开。", "action": "补充一个例子。", "criterion": "TR", "action_type": "support", "success_check": "例子能直接证明观点。"}] * 2,
        "sentence_corrections": [{"original": "Public transport reduces traffic.", "problem": "论证简略", "improved": "Reliable public transport can reduce urban congestion."}] * 3,
        "paragraph_feedback": [{"paragraph": 1, "strength": "观点清楚", "limitation": "展开不足", "improvement": "增加因果解释"}],
        "band_75_rewrite": "Reliable public transport can reduce urban congestion.",
        "useful_expressions": [{
            "expression": "urban congestion", "meaning": "城市拥堵",
            "usage_note": "用于城市交通题。", "example": "It reduces urban congestion.",
            "function_category": "core_collocation",
        }] * 6,
        "next_practice": {"task": "Write a paragraph about public transport.", "sentence_pattern": "If..., then...", "warning": "避免没有解释的断言。"},
        "sentence_training": [
            {"original": "Governments should improve bus services.", "goal": "展开这一观点", "reference": "Governments should improve bus services because reliable routes reduce car dependence."},
            {"original": "Public transport reduces traffic.", "goal": "补足逻辑", "reference": "Public transport reduces traffic by giving commuters a practical alternative to driving."},
        ],
        "logic_training": [{"problem": "观点简略", "original": "Public transport reduces traffic.", "task": "补充因果链", "requirements": ["增加一层解释"]}],
        "error_tags": ["idea_development"],
    }


class ReportSchemaTests(unittest.TestCase):
    def test_program_calculates_half_band(self):
        result = validate_examiner_result(valid_result(), ESSAY)
        self.assertEqual(result["raw_overall_band"], 6.5)
        self.assertEqual(result["overall_band"], 7.0)
        self.assertEqual(result["overall_calibration_version"], OVERALL_CALIBRATION_VERSION)
        report = examiner_result_to_markdown(result)
        self.assertIn("**Overall：7.0**", report)
        self.assertNotIn("预估分数区间", report)
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
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 7.0)

    def test_allows_separate_exact_lexical_items(self):
        data = valid_result()
        data["criteria"][2]["evidence"] = ["transport", "traffic", "services"]
        self.assertEqual(validate_examiner_result(data, ESSAY)["overall_band"], 7.0)

    def test_exact_evidence_normalizes_smart_typography_and_newlines(self):
        essay = ESSAY + "\r\n\r\nDon’t use long‑term shortcuts."
        data = valid_result()
        data["criteria"][0]["evidence"] = ["Don't use long-term shortcuts."]
        self.assertEqual(validate_examiner_result(data, essay)["overall_band"], 7.0)

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
        self.assertEqual(calculate_descriptor_overall(first), 6.5)
        self.assertEqual(calculate_descriptor_overall(second), 7.0)
        self.assertEqual(calculate_overall(first), 7.0)
        self.assertEqual(calculate_overall(second), 7.5)
        self.assertEqual(calculate_descriptor_overall([{"score": 9}] * 4), 9.0)
        self.assertEqual(calculate_overall([{"score": 9}] * 4), 9.0)
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
        self.assertEqual(result["raw_overall_band"], 7.0)
        self.assertEqual(result["overall_band"], 7.5)
        self.assertEqual(estimated_band_range(result), (7.0, 9.0))

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
        self.assertEqual(estimated_band_range(result), (6.5, 8.5))

    def test_practice_interval_v1_boundaries_and_caps(self):
        expected = {
            0.0: (0.0, 0.5), 4.5: (4.0, 5.0), 5.0: (4.0, 6.0),
            6.0: (5.0, 7.0), 6.5: (6.0, 7.5), 7.0: (6.5, 8.5),
            8.0: (7.5, 9.0), 9.0: (8.5, 9.0),
        }
        for score, interval in expected.items():
            with self.subTest(score=score):
                self.assertEqual(estimated_band_range({"overall_band": score}), interval)
                self.assertEqual(format_practice_band_interval(score), f"{interval[0]:.1f}–{interval[1]:.1f}")

    def test_point_overall_formatter_validates_half_band_scores(self):
        self.assertEqual(format_overall_band(7), "7.0")
        self.assertEqual(format_overall_band(6.5), "6.5")
        self.assertEqual(format_overall_band(None), "等待评分")
        for invalid in (True, 6.25, -0.5, 9.5, "7.0"):
            with self.subTest(invalid=invalid), self.assertRaises(ExaminerResultError):
                format_overall_band(invalid)  # type: ignore[arg-type]

    def test_first_priority_must_match_locked_limitation_and_link_training(self):
        data = valid_result()
        data["priorities"][0]["evidence"] = "Public transport reduces traffic."
        with self.assertRaisesRegex(ExaminerResultError, "must equal limitation evidence"):
            validate_examiner_result(data, ESSAY)
        data = valid_result()
        data["sentence_training"] = data["sentence_training"][:1]
        data["logic_training"] = []
        with self.assertRaisesRegex(ExaminerResultError, "Every priority must link"):
            validate_examiner_result(data, ESSAY)

    def test_rejects_invalid_coaching_contract_and_pseudo_scoring_rule(self):
        data = valid_result()
        data["priorities"][0]["action_type"] = "memorize_template"
        with self.assertRaisesRegex(ExaminerResultError, "invalid action type"):
            validate_examiner_result(data, ESSAY)
        data = valid_result()
        data["priorities"][0]["success_check"] = "IELTS要求必须写5段。"
        with self.assertRaisesRegex(ExaminerResultError, "must not present"):
            validate_examiner_result(data, ESSAY)

    def test_feedback_quality_flags_are_fully_deterministic(self):
        data = valid_result()
        flags = feedback_quality_flags(data, ESSAY, {"criteria": data["criteria"]})
        self.assertTrue(flags["structure_valid"])
        self.assertTrue(flags["evidence_valid"])
        self.assertTrue(flags["primary_limitation_aligned"])
        self.assertTrue(flags["feedback_training_closed_loop"])
        self.assertTrue(flags["action_success_complete"])
        self.assertEqual(flags["pseudo_scoring_rule_count"], 0)

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
