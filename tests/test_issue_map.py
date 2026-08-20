import unittest

from src.issue_map import (
    build_issue_map_html,
    correction_criterion,
    grouped_corrections,
    learning_replacements,
    map_essay_issues,
    report_essay_from_state,
)


class IssueMapTests(unittest.TestCase):
    def test_structured_map_groups_nodes_and_escapes_replacement_text(self):
        corrections = [
            {
                "criterion": "GRA",
                "issue_type": "主谓一致",
                "original": "This are useful.",
                "problem": "主谓不一致",
                "improved": "This is useful.",
                "problem_spans": ["are"],
                "learning_replacements": [],
            },
            {
                "criterion": "LR",
                "issue_type": "搭配不自然",
                "original": "Cars make bad traffic.",
                "problem": "搭配不自然 <unsafe>",
                "improved": "Cars cause severe congestion.",
                "problem_spans": ["make bad traffic"],
                "learning_replacements": [{
                    "source": "make bad traffic",
                    "target": "cause <severe> congestion",
                }],
            },
        ]
        body = build_issue_map_html(corrections)
        self.assertIn("LR", body)
        self.assertIn("GRA", body)
        self.assertIn("#1", body)
        self.assertIn("#2", body)
        self.assertIn("Cars ", body)
        self.assertIn("make bad traffic", body)
        self.assertIn('aria-label="原文证据"', body)
        self.assertIn("cause &lt;severe&gt; congestion", body)
        self.assertNotIn("<unsafe>", body)
        self.assertEqual([name for name, _items in grouped_corrections(corrections)], ["LR", "GRA"])

    def test_legacy_lr_correction_gets_a_best_effort_replacement_branch(self):
        correction = {
            "original": "Cars make bad traffic.",
            "problem": "词汇搭配不自然",
            "improved": "Cars cause severe congestion.",
            "problem_spans": ["make bad traffic"],
        }
        self.assertEqual(correction_criterion(correction), "LR")
        replacement = learning_replacements(correction)[0]
        self.assertEqual(replacement["source"], "make bad traffic")
        self.assertEqual(replacement["target"], "cause severe congestion")

    def test_explicit_empty_replacement_list_never_uses_legacy_diff(self):
        correction = {
            "criterion": "LR",
            "original": "It is benefical.",
            "problem": "单词拼写错误",
            "improved": "It is beneficial.",
            "problem_spans": ["benefical"],
            "learning_replacements": [],
        }
        self.assertEqual(learning_replacements(correction), [])

    def test_legacy_spelling_change_is_not_presented_as_a_dictionary_upgrade(self):
        correction = {
            "original": "It is benefical.",
            "problem": "词汇拼写错误",
            "improved": "It is beneficial.",
            "problem_spans": ["benefical"],
        }
        self.assertEqual(learning_replacements(correction), [])

    def test_repeated_quotes_receive_distinct_map_numbers(self):
        correction = {
            "original": "Cars cause traffic.",
            "problem": "词汇不准确",
            "improved": "Cars cause congestion.",
            "problem_spans": ["traffic"],
        }
        body, unmatched = map_essay_issues(
            "Cars cause traffic. Cars cause traffic.", [correction, correction],
        )
        self.assertEqual(unmatched, [])
        self.assertEqual(body.count('class="issue-mark"'), 2)
        self.assertIn("<sup>1</sup>", body)
        self.assertIn("<sup>2</sup>", body)

    def test_overlapping_nodes_merge_without_losing_numbers(self):
        sentence = {
            "original": "Cars make bad traffic.",
            "problem": "整句表达不自然",
            "improved": "Cars cause severe congestion.",
            "problem_spans": ["make bad traffic"],
        }
        phrase = {
            "original": "bad traffic",
            "problem": "搭配不自然",
            "improved": "severe congestion",
            "problem_spans": ["bad traffic"],
        }
        body, unmatched = map_essay_issues("Cars make bad traffic.", [sentence, phrase])
        self.assertEqual(unmatched, [])
        self.assertEqual(body.count('class="issue-mark"'), 1)
        self.assertIn("<sup>1 · 2</sup>", body)

    def test_map_matching_tolerates_line_wrapped_whitespace(self):
        correction = {
            "original": "Schools should teach practical skills.",
            "problem": "用词",
            "improved": "Schools should teach practical life skills.",
            "problem_spans": ["practical skills"],
        }
        body, unmatched = map_essay_issues(
            "Schools should teach\npractical skills.", [correction],
        )
        self.assertEqual(unmatched, [])
        self.assertIn("<br>", body)
        self.assertIn("<sup>1</sup>", body)

    def test_report_essay_prefers_durable_snapshot_over_editor_widget(self):
        state = {
            "draft_1_snapshot": {"text": "Current report essay."},
            "pending_guest_claim": {"essay": "Guest essay."},
            "essay_input": "Stale editor text.",
        }
        self.assertEqual(report_essay_from_state(state), "Current report essay.")
        self.assertEqual(
            report_essay_from_state({"pending_guest_claim": {"essay": "Guest essay."}}),
            "Guest essay.",
        )

    def test_report_essay_rejects_a_stale_snapshot_for_another_report(self):
        current = {"sentence_corrections": [{"original": "Current report evidence."}]}
        stale = {"sentence_corrections": [{"original": "Old report evidence."}]}
        state = {
            "active_run_id": "run-current",
            "latest_structured": current,
            "draft_1_snapshot": {
                "grading_run_id": "run-old",
                "structured": stale,
                "text": "Old report evidence.",
            },
            "essay_input": "Current report evidence.",
        }
        self.assertEqual(report_essay_from_state(state), "Current report evidence.")


if __name__ == "__main__":
    unittest.main()
