"""Workflow retries resume failed steps without repeating confirmed work."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.grading_workflow import (
    CloudStoreError, FirstDraftOperations, GradingPolicy, GradingSettlementError,
    generate_second_draft, persist_second_draft, run_first_draft, run_second_draft,
)
from test_membership_grading_flow import valid_package, valid_scoring_package


class FirstDraftWorkflowTests(unittest.TestCase):
    def build(self):
        self.cache, self.pending = {}, {}
        self.score = Mock(return_value=valid_scoring_package())
        self.teach = Mock(return_value=valid_package())
        self.report, self.errors = Mock(return_value="report.md"), Mock(return_value="errors.md")
        self.store = Mock()
        self.store.find_cached_grading.return_value = None
        self.store.find_cached_scoring.return_value = None
        self.store.save_grading_cycle.return_value = {"grading_run_id": "run"}
        self.complete, self.release = Mock(), Mock()
        self.reserve = Mock(return_value={"kind": "membership", "flow_id": "flow"})
        return lambda: run_first_draft(
            self.store, SimpleNamespace(id="user"), topic="topic", essay="essay",
            fingerprint="hash", word_count=1, cache_key=("user", "user", "hash"),
            cache=self.cache, pending_accesses=self.pending,
            policy=GradingPolicy("report-v", "scoring-v", "skill-v", "test"),
            operations=FirstDraftOperations(
                self.score, self.teach, lambda data: {"Overall Band": data["overall_band"]},
                self.report, self.errors,
            ), reserve=self.reserve, complete=self.complete, release=self.release,
        )

    def test_uncertain_settlement_retries_only_completion(self):
        run = self.build()
        self.complete.side_effect = [CloudStoreError("timeout"), None]
        with self.assertRaises(GradingSettlementError):
            run()
        result = run()
        self.assertTrue(result.settled)
        self.assertTrue(result.reused)
        self.assertEqual(self.pending, {})
        for operation in (self.reserve, self.score, self.teach, self.report, self.errors,
                          self.store.save_grading_cycle):
            operation.assert_called_once()
        self.assertEqual(self.complete.call_count, 2)
        self.release.assert_not_called()

    def test_cloud_save_failure_reuses_generated_report_and_backups(self):
        run = self.build()
        self.store.save_grading_cycle.side_effect = [CloudStoreError("offline"), {"grading_run_id": "run"}]
        with self.assertRaises(GradingSettlementError):
            run()
        self.complete.assert_not_called()
        self.assertTrue(run().settled)
        self.score.assert_called_once()
        self.teach.assert_called_once()
        self.report.assert_called_once()
        self.errors.assert_called_once()
        self.reserve.assert_called_once()
        self.release.assert_not_called()
        self.assertEqual(self.store.save_grading_cycle.call_count, 2)

    def test_partial_local_backup_retries_only_failed_writer(self):
        run = self.build()
        self.errors.side_effect = [OSError("disk full"), "errors.md"]
        self.complete.side_effect = [CloudStoreError("timeout"), None]
        with self.assertRaises(GradingSettlementError) as failure:
            run()
        self.assertIn("local_backup_warning", failure.exception.warnings)
        result = run()
        self.assertEqual(result.error_book_path, "errors.md")
        self.assertNotIn("local_backup_warning", result.warnings)
        self.report.assert_called_once()
        self.assertEqual(self.errors.call_count, 2)
        self.store.save_grading_cycle.assert_called_once()


class SecondDraftWorkflowTests(unittest.TestCase):
    def build(self):
        self.cache = {"access_ticket": {"flow_id": "original-flow"}}
        self.package = valid_package()
        self.draft = {"topic": "topic", "text": "original", "scores": {},
                      "feedback": "feedback", "grading_run_id": "parent"}
        self.score = Mock(return_value={"structured": self.package["structured"]})
        self.teach = Mock(return_value=self.package)
        self.compare = Mock(return_value="comparison")
        self.report = Mock()
        self.training_record = Mock(return_value="training.md")
        self.record_event, self.release, self.complete = Mock(), Mock(), Mock()
        self.store = Mock()
        self.store.save_second_draft_result.return_value = {"grading_run_id": "second"}
        scores = lambda data: {"Overall Band": data["overall_band"]}
        self.generate = lambda: generate_second_draft(
            provider="test", model="test", task_type="Task 2", topic="topic",
            draft_1_text="original", draft_1_scores={}, draft_2_text="revision",
            cached_generation=self.cache, score=self.score, teach=self.teach,
            compare=self.compare, prepare_comparison=lambda *a: (None, None), scores_from_report=scores,
        )
        self.persist = lambda **kwargs: persist_second_draft(
            self.store, SimpleNamespace(id="user"), **kwargs,
            complete=self.complete, count_words=lambda text: 1, submission_hash=lambda *a: "hash")
        return lambda: run_second_draft(
            draft_1=self.draft, draft_2_text="revision", task_type="Task 2", model="test",
            user_id="user", parent_run_id="parent", attempt_id="attempt",
            cached_generation=self.cache, generate=self.generate, save_report=self.report,
            save_training_record=self.training_record, count_words=lambda text: 1,
            scores_from_report=scores, record_grading_event=self.record_event,
            persist=self.persist, release=self.release,
        )

    def test_settlement_retry_does_not_repeat_models_backups_or_cloud_insert(self):
        run = self.build()
        self.complete.side_effect = [CloudStoreError("timeout"), None]
        result = run()
        self.assertTrue(result.display["settlement_pending"])
        self.assertEqual(self.cache["access_ticket"]["flow_id"], "original-flow")
        self.assertFalse(run().display["settlement_pending"])
        self.assertFalse(run().display["settlement_pending"])
        for operation in (self.score, self.teach, self.compare, self.report,
                          self.training_record, self.store.save_second_draft_result, self.record_event):
            operation.assert_called_once()
        self.assertEqual(self.complete.call_count, 2)
        self.release.assert_not_called()
        self.assertNotIn("access_ticket", self.cache)

    def test_partial_backup_failure_still_settles_and_preserves_successful_writer(self):
        run = self.build()
        self.training_record.side_effect = [OSError("disk full"), "training.md"]
        result = run()
        self.assertIn("local_backup_warning", result.warnings)
        self.assertFalse(result.display["settlement_pending"])
        self.assertEqual(run().display["path"], "training.md")
        self.report.assert_called_once()
        self.store.save_second_draft_result.assert_called_once()
        self.complete.assert_called_once()
        self.release.assert_not_called()

    def test_failed_generation_retains_receipt_when_release_is_uncertain(self):
        run = self.build()
        self.score.side_effect = ValueError("invalid score")
        self.release.side_effect = CloudStoreError("timeout")
        with self.assertRaisesRegex(ValueError, "invalid score"):
            run()
        self.assertEqual(self.cache["access_ticket"]["flow_id"], "original-flow")
        self.store.save_second_draft_result.assert_not_called()
        self.complete.assert_not_called()

    def test_no_release_after_confirmed_cloud_write(self):
        run = self.build()
        self.complete.side_effect = CloudStoreError("timeout")
        self.assertTrue(run().display["settlement_pending"])
        self.generate = Mock(side_effect=RuntimeError("unexpected retry failure"))
        with self.assertRaisesRegex(RuntimeError, "unexpected retry failure"):
            run()
        self.assertEqual(self.cache["cloud_ids"]["grading_run_id"], "second")
        self.release.assert_not_called()
        self.assertIn("access_ticket", self.cache)
