"""Strict examiner result schema, validation, scoring, and Markdown rendering."""

from __future__ import annotations

import math
import re
import unicodedata
import hashlib
from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "2.1"
PROMPT_VERSION = "task2-two-stage-zh-official-descriptors-v3-2026-08-10"
SKILL_VERSION = "ielts-writing-task2-official-v2"
CRITERIA = (
    "Task Response",
    "Coherence and Cohesion",
    "Lexical Resource",
    "Grammatical Range and Accuracy",
)

CRITERION_DISPLAY_NAMES = {
    "Task Response": "任务回应（TR）",
    "Coherence and Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammatical Range and Accuracy": "语法多样性与准确性（GRA）",
}

SCORE_DISPLAY_NAMES = {
    "Overall Band": "总分",
    "Task Response": "任务回应（TR）",
    "Coherence & Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammar Range & Accuracy": "语法多样性与准确性（GRA）",
}


def submission_hash(question: str, essay: str) -> str:
    """Return a privacy-safe content fingerprint for per-user result reuse."""
    normalized = "\n".join(
        " ".join(value.split()).casefold() for value in (question.strip(), essay.strip())
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


EXAMINER_JSON_SCHEMA: dict[str, Any] = {
    "name": "essaypilot_examiner_report",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "essay_topic_category",
            "summary",
            "criteria",
            "priorities",
            "problems",
            "sentence_corrections",
            "paragraph_feedback",
            "band_75_rewrite",
            "useful_expressions",
            "next_practice",
            "sentence_training",
            "logic_training",
            "error_tags",
        ],
        "properties": {
            "essay_topic_category": {
                "type": "string",
                "enum": [
                    "education", "technology", "environment", "health",
                    "society_family", "work_economy", "government_policy",
                    "media_culture", "crime_law", "cities_transport"
                ],
            },
            "summary": {"type": "string"},
            "criteria": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "score", "reason", "evidence", "next_band_limit"],
                    "properties": {
                        "criterion": {"type": "string", "enum": list(CRITERIA)},
                        "score": {"type": "integer", "minimum": 0, "maximum": 9},
                        "reason": {"type": "string"},
                        "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "next_band_limit": {"type": "string"},
                    },
                },
            },
            "priorities": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"$ref": "#/$defs/coaching_item"}},
            "problems": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"$ref": "#/$defs/coaching_item"}},
            "sentence_corrections": {
                "type": "array",
                "minItems": 3,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "problem", "improved"],
                    "properties": {
                        "original": {"type": "string"},
                        "problem": {"type": "string"},
                        "improved": {"type": "string"},
                    },
                },
            },
            "paragraph_feedback": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["paragraph", "strength", "limitation", "improvement"],
                    "properties": {
                        "paragraph": {"type": "integer", "minimum": 1},
                        "strength": {"type": "string"},
                        "limitation": {"type": "string"},
                        "improvement": {"type": "string"},
                    },
                },
            },
            "band_75_rewrite": {"type": "string"},
            "useful_expressions": {
                "type": "array",
                "minItems": 6,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["expression", "meaning", "usage_note", "example", "function_category"],
                    "properties": {
                        "expression": {"type": "string"},
                        "meaning": {"type": "string"},
                        "usage_note": {"type": "string"},
                        "example": {"type": "string"},
                        "function_category": {
                            "type": "string",
                            "enum": [
                                "core_collocation", "cause_effect", "contrast_concession",
                                "example_argument", "solution", "evaluation_stance"
                            ],
                        },
                    },
                },
            },
            "next_practice": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "sentence_pattern", "warning"],
                "properties": {
                    "task": {"type": "string"},
                    "sentence_pattern": {"type": "string"},
                    "warning": {"type": "string"},
                },
            },
            "sentence_training": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "goal", "reference"],
                    "properties": {
                        "original": {"type": "string"},
                        "goal": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                },
            },
            "logic_training": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["problem", "original", "task", "requirements"],
                    "properties": {
                        "problem": {"type": "string"},
                        "original": {"type": "string"},
                        "task": {"type": "string"},
                        "requirements": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    },
                },
            },
            "error_tags": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "coaching_item": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "evidence", "why", "action"],
                "properties": {
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                },
            }
        },
    },
}


SCORING_DECISION_JSON_SCHEMA: dict[str, Any] = {
    "name": "essaypilot_task2_scoring_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["criteria", "uncertainty"],
        "properties": {
            "criteria": deepcopy(EXAMINER_JSON_SCHEMA["schema"]["properties"]["criteria"]),
            "uncertainty": {
                "type": "object",
                "additionalProperties": False,
                "required": ["level", "adjacent_band_direction", "reason"],
                "properties": {
                    "level": {"type": "string", "enum": ["low", "material"]},
                    "adjacent_band_direction": {
                        "type": "string",
                        "enum": ["lower", "higher", "none"],
                    },
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


TEACHING_FEEDBACK_JSON_SCHEMA: dict[str, Any] = deepcopy(EXAMINER_JSON_SCHEMA)
TEACHING_FEEDBACK_JSON_SCHEMA["name"] = "essaypilot_task2_teaching_feedback"
TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["required"].remove("criteria")
del TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["properties"]["criteria"]


class ExaminerResultError(ValueError):
    """Raised when a structured examiner response is incomplete or inconsistent."""


def calculate_overall(criteria: list[dict[str, Any]]) -> float:
    """Calculate the IELTS half-band result from four whole-band criteria."""
    raw_scores = [item.get("score") for item in criteria]
    if (
        len(raw_scores) != 4
        or any(not isinstance(score, int) or isinstance(score, bool) for score in raw_scores)
        or any(score < 0 or score > 9 for score in raw_scores)
    ):
        raise ExaminerResultError("Exactly four whole-band criterion scores are required.")
    scores = [int(score) for score in raw_scores]
    average = sum(scores) / 4
    return math.floor(average * 2 + 0.5) / 2


def _quote_is_present(quote: str, essay: str) -> bool:
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = value.replace("’", "'").replace("‘", "'")
        return " ".join(value.strip().strip('“”\"').split())

    clean_quote = normalize(quote)
    clean_essay = normalize(essay)
    if len(clean_quote) >= 3 and clean_quote in clean_essay:
        return True
    fragments = [normalize(part) for part in re.split(r"\.{3,}|…+", quote)]
    if any(len(fragment.split()) >= 4 and fragment in clean_essay for fragment in fragments):
        return True
    quoted_terms = [normalize(part) for part in re.findall(r'["“]([^"”]+)["”]', quote)]
    if any(len(term) >= 3 and term in clean_essay for term in quoted_terms):
        return True
    list_terms = [normalize(part).strip(" .:()[]") for part in re.split(r"[,;/]", clean_quote)]
    if any(
        1 <= len(term.split()) <= 6
        and len(term) >= 4
        and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", clean_essay)
        for term in list_terms
    ):
        return True
    words = clean_quote.split()
    return any(" ".join(words[index : index + 6]) in clean_essay for index in range(max(0, len(words) - 5)))


def _exact_quote_is_present(quote: str, essay: str) -> bool:
    """Require the complete normalized quotation, not merely one matching fragment."""
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = value.replace("’", "'").replace("‘", "'")
        return " ".join(value.strip().strip('“”\"').split())

    clean_quote = normalize(quote)
    return len(clean_quote) >= 3 and clean_quote in normalize(essay)


def validate_scoring_decision(data: dict[str, Any], essay: str) -> dict[str, Any]:
    """Validate and freeze the score-only model response."""
    if not isinstance(data, dict):
        raise ExaminerResultError("The scoring response is not a JSON object.")
    if "overall_band" in data:
        raise ExaminerResultError("Overall Band is calculated by EssayPilot, not the model.")
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise ExaminerResultError("The scoring response is missing criterion scores.")
    labels = [item.get("criterion") for item in criteria if isinstance(item, dict)]
    if sorted(labels) != sorted(CRITERIA):
        raise ExaminerResultError("The examiner must return each IELTS criterion exactly once.")
    for item in criteria:
        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 9:
            raise ExaminerResultError("Criterion scores must be whole numbers from 0 to 9.")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ExaminerResultError(f"{item['criterion']} has no essay evidence.")
        if not all(_exact_quote_is_present(str(quote), essay) for quote in evidence):
            raise ExaminerResultError(
                f"Every {item['criterion']} evidence item must be present in the essay."
            )
        if not str(item.get("reason", "")).strip() or not str(item.get("next_band_limit", "")).strip():
            raise ExaminerResultError(f"{item['criterion']} lacks a complete descriptor explanation.")
    uncertainty = data.get("uncertainty")
    if not isinstance(uncertainty, dict):
        raise ExaminerResultError("The scoring response is missing uncertainty metadata.")
    level = uncertainty.get("level")
    direction = uncertainty.get("adjacent_band_direction")
    if level not in {"low", "material"} or direction not in {"lower", "higher", "none"}:
        raise ExaminerResultError("The scoring uncertainty metadata is invalid.")
    if level == "low" and direction != "none":
        raise ExaminerResultError("Low uncertainty must not claim an adjacent-band direction.")
    if level == "material" and direction == "none":
        raise ExaminerResultError("Material uncertainty must identify an adjacent-band direction.")
    normalized = deepcopy(data)
    normalized["overall_band"] = calculate_overall(criteria)
    return normalized


def estimated_band_range(scoring: dict[str, Any]) -> tuple[float, float]:
    """Derive a narrow display range from validated uncertainty, never a fixed offset."""
    overall = float(scoring["overall_band"])
    uncertainty = scoring["uncertainty"]
    if uncertainty["level"] == "low":
        return overall, overall
    if uncertainty["adjacent_band_direction"] == "lower":
        return max(0.0, overall - 0.5), overall
    return overall, min(9.0, overall + 0.5)


def validate_examiner_result(data: dict[str, Any], essay: str) -> dict[str, Any]:
    """Validate key invariants and add program-owned score/version metadata."""
    if not isinstance(data, dict):
        raise ExaminerResultError("The examiner response is not a JSON object.")
    if data.get("essay_topic_category") not in {
        "education", "technology", "environment", "health", "society_family",
        "work_economy", "government_policy", "media_culture", "crime_law", "cities_transport",
    }:
        raise ExaminerResultError("The essay topic category is missing or invalid.")
    expressions = data.get("useful_expressions")
    if not isinstance(expressions, list) or not 6 <= len(expressions) <= 8:
        raise ExaminerResultError("The examiner must return 6-8 useful expressions.")
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise ExaminerResultError("The examiner response is missing criterion scores.")
    labels = [item.get("criterion") for item in criteria if isinstance(item, dict)]
    if sorted(labels) != sorted(CRITERIA):
        raise ExaminerResultError("The examiner must return each IELTS criterion exactly once.")
    for item in criteria:
        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            raise ExaminerResultError("Criterion scores must be whole numbers.")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ExaminerResultError(f"{item['criterion']} has no essay evidence.")
        if not all(_exact_quote_is_present(str(quote), essay) for quote in evidence):
            raise ExaminerResultError(f"Every {item['criterion']} evidence item must be present in the essay.")
    for correction in data.get("sentence_corrections", []):
        if not _quote_is_present(str(correction.get("original", "")), essay):
            raise ExaminerResultError("A sentence correction does not quote the submitted essay.")
    for task in data.get("sentence_training", []):
        if not _quote_is_present(str(task.get("original", "")), essay):
            raise ExaminerResultError("A sentence training task does not quote the submitted essay.")
    normalized = dict(data)
    normalized["overall_band"] = calculate_overall(criteria)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["prompt_version"] = PROMPT_VERSION
    normalized["skill_version"] = SKILL_VERSION
    return normalized


def score_snapshot(data: dict[str, Any]) -> dict[str, float | None]:
    mapping = {item["criterion"]: float(item["score"]) for item in data.get("criteria", [])}
    return {
        "Overall Band": float(data["overall_band"]),
        "Task Response": mapping.get("Task Response"),
        "Coherence & Cohesion": mapping.get("Coherence and Cohesion"),
        "Lexical Resource": mapping.get("Lexical Resource"),
        "Grammar Range & Accuracy": mapping.get("Grammatical Range and Accuracy"),
    }
