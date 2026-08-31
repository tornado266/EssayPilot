import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


class DemoPageUiTests(unittest.TestCase):
    def test_demo_query_route_renders_current_static_flow(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["page"] = "demo"

        with patch(
            "src.auth_session._AUTH_COMPONENT",
            return_value=SimpleNamespace(
                auth_session={"status": "empty"}, auth_wake=0
            ),
        ), patch(
            "src.visitor_identity.browser_visitor_id", return_value=""
        ):
            app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["page_mode"], "demo")
        self.assertIn(
            "把示例原稿填入写作区",
            [button.label for button in app.button],
        )
        tab_labels = [tab.label for tab in app.tabs]
        top_level = [
            label for label in tab_labels
            if label in {"① 输入", "② 报告", "③ 训练", "④ 第二稿"}
        ]
        self.assertEqual(top_level, ["① 输入", "② 报告", "③ 训练", "④ 第二稿"])
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("CURRENT PRODUCT WALKTHROUGH", markdown)
        self.assertIn("0 TOKEN", markdown)

    def test_demo_renderer_has_no_model_or_cloud_side_effect_path(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        renderer = source.split("def render_demo_page", 1)[1].split(
            "APP_ROUTES =", 1
        )[0]

        self.assertIn("load_demo_package()", renderer)
        self.assertIn("build_issue_map_html", renderer)
        self.assertIn("build_vocabulary_cards_html", renderer)
        self.assertNotIn("grade_essay_package", renderer)
        self.assertNotIn("ensure_learning_assets", renderer)
        self.assertNotIn("record_usage_event", renderer)
        self.assertNotIn("DEMO_REPORT_PATH", renderer)
        self.assertNotIn("SAMPLE_ESSAY", renderer)
        self.assertNotIn("render_bookmark_rail", renderer)
        self.assertNotIn("demo-flow", renderer)


if __name__ == "__main__":
    unittest.main()
