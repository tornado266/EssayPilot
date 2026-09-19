import ast
import base64
import binascii
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import test_membership_grading_flow as grading_fixtures
from src import grading_workflow


ROOT = Path(__file__).resolve().parents[1]


def load_practice_matcher():
    source_path = ROOT / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = {"_normalize_practice_original_text", "_match_practice_attempt"}
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace: dict[str, object] = {}
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["_match_practice_attempt"]


def load_payment_qr_decoder(values: dict[str, str]):
    source_path = ROOT / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    decoder = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_payment_qr_bytes"
    )
    namespace: dict[str, object] = {
        "base64": base64,
        "binascii": binascii,
        "re": re,
        "LOGGER": logging.getLogger("payment-qr-test"),
        "_nested_secret_setting": lambda section, name: values.get(name, ""),
    }
    module = ast.Module(body=[decoder], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["_payment_qr_bytes"]


class MembershipAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_first_report_reserves_only_after_all_cache_checks(self):
        events = []
        grade, _, _ = grading_fixtures.MembershipGradingFlowTests().build(
            lambda **kw: events.append("teach") or grading_fixtures.valid_package(),
            lambda **kw: events.append("score") or grading_fixtures.valid_scoring_package(),
        )
        store = SimpleNamespace(
            find_cached_grading=lambda *a: events.append("report-cache"),
            find_cached_scoring=lambda *a: events.append("score-cache"),
            save_grading_cycle=lambda *a, **kw: {"grading_run_id": "run"},
        )
        grade(store, SimpleNamespace(id="user"), topic="topic", essay="essay",
              reserve_model_access=lambda *a: events.append("reserve") or {},
              complete_model_access=Mock())
        self.assertEqual(events, ["report-cache", "score-cache", "reserve", "score", "teach"])

    def test_valid_report_is_cached_before_cloud_settlement(self):
        grade, st, _ = grading_fixtures.MembershipGradingFlowTests().build(
            lambda **kw: grading_fixtures.valid_package())
        ticket = {"kind": "membership", "flow_id": "flow"}

        def save(*args, **kwargs):
            entry = next(iter(st.session_state.grading_cache.values()))
            self.assertEqual(entry["package"]["report"], "complete report")
            return {"grading_run_id": "run"}

        def complete(saved_ticket, run_id):
            self.assertEqual(saved_ticket, ticket)
            self.assertEqual(run_id, "run")
            entry = next(iter(st.session_state.grading_cache.values()))
            self.assertEqual(entry["cloud_ids"]["grading_run_id"], "run")
            raise grading_workflow.CloudStoreError("uncertain completion")

        store = SimpleNamespace(find_cached_grading=lambda *a: None,
                                find_cached_scoring=lambda *a: None, save_grading_cycle=save)
        with self.assertRaises(grading_workflow.GradingSettlementError):
            grade(store, SimpleNamespace(id="user"), topic="topic", essay="essay",
                  reserve_model_access=lambda *a: ticket, complete_model_access=complete)
        self.assertEqual(list(st.session_state.pending_first_report_access.values()), [ticket])
        self.assertNotIn("latest_report", st.session_state)

    def test_every_model_training_entry_has_a_server_reservation(self):
        self.assertIn("reserve_training_feedback_action(", self.source)
        self.assertIn("reserve_second_draft(", self.source)
        self.assertIn("release_training_feedback_action(", self.source)
        self.assertIn("release_second_draft(", self.source)
        self.assertNotIn("review_expression_sentence", self.source)
        self.assertIn("独立造句 AI 点评暂未开放", self.source)

    def test_practice_feedback_is_persisted_before_quota_settlement(self):
        sentence = self.source.split("def render_sentence_practice", 1)[1].split(
            "def extract_logic_practice_tasks", 1
        )[0]
        logic = self.source.split("def render_logic_practice", 1)[1].split(
            "def list_correction_history", 1
        )[0]
        for body, review_call in (
            (sentence, "feedback = review_sentence_rewrite"),
            (logic, "feedback = review_logic_rewrite"),
        ):
            generated = body.split(review_call, 1)[1]
            self.assertLess(
                generated.index("save_practice_attempt("),
                generated.index("complete_training_feedback_action("),
            )
            self.assertIn("st.session_state.get(saved_key) is True", generated)
            self.assertIn("保存成功前不会结算额度", generated)

    def test_practice_refresh_restores_feedback_and_read_only_disables_writes(self):
        sentence = self.source.split("def render_sentence_practice", 1)[1].split(
            "def extract_logic_practice_tasks", 1
        )[0]
        logic = self.source.split("def render_logic_practice", 1)[1].split(
            "def list_correction_history", 1
        )[0]
        for body in (sentence, logic):
            self.assertIn("read_only: bool = False", body)
            self.assertIn("list_practice_attempts_for_run(", body)
            self.assertIn('persisted_attempt.get("feedback")', body)
            self.assertIn('persisted_attempt.get("training_flow_id")', body)
            read_only = body.split("if read_only:", 1)[1].split("continue", 1)[0]
            self.assertNotIn("review_", read_only)
            self.assertNotIn("save_practice_attempt", read_only)
            self.assertNotIn("标记", read_only.replace("已标记为掌握。", ""))

    def test_legacy_practice_history_matches_only_the_strict_compatibility_key(self):
        match_attempt = load_practice_matcher()
        row = {
            "id": "legacy",
            "task_kind": "sentence",
            "task_key_hash": None,
            "task_index": 2,
            "original_text": "Students\u00a0need\tmore support.",
        }

        matched, legacy = match_attempt(
            [row],
            task_kind="sentence",
            task_key_hash="a" * 64,
            task_index=2,
            original_text="Students  need\nmore support.",
        )

        self.assertIs(matched, row)
        self.assertTrue(legacy)
        for changed in (
            {"task_kind": "logic"},
            {"task_index": 1},
            {"original_text": "students need more support."},
            {"original_text": "Students need more support!"},
            {"original_text": "Students’ need more support."},
        ):
            candidate = {**row, **changed}
            matched, legacy = match_attempt(
                [candidate],
                task_kind="sentence",
                task_key_hash="a" * 64,
                task_index=2,
                original_text="Students need more support.",
            )
            self.assertIsNone(matched)
            self.assertFalse(legacy)

    def test_current_hash_precedes_legacy_and_wrong_nonempty_hash_never_falls_back(self):
        match_attempt = load_practice_matcher()
        legacy_row = {
            "id": "legacy",
            "task_kind": "logic",
            "task_key_hash": "",
            "task_index": 1,
            "original_text": "The same task.",
        }
        current_row = {
            **legacy_row,
            "id": "current",
            "task_key_hash": "b" * 64,
        }

        matched, legacy = match_attempt(
            [legacy_row, current_row],
            task_kind="logic",
            task_key_hash="b" * 64,
            task_index=1,
            original_text="The same task.",
        )
        self.assertIs(matched, current_row)
        self.assertFalse(legacy)

        matched, legacy = match_attempt(
            [{**legacy_row, "task_key_hash": "c" * 64}],
            task_kind="logic",
            task_key_hash="b" * 64,
            task_index=1,
            original_text="The same task.",
        )
        self.assertIsNone(matched)
        self.assertFalse(legacy)

    def test_legacy_practice_history_is_excluded_from_every_completion_path(self):
        sentence = self.source.split("def render_sentence_practice", 1)[1].split(
            "def extract_logic_practice_tasks", 1
        )[0]
        logic = self.source.split("def render_logic_practice", 1)[1].split(
            "def list_correction_history", 1
        )[0]
        for body in (sentence, logic):
            proof = body.split("proof_ticket: dict[str, object] | None = None", 1)[1].split(
                "st.markdown(f", 1
            )[0]
            self.assertIn("if not legacy_restore:", proof)
            self.assertIn("and not legacy_restore", body)
            legacy_view = body.split("这是升级前保存的历史点评", 1)[1].split(
                "if read_only:", 1
            )[0]
            self.assertIn("continue", legacy_view)
            self.assertNotIn("complete_training_feedback_action", legacy_view)

    def test_second_draft_cache_preserves_the_original_reservation_ticket(self):
        ticket = {"flow_id": "original-flow"}
        cache = {"access_ticket": ticket}
        package = {"structured": {"overall_band": 7}, "report": "report"}
        generate = lambda: grading_workflow.generate_second_draft(
            provider="test", model="test", task_type="Task 2", topic="topic",
            draft_1_text="original", draft_1_scores={}, draft_2_text="revision",
            cached_generation=cache, score=lambda **kw: package,
            teach=lambda **kw: package, compare=lambda **kw: "comparison",
            prepare_comparison=lambda *a: (None, None), scores_from_report=lambda *a: {},
        )
        self.assertEqual(generate(), (package, "comparison"))
        self.assertIs(cache["access_ticket"], ticket)
        self.assertEqual(generate(), (package, "comparison"))
        self.assertIs(cache["access_ticket"], ticket)

    def test_offer_copy_discloses_first_and_renewal_prices_and_frozen_limits(self):
        offer = self.source.split("def render_founder_offer", 1)[1].split(
            "def render_training_access_gate", 1
        )[0]
        for text in ("¥7.5", "¥9.9", "3 篇", "3 次专项 AI 点评", "1 次二稿", "不自动续费"):
            self.assertIn(text, offer)
        self.assertIn("可重复续包", offer)
        self.assertIn("提交订单号后自动开通", offer)
        self.assertIn("提交并开通", offer)
        self.assertNotIn("人工核对", offer)

    def test_offer_fails_closed_and_hides_partial_payment_configuration(self):
        offer = self.source.split("def render_founder_offer", 1)[1].split(
            "def render_training_access_gate", 1
        )[0]
        self.assertLess(offer.index("if entitlement_error:"), offer.index("payment_qr ="))
        self.assertIn('if not entitlement.get("can_purchase"):', offer)
        self.assertIn('if not entitlement.get("server_offer_verified"):', offer)
        self.assertLess(
            offer.index('if not entitlement.get("server_offer_verified"):'),
            offer.index("payment_qr ="),
        )
        self.assertIn(
            "payment_ready = bool(payment_instructions and support_contact and refund_policy)",
            offer,
        )
        payment_branch = offer.split("if payment_ready:", 1)[1]
        self.assertIn('wechat_base64', offer)
        self.assertIn('alipay_base64', offer)
        self.assertIn("private_qr_codes", payment_branch)
        self.assertIn("st.image(payment_qr", payment_branch)
        self.assertIn("with st.form", payment_branch)

    def test_private_payment_qr_decoder_rejects_non_images_and_large_payloads(self):
        decoder = self.source.split("def _payment_qr_bytes", 1)[1].split(
            "def local_unmetered_ai_enabled", 1
        )[0]
        self.assertIn("base64.b64decode", decoder)
        self.assertIn("validate=True", decoder)
        self.assertIn("2_000_000", decoder)
        for signature in ("is_jpeg", "is_png", "is_webp"):
            self.assertIn(signature, decoder)

        jpeg = b"\xff\xd8\xff" + b"safe-qr-test"
        encoded = base64.b64encode(jpeg).decode("ascii")
        multiline = encoded[:8] + "\n" + encoded[8:]
        decode = load_payment_qr_decoder({"wechat_base64": multiline})
        self.assertEqual(decode("wechat_base64"), jpeg)

        invalid = load_payment_qr_decoder({"wechat_base64": "not-base64"})
        self.assertIsNone(invalid("wechat_base64"))

        oversized_bytes = b"\xff\xd8\xff" + (b"x" * 2_000_000)
        oversized = load_payment_qr_decoder(
            {"wechat_base64": base64.b64encode(oversized_bytes).decode("ascii")}
        )
        self.assertIsNone(oversized("wechat_base64"))

    def test_active_pack_with_remaining_quota_never_shows_purchase_form(self):
        offer = self.source.split("def render_founder_offer", 1)[1].split(
            "def render_training_access_gate", 1
        )[0]
        active_guard = offer.index(
            'if entitlement.get("active") and int(entitlement.get("runs_remaining") or 0) > 0:'
        )
        payment_form = offer.index("with st.form")
        self.assertLess(active_guard, payment_form)
        guarded_body = offer[active_guard: offer.index('if not entitlement.get("can_purchase"):', active_guard)]
        self.assertIn("return", guarded_body)

    def test_client_does_not_choose_or_send_a_plan_or_price(self):
        tree = ast.parse(self.source)
        render = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "render_founder_offer"
        )
        create_call = next(
            node
            for node in ast.walk(render)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_membership_request"
        )
        self.assertEqual(len(create_call.args), 2)
        self.assertEqual({keyword.arg for keyword in create_call.keywords}, {"paid_at", "note"})

    def test_expired_paid_runs_are_rendered_read_only(self):
        gate = self.source.split("def render_training_access_gate", 1)[1].split(
            "def reserve_training_feedback_action", 1
        )[0]
        self.assertIn('if access.get("history_readable"):', gate)
        self.assertIn('return {**access, "read_only": True}', gate)
        page = self.source.split("def render_training_page", 1)[1].split(
            "def _normalise_expression", 1
        )[0]
        self.assertEqual(page.count("read_only=read_only"), 3)

    def test_missing_quota_backend_fails_closed_without_explicit_local_opt_in(self):
        helper = self.source.split("def local_unmetered_ai_enabled", 1)[1].split(
            "def clear_membership_cache", 1
        )[0]
        self.assertIn('ALLOW_LOCAL_UNMETERED_AI', helper)
        self.assertIn("raise CloudStoreError", helper)

        first_report = self.source.split(
            "def reserve_first_report_access", 1
        )[1].split("def complete_first_report_access", 1)[0]
        self.assertIn("require_ai_access_backend(store)", first_report)

        training = self.source.split(
            "def reserve_training_feedback_action", 1
        )[1].split("def complete_training_feedback_action", 1)[0]
        second_draft = self.source.split("def reserve_second_draft", 1)[1].split(
            "def complete_second_draft", 1
        )[0]
        self.assertIn("require_ai_access_backend(store)", training)
        self.assertIn("require_ai_access_backend(store)", second_draft)


if __name__ == "__main__":
    unittest.main()
