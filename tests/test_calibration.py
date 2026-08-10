import unittest

from scripts.run_calibration import gold_metrics, repeatability_metrics, validate_dataset


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


if __name__ == "__main__":
    unittest.main()
