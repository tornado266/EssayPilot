"""The real second-draft UI must remove its custom loader on every exit."""
import ast
from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app.py').read_text(encoding='utf-8')
FUNCTION = next(ast.get_source_segment(SOURCE, node)
                for node in ast.parse(SOURCE).body
                if isinstance(node, ast.FunctionDef) and node.name == 'render_draft_2_training')
HARNESS = '''
from __future__ import annotations
import time
import uuid
import streamlit as st
from unittest.mock import Mock
from ui.alpine import render_scoring_loader
from src.ai_grader import AIGraderError

scores = {'Overall Band': 6.5, 'TR': 6, 'CC': 6, 'LR': 7, 'GRA': 7}
if 'draft_1_snapshot' not in st.session_state:
    st.session_state.draft_1_snapshot = {
        'topic': 'Test topic', 'text': 'Original draft.', 'scores': scores,
        'feedback': 'Original feedback', 'grading_run_id': 'test-run',
    }
format_overall_band = str
draft_training_focus = lambda scores: []
submission_hash = lambda topic, text: text
draft_2_cache_key = lambda *args: args
score_snapshot = lambda structured: scores
save_markdown_record = Mock()
save_draft_training_record = Mock(return_value='test-record')
record_grading_event = Mock()
count_words = lambda text: len(text.split())
render_training_stepper = Mock()
render_score_change = Mock()
paragraph_diff_html = lambda *args: '<p>Test comparison</p>'
render_text_diff = Mock()

def generate_draft_2_feedback(**kwargs):
    failure = st.session_state.get('failure')
    if failure == 'model':
        raise AIGraderError('test', 'test', 'https://example.test', True,
                            TimeoutError('Test timeout'))
    if failure == 'unexpected':
        raise RuntimeError('Test failure')
    return ({'report': 'Second draft report', 'structured': {}, 'model': 'test',
             'prompt_version': 'test', 'skill_version': 'test',
             'schema_version': 'test', 'graded_at': 'test'}, 'Comparison report')
'''


class SecondDraftLoaderTests(unittest.TestCase):
    def submit(self, failure=None):
        app = AppTest.from_string(
            HARNESS + '\n' + FUNCTION + '\n'
            "render_draft_2_training(provider='test', model='test', "
            "task_type='Task 2', user_id='test-user')", default_timeout=15)
        app.session_state['failure'] = failure
        app.run()
        app.text_area(key='draft_2_text').set_value('Revised draft with changes.')
        app.button(key='submit_draft_2').click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def assert_no_loader(self, app):
        self.assertFalse(any('ep-loader-panel' in item.value for item in app.markdown))

    def test_success_shows_report_without_loader(self):
        app = self.submit()
        self.assertEqual(app.session_state['draft_2_result']['report'], 'Second draft report')
        self.assertTrue(any(item.value == '两稿对比进步报告' for item in app.header))
        self.assert_no_loader(app)

    def test_model_failure_shows_error_without_loader(self):
        app = self.submit('model')
        self.assertTrue(any('第二稿评分失败' in item.value for item in app.error))
        self.assert_no_loader(app)

    def test_unexpected_failure_shows_error_without_loader(self):
        app = self.submit('unexpected')
        self.assertTrue(any('第二稿训练出现意外错误' in item.value for item in app.error))
        self.assert_no_loader(app)


if __name__ == '__main__':
    unittest.main()
