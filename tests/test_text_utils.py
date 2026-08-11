import unittest

from src.text_utils import count_paragraphs, count_words, text_diagnostics


class TextUtilsTests(unittest.TestCase):
    def test_counts_hyphens_smart_apostrophes_and_newline_styles_consistently(self):
        text = "A well-known idea doesn't vanish.\r\n\r\nIt’s still useful."
        self.assertEqual(count_words(text), 8)
        self.assertEqual(count_paragraphs(text), 2)
        self.assertEqual(
            text_diagnostics(text),
            {"word_count": 8, "non_empty_paragraphs": 2},
        )
        self.assertEqual(count_paragraphs(text.replace("\r\n", "\n")), 2)


if __name__ == "__main__":
    unittest.main()
