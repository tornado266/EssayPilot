import unittest

from src.prompts import build_structured_grading_prompt, load_skill_scoring_rules


class PromptTests(unittest.TestCase):
    def test_fixed_skill_scoring_rules_load(self):
        rules = load_skill_scoring_rules()
        self.assertIn("Phase 2：四维评分", rules)
        self.assertIn("Task Response", rules)

    def test_structured_prompt_excludes_markdown_report_contract(self):
        prompt = build_structured_grading_prompt("Task 2", "Question", "Essay text")
        self.assertIn("Return data matching the supplied JSON schema", prompt)
        self.assertNotIn("## 11.", prompt)

    def test_task1_is_not_silently_scored(self):
        with self.assertRaises(ValueError):
            build_structured_grading_prompt("Task 1", "Question", "Essay text")


if __name__ == "__main__":
    unittest.main()
