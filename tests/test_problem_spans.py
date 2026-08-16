import unittest

from src.problem_spans import (
    contextual_collocation,
    fallback_problem_ranges,
    highlight_problem_text,
    lexical_replacement,
)


class ProblemSpanTests(unittest.TestCase):
    def test_multiple_exact_spans_are_escaped_and_underlined(self):
        correction = {
            "original": "A <claim> has bad words.",
            "improved": "A claim uses precise language.",
            "problem_spans": ["<claim>", "bad words"],
        }
        rendered = highlight_problem_text(correction)
        self.assertIn('<u class="problem-span">&lt;claim&gt;</u>', rendered)
        self.assertIn('<u class="problem-span">bad words</u>', rendered)
        self.assertNotIn("<claim>", rendered)

    def test_old_report_fallback_is_conservative_for_whole_sentence_rewrite(self):
        self.assertEqual(
            fallback_problem_ranges("Every word here is completely different", "Nothing remains at all"),
            [],
        )
        rendered = highlight_problem_text({"original": "This are useful.", "improved": "This is useful."})
        self.assertIn('<u class="problem-span">are</u>', rendered)

    def test_lexical_replacement_and_collocation_use_improved_sentence_only(self):
        correction = {
            "original": "Cars make bad traffic.",
            "improved": "Cars cause severe congestion.",
        }
        replacement = lexical_replacement(correction)
        self.assertEqual(replacement, "cause severe congestion")
        self.assertEqual(contextual_collocation(correction["improved"], replacement), "Cars cause severe congestion")


if __name__ == "__main__":
    unittest.main()
