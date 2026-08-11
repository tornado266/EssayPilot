import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from src.ai_grader import (
    AIGraderError,
    build_client,
    grade_essay_package,
    grade_scoring_decision,
)
from src.report_schema import (
    FEEDBACK_PROMPT_VERSION,
    REPORT_PROMPT_VERSION,
    SCORING_DECISION_JSON_SCHEMA,
    SCORING_PROMPT_VERSION,
    SCORING_SKILL_VERSION,
    TEACHING_FEEDBACK_JSON_SCHEMA,
    validate_scoring_decision,
)
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
            model="response-model",
            system_fingerprint="fp-test",
        )


class TwoStageGraderTests(unittest.TestCase):
    def test_missing_api_key_fails_locally_without_a_provider_call(self):
        with patch(
            "src.ai_grader.get_provider_config",
            return_value=("OPENAI_API_KEY", None, "https://api.openai.com/v1"),
        ):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY is missing"):
                build_client("OpenAI")

    def scoring_payload(self):
        criteria = []
        for item in valid_result()["criteria"]:
            criteria.append(
                {
                    "criterion": item["criterion"],
                    "score": item["score"],
                    "reason": item["reason"],
                    "positive_evidence": ["Public transport reduces traffic."],
                    "limitation_evidence": ["Governments should improve bus services."],
                    "limitation_frequency": "occasional",
                    "readability_impact": "minor",
                    "why_not_lower_band": "Sustained control exceeds the lower descriptor.",
                    "next_band_limit": item["next_band_limit"],
                }
            )
        return {
            "criteria": criteria,
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

        self.assertEqual(package["prompt_version"], REPORT_PROMPT_VERSION)
        self.assertEqual(package["scoring_prompt_version"], SCORING_PROMPT_VERSION)
        self.assertEqual(package["feedback_prompt_version"], FEEDBACK_PROMPT_VERSION)
        self.assertIn("locked_scoring_decision", package["structured"])

        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(
            completions.calls[0]["response_format"]["json_schema"]["name"],
            SCORING_DECISION_JSON_SCHEMA["name"],
        )
        self.assertEqual(
            completions.calls[1]["response_format"]["json_schema"]["name"],
            TEACHING_FEEDBACK_JSON_SCHEMA["name"],
        )
        self.assertEqual(package["structured"]["raw_overall_band"], 6.5)
        self.assertEqual(package["structured"]["overall_band"], 7.0)
        self.assertEqual(package["estimated_band_range"], [6.5, 8.5])
        self.assertEqual(package["usage"]["total_tokens"], 60)

    def test_feedback_upgrade_reuses_locked_scoring_without_a_scoring_call(self):
        completions = FakeCompletions([
            json.dumps(self.teaching_payload(), ensure_ascii=False),
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        locked = validate_scoring_decision(self.scoring_payload(), ESSAY)
        cached_package = {
            "provider": "OpenAI",
            "model": "gpt-5.4-mini-2026-03-17",
            "prompt_version": SCORING_PROMPT_VERSION,
            "skill_version": SCORING_SKILL_VERSION,
            "scoring": locked,
            "usage": {},
        }
        with patch("src.ai_grader.build_client", return_value=client):
            package = grade_essay_package(
                task_type="Task 2",
                topic="Question",
                essay=ESSAY,
                locked_scoring_package=cached_package,
            )
        self.assertEqual(len(completions.calls), 1)
        self.assertTrue(package["scoring_reused"])
        self.assertEqual(package["structured"]["overall_band"], locked["overall_band"])
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
        completions = FakeCompletions([json.dumps(self.scoring_payload()), "{", "{"])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            with self.assertRaises(AIGraderError):
                grade_essay_package(task_type="Task 2", topic="Question", essay=ESSAY)
        self.assertEqual(len(completions.calls), 3)

    def test_invalid_scoring_response_is_retried_once_without_model_fallback(self):
        invalid = self.scoring_payload()
        invalid["criteria"][0]["positive_evidence"] = ["invented quote"]
        completions = FakeCompletions(
            [
                json.dumps(invalid),
                json.dumps(self.scoring_payload()),
                json.dumps(self.teaching_payload(), ensure_ascii=False),
            ]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        events = []
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            package = grade_essay_package(
                task_type="Task 2", topic="Question", essay=ESSAY, audit_hook=events.append
            )

        self.assertEqual([event["attempt"] for event in events], [1, 2, 1])
        self.assertIn("validation_error", events[0])
        self.assertIn("invented quote", completions.calls[1]["messages"][-2]["content"])
        self.assertIn("must be present", completions.calls[1]["messages"][-1]["content"])
        self.assertEqual({call["model"] for call in completions.calls}, {"gpt-5.4-mini-2026-03-17"})
        self.assertEqual(package["usage"]["total_tokens"], 90)

    def test_score_only_entrypoint_does_not_generate_teaching(self):
        completions = FakeCompletions([json.dumps(self.scoring_payload())])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            package = grade_scoring_decision(
                task_type="Task 2", topic="Question", essay=ESSAY
            )

        self.assertEqual(len(completions.calls), 1)
        self.assertEqual(package["structured"]["overall_band"], 7.0)
        self.assertNotIn("report", package)

    def test_deepseek_uses_json_object_and_disables_thinking(self):
        completions = FakeCompletions([json.dumps(self.scoring_payload())])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("DEEPSEEK_API_KEY", "key", "https://api.deepseek.com")),
        ):
            package = grade_scoring_decision(
                task_type="Task 2",
                topic="Question",
                essay=ESSAY,
                provider="DeepSeek",
                model="deepseek-v4-flash",
            )

        call = completions.calls[0]
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertIn("exact JSON shape", call["messages"][-1]["content"])
        self.assertNotIn("json_schema", str(call["response_format"]))
        self.assertEqual(package["provider"], "DeepSeek")

    def test_full_package_can_explicitly_use_deepseek_none_for_both_stages(self):
        completions = FakeCompletions([
            json.dumps(self.scoring_payload(), ensure_ascii=False),
            json.dumps(self.teaching_payload(), ensure_ascii=False),
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch(
                "src.ai_grader.get_provider_config",
                return_value=("DEEPSEEK_API_KEY", "key", "https://api.deepseek.com"),
            ),
        ):
            package = grade_essay_package(
                task_type="Task 2", topic="Question", essay=ESSAY,
                scoring_provider="DeepSeek", scoring_model="deepseek-v4-pro",
                teaching_provider="DeepSeek", teaching_model="deepseek-v4-pro",
                reasoning_effort="none", teaching_reasoning_effort="none",
            )
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual({call["model"] for call in completions.calls}, {"deepseek-v4-pro"})
        self.assertTrue(all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in completions.calls))
        self.assertEqual(package["teaching_provider"], "DeepSeek")
        self.assertEqual(package["teaching_reasoning_effort"], "none")

    def test_scoring_and_feedback_models_are_configured_independently(self):
        completions = FakeCompletions([
            json.dumps(self.scoring_payload(), ensure_ascii=False),
            json.dumps(self.teaching_payload(), ensure_ascii=False),
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch(
                "src.ai_grader.get_provider_config",
                side_effect=lambda provider: (
                    ("DEEPSEEK_API_KEY", "key", "https://api.deepseek.com")
                    if provider == "DeepSeek"
                    else ("OPENAI_API_KEY", "key", "https://api.openai.com/v1")
                ),
            ),
        ):
            package = grade_essay_package(
                task_type="Task 2", topic="Question", essay=ESSAY,
                scoring_provider="OpenAI", scoring_model="gpt-5.4-mini-2026-03-17",
                teaching_provider="DeepSeek", teaching_model="deepseek-v4-pro",
                reasoning_effort="none", teaching_reasoning_effort="none",
            )
        self.assertIn("json_schema", completions.calls[0]["response_format"])
        self.assertEqual(completions.calls[1]["response_format"], {"type": "json_object"})
        self.assertEqual(completions.calls[1]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(package["teaching_provider"], "DeepSeek")
        self.assertEqual(package["teaching_model"], "deepseek-v4-pro")

    def test_deepseek_explicitly_enables_high_and_max_thinking(self):
        for effort in ("high", "max"):
            with self.subTest(effort=effort):
                completions = FakeCompletions([json.dumps(self.scoring_payload())])
                client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
                with (
                    patch("src.ai_grader.build_client", return_value=client),
                    patch(
                        "src.ai_grader.get_provider_config",
                        return_value=(
                            "DEEPSEEK_API_KEY",
                            "key",
                            "https://api.deepseek.com",
                        ),
                    ),
                ):
                    package = grade_scoring_decision(
                        task_type="Task 2",
                        topic="Question",
                        essay=ESSAY,
                        provider="DeepSeek",
                        model="deepseek-v4-pro",
                        reasoning_effort=effort,
                    )

                call = completions.calls[0]
                self.assertEqual(call["reasoning_effort"], effort)
                self.assertEqual(
                    call["extra_body"], {"thinking": {"type": "enabled"}}
                )
                self.assertEqual(call["timeout"], 180.0)
                self.assertEqual(package["reasoning_effort"], effort)

    def test_second_teaching_attempt_drops_only_unsupported_optional_items(self):
        teaching = self.teaching_payload()
        teaching["problems"] = [
            {
                "title": "x", "evidence": "invented quote", "why": "x", "action": "x",
                "criterion": "TR", "action_type": "repair", "success_check": "x",
            }
        ]
        completions = FakeCompletions(
            [
                json.dumps(self.scoring_payload()),
                json.dumps(teaching),
                json.dumps(teaching),
            ]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with (
            patch("src.ai_grader.build_client", return_value=client),
            patch("src.ai_grader.get_provider_config", return_value=("OPENAI_API_KEY", "key", "https://api.openai.com/v1")),
        ):
            package = grade_essay_package(task_type="Task 2", topic="Question", essay=ESSAY)

        self.assertEqual(package["structured"]["problems"], [])
        self.assertEqual(package["sanitized_teaching_fields"], ["problems"])
        self.assertEqual(len(completions.calls), 3)


if __name__ == "__main__":
    unittest.main()
