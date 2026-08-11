import unittest

from scripts.compare_calibration import acceptance, reasoning_gate, render_report


def run(mae, errors, spreads, success=21):
    cases = []
    for index, (error, spread) in enumerate(zip(errors, spreads, strict=True), 1):
        expected = 5.0 if index <= 2 else 7.0
        cases.append(
            {
                "case_id": f"case-{index}",
                "expected_overall": expected,
                "mean_scores": {"Overall Band": None if error is None else expected + error},
                "absolute_error": error,
                "max_spread": {"Overall Band": spread},
            }
        )
    return {
        "metadata": {"usage": {"estimated_usd": 1.0}},
        "summary": {
            "cases": cases,
            "overall": {
                "mae": mae,
                "within_0_5_rate": 1.0,
                "max_absolute_error": max(value for value in errors if value is not None),
                "successful_runs": success,
                "attempted_runs": 21,
                "success_rate": success / 21,
            },
        },
    }


class CompareCalibrationTests(unittest.TestCase):
    def test_acceptance_requires_all_registered_conditions(self):
        candidate = run(0.4, [0.5] * 6 + [0.75], [0.5] * 7)
        checks = acceptance(candidate)
        self.assertTrue(checks["at_least_6_of_7_within_0_5"])
        self.assertTrue(checks["mae_at_most_0_5"])
        self.assertTrue(checks["no_case_error_over_1_0"])  # 0.75 remains allowed

    def test_reasoning_gate_rejects_worse_mae_and_success(self):
        none = run(0.5, [0.5] * 7, [0.5] * 7)
        low = run(0.7, [0.5] * 7, [0.5] * 7, success=20)
        gate = reasoning_gate(none, low)
        self.assertFalse(gate["mae_improves_by_at_least_0_10"])
        self.assertFalse(gate["success_rate_not_worse"])
        self.assertIn("Adopt improved-low: no", render_report([("baseline", none), ("improved-none", none), ("improved-low", low)]))


if __name__ == "__main__":
    unittest.main()
