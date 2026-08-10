import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from src.ai_grader import AIGraderError, grade_essay_package
from src.report_schema import SCORING_DECISION_JSON_SCHEMA, TEACHING_FEEDBACK_JSON_SCHEMA
from test_report_schema import ESSAY, valid_result


class FakeCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class TwoStageGraderTests(unittest.TestCase):
    def scoring_payload(self):
        return {
            "criteria": deepcopy(valid_result()["criteria"]),
            "uncertainty": {
                "level": "low",
                "adjacent_band_direction": "none",
                "reason": "四项证据足以支持单点判断。",
            },
        }

    def teaching_payload(self):
        data = valid_result()
        del data["criteria"]
        return data

    def test_score_is_validated_then_locked_before_teaching(self):
        completions = FakeCompletions(
            [json.dumps(self.scoring_payload(), ensure_ascii=False), json.dumps(self.teaching_payload(), ensure_ascii=False)]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            package = grade_essay_package(task_type="Task 2", topic="Question", essay=ESSAY)

        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(
            completions.calls[0]["response_format"]["json_schema"]["name"],
            SCORING_DECISION_JSON_SCHEMA["name"],
        )
        self.assertEqual(
            completions.calls[1]["response_format"]["json_schema"]["name"],
            TEACHING_FEEDBACK_JSON_SCHEMA["name"],
        )
        self.assertEqual(package["structured"]["overall_band"], 6.5)
        self.assertEqual(package["estimated_band_range"], [6.5, 6.5])
        self.assertEqual(package["usage"]["total_tokens"], 60)
        self.assertIn("estimated practice band", package["report"])

    def test_private_audit_hook_observes_both_calls_without_changing_them(self):
        completions = FakeCompletions(
            [json.dumps(self.scoring_payload(), ensure_ascii=False), json.dumps(self.teaching_payload(), ensure_ascii=False)]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        events = []
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            grade_essay_package(task_type="Task 2", topic="Question", essay=ESSAY, audit_hook=events.append)

        self.assertEqual([event["stage"] for event in events], ["scoring", "teaching"])
        self.assertEqual(events[0]["messages"], completions.calls[0]["messages"])
        self.assertEqual(events[1]["messages"], completions.calls[1]["messages"])
        self.assertNotIn("api_key", str(events).casefold())

    def test_invalid_teaching_response_fails_the_whole_package(self):
        completions = FakeCompletions([json.dumps(self.scoring_payload()), "{"])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            with self.assertRaises(AIGraderError):
                grade_essay_package(task_type="Task 2", topic="Question", essay=ESSAY)


if __name__ == "__main__":
    unittest.main()
