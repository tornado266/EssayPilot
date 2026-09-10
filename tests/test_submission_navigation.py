"""Exercise submission through the real Streamlit widgets and report route."""
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.ai_grader import AIGraderError, REPORT_PROMPT_VERSION
from tests.test_membership_grading_flow import valid_package, valid_scoring_package

ROOT = Path(__file__).resolve().parents[1]


class SubmissionNavigationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('src.cloud_store._setting', return_value=''))
        self.stack.enter_context(patch('src.auth_session._AUTH_COMPONENT', return_value=
                                      SimpleNamespace(auth_session={'status': 'missing'}, auth_wake=0)))
        self.stack.enter_context(patch('src.cloud_store.requests.request',
                                      side_effect=AssertionError('Unexpected cloud request')))
        self.stack.enter_context(patch('src.visitor_identity.browser_visitor_id', return_value=''))
        self.stack.enter_context(patch('src.product_analytics.record_event_safely'))
        self.stack.enter_context(patch('src.analytics.record_grading_event'))
        self.scorer = self.stack.enter_context(patch('src.ai_grader.grade_scoring_decision',
                                                    return_value=valid_scoring_package()))
        package = valid_package()
        package['prompt_version'] = REPORT_PROMPT_VERSION
        self.grader = self.stack.enter_context(patch('src.ai_grader.grade_essay_package', return_value=package))
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30)
        self.app.secrets['ALLOW_LOCAL_UNMETERED_AI'] = 'true'
        self.app.query_params['page'] = 'write'
        self.app.run()
        self.app.text_area(key='topic_input').set_value('Discuss school holidays.')
        self.app.text_area(key='essay_input').set_value('Students need time to learn and rest.')

    def submit(self):
        next(b for b in self.app.button if b.label in ('开始批改作文', '重新尝试批改')).click().run()

    def test_generated_report_opens_and_inputs_survive_return_to_write(self):
        self.submit()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.error), 0,
                         [e.value for e in self.app.error] + [c.value for c in self.app.code])
        self.assertEqual(self.app.query_params['page'], ['report'])
        self.assertEqual(self.app.session_state['latest_report'], 'complete report')
        self.assertFalse(self.app.session_state['grading_failed'])
        self.scorer.assert_called_once()
        self.grader.assert_called_once()
        self.app.button(key='mobile_nav_write').click().run()
        self.assertEqual(self.app.text_area(key='essay_input').value, 'Students need time to learn and rest.')
        self.assertEqual(self.app.text_area(key='topic_input').value, 'Discuss school holidays.')
        # A report already generated in this session must be reused on retry.
        self.submit()
        self.assertEqual(self.app.query_params['page'], ['report'])
        self.scorer.assert_called_once()
        self.grader.assert_called_once()

    def test_failed_model_shows_error_preserves_inputs_and_retry_opens_report(self):
        self.scorer.side_effect = AIGraderError('test', 'test', 'https://example.test',
                                               True, TimeoutError('Test timeout'))
        self.submit()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(self.app.session_state['grading_failed'])
        self.assertTrue(any('评分服务暂时不可用' in e.value for e in self.app.error))
        self.assertEqual(self.app.text_area(key='essay_input').value, 'Students need time to learn and rest.')
        self.scorer.side_effect = None
        self.submit()
        self.assertEqual(len(self.app.error), 0)
        self.assertEqual(self.app.query_params['page'], ['report'])


if __name__ == '__main__':
    unittest.main()
