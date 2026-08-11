import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from src.kaggle_skill_dataset import (
    HoldoutAccessError,
    TARGET_QUOTAS,
    blind_scoring_input,
    feedback_metrics,
    frozen_split_from_manifest,
    learner_coverage_profile,
    load_skill_split,
    public_split_manifest,
    scoring_skill_eligibility,
    split_examiner_claimed,
    structure_examiner_feedback,
    weak_scoring_metrics,
)


COMMENT = (
    "The task is covered and the position is clear. However, the main idea is not developed "
    "well enough and the consequence is unclear. Grammar errors recur in several sentences, "
    "although the response remains easy to understand. The writer should explain the central "
    "idea before correcting the repeated grammar pattern."
)


def claimed_case(case_id: str, band: int, question: str) -> dict:
    return {
        "case_id": case_id,
        "source": "kaggle_ielts",
        "task_type": "task2",
        "question": question,
        "essay_clean": "The policy is useful. It can help many people. This idea needs more explanation.",
        "original_overall_score": float(band),
        "score_confidence": "low",
        "provenance_tier": "examiner_claimed",
        "use_for_score_calibration": False,
        "human_feedback_original": COMMENT,
        "feedback_extracted": None,
        "cleaning_status": "clean",
        "needs_review": False,
        "duplicate_status": "unique",
        "possible_near_duplicate": False,
        "near_duplicate_case_ids": [],
    }


class KaggleSkillDatasetTests(unittest.TestCase):
    def _cases(self) -> list[dict]:
        totals = Counter()
        for split in TARGET_QUOTAS.values():
            totals.update(split)
        cases = []
        for band, count in totals.items():
            for index in range(count):
                cases.append(claimed_case(f"case_{band}_{index:02d}", band, f"Unique question {band}-{index}"))
        return cases

    def test_fixed_split_has_exact_quotas_and_no_question_leakage(self):
        splits = split_examiner_claimed(self._cases())
        self.assertEqual({name: len(cases) for name, cases in splits.items()}, {"development": 42, "validation": 8, "holdout": 12})
        for name, quotas in TARGET_QUOTAS.items():
            self.assertEqual(Counter(int(case["original_overall_score"]) for case in splits[name]), Counter(quotas))
        question_sets = {name: {case["question"] for case in cases} for name, cases in splits.items()}
        self.assertFalse(question_sets["development"] & question_sets["validation"])
        self.assertFalse(question_sets["development"] & question_sets["holdout"])
        self.assertFalse(question_sets["validation"] & question_sets["holdout"])

    def test_public_manifest_contains_no_essay_or_feedback(self):
        manifest = public_split_manifest(split_examiner_claimed(self._cases()))
        rendered = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn("essay_clean", rendered)
        self.assertNotIn("human_feedback", rendered)
        self.assertNotIn(COMMENT, rendered)

    def test_frozen_manifest_reproduces_ids_and_rejects_content_changes(self):
        cases = self._cases()
        original = split_examiner_claimed(cases)
        manifest = public_split_manifest(original)
        reproduced = frozen_split_from_manifest(cases, manifest)
        self.assertEqual(
            {name: [case["case_id"] for case in values] for name, values in original.items()},
            {name: [case["case_id"] for case in values] for name, values in reproduced.items()},
        )
        cases[0]["essay_clean"] += " Changed."
        with self.assertRaisesRegex(ValueError, "content hash changed"):
            frozen_split_from_manifest(cases, manifest)

    def test_holdout_loader_fails_closed_without_explicit_unlock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "holdout.jsonl"
            path.write_text(json.dumps({"case_id": "secret"}) + "\n", encoding="utf-8")
            with self.assertRaises(HoldoutAccessError):
                load_skill_split(path, split="holdout")
            self.assertEqual(load_skill_split(path, split="holdout", unlock_holdout=True)[0]["case_id"], "secret")

    def test_feedback_labels_keep_human_provenance_and_limit_priorities(self):
        case = claimed_case("case", 6, "Question")
        label = structure_examiner_feedback(case)
        self.assertEqual(label["provenance_tier"], "examiner_claimed")
        self.assertLessEqual(len(label["priority_tags"]), 2)
        self.assertIn("TR.idea_development", label["weakness_tags"])
        self.assertNotIn("model_annotation", label)

    def test_positive_no_error_comment_does_not_create_fake_weaknesses(self):
        case = claimed_case("case", 8, "Question")
        case["human_feedback_original"] = (
            "Paragraphing is correct and effective. There are essentially no errors in "
            "word choice or collocation, and the bulk of the sentences are error-free."
        )
        label = structure_examiner_feedback(case)
        self.assertEqual(label["weakness_tags"], [])
        self.assertIn("CC.paragraphing", label["strength_tags"])
        self.assertIn("LR.word_choice", label["strength_tags"])

    def test_contrast_pronoun_keeps_idea_development_as_a_weakness(self):
        case = claimed_case("case", 6, "Question")
        case["human_feedback_original"] = (
            "The main ideas are relevant, but not all of them are developed well enough."
        )
        label = structure_examiner_feedback(case)
        self.assertIn("TR.idea_development", label["weakness_tags"])
        self.assertEqual(label["priority_tags"][0], "TR.idea_development")

    def test_corrections_does_not_turn_a_needs_work_clause_positive(self):
        case = claimed_case("case", 5, "Question")
        case["human_feedback_original"] = (
            "This essay needs work in grammar, sentence structure and word choice "
            "(suggested corrections are shown separately)."
        )
        label = structure_examiner_feedback(case)
        self.assertIn("LR.word_choice", label["weakness_tags"])
        self.assertIn("GRA.accuracy", label["weakness_tags"])

    def test_eg_abbreviation_does_not_hide_overgeneralization(self):
        case = claimed_case("case", 6, "Question")
        case["human_feedback_original"] = (
            "Be careful about making assertive statements, e.g. advertisements motivate everyone."
        )
        label = structure_examiner_feedback(case)
        self.assertIn("TR.overgeneralization", label["weakness_tags"])

    def test_scoring_eligibility_is_low_weight_and_quality_gated(self):
        case = claimed_case("case", 6, "Question")
        label = structure_examiner_feedback(case)
        eligible, reasons = scoring_skill_eligibility(case, label)
        self.assertTrue(eligible, reasons)
        case["needs_review"] = True
        self.assertFalse(scoring_skill_eligibility(case, label)[0])

    def test_feedback_gate_matches_10_11_and_one_case_thresholds(self):
        gold = [
            {"case_id": f"case_{index}", "priority_tags": ["TR.idea_development"], "weakness_tags": ["TR.idea_development"]}
            for index in range(12)
        ]
        predictions = [
            {"case_id": f"case_{index}", "priority_tags": ["TR.idea_development"]}
            for index in range(11)
        ] + [{"case_id": "case_11", "priority_tags": ["LR.spelling"]}]
        metrics = feedback_metrics(gold, predictions)
        self.assertEqual(metrics["top1_hits"], 11)
        self.assertEqual(metrics["unsupported_cases"], 1)
        self.assertTrue(metrics["passed"])

    def test_generic_accuracy_gold_supports_a_specific_grammar_subtype(self):
        gold = [{
            "case_id": "case", "priority_tags": ["GRA.accuracy"],
            "weakness_tags": ["GRA.accuracy"],
        }]
        predictions = [{"case_id": "case", "priority_tags": ["GRA.subject_verb_agreement"]}]
        metrics = feedback_metrics(gold, predictions)
        self.assertEqual(metrics["top1_hits"], 1)
        self.assertEqual(metrics["unsupported_cases"], 0)

    def test_every_unlabelled_case_contributes_to_local_profile_but_only_twenty_are_candidates(self):
        cases = []
        for index in range(25):
            record = claimed_case(f"u_{index}", 6, f"Question {index}")
            record["provenance_tier"] = "learner_unlabelled"
            record["human_feedback_original"] = None
            record["use_for_training"] = True
            record["essay_clean"] = "Firstly, this is useful. Firstly, it helps people. Firstly, it reduces costs."
            cases.append(record)
        profile, candidates = learner_coverage_profile(cases)
        self.assertEqual(profile["corpus_size"], 25)
        self.assertTrue(profile["all_records_used_for_local_profile"])
        self.assertLessEqual(len(candidates), 20)
        self.assertTrue(all(case["model_annotation"] is None for case in candidates))

    def test_blind_scoring_projection_excludes_all_labels_and_comments(self):
        case = claimed_case("secret-id", 8, "Question")
        projected = blind_scoring_input(case)
        rendered = json.dumps(projected, ensure_ascii=False)
        self.assertEqual(set(projected), {"task_type", "topic", "essay"})
        self.assertNotIn("secret-id", rendered)
        self.assertNotIn(COMMENT, rendered)
        self.assertNotIn("8.0", rendered)

    def test_kaggle_scoring_metrics_are_always_separate_and_low_confidence(self):
        cases = [claimed_case("one", 6, "Question"), claimed_case("two", 7, "Question 2")]
        metrics = weak_scoring_metrics(cases, [
            {"case_id": "one", "status": "complete", "predicted_overall": 6.5},
            {"case_id": "two", "status": "complete", "predicted_overall": 6.0},
        ])
        self.assertEqual(metrics["mae"], 0.75)
        self.assertTrue(metrics["secondary_low_confidence_only"])
        self.assertFalse(metrics["combined_with_official_metrics"])


if __name__ == "__main__":
    unittest.main()
