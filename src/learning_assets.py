"""Build reusable learning assets from structured grading reports and catalog entries."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from src.expression_catalog import FUNCTION_LABELS, TOPIC_LABELS


EXPRESSION_VIEW_CURATED = "题材精选"
EXPRESSION_VIEW_REPORT = "来自我的作文"
EXPRESSION_VIEW_PRACTICE = "造句练习"
EXPRESSION_VIEW_ALIASES = {
    "题材表达库": EXPRESSION_VIEW_CURATED,
    "我的表达": EXPRESSION_VIEW_REPORT,
    "表达练习": EXPRESSION_VIEW_PRACTICE,
}


CATEGORY_LABELS = {
    "task_response": "任务回应",
    "coherence": "逻辑与衔接",
    "vocabulary": "词汇与搭配",
    "grammar": "语法与句型",
    "expression": "可复用表达",
}

CRITERION_LABELS = {
    "task_response": "任务回应（TR）",
    "coherence": "连贯与衔接（CC）",
    "vocabulary": "词汇资源（LR）",
    "grammar": "语法多样性与准确性（GRA）",
    "expression": "词汇资源（LR）",
}

_TOPIC_PATTERNS = {
    "education": r"school|student|teacher|university|education|curriculum|homework|exam",
    "technology": r"technology|internet|online|computer|artificial intelligence|automation|digital",
    "environment": r"environment|climate|pollution|energy|wildlife|recycl|carbon|nature",
    "health": r"health|hospital|doctor|diet|exercise|obesity|medical|smoking",
    "society_family": r"family|parent|child|elderly|community|generation|society|social",
    "work_economy": r"work|job|career|salary|business|economy|employ|company",
    "government_policy": r"government|public policy|tax|spending|authority|regulation|state",
    "media_culture": r"media|advertis|culture|art|music|film|news|tradition",
    "crime_law": r"crime|criminal|prison|law|police|punish|offender|court",
    "cities_transport": r"city|urban|transport|traffic|car|housing|road|commut",
}


def infer_category(text: str) -> str:
    """Infer a stable correction category for older reports."""
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


def infer_topic_category(question: str) -> str:
    """Classify older reports locally so opening history never spends tokens."""
    value = question.casefold()
    scores = {topic: len(re.findall(pattern, value)) for topic, pattern in _TOPIC_PATTERNS.items()}
    best = max(scores, key=scores.get) if scores else "society_family"
    return best if scores.get(best, 0) else "society_family"


def infer_function_category(expression: str, usage_note: str = "") -> str:
    """Supply a useful function for expressions created by older prompt versions."""
    value = f"{expression} {usage_note}".casefold()
    if re.search(r"although|despite|while|whereas|rather than|让步|对比", value):
        return "contrast_concession"
    if re.search(r"for example|for instance|illustrat|case in point|举例|论证", value):
        return "example_argument"
    if re.search(r"should|need to|can address|solution|解决|措施|建议", value):
        return "solution"
    if re.search(r"lead to|result in|because|impact|effect|原因|影响", value):
        return "cause_effect"
    if re.search(r"outweigh|essential|priority|justified|立场|评价", value):
        return "evaluation_stance"
    return "core_collocation"


def learning_item_key(grading_run_id: str, item_type: str, source_text: str, target_text: str) -> str:
    normalized = "|".join(
        " ".join(part.strip().casefold().split())
        for part in (grading_run_id, item_type, source_text, target_text)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def catalog_learning_item(catalog_item: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    """Create the one personal row written when a catalog item is used."""
    catalog_id = str(catalog_item.get("catalog_id") or "")
    expression = str(catalog_item.get("expression") or "").strip()
    example = str(catalog_item.get("example") or "").strip()
    return {
        "user_id": user_id,
        "grading_run_id": None,
        "item_key": f"catalog:{catalog_id}",
        "item_type": "expression",
        "category": "expression",
        "source_text": expression,
        "target_text": example,
        "explanation": str(catalog_item.get("meaning") or ""),
        "origin": "catalog",
        "topic_category": str(catalog_item.get("topic_category") or "society_family"),
        "function_category": str(catalog_item.get("function_category") or "core_collocation"),
        "usage_note": str(catalog_item.get("usage_note") or ""),
        "favorite": False,
        "status": "new",
    }


def expression_status_label(status: object) -> str:
    """Map stored expression states to conservative learner-facing language."""
    return {
        "new": "未练习",
        "practicing": "继续练习",
        "mastered": "已正确使用一次",
    }.get(str(status), "未练习")


def report_expression_items(
    items: list[dict[str, Any]], *, grading_run_id: str = ""
) -> list[dict[str, Any]]:
    """Return only report-derived expressions, prioritising one essay when requested."""
    report_items = [
        item
        for item in items
        if item.get("item_type") == "expression" and item.get("origin", "report") == "report"
    ]
    if not grading_run_id:
        return report_items
    return sorted(
        report_items,
        key=lambda item: str(item.get("grading_run_id") or "") != grading_run_id,
    )


def resolve_expression_view(
    *, stored_view: object, authenticated: bool, has_report_expressions: bool, mode: str = ""
) -> str:
    """Choose a valid expression view without breaking sessions that hold old labels."""
    if mode == "expressions-from-report" and authenticated and has_report_expressions:
        return EXPRESSION_VIEW_REPORT
    if mode == "expressions":
        return EXPRESSION_VIEW_REPORT if authenticated and has_report_expressions else EXPRESSION_VIEW_CURATED
    if mode == "practice" and authenticated:
        return EXPRESSION_VIEW_PRACTICE

    mapped = EXPRESSION_VIEW_ALIASES.get(str(stored_view), str(stored_view))
    options = [EXPRESSION_VIEW_CURATED]
    if authenticated:
        options.extend([EXPRESSION_VIEW_REPORT, EXPRESSION_VIEW_PRACTICE])
    if mapped in options:
        return mapped
    return EXPRESSION_VIEW_REPORT if authenticated and has_report_expressions else EXPRESSION_VIEW_CURATED


def build_learning_items(
    report_json: dict[str, Any], *, user_id: str, grading_run_id: str, question: str = ""
) -> list[dict[str, Any]]:
    """Convert corrections and 6-8 report expressions into idempotent rows."""
    rows: list[dict[str, Any]] = []
    topic = str(report_json.get("essay_topic_category") or "")
    if topic not in TOPIC_LABELS:
        topic = infer_topic_category(question)
    for correction in report_json.get("sentence_corrections", []) or []:
        if not isinstance(correction, dict):
            continue
        source = str(correction.get("original") or "").strip()
        target = str(correction.get("improved") or "").strip()
        explanation = str(correction.get("problem") or "").strip()
        if source and target:
            rows.append({
                "user_id": user_id, "grading_run_id": grading_run_id,
                "item_key": learning_item_key(grading_run_id, "error", source, target),
                "item_type": "error", "category": infer_category(explanation),
                "source_text": source, "target_text": target, "explanation": explanation,
                "origin": "report", "topic_category": topic,
                "function_category": "core_collocation", "usage_note": "",
                "favorite": False, "status": "new",
            })

    seen: set[str] = set()
    for expression in report_json.get("useful_expressions", []) or []:
        if not isinstance(expression, dict):
            continue
        source = str(expression.get("expression") or "").strip()
        example = str(expression.get("example") or "").strip()
        meaning = str(expression.get("meaning") or "").strip()
        normalized = " ".join(source.casefold().split())
        if not source or normalized in seen:
            continue
        seen.add(normalized)
        usage_note = str(expression.get("usage_note") or "").strip()
        function = str(expression.get("function_category") or "")
        if function not in FUNCTION_LABELS:
            function = infer_function_category(source, usage_note)
        rows.append({
            "user_id": user_id, "grading_run_id": grading_run_id,
            "item_key": learning_item_key(grading_run_id, "expression", source, example),
            "item_type": "expression", "category": "expression",
            "source_text": source, "target_text": example, "explanation": meaning,
            "origin": "report", "topic_category": topic,
            "function_category": function, "usage_note": usage_note,
            "favorite": False, "status": "new",
        })
    return rows


def criterion_for_problem(problem: str) -> str:
    return CRITERION_LABELS[infer_category(problem)]
