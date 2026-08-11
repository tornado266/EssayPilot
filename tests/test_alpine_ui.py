import unittest
from pathlib import Path
from unittest.mock import patch

from ui.alpine import CSS_PATH, HERO_JPG_PATH, HERO_WEBP_PATH, render_text_diff


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


if __name__ == "__main__":
    unittest.main()
