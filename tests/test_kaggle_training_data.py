import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.kaggle_training_data import (
    clean_dataset,
    file_sha256,
    load_source_rows,
    source_manifest,
    write_outputs,
)


QUESTION = "Some people think public transport should be free. To what extent do you agree or disagree?"


def essay_with(marker: str = "") -> str:
    sentences = [
        "Public transport is an important service for people who live in large cities.",
        "Affordable buses allow workers and students to travel without depending on private cars.",
        "This can reduce congestion because fewer vehicles occupy limited road space every morning.",
        "It may also improve access to education and employment for families with low incomes.",
        "However, a completely free system would still require reliable funding from public budgets.",
        "Governments should therefore combine targeted subsidies with careful service planning.",
        "This approach protects vulnerable passengers while keeping the network financially sustainable.",
        "In conclusion, lower fares are beneficial, although universal free travel is not always practical.",
    ]
    if marker:
        sentences[5] = marker
    return " ".join(sentences)


HIGH_FEEDBACK = (
    "The position is clear and the main idea is relevant. However, idea development remains limited "
    "because the second paragraph does not explain the consequence of its claim. The example should "
    "be developed and connected to the topic sentence. Logical progression is generally clear, but "
    "some linking is mechanical. Word choice is mostly appropriate, although one collocation is awkward. "
    "Sentence structure shows variety, but article and preposition errors recur in several sentences."
)


class KaggleTrainingDataTests(unittest.TestCase):
    def _write_csv(self, directory: Path, rows: list[dict[str, str]]) -> Path:
        path = directory / "ielts.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["Task_Type", "Question", "Essay", "Examiner_Comment", "Overall"],
            )
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_truncated_examiner_column_name_is_recognized(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "actual_columns.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Task_Type", "Question", "Essay", "Examiner_Commen", "Overall"])
                writer.writeheader()
                writer.writerow({
                    "Task_Type": "Task 2", "Question": QUESTION, "Essay": essay_with(),
                    "Examiner_Commen": HIGH_FEEDBACK, "Overall": "6",
                })
            rows, _, _ = load_source_rows(path)
            result = clean_dataset(rows)
            case = result["all_records"][0]
            self.assertEqual(case["human_feedback_original"], HIGH_FEEDBACK)
            self.assertEqual(case["provenance_tier"], "examiner_claimed")

    def test_cleaning_filters_task1_preserves_short_learner_and_never_enables_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            short_real = " ".join(["Students need affordable transport because it supports education."] * 20)
            path = self._write_csv(directory, [
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": short_real, "Examiner_Comment": "", "Overall": "5.5"},
                {"Task_Type": "Task 1", "Question": "The chart shows population changes. Summarise the information.", "Essay": essay_with(), "Examiner_Comment": "", "Overall": "6"},
            ])
            before = file_sha256(path)
            rows, _, _ = load_source_rows(path)
            result = clean_dataset(rows)
            self.assertEqual(len(result["learner_corpus"]), 1)
            case = result["learner_corpus"][0]
            self.assertTrue(case["word_count_warning"])
            self.assertTrue(case["use_for_training"])
            self.assertIs(case["use_for_score_calibration"], False)
            self.assertEqual(file_sha256(path), before)

    def test_exact_duplicates_are_traceable_and_not_silently_kept_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            rows_data = [
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": essay_with(), "Examiner_Comment": HIGH_FEEDBACK, "Overall": "6.5"},
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": "  " + essay_with() + "\n", "Examiner_Comment": HIGH_FEEDBACK, "Overall": "7"},
            ]
            rows, _, _ = load_source_rows(self._write_csv(directory, rows_data))
            result = clean_dataset(rows)
            task2 = [case for case in result["all_records"] if case["task_type"] == "task2"]
            self.assertEqual({case["duplicate_status"] for case in task2}, {"canonical", "exact_duplicate"})
            self.assertEqual(len(result["learner_corpus"]), 1)
            duplicate = next(case for case in task2 if case["duplicate_status"] == "exact_duplicate")
            self.assertIn("exact_duplicate", duplicate["rejection_reasons"])
            self.assertEqual(duplicate["duplicate_count"], 2)

    def test_near_duplicates_are_review_flags_not_automatic_deletions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = essay_with()
            second = essay_with("Authorities should therefore combine targeted subsidies with careful service planning.")
            rows, _, _ = load_source_rows(self._write_csv(directory, [
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": first, "Examiner_Comment": "", "Overall": "6"},
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": second, "Examiner_Comment": "", "Overall": "6.5"},
            ]))
            result = clean_dataset(rows)
            self.assertEqual(len(result["learner_corpus"]), 2)
            self.assertTrue(all(case["possible_near_duplicate"] for case in result["learner_corpus"]))
            self.assertTrue(all(case["needs_review"] for case in result["learner_corpus"]))

    def test_feedback_is_split_only_at_a_high_confidence_boundary(self):
        contaminated = essay_with() + "\n\nExaminer Comment: " + HIGH_FEEDBACK
        uncertain = essay_with() + " The overall band discussion is not part of a formal heading."
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            rows, _, _ = load_source_rows(self._write_csv(directory, [
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": contaminated, "Examiner_Comment": "", "Overall": "6"},
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": uncertain, "Examiner_Comment": "", "Overall": "6"},
            ]))
            result = clean_dataset(rows)
            extracted = next(case for case in result["all_records"] if case["contamination_status"] == "extracted")
            self.assertNotIn("Examiner Comment", extracted["essay_clean"])
            self.assertIn("idea development", extracted["feedback_extracted"])
            quarantined = next(case for case in result["quarantine"] if case["contamination_status"] == "uncertain")
            self.assertIn("uncertain_feedback_boundary", quarantined["rejection_reasons"])

    def test_outputs_keep_candidates_unapproved_and_report_invariants(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = self._write_csv(directory, [
                {"Task_Type": "Task 2", "Question": QUESTION, "Essay": essay_with(), "Examiner_Comment": HIGH_FEEDBACK, "Overall": "6.5"},
            ])
            rows, profile, files = load_source_rows(source)
            result = clean_dataset(rows)
            output = directory / "processed"
            report = write_outputs(output, result, profile, source_manifest(files))
            self.assertTrue(report["invariants"]["all_kaggle_score_calibration_flags_false"])
            self.assertEqual((output / "core_training_cases.jsonl").read_text(encoding="utf-8"), "")
            candidate_text = (output / "TOP_CORE_CASE_CANDIDATES.md").read_text(encoding="utf-8")
            self.assertIn("尚未经过人工批准", candidate_text)
            saved = [json.loads(line) for line in (output / "learner_corpus.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(case["use_for_score_calibration"] is False for case in saved))


if __name__ == "__main__":
    unittest.main()
