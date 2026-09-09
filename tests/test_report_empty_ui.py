import ast
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class ReportEmptyUiTests(unittest.TestCase):
    def test_empty_report_has_guided_actions_without_grading(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "render_report_page")
        script = "\n".join([
            "from __future__ import annotations",
            "import streamlit as st",
            ast.get_source_segment(source, function),
            "render_report_page(None, None)",
        ])
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        body = "\n".join(str(item.proto.body) for item in app.get("html"))
        self.assertIn('href="?page=write"', body)
        self.assertIn('href="?page=demo"', body)
        self.assertIn('aria-hidden="true"', body)
        self.assertIn("你的第一份报告，还在等一篇作文", body)
        self.assertEqual(len(app.info), 0)


if __name__ == "__main__":
    unittest.main()
