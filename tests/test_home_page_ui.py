import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from src.cloud_store import CloudStoreError, CloudUser

from ui.alpine import (
    CSS_PATH,
    ESSAYPILOT_LOGO_PATH,
    render_guest_home_intro,
    render_home_action_card,
    render_home_heading,
    render_home_preview_link,
)


ROOT = Path(__file__).resolve().parents[1]


class HomePageUiTests(unittest.TestCase):
    def test_signed_in_snapshot_renders_pending_history_and_failure_actions(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "render_learning_dashboard")
        script = "\n".join([
            "import streamlit as st",
            "from urllib.parse import urlencode",
            "from src.cloud_store import SupabaseStore, CloudStoreError, CloudUser",
            "from src.home_dashboard import build_home_summary",
            "from ui.alpine import render_home_heading, render_home_action_card",
            ast.get_source_segment(source, function),
            "render_learning_dashboard(SupabaseStore(), CloudUser('user-a', 'a@example.com', 'token'))",
        ])
        runs = [{"id": "run-latest", "overall_band": 7, "criteria": [], "created_at": "2026-09-01"}]
        pending = [{"grading_run_id": "run-older", "task_kind": "logic", "original_text": "Explain why."}]
        for snapshot, label, href in (
            ((runs, pending), "继续这项训练", "?page=training&amp;run_id=run-older"),
            ((runs, []), "从剑雅真题开始", "?page=report&amp;run_id=run-latest"),
            (CloudStoreError("unavailable"), "从剑雅真题开始", "?page=write&amp;mode=topics"),
        ):
            with self.subTest(label=label, snapshot=snapshot):
                with patch("src.cloud_store.SupabaseStore.get_home_snapshot") as load:
                    if isinstance(snapshot, Exception):
                        load.side_effect = snapshot
                    else:
                        load.return_value = snapshot
                    app = AppTest.from_string(script).run()
                self.assertEqual(len(app.exception), 0)
                load.assert_called_once_with(CloudUser("user-a", "a@example.com", "token"))
                html = "\n".join(str(item.proto.body) for item in app.get("html"))
                self.assertIn(label, html)
                self.assertIn(href, html)

    def test_guest_home_renders_actions_as_html_not_a_code_block(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["page"] = "home"

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
        self.assertEqual(len(app.code), 0)
        html_bodies = "\n".join(
            str(getattr(item.proto, "body", "")) for item in app.get("html")
        )
        self.assertIn("开始批改", html_bodies)
        self.assertIn("从剑雅真题选题", html_bodies)
        self.assertIn("?page=demo", html_bodies)

    def test_guest_purchase_mode_renders_offer_and_login_action(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["page"] = "home"
        app.query_params["mode"] = "purchase"

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
        markdown = "\n".join(str(item.value) for item in app.markdown)
        self.assertIn("首包 ¥7.5，之后每包 ¥9.9", markdown)
        self.assertIn("登录后查看开通方式", [button.label for button in app.button])

    def test_action_card_escapes_copy_and_route_values(self):
        captured: dict[str, object] = {}

        def fake_html(body: str, **kwargs: object) -> None:
            captured["body"] = body
            captured["kwargs"] = kwargs

        with patch("ui.alpine.st.html", side_effect=fake_html):
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
        self.assertEqual(captured["kwargs"], {})

    def test_signed_in_home_heading_uses_official_logo_asset(self):
        captured: dict[str, object] = {}

        with patch(
            "ui.alpine.st.html",
            side_effect=lambda body, **kwargs: captured.update(body=body, kwargs=kwargs),
        ):
            render_home_heading()

        body = str(captured["body"])
        self.assertTrue(ESSAYPILOT_LOGO_PATH.is_file())
        self.assertIn('class="ep-home-heading__logo"', body)
        self.assertIn('src="data:image/png;base64,', body)
        self.assertIn('alt="EssayPilot"', body)

    def test_signed_in_home_is_action_first_and_reads_only_small_snapshots(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        dashboard = source.split("def render_learning_dashboard", 1)[1].split(
            "def render_demo_page", 1
        )[0]

        self.assertIn("store.get_home_snapshot(user)", dashboard)
        self.assertNotIn("list_grading_runs", dashboard)
        self.assertNotIn("list_pending_practice", dashboard)
        self.assertIn('primary_label=summary.primary_label', dashboard)
        self.assertNotIn("list_learning_items", dashboard)
        self.assertNotIn("list_draft_revisions", dashboard)
        self.assertNotIn("render_dashboard_stats", dashboard)
        self.assertNotIn("st.altair_chart", dashboard)
        self.assertNotIn("近期常见问题", dashboard)
        self.assertNotIn("今日训练", dashboard)

    def test_guest_home_has_ranked_actions_without_feature_wall(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        home = source.split("def render_home_page", 1)[1].split(
            "def grade_submission", 1
        )[0]

        self.assertIn("render_guest_home_intro", home)
        self.assertNotIn("st.columns(3)", home)
        self.assertNotIn("render_feature_bento", home)
        self.assertNotIn("今天只做四步", home)
        self.assertNotIn("render_dashboard_stats", home)

    def test_purchase_entry_reuses_home_mode_and_login_return(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        helper = source.split("def open_purchase_offer", 1)[1].split(
            "def hydrate_grading_run", 1
        )[0]
        home = source.split("def render_home_page", 1)[1].split(
            "def grade_submission", 1
        )[0]

        self.assertIn('navigate("home", "", "purchase")', helper)
        self.assertIn('open_cloud_login("home", "purchase")', helper)
        self.assertIn('st.query_params.get("mode", "")', home)
        self.assertIn('key="home_purchase"', home)
        self.assertLess(
            home.index('key="home_purchase"'),
            home.index("render_learning_dashboard(store, user)"),
        )

    def test_purchase_entry_is_visible_in_each_responsive_navigation(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")

        for token in (
            'key="sidebar_purchase"',
            'key="desktop_purchase"',
            'key="mobile_purchase"',
            '"购买批改包"',
        ):
            self.assertIn(token, source)
        self.assertEqual(source.count('key="sidebar_purchase"'), 1)
        self.assertEqual(source.count('key="desktop_purchase"'), 2)
        self.assertEqual(source.count('key="mobile_purchase"'), 2)

    def test_guest_intro_renders_one_primary_action_and_quiet_demo_preview(self):
        captured: dict[str, object] = {}

        def fake_html(body: str, **kwargs: object) -> None:
            captured["body"] = body
            captured["kwargs"] = kwargs

        with patch("ui.alpine.st.html", side_effect=fake_html):
            render_guest_home_intro()

        body = str(captured["body"])
        self.assertEqual(body.count("ep-home-action__link--primary"), 1)
        self.assertIn("开始批改", body)
        self.assertIn("从剑雅真题选题", body)
        self.assertIn("先看完整效果", body)
        self.assertIn("?page=write&amp;mode=topics", body)
        self.assertIn("?page=demo", body)
        self.assertIn("不会调用模型", body)
        self.assertEqual(captured["kwargs"], {})

    def test_inline_demo_preview_is_a_shareable_route(self):
        captured: dict[str, object] = {}

        with patch(
            "ui.alpine.st.html",
            side_effect=lambda body, **kwargs: captured.update(body=body, kwargs=kwargs),
        ):
            render_home_preview_link(label="Preview <now>", href="?page=demo&from=home")

        body = str(captured["body"])
        self.assertIn("Preview &lt;now&gt;", body)
        self.assertIn("?page=demo&amp;from=home", body)
        self.assertIn("ep-home-preview--inline", body)

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
        self.assertIn(".ep-home-heading__logo", css)
        self.assertIn(':has(.mobile-product-nav)', css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("display: block", css)
        self.assertIn(".ep-home-preview", css)
        self.assertIn("grid-row: 2", css)


if __name__ == "__main__":
    unittest.main()
