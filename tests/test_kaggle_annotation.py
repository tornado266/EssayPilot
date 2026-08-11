import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.kaggle_annotation import annotate_case, annotation_schema, validate_annotation


class KaggleAnnotationTests(unittest.TestCase):
    def test_schema_uses_only_stable_taxonomy_tags(self):
        schema = annotation_schema()
        enum = schema["schema"]["properties"]["problem_tags"]["items"]["enum"]
        self.assertIn("TR.idea_development", enum)
        self.assertIn("GRA.article", enum)

    def test_validation_rejects_unknown_tags(self):
        with self.assertRaises(ValueError):
            validate_annotation({
                "problem_tags": ["Band.6"],
                "strengths": [],
                "weaknesses": ["x"],
                "training_targets": ["y"],
            })

    def test_annotation_request_excludes_kaggle_score_metadata(self):
        payload = {
            "problem_tags": ["TR.idea_development"],
            "strengths": ["Position is clear."],
            "weaknesses": ["The idea is not developed."],
            "training_targets": ["Explain one consequence."],
        }
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        completions = SimpleNamespace(create=lambda **kwargs: response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return response

        client.chat.completions.create = create
        with patch("src.kaggle_annotation.build_client", return_value=client):
            annotation, usage = annotate_case({
                "question": "Discuss both views.",
                "essay_clean": "One idea is stated but not developed.",
                "human_feedback_original": "Develop the idea.",
                "original_overall_score": 8.5,
            }, model="gpt-5.4-mini-2026-03-17")
        messages = json.dumps(captured["messages"], ensure_ascii=False)
        self.assertNotIn("8.5", messages)
        self.assertNotIn("overall", messages.casefold())
        self.assertEqual(annotation["problem_tags"], ["TR.idea_development"])
        self.assertEqual(usage["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
