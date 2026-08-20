import unittest

from src.vocabulary_cards import build_vocabulary_cards_html, report_vocabulary_items


class VocabularyCardTests(unittest.TestCase):
    def test_cards_show_source_link_route_and_dictionary_fields_safely(self):
        item = {
            "kind": "upgrade",
            "source": "bad effects",
            "target": "detrimental effects",
            "headword": "detrimental",
            "part_of_speech": "adjective",
            "register": "formal",
            "meaning_zh": "有害的；不利的",
            "simple_definition": "causing harm or damage <unsafe>",
            "pattern": "be detrimental to + noun",
            "collocations": ["detrimental effect", "detrimental to health"],
            "source_sentence": "Pollution has bad effects on health.",
            "reason_zh": "比 bad 更正式、更准确。",
            "example_en": "Pollution has detrimental effects on public health.",
            "example_zh": "污染会对公众健康造成不利影响。",
        }
        body = build_vocabulary_cards_html([item])
        self.assertIn("可优化词 / 短语", body)
        self.assertIn("bad effects", body)
        self.assertIn("detrimental effects", body)
        self.assertIn("adjective", body)
        self.assertIn("formal", body)
        self.assertIn("vocab-source-mark", body)
        self.assertIn("&lt;unsafe&gt;", body)
        self.assertNotIn("<unsafe>", body)

    def test_explicit_empty_panel_does_not_fall_back_to_old_fields(self):
        report = {
            "vocabulary_recommendations": [],
            "useful_expressions": [{
                "expression": "public transport",
                "meaning": "公共交通",
                "example": "Public transport is essential.",
            }],
        }
        self.assertEqual(
            report_vocabulary_items(report, "Public transport is useful."),
            [],
        )

    def test_legacy_report_uses_only_items_that_link_back_to_the_essay(self):
        report = {
            "sentence_corrections": [],
            "useful_expressions": [
                {
                    "expression": "public transport",
                    "meaning": "公共交通",
                    "usage_note": "城市交通类核心表达",
                    "example": "Public transport should be reliable.",
                },
                {
                    "expression": "invented expression",
                    "meaning": "不在原文中",
                    "example": "This was invented.",
                },
            ],
        }
        items = report_vocabulary_items(
            report, "Reliable public transport can reduce congestion.",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "recommended")
        self.assertEqual(items[0]["source"], "public transport")
        self.assertIn("Reliable public transport", items[0]["source_sentence"])

    def test_legacy_correction_is_hidden_when_original_essay_is_unavailable(self):
        report = {
            "sentence_corrections": [{
                "criterion": "LR",
                "original": "This has bad effects.",
                "improved": "This has detrimental effects.",
                "problem": "用词笼统。",
                "learning_replacements": [{
                    "source": "bad effects",
                    "target": "detrimental effects",
                    "headword": "detrimental",
                    "part_of_speech": "adjective",
                    "meaning_zh": "有害的",
                    "simple_definition": "causing harm or damage",
                    "pattern": "detrimental to + noun",
                    "collocations": ["detrimental effect"],
                    "usage_note_zh": "用于正式说明负面影响。",
                }],
            }],
        }
        self.assertEqual(report_vocabulary_items(report, ""), [])


if __name__ == "__main__":
    unittest.main()
