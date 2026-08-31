import ast
import json
import tempfile
import unittest
from pathlib import Path

from src.chinese_report import examiner_result_to_markdown
from src.demo_package import (
    DEFAULT_DEMO_PACKAGE_PATH,
    DemoPackage,
    DemoPackageError,
    load_demo_package,
)
from src.report_schema import REPORT_PROMPT_VERSION, SCHEMA_VERSION, validate_examiner_result


ROOT = Path(__file__).resolve().parents[1]


class DemoPackageTests(unittest.TestCase):
    def test_default_package_is_current_and_fully_validated(self):
        package = load_demo_package()

        self.assertIsInstance(package, DemoPackage)
        self.assertEqual(package.word_count, 239)
        self.assertEqual(package.structured["schema_version"], SCHEMA_VERSION)
        self.assertEqual(package.structured["prompt_version"], REPORT_PROMPT_VERSION)
        self.assertEqual(package.structured["overall_band"], 7.0)
        self.assertEqual(len(package.structured["criteria"]), 4)
        self.assertEqual(len(package.structured["priorities"]), 2)
        self.assertGreaterEqual(len(package.structured["sentence_corrections"]), 3)
        self.assertGreaterEqual(len(package.structured["vocabulary_recommendations"]), 4)
        self.assertGreaterEqual(len(package.structured["useful_expressions"]), 6)
        self.assertGreaterEqual(len(package.structured["sentence_training"]), 2)
        self.assertGreaterEqual(len(package.structured["logic_training"]), 1)
        self.assertEqual(set(package.draft_changes), {"retained", "improved", "next"})
        self.assertIn("Some university students want to learn", package.question)
        self.assertIn("studying for a qualification", package.question)
        self.assertTrue(package.essay)
        self.assertTrue(package.draft_2)

        revalidated = validate_examiner_result(package.structured, package.essay)
        self.assertEqual(package.report, examiner_result_to_markdown(revalidated))
        self.assertIn("## 8. 本篇可迁移表达", package.report)
        self.assertIn("## 11. 单句提分训练", package.report)

    def test_evidence_supports_issue_map_vocabulary_and_training_links(self):
        package = load_demo_package()
        structured = package.structured

        for item in structured["sentence_corrections"]:
            self.assertIn(item["original"], package.essay)
            for span in item["problem_spans"]:
                self.assertIn(span, item["original"])
            for replacement in item["learning_replacements"]:
                self.assertIn(replacement["source"], item["original"])
                self.assertIn(replacement["target"], item["improved"])

        for item in structured["vocabulary_recommendations"]:
            self.assertIn(item["source_sentence"], package.essay)
            self.assertIn(item["source"], item["source_sentence"])
            self.assertIn(item["target"], item["example_en"])

        training_originals = {
            item["original"]
            for field in ("sentence_training", "logic_training")
            for item in structured[field]
        }
        self.assertTrue(
            all(priority["evidence"] in training_originals for priority in structured["priorities"])
        )

    def test_each_load_returns_independent_mutable_payloads(self):
        first = load_demo_package()
        second = load_demo_package()

        first.structured["summary"] = "changed"
        first.draft_changes["retained"] = "changed"

        self.assertNotEqual(second.structured["summary"], "changed")
        self.assertNotEqual(second.draft_changes["retained"], "changed")

    def test_custom_path_is_supported_and_invalid_schema_is_wrapped(self):
        raw = json.loads(DEFAULT_DEMO_PACKAGE_PATH.read_text(encoding="utf-8"))
        raw["structured"]["essay_topic_category"] = "invalid-topic"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.json"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(DemoPackageError) as raised:
                load_demo_package(path)

        self.assertNotIn("invalid-topic", str(raised.exception))

    def test_malformed_or_incomplete_files_fail_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.json"
            malformed.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(DemoPackageError):
                load_demo_package(malformed)

            incomplete = Path(directory) / "incomplete.json"
            incomplete.write_text(json.dumps({"question": "Question"}), encoding="utf-8")
            with self.assertRaises(DemoPackageError):
                load_demo_package(incomplete)

    def test_data_layer_has_no_ui_cloud_or_model_dependency(self):
        source_path = ROOT / "src" / "demo_package.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden = {"streamlit", "src.cloud_store", "src.ai_grader", "openai"}
        self.assertTrue(forbidden.isdisjoint(imported_modules))


if __name__ == "__main__":
    unittest.main()
