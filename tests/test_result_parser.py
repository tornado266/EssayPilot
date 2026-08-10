import unittest

from src.result_parser import parse_band


class ResultParserTests(unittest.TestCase):
    def test_parse_band_accepts_only_legal_half_bands(self):
        self.assertEqual(parse_band(6.5), 6.5)
        self.assertEqual(parse_band("Band 7.0"), 7.0)
        for value in (True, False, 6.2, "6.25", -0.5, 9.5, None):
            with self.subTest(value=value):
                self.assertIsNone(parse_band(value))


if __name__ == "__main__":
    unittest.main()
