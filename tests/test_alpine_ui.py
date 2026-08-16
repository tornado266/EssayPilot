import unittest
from pathlib import Path
from unittest.mock import patch

from ui.alpine import (
    CSS_PATH, HERO_JPG_PATH, HERO_WEBP_PATH, align_draft_paragraphs,
    paragraph_diff_html, render_text_diff, split_draft_paragraphs,
)


class AlpineUiTests(unittest.TestCase):
    def test_local_assets_and_theme_tokens_exist(self):
        self.assertTrue(HERO_JPG_PATH.exists())
        self.assertTrue(HERO_WEBP_PATH.exists())
        self.assertLess(HERO_JPG_PATH.stat().st_size, 500_000)
        self.assertLess(HERO_WEBP_PATH.stat().st_size, 500_000)

        css = Path(CSS_PATH).read_text(encoding="utf-8")
        for token in (
            "--ep-bg: #f4f7fa",
            "--ep-primary: #1769aa",
            "--ep-mountain: #0e3b5f",
            "--ep-danger: #c84c55",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(token, css)

    def test_mobile_account_entry_is_present_and_responsive(self):
        project_root = Path(__file__).resolve().parents[1]
        app_source = (project_root / "app.py").read_text(encoding="utf-8")
        css = Path(CSS_PATH).read_text(encoding="utf-8")

        for token in (
            "def open_cloud_login(return_route: str = \"home\", return_mode: str = \"\")",
            'key="desktop_account_bar"',
            'key="mobile_account_bar"',
            'key="mobile_login"',
            'key="mobile_logout"',
            "登录 / 保存学习档案",
            "登录并同步进度",
        ):
            self.assertIn(token, app_source)

        self.assertIn(".st-key-mobile_account_bar", css)
        self.assertIn(".st-key-desktop_account_bar", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("margin: 0 0 0.75rem", css)

    def test_diff_is_real_and_escapes_user_text(self):
        captured = {}

        def fake_markdown(body, **kwargs):
            captured["body"] = body
            captured["kwargs"] = kwargs

        with patch("ui.alpine.st.markdown", side_effect=fake_markdown):
            render_text_diff("A <script> weak claim", "A clearer claim")

        body = captured["body"]
        self.assertIn("<del>&lt;script&gt; weak</del>", body)
        self.assertIn("<ins>clearer</ins>", body)
        self.assertNotIn("<script>", body)
        self.assertTrue(captured["kwargs"]["unsafe_allow_html"])

    def test_paragraph_alignment_handles_crlf_single_lines_and_whole_add_delete(self):
        self.assertEqual(split_draft_paragraphs("One\r\nTwo"), ["One", "Two"])
        aligned = align_draft_paragraphs("Keep\n\nDelete me\n\nLast", "Keep\n\nAdded here\n\nLast")
        self.assertEqual(aligned[0].before, "Keep")
        self.assertEqual(aligned[-1].after, "Last")
        body = paragraph_diff_html("Safe <tag>\n\nRemoved", "Safe text\n\nAdded")
        self.assertIn("新增", body)
        self.assertIn("删除", body)
        self.assertIn("&lt;tag&gt;", body)
        self.assertNotIn("<tag>", body)


if __name__ == "__main__":
    unittest.main()
