import unittest
from datetime import datetime
from pathlib import Path

from src.report_schema import PROMPT_VERSION
from src.storage import build_markdown_record, markdown_to_pdf


class ChineseLocalizationTests(unittest.TestCase):
    def test_prompt_version_distinguishes_chinese_reports(self):
        self.assertIn("-zh-", PROMPT_VERSION)

    def test_static_demo_is_utf8_chinese_and_keeps_english_material(self):
        report = (Path(__file__).parents[1] / "data" / "demo_report.md").read_text(encoding="utf-8")
        self.assertIn("## 2. 四项评分", report)
        self.assertIn("## 7. Band 7.5 英文示范改写", report)
        self.assertIn("University study is demanding", report)
        for broken in ("鈥", "锛", "闆", "绗"):
            self.assertNotIn(broken, report)

    def test_chinese_markdown_record_and_pdf(self):
        record = build_markdown_record(
            "Task 2",
            "Discuss both views.",
            "This is a student essay.",
            "# 雅思写作批改报告\n\n## 1. 总分\n\n**最可能分数：6.5**",
            250,
            created_at=datetime(2026, 8, 9, 12, 0, 0),
            overall_band=6.5,
        )
        self.assertIn("- 词数: 250", record)
        self.assertIn("## 学生原稿", record)
        self.assertTrue(markdown_to_pdf(record).startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
