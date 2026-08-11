import unittest

from scripts.select_scoring_model import select_cheapest_passing


def calibration_run(*, cases, cost, provider="OpenAI", model="model"):
    attempted = len(cases) * 3
    return {
        "metadata": {
            "provider": provider,
            "model": model,
            "usage": {"estimated_usd": cost},
        },
        "summary": {
            "cases": cases,
            "overall": {
                "mae": sum(item["absolute_error"] for item in cases) / len(cases),
                "max_absolute_error": max(item["absolute_error"] for item in cases),
                "successful_runs": attempted,
            },
        },
        "results": [
            {"runs": [{"status": "ok", "latency_seconds": 1.0}] * 3}
            for _ in cases
        ],
    }


def cases(count, error=0.5, expected=7.0):
    return [
        {
            "case_id": f"case-{index}",
            "expected_overall": expected if index > 1 else 5.0,
            "absolute_error": error,
            "max_spread": {"Overall Band": 0.5},
        }
        for index in range(count)
    ]


class ScoringModelSelectionTests(unittest.TestCase):
    def test_quality_gates_precede_cost(self):
        baseline = calibration_run(cases=cases(7, 0.5), cost=1.0)
        cheap_failed = calibration_run(cases=cases(7, 0.75), cost=0.01)
        cheap_holdout_failed = calibration_run(cases=cases(6, 0.75), cost=0.01)
        passing_dev = calibration_run(cases=cases(7, 0.5), cost=0.2, model="passing")
        passing_holdout = calibration_run(cases=cases(6, 0.5), cost=0.2, model="passing")
        result = select_cheapest_passing(
            baseline,
            [
                ("cheap", cheap_failed, cheap_holdout_failed),
                ("passing", passing_dev, passing_holdout),
            ],
        )
        self.assertEqual(result["selected"]["name"], "passing")

    def test_cheapest_passing_candidate_wins(self):
        baseline = calibration_run(cases=cases(7, 0.5), cost=1.0)
        flash_dev = calibration_run(cases=cases(7, 0.5), cost=0.03, provider="DeepSeek", model="flash")
        flash_holdout = calibration_run(cases=cases(6, 0.5), cost=0.03, provider="DeepSeek", model="flash")
        mini_dev = calibration_run(cases=cases(7, 0.5), cost=0.2, model="mini")
        mini_holdout = calibration_run(cases=cases(6, 0.5), cost=0.2, model="mini")
        result = select_cheapest_passing(
            baseline,
            [("mini", mini_dev, mini_holdout), ("flash", flash_dev, flash_holdout)],
        )
        self.assertEqual(result["selected"]["name"], "flash")


if __name__ == "__main__":
    unittest.main()
