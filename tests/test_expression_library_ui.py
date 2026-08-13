import unittest
from pathlib import Path

from src.learning_assets import (
    EXPRESSION_VIEW_CURATED,
    EXPRESSION_VIEW_PRACTICE,
    EXPRESSION_VIEW_REPORT,
    expression_status_label,
    report_expression_items,
    resolve_expression_view,
)


ROOT = Path(__file__).resolve().parents[1]


class ExpressionLibraryUiTests(unittest.TestCase):
    def test_expression_statuses_use_conservative_display_labels(self):
        self.assertEqual(expression_status_label("new"), "未练习")
        self.assertEqual(expression_status_label("practicing"), "继续练习")
        self.assertEqual(expression_status_label("mastered"), "已正确使用一次")

    def test_first_view_prefers_report_expressions_only_when_available(self):
        self.assertEqual(
            resolve_expression_view(
                stored_view=None,
                authenticated=True,
                has_report_expressions=True,
            ),
            EXPRESSION_VIEW_REPORT,
        )
        self.assertEqual(
            resolve_expression_view(
                stored_view=None,
                authenticated=True,
                has_report_expressions=False,
            ),
            EXPRESSION_VIEW_CURATED,
        )
        self.assertEqual(
            resolve_expression_view(
                stored_view=None,
                authenticated=False,
                has_report_expressions=True,
            ),
            EXPRESSION_VIEW_CURATED,
        )

    def test_old_session_views_map_to_new_names(self):
        cases = {
            "题材表达库": EXPRESSION_VIEW_CURATED,
            "我的表达": EXPRESSION_VIEW_REPORT,
            "表达练习": EXPRESSION_VIEW_PRACTICE,
        }
        for old, expected in cases.items():
            with self.subTest(old=old):
                self.assertEqual(
                    resolve_expression_view(
                        stored_view=old,
                        authenticated=True,
                        has_report_expressions=True,
                    ),
                    expected,
                )

    def test_report_view_excludes_catalog_and_prioritises_current_essay(self):
        items = [
            {"id": "catalog", "item_type": "expression", "origin": "catalog", "grading_run_id": None},
            {"id": "old", "item_type": "expression", "origin": "report", "grading_run_id": "run-old"},
            {"id": "current", "item_type": "expression", "origin": "report", "grading_run_id": "run-current"},
            {"id": "error", "item_type": "error", "origin": "report", "grading_run_id": "run-current"},
        ]
        shown = report_expression_items(items, grading_run_id="run-current")
        self.assertEqual([item["id"] for item in shown], ["current", "old"])

    def test_routes_and_expression_entry_copy_are_wired_without_schema_changes(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        alpine_source = (ROOT / "ui" / "alpine.py").read_text(encoding="utf-8")
        visible_source = app_source + alpine_source

        for token in (
            '"growth": "学习档案"',
            '"growth": "档案"',
            "?page=growth&amp;mode=expressions",
            "练习本篇表达",
            "开始第二稿训练",
            "题材精选",
            "来自我的作文",
            "造句练习",
            "150 条内置精选表达",
        ):
            self.assertIn(token, visible_source)

        for old_product_name in (
            "个人素材库",
            "表达积累",
            "题材表达库",
            "我的表达",
            "150 条人工整理表达",
        ):
            self.assertNotIn(old_product_name, visible_source)

        self.assertIn("def compare_draft_progress(", (ROOT / "src" / "ai_grader.py").read_text(encoding="utf-8"))
        self.assertIn('"useful_expressions"', (ROOT / "src" / "report_schema.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
