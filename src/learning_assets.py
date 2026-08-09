"""Build reusable learning assets from an existing structured grading report."""

from __future__ import annotations

import hashlib
import re
from typing import Any


CATEGORY_LABELS = {
    "task_response": "任务回应",
    "coherence": "逻辑与衔接",
    "vocabulary": "词汇与搭配",
    "grammar": "语法与句式",
    "expression": "可复用表达",
}

CRITERION_LABELS = {
    "task_response": "任务回应（TR）",
    "coherence": "连贯与衔接（CC）",
    "vocabulary": "词汇资源（LR）",
    "grammar": "语法多样性与准确性（GRA）",
    "expression": "词汇资源（LR）",
}


def infer_category(text: str) -> str:
    """Infer a stable display category without changing the report schema."""
    value = text.casefold()
    rules = (
        ("grammar", r"语法|时态|主谓|从句|冠词|单复数|grammar|tense|clause|article"),
        ("vocabulary", r"词汇|搭配|用词|重复|lexical|vocab|collocation|word choice"),
        ("coherence", r"逻辑|衔接|段落|主题句|连贯|coherence|cohesion|logic|paragraph"),
        ("task_response", r"任务|回应|论证|观点|例子|展开|task response|position|develop"),
    )
    for category, pattern in rules:
        if re.search(pattern, value):
            return category
    return "grammar"


def learning_item_key(
    grading_run_id: str,
    item_type: str,
    source_text: str,
    target_text: str,
) -> str:
    normalized = "|".join(
        " ".join(part.strip().casefold().split())
        for part in (grading_run_id, item_type, source_text, target_text)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_learning_items(
    report_json: dict[str, Any],
    *,
    user_id: str,
    grading_run_id: str,
) -> list[dict[str, Any]]:
    """Convert corrections and expressions into idempotent database rows."""
    rows: list[dict[str, Any]] = []
    for correction in report_json.get("sentence_corrections", []) or []:
        if not isinstance(correction, dict):
            continue
        source = str(correction.get("original") or "").strip()
        target = str(correction.get("improved") or "").strip()
        explanation = str(correction.get("problem") or "").strip()
        if not source or not target:
            continue
        category = infer_category(explanation)
        rows.append(
            {
                "user_id": user_id,
                "grading_run_id": grading_run_id,
                "item_key": learning_item_key(grading_run_id, "error", source, target),
                "item_type": "error",
                "category": category,
                "source_text": source,
                "target_text": target,
                "explanation": explanation,
                "status": "new",
            }
        )

    for expression in report_json.get("useful_expressions", []) or []:
        if not isinstance(expression, dict):
            continue
        source = str(expression.get("expression") or "").strip()
        example = str(expression.get("example") or "").strip()
        meaning = str(expression.get("meaning") or "").strip()
        if not source:
            continue
        rows.append(
            {
                "user_id": user_id,
                "grading_run_id": grading_run_id,
                "item_key": learning_item_key(grading_run_id, "expression", source, example),
                "item_type": "expression",
                "category": "expression",
                "source_text": source,
                "target_text": example,
                "explanation": meaning,
                "status": "new",
            }
        )
    return rows


def criterion_for_problem(problem: str) -> str:
    return CRITERION_LABELS[infer_category(problem)]
