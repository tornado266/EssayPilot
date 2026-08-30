import unittest
from pathlib import Path
from unittest.mock import patch

from ui.alpine import CSS_PATH, render_home_action_card


ROOT = Path(__file__).resolve().parents[1]


class HomePageUiTests(unittest.TestCase):
    def test_action_card_escapes_copy_and_route_values(self):
        captured: dict[str, object] = {}

        def fake_markdown(body: str, **kwargs: object) -> None:
            captured["body"] = body
            captured["kwargs"] = kwargs

        with patch("ui.alpine.st.markdown", side_effect=fake_markdown):
            render_home_action_card(
                eyebrow="Today",
                title="Continue <script>alert(1)</script>",
                body="One useful step",
                primary_label="Open",
                primary_href="?page=training&run_id=a&next=bad",
                secondary_actions=(("Topics", "?page=write&mode=topics"),),
                facts=(("Overall", "7.0"),),
            )

        body = str(captured["body"])
        self.assertIn("Continue &lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("?page=training&amp;run_id=a&amp;next=bad", body)
        self.assertIn("?page=write&amp;mode=topics", body)
        self.assertTrue(captured["kwargs"]["unsafe_allow_html"])

    def test_signed_in_home_is_action_first_and_reads_only_small_snapshots(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        dashboard = source.split("def render_learning_dashboard", 1)[1].split(
            "def render_product_hero", 1
        )[0]

        self.assertIn("list_grading_runs(user, limit=2)", dashboard)
        self.assertIn("list_pending_practice(user, limit=1)", dashboard)
        self.assertIn('primary_label=summary.primary_label', dashboard)
        self.assertNotIn("list_learning_items", dashboard)
        self.assertNotIn("list_draft_revisions", dashboard)
        self.assertNotIn("render_dashboard_stats", dashboard)
        self.assertNotIn("st.altair_chart", dashboard)
        self.assertNotIn("近期常见问题", dashboard)
        self.assertNotIn("今日训练", dashboard)

    def test_guest_home_has_three_actions_without_feature_wall(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        home = source.split("def render_home_page", 1)[1].split(
            "def grade_submission", 1
        )[0]

        for label in ("开始批改", "从剑雅真题选题", "查看零 Token 示例"):
            self.assertIn(label, home)
        self.assertNotIn("render_feature_bento", home)
        self.assertNotIn("今天只做四步", home)
        self.assertNotIn("render_dashboard_stats", home)

    def test_score_trend_is_archived_collapsed_and_requires_two_dates(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        trend = source.split("def render_score_trend", 1)[1].split(
            "def _criterion_history_scores", 1
        )[0]
        growth = source.split("def render_growth_page", 1)[1].split(
            "def render_product_route", 1
        )[0]

        self.assertIn("len(practice_dates) < 2", trend)
        self.assertIn('st.expander("成绩趋势", expanded=False)', trend)
        self.assertIn("render_score_trend(runs)", growth)

    def test_home_layout_is_single_column_and_touch_safe_on_mobile(self):
        css = Path(CSS_PATH).read_text(encoding="utf-8")

        self.assertIn(".ep-home-action", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn(".st-key-guest_home_actions", css)


if __name__ == "__main__":
    unittest.main()
