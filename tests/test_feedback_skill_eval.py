import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_feedback_skill_eval import _gate, _require_previous_gate, parse_args
from src.report_schema import FEEDBACK_PROMPT_VERSION


class FeedbackSkillEvalTests(unittest.TestCase):
    def test_defaults_use_openai_scoring_and_deepseek_none_feedback(self):
        with patch("sys.argv", ["run_feedback_skill_eval.py"]):
            args = parse_args()
        self.assertEqual(args.scoring_provider, "OpenAI")
        self.assertEqual(args.scoring_model, "gpt-5.4-mini-2026-03-17")
        self.assertEqual(args.scoring_reasoning_effort, "none")
        self.assertEqual(args.feedback_provider, "DeepSeek")
        self.assertEqual(args.feedback_model, "deepseek-v4-pro")
        self.assertEqual(args.feedback_reasoning_effort, "none")
        self.assertFalse(args.execute)

    def test_validation_and_holdout_gates_are_exact(self):
        validation = {
            "selected_cases": 8, "complete_cases": 8, "structure_valid_count": 8,
            "evidence_valid_count": 8, "primary_limitation_aligned_count": 8,
            "feedback_training_closed_loop_count": 7,
            "action_success_complete_count": 7, "pseudo_scoring_rule_count": 0,
        }
        self.assertTrue(_gate("validation", validation))
        validation["primary_limitation_aligned_count"] = 7
        self.assertFalse(_gate("validation", validation))

    def test_holdout_stays_locked_until_current_official_gate_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(RuntimeError):
                _require_previous_gate("holdout", output)
            metrics_path = output / "official-metrics.json"
            metrics_path.write_text(json.dumps({
                "passed": True, "feedback_prompt_version": "old-version"
            }), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _require_previous_gate("holdout", output)
            metrics_path.write_text(json.dumps({
                "passed": True, "feedback_prompt_version": FEEDBACK_PROMPT_VERSION
            }), encoding="utf-8")
            _require_previous_gate("holdout", output)


if __name__ == "__main__":
    unittest.main()
