import math
import unittest

from src.home_dashboard import HomeSummary, build_home_summary


class HomeDashboardTests(unittest.TestCase):
    def test_builds_latest_score_real_delta_weakness_and_pending_action(self):
        runs = [
            {
                "id": "run-old",
                "overall_band": 6,
                "created_at": "2026-08-10T10:00:00Z",
                "criteria": [{"criterion": "Task Response", "score": 5}],
            },
            {
                "id": "run-new",
                "overall_band": "6.5",
                "created_at": "2026-08-20T10:00:00+00:00",
                "criteria": [
                    {"criterion": "Task Response", "score": 6},
                    {"criterion": "Coherence and Cohesion", "score": "5"},
                    {"criterion": "Lexical Resource", "score": 6},
                ],
            },
        ]
        pending = [
            {
                "id": "task-old",
                "grading_run_id": "run-old",
                "task_kind": "logic",
                "task_index": 1,
                "original_text": "Old task",
                "updated_at": "2026-08-21T09:00:00Z",
            },
            {
                "id": "task-new",
                "grading_run_id": "run-new",
                "task_kind": "sentence",
                "task_index": "2",
                "original_text": "Academic subjects gives students knowledge.",
                "updated_at": "2026-08-22T09:00:00Z",
            },
        ]

        summary = build_home_summary(runs, pending)

        self.assertEqual(summary.latest_overall, 6.5)
        self.assertEqual(summary.score_delta, 0.5)
        self.assertEqual(summary.weakest_criterion, "CC 连贯衔接")
        self.assertEqual(summary.weakest_score, 5.0)
        self.assertEqual(summary.latest_grading_run_id, "run-new")
        self.assertTrue(summary.has_history)
        self.assertTrue(summary.has_pending)
        self.assertEqual(summary.continue_grading_run_id, "run-new")
        self.assertEqual(summary.pending.id, "task-new")
        self.assertEqual(summary.pending.task_index, 2)
        self.assertEqual(summary.primary_label, "继续这项训练")
        self.assertEqual(summary.primary_href, "?page=training&run_id=run-new")
        self.assertEqual([fact.key for fact in summary.facts], ["overall", "weakest", "delta"])
        self.assertEqual([fact.value for fact in summary.facts], ["6.5", "CC 连贯衔接", "+0.5"])
        self.assertEqual(
            summary.pending.summary,
            "单句改写：Academic subjects gives students knowledge.",
        )

    def test_delta_is_absent_until_two_valid_scores_exist(self):
        summary = build_home_summary(
            [{"id": "run-1", "overall_band": 7, "created_at": "2026-08-20"}],
            [],
        )

        self.assertEqual(summary.latest_overall, 7.0)
        self.assertIsNone(summary.score_delta)
        self.assertIsNone(summary.weakest_criterion)
        self.assertFalse(summary.has_pending)
        self.assertEqual(summary.primary_label, "从剑雅真题开始")
        self.assertEqual(summary.primary_href, "?page=write&mode=topics")
        self.assertEqual([fact.key for fact in summary.facts], ["overall"])
        self.assertNotIn("delta", [fact.key for fact in summary.facts])

    def test_dirty_rows_are_skipped_without_inventing_display_values(self):
        runs = [
            {
                "id": "broken-latest",
                "overall_band": math.nan,
                "created_at": "2026-08-30T00:00:00Z",
                "criteria": "not-a-list",
            },
            "not-a-run",
            {
                "id": "usable",
                "overall_band": "6.0",
                "created_at": "not-a-date",
                "criteria": [
                    None,
                    {"criterion": "Lexical Resource", "score": True},
                    {"criterion": "Custom Criterion", "score": 4.5},
                ],
            },
            {"id": "out-of-range", "overall_band": 10, "created_at": "2026-08-29"},
            {"id": "", "overall_band": 8, "created_at": "2026-08-28"},
        ]
        pending = [
            {"grading_run_id": "", "updated_at": "2026-08-30T00:00:00Z"},
            None,
            {
                "grading_run_id": "run-fallback",
                "task_kind": "future-kind",
                "task_index": -1,
                "original_text": "  ",
                "updated_at": "broken",
            },
        ]

        summary = build_home_summary(runs, pending)

        self.assertEqual(summary.latest_grading_run_id, "usable")
        self.assertEqual(summary.latest_overall, 6.0)
        self.assertIsNone(summary.score_delta)
        self.assertEqual(summary.weakest_criterion, "Custom Criterion")
        self.assertEqual(summary.weakest_score, 4.5)
        self.assertEqual(summary.pending.summary, "专项训练")
        self.assertIsNone(summary.pending.task_index)
        self.assertEqual([fact.key for fact in summary.facts], ["overall", "weakest"])

    def test_empty_and_invalid_collections_return_stable_empty_model(self):
        self.assertEqual(build_home_summary(None, None), HomeSummary())
        self.assertEqual(build_home_summary("bad", {"bad": "mapping"}), HomeSummary())
        self.assertEqual(build_home_summary(42, object()), HomeSummary())

    def test_same_score_has_real_zero_delta_and_criteria_tie_keeps_report_order(self):
        summary = build_home_summary(
            [
                {
                    "id": "new",
                    "overall_band": 6.5,
                    "created_at": "2026-08-20",
                    "criteria": [
                        {"criterion": "Lexical Resource", "score": 5},
                        {"criterion": "Task Response", "score": 5},
                    ],
                },
                {"id": "old", "overall_band": 6.5, "created_at": "2026-08-10"},
            ],
            [],
        )

        self.assertEqual(summary.score_delta, 0.0)
        self.assertEqual(summary.weakest_criterion, "LR 词汇资源")
        self.assertEqual(summary.facts[-1].value, "0.0")

    def test_pending_cta_url_is_bound_to_run_and_safely_encoded(self):
        summary = build_home_summary(
            [],
            [{"grading_run_id": "run with spaces&next=bad", "task_kind": "logic"}],
        )

        self.assertEqual(summary.primary_label, "继续这项训练")
        self.assertEqual(
            summary.primary_href,
            "?page=training&run_id=run+with+spaces%26next%3Dbad",
        )
        self.assertEqual(summary.continue_grading_run_id, "run with spaces&next=bad")

    def test_pending_copy_is_normalized_and_bounded(self):
        source = "  " + "word " * 30
        summary = build_home_summary(
            [],
            [{"grading_run_id": "r1", "task_kind": "logic", "original_text": source}],
        )

        self.assertTrue(summary.pending.summary.startswith("逻辑训练：word word"))
        self.assertTrue(summary.pending.summary.endswith("…"))
        self.assertLessEqual(len(summary.pending.summary), 78)


if __name__ == "__main__":
    unittest.main()
