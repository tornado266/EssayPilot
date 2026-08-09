import unittest
from xml.etree import ElementTree

from src.expression_catalog import FUNCTION_LABELS, TOPIC_LABELS, load_expression_catalog
from src.learning_assets import build_learning_items, criterion_for_problem
from src.share_card import build_result_card_svg


class LearningAssetTests(unittest.TestCase):
    def test_learning_items_are_stable_and_categorized(self):
        report = {
            "sentence_corrections": [
                {"original": "People is busy.", "problem": "主谓一致错误。", "improved": "People are busy."}
            ],
            "useful_expressions": [
                {"expression": "a balanced approach", "meaning": "平衡的方法", "example": "A balanced approach is needed."}
            ],
        }
        first = build_learning_items(report, user_id="u1", grading_run_id="r1")
        second = build_learning_items(report, user_id="u1", grading_run_id="r1")
        self.assertEqual(first, second)
        self.assertEqual(first[0]["category"], "grammar")
        self.assertEqual(first[1]["item_type"], "expression")
        self.assertEqual(criterion_for_problem("论证没有充分展开"), "任务回应（TR）")

    def test_result_card_is_anonymous(self):
        svg = build_result_card_svg(
            overall_band=6.5,
            criteria=[{"criterion": "Task Response", "score": 6}],
            priority="补充论证解释链",
            mastered_count=2,
            draft_gain=0.5,
        )
        self.assertIn("6.5", svg)
        self.assertIn("补充论证解释链", svg)
        self.assertNotIn("email", svg.casefold())
        self.assertEqual(ElementTree.fromstring(svg).tag, "{http://www.w3.org/2000/svg}svg")

    def test_catalog_has_fifteen_curated_items_per_topic(self):
        catalog = load_expression_catalog()
        self.assertEqual(len(catalog), 150)
        self.assertEqual({item["topic_category"] for item in catalog}, set(TOPIC_LABELS))
        self.assertEqual({item["function_category"] for item in catalog}, set(FUNCTION_LABELS))
        for topic in TOPIC_LABELS:
            topic_items = [item for item in catalog if item["topic_category"] == topic]
            self.assertEqual(len(topic_items), 15)
            self.assertGreaterEqual(len({item["function_category"] for item in topic_items}), 5)
        self.assertEqual(len({item["catalog_id"] for item in catalog}), 150)


if __name__ == "__main__":
    unittest.main()
