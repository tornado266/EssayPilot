import ast
import subprocess
import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
TREND_CONSTANTS = {
    "CRITERION_COMPACT_NAMES", "CHART_CRITERION_DOMAIN", "ALPINE_CHART_COLORS",
    "ALPINE_CHART_DASHES", "ALPINE_CHART_SHAPES",
}


def trend_source() -> str:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    nodes = [
        node for node in ast.parse(source).body
        if (
            isinstance(node, ast.FunctionDef) and node.name == "render_score_trend"
        ) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in TREND_CONSTANTS
                    for target in node.targets)
        )
    ]
    return "\n".join(ast.get_source_segment(source, node) for node in nodes)


class StartupLoadingTests(unittest.TestCase):
    def test_app_imports_and_insufficient_history_do_not_load_chart_libraries(self):
        # A fresh interpreter prevents earlier chart tests from hiding eager imports.
        script = """
import ast
import pathlib
import sys
import streamlit

source = pathlib.Path('app.py').read_text(encoding='utf-8')
imports = [node for node in ast.parse(source).body
           if isinstance(node, (ast.Import, ast.ImportFrom))]
namespace = {}
exec(compile(ast.Module(body=imports, type_ignores=[]), 'app-imports', 'exec'), namespace)
assert 'altair' not in sys.modules, 'App imports loaded Altair before a chart was needed'
assert 'pandas' not in sys.modules, 'App imports loaded Pandas before a chart was needed'
"""
        script += "\nexec(" + repr(trend_source()) + ", namespace)\n"
        script += """
render = namespace['render_score_trend']
render([])
run = {'created_at': '2026-09-01', 'criteria': [{'criterion': 'Task Response', 'score': 6}]}
render([run, dict(run)])
assert 'altair' not in sys.modules, 'History below the date threshold loaded Altair'
assert 'pandas' not in sys.modules, 'History below the date threshold loaded Pandas'
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_archive_trend_still_renders_after_lazy_import(self):
        runs = [
            {"created_at": "2026-09-02", "criteria": [{"criterion": "Task Response", "score": 7}]},
            {"created_at": "2026-09-01", "criteria": [{"criterion": "Task Response", "score": 6}]},
        ]
        script = "import streamlit as st\n" + trend_source()
        script += f"\nrender_score_trend({runs!r})\n"
        app = AppTest.from_string(script, default_timeout=15).run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("vega_lite_chart")), 1)
        self.assertEqual(app.expander[0].label, "成绩趋势")
        self.assertFalse(app.expander[0].proto.expanded)

    def test_both_admin_daily_charts_still_render_after_lazy_import(self):
        script = """
from src.admin_dashboard import _render_tracking_metrics, _render_trends_and_retention

_render_tracking_metrics({
    'daily': [{'day': '2026-09-01', 'active_users': 5, 'gradings': 3}],
})
_render_trends_and_retention({
    'daily': [{'day': '2026-09-01', 'active_users': 5, 'reports': 3, 'failures': 0}],
})
"""
        app = AppTest.from_string(script, default_timeout=15).run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("vega_lite_chart")), 2)


if __name__ == "__main__":
    unittest.main()
