"""Exercise repeated normalization and real card widgets without cloud access."""
import ast
from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
FUNCTIONS = {
    node.name: ast.get_source_segment(SOURCE, node)
    for node in ast.parse(SOURCE).body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"_normalise_expression", "_render_expression_card"}
}
ROW = {
    "id": "expression-123", "grading_run_id": "run-123", "item_type": "expression",
    "origin": "report", "source_text": "broaden their horizons",
    "explanation": "开阔眼界", "target_text": "Students can broaden their horizons.",
    "usage_note": "适合讨论跨学科学习。", "topic_category": "education",
    "function_category": "core_collocation", "status": "new", "favorite": False,
    "grading_runs": {"essays": {"question": "Should students study other subjects?"}},
}


class ExpressionCardRenderingTests(unittest.TestCase):
    def test_normalization_is_idempotent_and_preserves_identity_and_content(self):
        namespace = {}
        exec(FUNCTIONS["_normalise_expression"], namespace)
        normalize = namespace["_normalise_expression"]
        first = normalize(ROW)
        self.assertEqual(normalize(first), first)
        self.assertEqual(first["learning_item_id"], ROW["id"])
        self.assertEqual(first["expression"], ROW["source_text"])
        self.assertEqual(first["meaning"], ROW["explanation"])
        self.assertEqual(first["example"], ROW["target_text"])
        self.assertEqual(first["source_question"], ROW["grading_runs"]["essays"]["question"])
        catalog = {"catalog_id": "education-1", "expression": "test", "example": "Test example."}
        self.assertEqual(normalize(normalize(catalog)), catalog)

    def app(self):
        harness = '''
from __future__ import annotations
from types import SimpleNamespace
import streamlit as st
from src.expression_catalog import TOPIC_LABELS, FUNCTION_LABELS
from src.learning_assets import expression_status_label
class CloudStoreError(Exception):
    pass
if 'updates' not in st.session_state:
    st.session_state.updates = []
def update(user, item_id, **kwargs):
    st.session_state.updates.append((item_id, kwargs))
store = SimpleNamespace(update_learning_item=update)
user = SimpleNamespace(id='test-user')
def _persist_catalog_expression(*args):
    raise AssertionError('Existing report expressions must not be inserted again')
'''
        code = harness + '\n' + '\n'.join(FUNCTIONS.values())
        code += f'\nitem = _normalise_expression({ROW!r})\n'
        code += "_render_expression_card(item, store=store, user=user, key='test')\n"
        app = AppTest.from_string(code).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_card_shows_expression_meaning_and_example_after_list_normalization(self):
        app = self.app()
        text = '\n'.join(item.value for item in app.markdown)
        for value in (ROW['source_text'], ROW['explanation'], ROW['target_text']):
            self.assertIn(value, text)

    def test_favorite_uses_existing_record_id(self):
        app = self.app()
        app.button(key='fav_test').click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn((ROW['id'], {'favorite': True}), app.session_state.updates)

    def test_practice_keeps_full_expression_and_existing_record_id(self):
        app = self.app()
        app.button(key='practice_test').click().run()
        self.assertEqual(len(app.exception), 0)
        item = app.session_state.expression_practice_item
        self.assertEqual(item['learning_item_id'], ROW['id'])
        self.assertEqual(item['expression'], ROW['source_text'])
        self.assertEqual(item['example'], ROW['target_text'])
        self.assertIn((ROW['id'], {'status': 'practicing'}), app.session_state.updates)
