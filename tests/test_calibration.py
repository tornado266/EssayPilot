import unittest

from scripts.run_calibration import (
    BlindModelInput,
    CalibrationCase,
    EvaluationLabel,
    acceptance_status,
    apply_split_manifest,
    gold_metrics,
    normalize_cases,
    repeatability_metrics,
    run_evaluation,
    summarize_gold,
    validate_dataset,
)


class CalibrationTests(unittest.TestCase):
    def test_synthetic_case_is_rejected_as_gold(self):
        case = {
            "id": "synthetic",
            "question": "Question",
            "essay": "Essay",
            "source_type": "synthetic_regression",
            "provenance": "fixture",
        }
        validate_dataset([case], "repeatability")
        with self.assertRaises(ValueError):
            validate_dataset([case], "gold")

    def test_repeatability_metrics_report_spread_and_agreement(self):
        snapshots = [
            {"Overall Band": 6.5, "Task Response": 6.0, "Coherence & Cohesion": 7.0, "Lexical Resource": 6.0, "Grammar Range & Accuracy": 7.0},
            {"Overall Band": 7.0, "Task Response": 7.0, "Coherence & Cohesion": 7.0, "Lexical Resource": 6.0, "Grammar Range & Accuracy": 7.0},
        ]
        metrics = repeatability_metrics(snapshots)
        self.assertEqual(metrics["max_spread"]["Overall Band"], 0.5)
        self.assertEqual(metrics["max_spread"]["Task Response"], 1.0)

    def test_gold_metrics_expose_mae_bias_and_confusion(self):
        record = {
            "predicted": {"Overall Band": 6.5, "Task Response": 6.0},
            "gold": {"overall": 7.0, "criteria": {"Task Response": 7.0}},
        }
        metrics = gold_metrics([record])
        self.assertEqual(metrics["mae"]["Overall Band"], 0.5)
        self.assertEqual(metrics["mean_bias"]["Task Response"], -1.0)
        self.assertIsNone(metrics["weighted_kappa"])

    def test_private_gold_accepts_overall_without_criterion_labels(self):
        payload = {
            "source_type": "official_internal",
            "cases": [
                {
                    "model_input": {"task_prompt": "Question", "candidate_response": "Essay"},
                    "evaluation": {
                        "case_id": "official-01",
                        "expected_overall": 7.5,
                        "examiner_comment": "Must stay private",
                        "source_heading": "Official source",
                    },
                }
            ],
        }
        cases = normalize_cases(payload)
        validate_dataset(cases, "gold")
        self.assertEqual(cases[0].evaluation.expected_overall, 7.5)

    def test_private_manifest_separates_holdout_and_interval_label(self):
        payload = {
            "source_type": "official_internal",
            "cases": [
                {
                    "model_input": {"task_prompt": "Question", "candidate_response": "Essay"},
                    "evaluation": {
                        "case_id": "case-1",
                        "expected_overall": 8.5,
                        "source_heading": "Official source",
                    },
                }
            ],
        }
        cases = apply_split_manifest(
            normalize_cases(payload),
            {"sensitivity": ["case-1"], "label_ranges": {"case-1": [8.0, 8.5]}},
        )
        self.assertEqual(cases[0].split, "sensitivity")
        self.assertEqual(cases[0].evaluation.expected_overall_range, (8.0, 8.5))

    def test_interval_label_has_zero_error_inside_range(self):
        summary = summarize_gold([
            {
                "case_id": "case-1",
                "expected_overall": 8.5,
                "expected_overall_range": [8.0, 8.5],
                "runs": [{
                    "status": "ok",
                    "snapshot": {
                        "Overall Band": 8.0,
                        "Task Response": 8.0,
                        "Coherence & Cohesion": 8.0,
                        "Lexical Resource": 8.0,
                        "Grammar Range & Accuracy": 8.0,
                    },
                }],
            }
        ])
        self.assertEqual(summary["cases"][0]["absolute_error"], 0.0)

    def test_development_acceptance_requires_registered_case_count(self):
        cases = [
            {
                "case_id": f"case-{index}",
                "expected_overall": 7.0,
                "mean_scores": {"Overall Band": 7.0},
                "absolute_error": 0.0,
                "max_spread": {"Overall Band": 0.5},
            }
            for index in range(7)
        ]
        status = acceptance_status(
            {"cases": cases, "overall": {"mae": 0.0, "max_absolute_error": 0.0}},
            "development",
        )
        self.assertTrue(status["passed"])

    def test_labels_never_cross_the_grader_boundary(self):
        received = []

        def grader(**kwargs):
            received.append(kwargs)
            return {
                "structured": {
                    "overall_band": 7.0,
                    "criteria": [
                        {"criterion": "Task Response", "score": 7},
                        {"criterion": "Coherence and Cohesion", "score": 7},
                        {"criterion": "Lexical Resource", "score": 7},
                        {"criterion": "Grammatical Range and Accuracy", "score": 7},
                    ],
                },
                "usage": {},
                "model": "test-model",
                "prompt_version": "test",
            }

        case = CalibrationCase(
            model_input=BlindModelInput("Question only", "Essay only"),
            evaluation=EvaluationLabel("secret-case-id", 7.5, "Private examiner comment"),
            source_type="official_internal",
            provenance="Official source",
        )
        results, _ = run_evaluation([case], 1, grader=grader)

        self.assertEqual(received[0]["topic"], "Question only")
        self.assertEqual(received[0]["essay"], "Essay only")
        self.assertNotIn("secret-case-id", str(received[0]))
        self.assertNotIn("Private examiner comment", str(received[0]))
        summary = summarize_gold(results)
        self.assertEqual(summary["cases"][0]["absolute_error"], 0.5)

    def test_failed_run_is_recorded_without_aborting_remaining_runs(self):
        calls = 0

        def grader(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["audit_hook"]({"stage": "scoring", "raw_response": "invalid"})
                raise ValueError("invalid evidence")
            return {
                "structured": {
                    "overall_band": 7.0,
                    "criteria": [
                        {"criterion": "Task Response", "score": 7},
                        {"criterion": "Coherence and Cohesion", "score": 7},
                        {"criterion": "Lexical Resource", "score": 7},
                        {"criterion": "Grammatical Range and Accuracy", "score": 7},
                    ],
                },
                "usage": {},
                "model": "test-model",
                "prompt_version": "test",
            }

        case = CalibrationCase(
            model_input=BlindModelInput("Question", "Essay"),
            evaluation=EvaluationLabel("official-01", 7.0),
            source_type="official_internal",
            provenance="Official source",
        )
        results, events = run_evaluation([case], 2, grader=grader)

        self.assertEqual([run["status"] for run in results[0]["runs"]], ["error", "ok"])
        self.assertEqual(results[0]["runs"][0]["error_type"], "ValueError")
        self.assertEqual(events[0]["raw_response"], "invalid")
        summary = summarize_gold(results)
        self.assertEqual(summary["overall"]["successful_runs"], 1)
        self.assertEqual(summary["overall"]["failed_runs"], 1)

    def test_runtime_leak_check_rejects_evaluation_metadata_in_messages(self):
        def grader(**kwargs):
            kwargs["audit_hook"](
                {
                    "stage": "scoring",
                    "messages": [{"role": "user", "content": "secret-case-id"}],
                    "raw_response": "{}",
                }
            )
            return {
                "structured": {
                    "overall_band": 7.0,
                    "criteria": [
                        {"criterion": "Task Response", "score": 7},
                        {"criterion": "Coherence and Cohesion", "score": 7},
                        {"criterion": "Lexical Resource", "score": 7},
                        {"criterion": "Grammatical Range and Accuracy", "score": 7},
                    ],
                },
                "usage": {},
                "model": "test-model",
                "prompt_version": "test",
            }

        case = CalibrationCase(
            model_input=BlindModelInput("Question", "Essay"),
            evaluation=EvaluationLabel("secret-case-id", 7.0, "private comment"),
            source_type="official_internal",
            provenance="private source",
        )
        with self.assertRaisesRegex(RuntimeError, "metadata leaked"):
            run_evaluation([case], 1, grader=grader)


if __name__ == "__main__":
    unittest.main()
