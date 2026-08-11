"""Create an anonymous, downloadable learning-result card as SVG."""

from __future__ import annotations

from html import escape
from typing import Any

from src.report_schema import format_practice_band_interval


def build_result_card_svg(
    *,
    overall_band: float,
    criteria: list[dict[str, Any]],
    priority: str,
    mastered_count: int,
    draft_gain: float | None,
) -> str:
    labels = {
        "Task Response": "TR",
        "Coherence and Cohesion": "CC",
        "Lexical Resource": "LR",
        "Grammatical Range and Accuracy": "GRA",
    }
    score_cells = []
    for index, item in enumerate(criteria[:4]):
        x = 95 + index * 235
        label = labels.get(str(item.get("criterion")), "-")
        score = item.get("score", "-")
        score_cells.append(
            f'<rect x="{x}" y="520" width="195" height="150" rx="28" fill="#ffffff" fill-opacity=".82"/>'
            f'<text x="{x + 28}" y="570" class="small">{escape(label)}</text>'
            f'<text x="{x + 28}" y="635" class="score-small">{escape(str(score))}</text>'
        )
    interval = format_practice_band_interval(overall_band)
    gain = "尚未提交第二稿" if draft_gain is None else "已完成第二稿验证"
    safe_priority = escape(priority[:54] or "继续完成本轮专项训练")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1440" viewBox="0 0 1080 1440">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#eaf9fb"/><stop offset=".55" stop-color="#fffdf7"/><stop offset="1" stop-color="#eef6e8"/></linearGradient></defs>
<style>.brand{{font:700 26px Arial;letter-spacing:5px;fill:#17616c}}.title{{font:700 58px Arial,'Microsoft YaHei';fill:#173d46}}.band{{font:800 112px Arial;fill:#d9772d}}.label{{font:600 30px Arial,'Microsoft YaHei';fill:#315862}}.small{{font:700 28px Arial;fill:#52727a}}.score-small{{font:800 58px Arial;fill:#17616c}}.body{{font:500 32px Arial,'Microsoft YaHei';fill:#284b54}}</style>
<rect width="1080" height="1440" fill="url(#bg)"/><circle cx="925" cy="115" r="170" fill="#bfe9ee" opacity=".42"/><circle cx="120" cy="1320" r="250" fill="#f5dfc6" opacity=".36"/>
<text x="78" y="100" class="brand">ESSAYPILOT</text><text x="78" y="205" class="title">我的本轮写作成长</text>
<rect x="70" y="270" width="940" height="210" rx="38" fill="#fff8ef" stroke="#efd3b5" stroke-width="3"/><text x="110" y="340" class="label">IELTS Writing Task 2 · 练习估分区间</text><text x="110" y="445" class="band">{interval}</text>
{''.join(score_cells)}
<rect x="70" y="725" width="940" height="230" rx="38" fill="#ffffff" fill-opacity=".82"/><text x="110" y="790" class="small">本轮最重要的提分方向</text><text x="110" y="860" class="body">{safe_priority}</text>
<rect x="70" y="1000" width="450" height="170" rx="34" fill="#dff4f5"/><text x="110" y="1060" class="small">已掌握训练</text><text x="110" y="1135" class="score-small">{mastered_count} 项</text>
<rect x="560" y="1000" width="450" height="170" rx="34" fill="#f6ead8"/><text x="600" y="1060" class="small">写作验证</text><text x="600" y="1135" class="body">{escape(gain)}</text>
<text x="78" y="1345" class="label">不是只看一份报告，而是把问题练会。</text></svg>'''
