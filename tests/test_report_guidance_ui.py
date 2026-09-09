import ast
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def function_source(name):
    node = next(node for node in ast.parse(SOURCE).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.get_source_segment(SOURCE, node)


class ReportGuidanceUiTests(unittest.TestCase):
    def test_score_explanation_uses_report_metadata_without_changing_score(self):
        for raw, offset, score in [(6.5, 0.5, 7.0), (9.0, 0.5, 9.0), (6.5, 0.0, 6.5)]:
            script = "\n".join([
                "import html, streamlit as st",
                "from src.report_schema import format_overall_band",
                function_source("render_overall_band"),
                f"render_overall_band({score}, {{'raw_overall_band': {raw}, 'overall_calibration_offset': {offset}}})",
            ])
            app = AppTest.from_string(script).run()
            self.assertEqual(len(app.exception), 0)
            body = "\n".join(item.value for item in app.markdown)
            self.assertIn(f"产品估分调整：{offset:+.1f}", body)
            self.assertIn(f"调整后展示为 {score:.1f}", body)
            self.assertIn("不是 IELTS 官方加分规则", body)

    def test_legacy_score_does_not_invent_adjustment_metadata(self):
        script = "\n".join([
            "import html, streamlit as st",
            "from src.report_schema import format_overall_band",
            function_source("render_overall_band"),
            "render_overall_band(6.5, {})",
        ])
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("产品估分调整", "\n".join(item.value for item in app.markdown))

    def test_early_actions_keep_login_and_training_routes(self):
        renderer = function_source("render_report_page")
        self.assertLess(renderer.index('st.button("先做专项训练"'), renderer.index("report_sections ="))
        self.assertEqual(renderer.count('st.button("开始第二稿训练"'), 1)
        # Exercise the real report header/actions without cloud reads or grading.
        header = renderer.split('    raw_corrections =', 1)[0] + "    return\n"
        for signed_in, label, expected in [
            (False, "登录并保存本次报告", ("login", "training", "draft")),
            (False, "登录后查看训练权益", ("login", "training", "practice")),
            (True, "开始第二稿训练", ("training", "run-test", "draft")),
            (True, "先做专项训练", ("training", "run-test", "practice")),
        ]:
            with self.subTest(label=label):
                script = "\n".join([
                    "from __future__ import annotations",
                    "import html, streamlit as st",
                    "st.session_state.latest_report = 'test report'",
                    "st.session_state.latest_structured = {'overall_band': 7, 'priorities': [{'title': '练习重点', 'action': '补充解释'}]}",
                    "st.session_state.active_run_id = 'run-test'",
                    "learner_safe_report_markdown = lambda report, score: report",
                    "ensure_learning_assets = lambda *args: None",
                    "record_usage_event = lambda *args, **kwargs: None",
                    "render_training_stepper = lambda **kwargs: None",
                    "render_overall_band = lambda *args: None",
                    "render_structured_criteria_overview = lambda *args: None",
                    "def navigate(*args): st.session_state.destination = args",
                    "def open_cloud_login(*args): st.session_state.destination = ('login', *args)",
                    header,
                    f"render_report_page(None, {'object()' if signed_in else 'None'})",
                ])
                app = AppTest.from_string(script).run()
                self.assertEqual(len(app.exception), 0)
                next(button for button in app.button if button.label == label).click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(app.session_state.destination, expected)


if __name__ == "__main__":
    unittest.main()
