import copy
import unittest
from collections import Counter
from pathlib import Path

from src.expression_catalog import TOPIC_LABELS, load_expression_catalog
from src.topic_bank import (
    QUESTION_TYPE_LABELS,
    TopicBankError,
    apply_topic_selection,
    filter_topics_by_category,
    load_topic_bank,
    validate_topic_bank,
)


ROOT = Path(__file__).resolve().parents[1]


class TopicBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topics = load_topic_bank()

    def test_bank_has_twenty_unique_cambridge_topics_across_all_categories(self):
        counts = Counter(item["topic_category"] for item in self.topics)
        self.assertEqual(len(self.topics), 20)
        self.assertEqual(set(counts), set(TOPIC_LABELS))
        self.assertTrue(all(counts[category] >= 1 for category in TOPIC_LABELS))
        self.assertEqual(len({item["id"] for item in self.topics}), 20)
        self.assertEqual(
            {item["question_type"] for item in self.topics},
            {
                "agree_disagree",
                "discuss_both_views",
                "advantages_disadvantages",
                "two_part",
                "positive_negative",
            },
        )
        self.assertEqual(
            [item["id"] for item in self.topics],
            [
                f"cambridge_{book}_test_{test}"
                for book in range(20, 15, -1)
                for test in range(1, 5)
            ],
        )
        self.assertTrue(all(item["source_book"] for item in self.topics))
        self.assertTrue(all(item["source_test"] for item in self.topics))

    def test_filter_returns_only_the_requested_category(self):
        education = filter_topics_by_category(self.topics, "education")
        self.assertEqual(len(education), 2)
        self.assertTrue(all(item["topic_category"] == "education" for item in education))
        with self.assertRaises(TopicBankError):
            filter_topics_by_category(self.topics, "not_a_topic")

    def test_invalid_category_type_empty_question_and_duplicate_id_are_detected(self):
        valid = self.topics[0]
        cases = {
            "category": [{**valid, "topic_category": "invalid"}],
            "type": [{**valid, "question_type": "invalid"}],
            "question": [{**valid, "question": "   "}],
            "null_question": [{**valid, "question": None}],
            "source": [{**valid, "source_book": "   "}],
            "duplicate": [valid, copy.deepcopy(valid)],
            "empty_bank": [],
        }
        for name, records in cases.items():
            with self.subTest(name=name), self.assertRaises(TopicBankError):
                validate_topic_bank(records)

    def test_selection_fills_topic_without_touching_essay_or_learning_state(self):
        chosen = self.topics[0]
        state = {
            "topic_input": "Old question",
            "essay_input": "An unfinished essay.",
            "latest_report": "Existing report",
            "draft_1_snapshot": {"text": "Earlier draft"},
            "training_step": 2,
        }
        protected = {
            key: copy.deepcopy(state[key])
            for key in ("essay_input", "latest_report", "draft_1_snapshot", "training_step")
        }

        result = apply_topic_selection(state, chosen)
        self.assertEqual(result, "confirmation_required")
        self.assertEqual(state["topic_input"], "Old question")
        self.assertEqual(state["essay_input"], protected["essay_input"])

        result = apply_topic_selection(state, state["pending_topic_selection"], confirm_existing_essay=True)
        self.assertEqual(result, "selected")
        self.assertEqual(state["topic_input"], chosen["question"])
        self.assertEqual(state["selected_topic_category"], chosen["topic_category"])
        self.assertEqual(state["selected_topic_question"], chosen["question"])
        self.assertNotIn("pending_topic_selection", state)
        for key, value in protected.items():
            self.assertEqual(state[key], value)

    def test_selection_without_an_essay_needs_no_confirmation(self):
        state = {"topic_input": "", "essay_input": "  "}
        result = apply_topic_selection(state, self.topics[-1])
        self.assertEqual(result, "selected")
        self.assertEqual(state["topic_input"], self.topics[-1]["question"])
        self.assertEqual(state["essay_input"], "  ")

    def test_selected_category_reuses_existing_expression_catalog(self):
        selected_category = self.topics[0]["topic_category"]
        expressions = [
            item for item in load_expression_catalog()
            if item["topic_category"] == selected_category
        ][:5]
        self.assertEqual(len(expressions), 5)
        for item in expressions:
            self.assertTrue(item["expression"])
            self.assertTrue(item["meaning"])
            self.assertTrue(item["example"])

    def test_topics_route_and_zero_token_ui_are_wired(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        home_source = (ROOT / "ui" / "alpine.py").read_text(encoding="utf-8")
        topic_source = (ROOT / "src" / "topic_bank.py").read_text(encoding="utf-8")

        self.assertIn('== "topics"', app_source)
        self.assertIn('?page=write&amp;mode=topics', home_source)
        self.assertIn('primary_href="?page=write&mode=topics"', app_source)
        self.assertIn('("从剑雅真题选题", "?page=write&mode=topics")', app_source)
        self.assertNotIn('class="ep-topic-home-entry"', app_source)
        self.assertIn("从主题题库选题", app_source)
        self.assertIn("用这题开始写", app_source)
        self.assertIn("Cambridge IELTS 16–20 Academic", app_source)
        self.assertIn("item['source_book']", app_source)
        self.assertIn("load_expression_catalog()", app_source)
        write_page_source = app_source.split("def render_write_page", 1)[1].split(
            "def render_correction_original", 1
        )[0]
        self.assertLess(
            write_page_source.index("render_topic_bank_picker()"),
            write_page_source.index('key="essay_editor"'),
        )
        for forbidden in ("openai", "grade_essay_package", "review_sentence_rewrite"):
            self.assertNotIn(forbidden, topic_source.lower())


if __name__ == "__main__":
    unittest.main()
