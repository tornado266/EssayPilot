"""Recover missing training links without relaxing score or evidence validation."""

import unittest
from copy import deepcopy

from src.chinese_report import examiner_result_to_markdown
from src.report_schema import (
    ExaminerResultError,
    feedback_quality_flags,
    validate_examiner_result,
    validate_teaching_training_links,
)
from test_report_schema import ESSAY, valid_result


class PriorityTrainingLinkTests(unittest.TestCase):
    def test_valid_feedback_is_unchanged(self):
        data = valid_result()
        result, repaired = validate_teaching_training_links(data, ESSAY)
        self.assertEqual(result, validate_examiner_result(data, ESSAY))
        self.assertEqual(repaired, [])

    def test_shorter_training_quote_gets_exact_priority_task(self):
        data = valid_result()
        data["sentence_training"][0]["original"] = "improve bus services"
        before = deepcopy(data)
        with self.assertRaisesRegex(ExaminerResultError, "Every priority must link"):
            validate_examiner_result(data, ESSAY)

        result, repaired = validate_teaching_training_links(data, ESSAY)

        priority = data["priorities"][0]
        self.assertEqual(repaired, [priority["evidence"]])
        self.assertEqual(result["logic_training"][-1], {
            "original": priority["evidence"], "problem": priority["title"],
            "task": priority["action"], "requirements": [priority["success_check"]],
        })
        self.assertEqual(result["sentence_training"], data["sentence_training"])
        self.assertEqual(result["priorities"], data["priorities"])
        self.assertEqual(result["criteria"], data["criteria"])
        self.assertEqual(data, before)
        flags = feedback_quality_flags(result, ESSAY, {"criteria": data["criteria"]})
        self.assertTrue(flags["feedback_training_closed_loop"])
        self.assertTrue(flags["primary_limitation_aligned"])
        self.assertTrue(flags["evidence_valid"])
        again, repeated = validate_teaching_training_links(result, ESSAY)
        self.assertEqual(again, result)
        self.assertEqual(repeated, [])

    def test_both_missing_priorities_route_by_action_without_fabricated_answers(self):
        for action in ("replace", "repair", "proofread_recurring"):
            with self.subTest(action=action):
                data = valid_result()
                data["sentence_training"] = []
                data["logic_training"] = []
                data["priorities"][1].update(criterion="GRA", action_type=action)
                result, repaired = validate_teaching_training_links(data, ESSAY)
                self.assertEqual(len(repaired), 2)
                self.assertEqual(len(result["logic_training"]), 1)
                self.assertEqual(len(result["sentence_training"]), 1)
                sentence = result["sentence_training"][0]
                priority = data["priorities"][1]
                self.assertEqual(sentence["original"], priority["evidence"])
                self.assertIn(priority["action"], sentence["goal"])
                self.assertIn(priority["success_check"], sentence["goal"])
                self.assertEqual(sentence["reference"], "")
                report = examiner_result_to_markdown(result)
                self.assertIn(priority["success_check"], report)
                self.assertNotIn("英文参考：", report)

    def test_full_task_lists_keep_existing_priority_links_and_schema_limits(self):
        for collection, limit, action in (
            ("sentence_training", 4, "repair"),
            ("logic_training", 3, "develop"),
        ):
            for original in ("Public transport reduces traffic.", "improve bus services"):
                with self.subTest(collection=collection, original=original):
                    data = valid_result()
                    kept = deepcopy(data["logic_training"][0])
                    data["sentence_training"] = []
                    data["logic_training"] = [kept]
                    task = {
                        "original": original, "goal": "已有训练", "reference": "",
                    } if collection == "sentence_training" else {
                        "original": original, "problem": "已有任务", "task": "已有训练",
                        "requirements": ["已有要求"],
                    }
                    data[collection] = [deepcopy(task) for _ in range(limit)]
                    data["priorities"][0]["action_type"] = action
                    result, _ = validate_teaching_training_links(data, ESSAY)
                    self.assertLessEqual(len(result["sentence_training"]), 4)
                    self.assertLessEqual(len(result["logic_training"]), 3)
                    flags = feedback_quality_flags(result, ESSAY, {"criteria": data["criteria"]})
                    self.assertTrue(flags["feedback_training_closed_loop"])

    def test_invalid_evidence_and_actions_still_fail(self):
        cases = (
            ("evidence", "This sentence was never submitted.", "does not quote"),
            ("evidence", "Public transport reduces traffic.", "must equal limitation"),
            ("action", "", "no concrete action"),
            ("success_check", "", "no success check"),
            ("action_type", "memorize_template", "invalid action type"),
            ("success_check", "IELTS要求必须写5段。", "must not present"),
        )
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                data = valid_result()
                data["sentence_training"] = []
                data["logic_training"] = []
                data["priorities"][0][field] = value
                with self.assertRaisesRegex(ExaminerResultError, error):
                    validate_teaching_training_links(data, ESSAY)

    def test_invalid_existing_training_quote_is_not_repaired(self):
        data = valid_result()
        data["sentence_training"][0]["original"] = "Invented essay text."
        with self.assertRaisesRegex(ExaminerResultError, "does not quote"):
            validate_teaching_training_links(data, ESSAY)


if __name__ == "__main__":
    unittest.main()
